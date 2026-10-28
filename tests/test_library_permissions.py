"""部门资料：成员上传待部长审批，荣誉社长可跨部门，删除要校验登录密码。"""
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text

import app.db as database
from app.models import LibraryFile, User
from app import services as svc


def csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def login(client, username, password="secret12"):
    page = client.get("/login")
    return client.post(
        "/login",
        data={"csrf": csrf(page), "account": username, "password": password},
        follow_redirects=True,
    )


def post(client, url, data=None, files=None):
    payload = dict(data or {})
    payload["csrf"] = csrf(client.get("/"))
    return client.post(url, data=payload, files=files, follow_redirects=True)


def upload(client, department, name, body=b"notes"):
    return post(client, "/library", {"department": department}, files=[("file", (name, body, "text/plain"))])


def approval_badge(html):
    nav = re.search(r'href="/approvals"[^>]*>[\s\S]*?</a>', html)
    assert nav, html
    found = re.search(r"<em>(\d+)</em>", nav.group(0))
    return int(found.group(1)) if found else 0


def file_link(html, name):
    match = re.search(rf'href="(/library/\d+)">{re.escape(name)}', html)
    assert match, name
    return match.group(1)


def file_id(html, name):
    return file_link(html, name).rsplit("/", 1)[-1]


def db_bytes():
    path = Path(database.engine.url.database)
    blob = path.read_bytes()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            blob += extra.read_bytes()
    return blob


@pytest.fixture
def club(tmp_path, monkeypatch):
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'club.db'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.main import create_app

    app = create_app()
    admin = TestClient(app)
    assert login(admin, "admin", "admin123").status_code == 200
    counter = {"n": 0}

    def member(name, department="技术部", roles=None):
        counter["n"] += 1
        client = TestClient(app)
        registered = client.post("/register", data={
            "csrf": csrf(client.get("/register")), "username": name, "password": "secret12",
            "real_name": name, "phone": f"1370003{counter['n']:04d}", "department": department,
            "college": "学院", "class_name": "一班",
        }, follow_redirects=True)
        assert "等待超级管理员审批" in registered.text
        with database.SessionLocal() as db:
            uid = db.scalar(select(User.id).where(User.username == name))
        assert "已通过注册" in post(admin, f"/members/{uid}/approve").text
        if roles:
            assert post(admin, f"/members/{uid}/roles", {"roles": roles}).status_code == 200
        assert login(client, name).url.path == "/"
        return client, uid

    yield {"app": app, "admin": admin, "member": member, "root": tmp_path}
    database.engine.dispose()
    database.engine, database.SessionLocal = previous


def test_blank_library_status_stays_visible_to_the_department():
    row = SimpleNamespace(status=None, department="技术部", uploader_id=9, delete_requester_id=None)
    assert svc.library_status(row) == "approved"
    row.status = "  "
    assert svc.library_status(row) == "approved"
    member = User(id=4, is_admin=False, department="技术部", status="active")
    outsider = User(id=5, is_admin=False, department="财务部", status="active")
    honor = User(id=6, is_admin=False, department="人事部", status="active")
    president = User(id=7, is_admin=False, department="宣传部", status="active")
    assert svc.can_see_library_file(member, ["技术部成员"], row) is True
    assert svc.can_see_library_file(outsider, ["财务部成员"], row) is False
    assert svc.can_see_library_file(president, ["社长", "宣传部成员"], row) is True
    row.status = "pending"
    assert svc.can_see_library_file(member, ["技术部成员"], row) is False
    assert svc.can_see_library_file(member, ["技术部部长"], row) is True
    assert svc.can_see_library_file(honor, ["荣誉社长"], row) is True
    assert svc.can_see_library_file(president, ["社长", "宣传部成员"], row) is False
    assert svc.can_see_library_file(User(id=9, is_admin=False, department="财务部"), ["财务部成员"], row) is True


def test_library_approval_visibility_and_password_delete(club, caplog):
    admin = club["admin"]
    tech, _tech_id = club["member"]("林夏同学", "技术部")
    peer, _peer_id = club["member"]("陈一同学", "技术部")
    lead, _lead_id = club["member"]("钱负责人", "技术部", ["技术部算法负责人"])
    finance, _finance_id = club["member"]("何琪同学", "财务部")
    minister, _minister_id = club["member"]("赵部长", "技术部", ["技术部部长"])
    honor, _honor_id = club["member"]("唐宁同学", "人事部", ["荣誉社长"])
    president, _president_id = club["member"]("周社长", "宣传部", ["社长", "宣传部成员"])

    assert "没有权限上传这个部门的资料" in upload(tech, "宣传部", "越权.txt").text
    assert "没有权限上传这个部门的资料" in upload(finance, "技术部", "越权.txt").text
    assert "上传到技术部" not in president.get("/library?department=技术部").text
    assert "上传到宣传部" in president.get("/library?department=宣传部").text
    assert "技术部" in honor.get("/library").text
    assert "财务部" in honor.get("/library?department=财务部").text
    assert "可查看、上传任意部门资料" in honor.get("/library").text

    caplog.set_level(0)
    submitted = upload(tech, "技术部", "方案.txt", b"draft-notes")
    assert "已提交，待部长审批" in submitted.text
    assert "待部长审批" in submitted.text
    assert "/review" not in submitted.text
    assert 'type="password"' not in submitted.text
    assert "/delete" not in submitted.text
    link = file_link(submitted.text, "方案.txt")
    fid = link.rsplit("/", 1)[-1]
    assert tech.get(link).content == b"draft-notes"
    assert "方案.txt" not in peer.get("/library?department=技术部").text
    assert "这个部门还没有资料" in peer.get("/library?department=技术部").text
    hidden = peer.get(link, follow_redirects=True)
    assert "找不到这个文件" in hidden.text
    assert b"draft-notes" not in hidden.content
    president_page = president.get("/library?department=技术部")
    assert "方案.txt" not in president_page.text
    assert "找不到这个文件" in president.get(link, follow_redirects=True).text
    assert "方案.txt" not in lead.get("/library?department=技术部").text
    honor_pending = honor.get("/library?department=技术部")
    assert "方案.txt" in honor_pending.text
    assert 'type="password"' in honor_pending.text
    assert "/delete-request" not in honor_pending.text
    assert honor.get(link).content == b"draft-notes"
    minister_page = minister.get("/library?department=技术部")
    assert "方案.txt" in minister_page.text
    assert "通过" in minister_page.text
    assert "驳回" in minister_page.text
    assert 'type="password"' not in minister_page.text
    assert "/delete-request" not in minister_page.text
    assert approval_badge(minister_page.text) == 1
    assert "部门资料" in minister.get("/approvals").text
    assert "方案.txt" in minister.get("/approvals").text
    assert "方案.txt" not in honor.get("/approvals").text
    assert "方案.txt" not in president.get("/approvals").text
    assert "找不到这个文件" in post(lead, f"/library/{fid}/review", {"decision": "approved"}).text
    with database.SessionLocal() as db:
        assert db.get(LibraryFile, int(fid)).status == "pending"

    rejected = post(minister, f"/library/{fid}/review", {"decision": "rejected"})
    assert "资料已驳回" in rejected.text
    assert "已驳回" in rejected.text
    assert "方案.txt" not in peer.get("/library?department=技术部").text
    assert "找不到这个文件" in peer.get(link, follow_redirects=True).text
    assert "已驳回" in tech.get("/library?department=技术部").text
    assert approval_badge(minister.get("/library").text) == 0

    again = upload(tech, "技术部", "定稿.txt", b"final-notes")
    assert "已提交，待部长审批" in again.text
    final_link = file_link(again.text, "定稿.txt")
    final_id = final_link.rsplit("/", 1)[-1]
    approved = post(minister, f"/library/{final_id}/review", {"decision": "approved"})
    assert "资料已通过" in approved.text
    assert "待部长审批" not in peer.get("/library?department=技术部").text
    peer_page = peer.get("/library?department=技术部")
    assert "定稿.txt" in peer_page.text
    assert 'type="password"' not in peer_page.text
    assert "/delete" not in peer_page.text
    assert peer.get(final_link).content == b"final-notes"
    assert president.get(final_link).content == b"final-notes"
    assert lead.get(final_link).content == b"final-notes"
    ready = minister.get("/library?department=技术部")
    assert "申请删除" in ready.text
    assert 'type="password"' in ready.text
    assert re.search(rf'action="/library/{final_id}/delete"(?!-)', ready.text) is None
    assert re.search(r'type="password"[^>]*value=', ready.text) is None

    wrong = post(minister, f"/library/{final_id}/delete-request", {"password": "WrongPass-9421"})
    assert "密码不正确" in wrong.text
    assert "WrongPass-9421" not in wrong.text
    assert "定稿.txt" in wrong.text
    assert b"WrongPass-9421" not in db_bytes()
    leaked = post(tech, f"/library/{final_id}/delete", {"password": "secret12"})
    assert "没有权限删除这份资料" in leaked.text
    assert "secret12" not in leaked.text
    with database.SessionLocal() as db:
        kept = db.get(LibraryFile, int(final_id))
        assert kept is not None
        assert kept.delete_requester_id is None
        assert kept.status == "approved"

    asked = post(minister, f"/library/{final_id}/delete-request", {"password": "secret12"})
    assert "删除申请已提交，待荣誉社长审核" in asked.text
    assert "secret12" not in asked.text
    assert "删除待审核" in asked.text
    assert peer.get(final_link).content == b"final-notes"
    assert "资料删除" in honor.get("/approvals").text
    assert "定稿.txt" in honor.get("/approvals").text
    assert approval_badge(honor.get("/").text) >= 1
    honor_review = honor.get("/library?department=技术部")
    assert "通过删除" in honor_review.text
    assert re.search(rf'action="/library/{final_id}/delete"(?!-)', honor_review.text) is None
    refused = post(president, f"/library/{final_id}/delete-review", {"decision": "approved"})
    assert "没有权限审核删除" in refused.text
    cancelled = post(honor, f"/library/{final_id}/delete-review", {"decision": "rejected"})
    assert "已驳回删除申请" in cancelled.text
    assert "删除待审核" not in peer.get("/library?department=技术部").text
    assert peer.get(final_link).content == b"final-notes"
    with database.SessionLocal() as db:
        assert db.get(LibraryFile, int(final_id)).delete_requester_id is None
    with database.SessionLocal() as db:
        stored_name = db.get(LibraryFile, int(final_id)).stored_name
    assert "删除申请已提交，待荣誉社长审核" in post(minister, f"/library/{final_id}/delete-request", {"password": "secret12"}).text
    removed = post(honor, f"/library/{final_id}/delete-review", {"decision": "approved"})
    assert "资料已删除" in removed.text
    assert "定稿.txt" not in peer.get("/library?department=技术部").text
    assert "找不到这个文件" in peer.get(final_link, follow_redirects=True).text
    with database.SessionLocal() as db:
        assert db.get(LibraryFile, int(final_id)) is None
    assert not (club["root"] / "uploads" / "library" / "技术部" / stored_name).exists()

    published = upload(honor, "财务部", "跨部门.txt", b"honor-notes")
    assert "资料已放入部门库" in published.text
    assert "待部长审批" not in published.text
    shared = file_link(published.text, "跨部门.txt")
    assert finance.get("/library?department=财务部").text.count("跨部门.txt") == 1
    assert finance.get(shared).content == b"honor-notes"
    assert "申请删除" not in finance.get("/library?department=财务部").text
    changed = post(honor, "/profile/password", {"current": "secret12", "new_password": "honor-pass-1", "confirm": "honor-pass-1"})
    assert "密码已更新" in changed.text
    stale = post(honor, f"/library/{file_id(published.text, '跨部门.txt')}/delete", {"password": "secret12"})
    assert "密码不正确" in stale.text
    assert "secret12" not in stale.text
    assert "跨部门.txt" in honor.get("/library?department=财务部").text
    gone = post(honor, f"/library/{file_id(published.text, '跨部门.txt')}/delete", {"password": "honor-pass-1"})
    assert "资料已删除" in gone.text
    assert "honor-pass-1" not in gone.text
    assert "跨部门.txt" not in finance.get("/library?department=财务部").text
    assert b"secret12" not in db_bytes()
    assert b"honor-pass-1" not in db_bytes()
    assert b"WrongPass-9421" not in db_bytes()
    assert "secret12" not in caplog.text
    assert "honor-pass-1" not in caplog.text
    assert "WrongPass-9421" not in caplog.text

    admin_saved = upload(admin, "技术部", "笔记.txt", b"admin-notes")
    assert "资料已放入部门库" in admin_saved.text
    assert peer.get(file_link(admin_saved.text, "笔记.txt")).content == b"admin-notes"


def test_legacy_library_file_without_status_stays_visible(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    upload_dir = tmp_path / "uploads"
    folder = upload_dir / "library" / "技术部"
    folder.mkdir(parents=True)
    (folder / "legacy.txt").write_bytes(b"old-bytes")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE library_files (
            id INTEGER PRIMARY KEY,
            department VARCHAR(40) NOT NULL,
            stored_name VARCHAR(80) NOT NULL,
            original_name VARCHAR(180) NOT NULL,
            size INTEGER NOT NULL,
            uploader_id INTEGER NOT NULL,
            uploaded_at DATETIME
        )
        """
    )
    conn.execute(
        "INSERT INTO library_files (id, department, stored_name, original_name, size, uploader_id, uploaded_at) "
        "VALUES (3, '技术部', 'legacy.txt', '旧资料.txt', 9, 1, '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    try:
        from app.main import create_app

        app = create_app()
        with database.engine.connect() as db:
            cols = {row[1]: row for row in db.execute(text("PRAGMA table_info(library_files)")).all()}
            assert cols["status"][3] == 1
            assert db.execute(text("SELECT status, original_name FROM library_files WHERE id = 3")).one() == ("approved", "旧资料.txt")
        with database.SessionLocal() as db:
            row = db.get(LibraryFile, 3)
            assert row.status == "approved"
            assert svc.library_status(row) == "approved"
        admin = TestClient(app)
        assert login(admin, "admin", "admin123").status_code == 200
        tech = TestClient(app)
        registered = tech.post("/register", data={
            "csrf": csrf(tech.get("/register")), "username": "legacytech", "password": "secret12",
            "real_name": "旧库成员", "phone": "13700038888", "department": "技术部",
            "college": "学院", "class_name": "一班",
        }, follow_redirects=True)
        assert "等待超级管理员审批" in registered.text
        with database.SessionLocal() as db:
            uid = db.scalar(select(User.id).where(User.username == "legacytech"))
        assert "已通过注册" in post(admin, f"/members/{uid}/approve").text
        assert login(tech, "legacytech").url.path == "/"
        page = tech.get("/library?department=技术部")
        assert "旧资料.txt" in page.text
        assert "待部长审批" not in page.text
        assert tech.get(file_link(page.text, "旧资料.txt")).content == b"old-bytes"
        promo = TestClient(app)
        registered = promo.post("/register", data={
            "csrf": csrf(promo.get("/register")), "username": "legacypromo", "password": "secret12",
            "real_name": "旧库宣传", "phone": "13700037777", "department": "宣传部",
            "college": "学院", "class_name": "一班",
        }, follow_redirects=True)
        assert "等待超级管理员审批" in registered.text
        with database.SessionLocal() as db:
            promo_id = db.scalar(select(User.id).where(User.username == "legacypromo"))
        assert "已通过注册" in post(admin, f"/members/{promo_id}/approve").text
        assert login(promo, "legacypromo").url.path == "/"
        assert "技术部" not in promo.get("/library").text
        assert "找不到这个文件" in promo.get("/library/3", follow_redirects=True).text
    finally:
        database.engine.dispose()
        database.engine, database.SessionLocal = previous
