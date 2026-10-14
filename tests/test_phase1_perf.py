"""Phase 1 止血：验密、批量查询、导航缓存、流式上传。"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

import app.db as database
import app.routes as routes
import app.security as security
from app.config import load_settings
from app.main import create_app
from app.models import Asset, Borrow, Setting, User
from app.security import issue_token
from app.services import AppError
from app import services as svc

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SHANGHAI = timezone(timedelta(hours=8))


def csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def login(client, username, password="secret12"):
    return client.post("/login", data={"csrf": csrf(client.get("/login")), "account": username, "password": password}, follow_redirects=True)


def post(client, url, data=None, files=None):
    payload = dict(data or {})
    payload["csrf"] = csrf(client.get("/"))
    return client.post(url, data=payload, files=files, follow_redirects=True)


def nav_count(html: str, label: str) -> int:
    match = re.search(rf"<span>{label}</span>(?:<em>(\d+)</em>)?", html)
    if match is None or not match.group(1):
        return 0
    return int(match.group(1))


class SqlLog:
    def __init__(self, engine):
        self.statements: list[str] = []
        event.listen(engine, "before_cursor_execute", self._capture)

    def _capture(self, _conn, _cursor, statement, _parameters, _context, _executemany):
        self.statements.append(statement)

    def close(self, engine):
        event.remove(engine, "before_cursor_execute", self._capture)

    def containing(self, text: str) -> list[str]:
        needle = text.lower()
        return [item for item in self.statements if needle in item.lower()]


@pytest.fixture
def perf(tmp_path, monkeypatch):
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'club.db'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app()
    admin = TestClient(app)
    login(admin, "admin", "admin123")
    counter = {"n": 0}

    def member(name, roles=None, department="技术部"):
        counter["n"] += 1
        client = TestClient(app)
        registered = client.post("/register", data={
            "csrf": csrf(client.get("/register")), "username": name, "password": "secret12",
            "real_name": name, "phone": f"1370000{counter['n']:04d}", "department": department,
            "college": "学院", "class_name": "一班",
        }, follow_redirects=True)
        assert "等待超级管理员审批" in registered.text
        with database.SessionLocal() as db:
            uid = db.scalar(select(User.id).where(User.username == name))
        post(admin, f"/members/{uid}/approve")
        if roles:
            post(admin, f"/members/{uid}/roles", {"roles": roles})
        assert login(client, name).url.path == "/"
        return client, uid

    yield app, admin, member, tmp_path
    database.engine.dispose()
    database.engine, database.SessionLocal = previous


def test_warn_password_is_checked_at_login_not_on_each_page(perf, monkeypatch):
    _app, admin, member, _root = perf
    calls = {"routes": 0}
    real = security.verify_password

    def counted(password, stored):
        calls["routes"] += 1
        return real(password, stored)

    monkeypatch.setattr(routes, "verify_password", counted)
    home = admin.get("/")
    profile = admin.get("/profile")
    members = admin.get("/members")
    assert calls["routes"] == 0
    assert "还在用初始密码" in home.text
    assert "还在用初始密码" in profile.text
    assert "还在用初始密码" in members.text

    changed = post(admin, "/profile/password", {"current": "admin123", "new_password": "secret99", "confirm": "secret99"})
    assert "密码已更新" in changed.text
    assert "还在用初始密码" not in changed.text
    assert "还在用初始密码" not in admin.get("/").text
    assert calls["routes"] == 0

    restored = post(admin, "/profile/password", {"current": "secret99", "new_password": "admin123", "confirm": "admin123"})
    assert "还在用初始密码" in restored.text
    assert calls["routes"] == 0

    _client, _uid = member("ordinary")
    ordinary = TestClient(_app)
    page = login(ordinary, "ordinary")
    assert "还在用初始密码" not in page.text
    assert calls["routes"] == 0


def test_existing_session_verifies_default_password_once(perf, monkeypatch):
    app, _admin, _member, _root = perf
    calls = {"n": 0}
    real = security.verify_password

    def counted(password, stored):
        calls["n"] += 1
        return real(password, stored)

    monkeypatch.setattr(routes, "verify_password", counted)
    with database.SessionLocal() as db:
        admin_id = db.scalar(select(User.id).where(User.username == "admin"))
    client = TestClient(app)
    client.cookies.set("club_jwt", issue_token(load_settings().secret_key, admin_id))
    first = client.get("/")
    assert calls["n"] == 1
    assert "还在用初始密码" in first.text
    calls["n"] = 0
    assert "还在用初始密码" in client.get("/profile").text
    assert calls["n"] == 0


def test_roles_are_batched_and_reused_in_the_same_request(perf):
    _app, admin, member, _root = perf
    _client, first = member("成员甲", ["技术部成员"])
    _client, second = member("成员乙", ["社长", "宣传部成员"])
    _client, third = member("成员丙", ["荣誉社长"])
    viewer, _viewer_id = member("成员丁")

    with database.SessionLocal() as db:
        loaded = svc.roles_of_many(db, [first, second, third, second, 0])
        assert loaded[first] == ["技术部成员"]
        assert loaded[second] == ["社长", "宣传部成员"]
        assert loaded[third] == ["荣誉社长"]
        assert svc.roles_of(db, second) == loaded[second]
        log = SqlLog(database.engine)
        try:
            again = svc.roles_of_many(db, [first, second, third])
            assert again == loaded
            assert log.containing("user_roles") == []
        finally:
            log.close(database.engine)
        svc.set_roles(db, db.get(User, 1), second, ["财务部部长"])
        assert svc.roles_of(db, second) == ["财务部部长"]

    for path, expected in (("/", 1), ("/members", 2), ("/org", 2)):
        log = SqlLog(database.engine)
        try:
            page = admin.get(path)
            assert page.status_code == 200
            role_queries = log.containing("user_roles")
        finally:
            log.close(database.engine)
        assert len(role_queries) == expected
        if path == "/members":
            assert any(" in " in item.lower() for item in role_queries)

    log = SqlLog(database.engine)
    try:
        assert viewer.get("/").status_code == 200
        assert len(log.containing("user_roles")) == 1
    finally:
        log.close(database.engine)

    assert "成员乙" in admin.get("/members").text
    assert "社长" in admin.get("/org").text


def test_asset_available_quantities_use_one_grouped_sum(perf):
    _app, admin, _member, _root = perf
    for name, qty in (("开发板", "10"), ("万用表", "4"), ("外壳", "6")):
        assert "资产已登记" in post(admin, "/assets", {"name": name, "category": "物资", "total_qty": qty, "location": "柜", "description": ""}).text
    page = admin.get("/assets")
    asset_id = re.search(r'<option value="(\d+)">开发板', page.text).group(1)
    due = (datetime.now(SHANGHAI) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    assert "借用申请已提交" in post(admin, "/borrows", {"asset_id": asset_id, "qty": "2", "reason": "比赛", "due_at": due}).text
    review = admin.get("/assets")
    borrow_id = re.search(r"/borrows/(\d+)/review", review.text).group(1)
    assert "借用审批已保存" in post(admin, f"/borrows/{borrow_id}/review", {"decision": "approved", "comment": "可以"}).text

    log = SqlLog(database.engine)
    try:
        shown = admin.get("/assets")
        sums = [item for item in log.statements if "sum(" in item.lower() and "borrow" in item.lower()]
    finally:
        log.close(database.engine)
    assert len(sums) == 1
    assert "group by" in sums[0].lower()
    assert "8 / 10" in shown.text
    assert "4 / 4" in shown.text
    assert "6 / 6" in shown.text

    with database.SessionLocal() as db:
        assets = list(db.scalars(select(Asset).order_by(Asset.id)))
        batched = svc.available_quantities(db, assets)
        assert batched == {asset.id: svc.available_qty(db, asset) for asset in assets}
        assert sum(1 for row in db.scalars(select(Borrow)) if row.status == "approved") == 1


def test_badge_and_club_name_cache_until_invalidated(perf, monkeypatch):
    _app, admin, _member, _root = perf
    assert nav_count(admin.get("/").text, "账号") == 0
    guest = TestClient(_app)
    registered = guest.post("/register", data={
        "csrf": csrf(guest.get("/register")), "username": "pendingone", "password": "secret12",
        "real_name": "待审", "phone": "13600001111", "department": "人事部",
        "college": "学院", "class_name": "一班",
    }, follow_redirects=True)
    assert "等待超级管理员审批" in registered.text
    assert nav_count(admin.get("/").text, "账号") == 1
    assert nav_count(admin.get("/").text, "审批") == 1

    with database.SessionLocal() as db:
        admin_user = db.scalar(select(User).where(User.username == "admin"))
        cached = svc.approval_count(db, admin_user, [])
        db.add(User(
            username="directpending", password_hash="x", real_name="直插", phone="13600002222",
            college="学院", class_name="一班", department="财务部", status="pending", is_admin=False,
        ))
        db.commit()
        assert svc.approval_count(db, admin_user, []) == cached
        assert svc.pending_member_count(db, admin_user) == cached
        svc.invalidate_nav_badges(db)
        assert svc.approval_count(db, admin_user, []) == cached + 1

    members = admin.get("/members")
    user_id = re.search(r"/members/(\d+)/approve", members.text).group(1)
    approved = post(admin, f"/members/{user_id}/approve")
    assert "已通过注册" in approved.text
    assert nav_count(admin.get("/").text, "账号") == 1

    monkeypatch.setattr(svc, "BADGE_TTL_SECONDS", 0)
    with database.SessionLocal() as db:
        admin_user = db.scalar(select(User).where(User.username == "admin"))
        before = svc.pending_member_count(db, admin_user)
        db.add(User(
            username="freshpending", password_hash="x", real_name="即时", phone="13600003333",
            college="学院", class_name="一班", department="宣传部", status="pending", is_admin=False,
        ))
        db.commit()
        assert svc.pending_member_count(db, admin_user) == before + 1

    assert "青禾社" in admin.get("/").text
    with database.SessionLocal() as db:
        db.get(Setting, "club_name").value = "偷偷改"
        db.commit()
    assert "青禾社" in admin.get("/profile").text
    assert "偷偷改" not in admin.get("/profile").text
    renamed = post(admin, "/profile/club", {"club_name": "新社团名"})
    assert "社团名称已更新" in renamed.text
    assert "新社团名" in renamed.text
    assert "新社团名" in admin.get("/login").text or "新社团名" in TestClient(_app).get("/login").text


def test_honor_badge_counts_only_own_drafts_from_the_snapshot(perf):
    _app, admin, member, _root = perf
    honor, honor_id = member("honor", ["荣誉社长"])
    other, other_id = member("minister", ["技术部部长"])
    now = datetime.now(timezone.utc)
    fields = {
        "title": "自己的草稿", "department": "全社", "start_at": now.isoformat(),
        "end_at": (now + timedelta(days=1)).isoformat(), "capacity": "0",
    }
    assert post(honor, "/activities", fields).url.path.startswith("/activities/")
    assert post(other, "/activities", {**fields, "title": "别人的草稿", "department": "技术部"}).url.path.startswith("/activities/")
    with database.SessionLocal() as db:
        honor_user = db.get(User, honor_id)
        other_user = db.get(User, other_id)
        honor_roles = svc.roles_of(db, honor_id)
        other_roles = svc.roles_of(db, other_id)
        svc.invalidate_nav_badges(db)
        assert svc.approval_count(db, honor_user, honor_roles) == 1
        minister_count = svc.approval_count(db, other_user, other_roles)
        admin_user = db.scalar(select(User).where(User.is_admin.is_(True)))
        admin_count = svc.approval_count(db, admin_user, [])
    assert "自己的草稿" in honor.get("/approvals").text
    assert "别人的草稿" not in honor.get("/approvals").text
    assert minister_count == 0
    assert admin_count >= 2


class Chunked:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0
        self.requested: list[int] = []

    def read(self, size=-1):
        assert isinstance(size, int) and 0 < size <= svc.UPLOAD_CHUNK
        self.requested.append(size)
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class Endless:
    def __init__(self):
        self.reads = 0

    def read(self, size=-1):
        assert isinstance(size, int) and 0 < size <= svc.UPLOAD_CHUNK
        self.reads += 1
        return b"x" * size


def test_uploads_stream_to_disk_and_stop_past_the_limit(tmp_path):
    payload = b"%PDF-1.4\n" + (b"0123456789abcdef" * 5000)
    source = Chunked(payload)
    path, size, header = svc.spool_upload(
        tmp_path, ".pdf", source, max_size=svc.MAX_FILE_SIZE,
        empty_message="请选择文件", size_message="单个文件不能超过 20MB",
    )
    assert size == len(payload)
    assert path.read_bytes() == payload
    assert header.startswith(b"%PDF-")
    assert source.requested
    assert max(source.requested) <= svc.UPLOAD_CHUNK
    assert len(source.requested) > 1

    endless = Endless()
    with pytest.raises(AppError, match="太大"):
        svc.spool_upload(tmp_path, ".txt", endless, max_size=1000, empty_message="空", size_message="太大")
    assert endless.reads == 1
    assert [item for item in tmp_path.iterdir() if item.suffix == ".txt"] == []


def test_upload_slot_limits_concurrency_and_library_roundtrip(perf):
    _app, admin, _member, root = perf
    held = []
    try:
        for _ in range(svc.MAX_CONCURRENT_UPLOADS):
            ctx = svc.upload_slot()
            ctx.__enter__()
            held.append(ctx)
        with pytest.raises(AppError, match="同时上传"):
            with svc.upload_slot():
                pass
        blocked = post(admin, "/library", {"department": "技术部"}, files=[("file", ("笔记.txt", b"should-not-land", "text/plain"))])
        assert "同时上传" in blocked.text
        assert not list((root / "uploads").rglob("*.txt"))
    finally:
        for ctx in held:
            ctx.__exit__(None, None, None)

    payload = b"library-notes\n" + (b"0123456789abcdef" * 5000)
    saved = post(admin, "/library", {"department": "技术部"}, files=[("file", ("笔记.txt", payload, "text/plain"))])
    assert "资料已放入部门库" in saved.text
    stored = next((root / "uploads").rglob("*.txt"))
    assert stored.read_bytes() == payload

    pdf = b"%PDF-1.4\n" + (b"invoice-body" * 8000)
    claim = post(admin, "/finance/claims", {"amount": "3.50", "reason": "流式报销"}, files=[
        ("invoices", ("发票.pdf", pdf, "application/pdf")),
        ("qr", ("收款.png", PNG, "image/png")),
    ])
    assert "报销已提交" in claim.text
    invoice = next((root / "uploads").rglob("*.pdf"))
    assert invoice.read_bytes() == pdf
    incoming = root / "uploads" / "_incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_compose_limits_fit_two_core_host():
    text = Path("docker-compose.yml").read_text(encoding="utf-8")
    memory = re.search(r"mem_limit:\s*(\d+)m", text)
    cpus = re.search(r"cpus:\s*([0-9.]+)", text)
    assert memory and cpus
    assert 512 <= int(memory.group(1)) <= 768
    assert 1.0 <= float(cpus.group(1)) <= 1.5
