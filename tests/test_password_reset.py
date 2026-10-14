"""管理员重置密码：临时密码只显示一次，登录后必须改密。"""
import re

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select

import app.db as database
from app.main import create_app
from app.models import PasswordReset, User
from app import services as svc


def csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def login(client, username, password="secret12", follow=True):
    page = client.get("/login")
    return client.post(
        "/login",
        data={"csrf": csrf(page), "account": username, "password": password},
        follow_redirects=follow,
    )


def post(client, url, data=None):
    payload = dict(data or {})
    payload["csrf"] = csrf(client.get("/"))
    return client.post(url, data=payload, follow_redirects=True)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'club.db'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app()
    admin = TestClient(app)
    assert login(admin, "admin", "admin123").status_code == 200
    counter = {"n": 0}

    def member(name):
        counter["n"] += 1
        client = TestClient(app)
        registered = client.post("/register", data={
            "csrf": csrf(client.get("/register")), "username": name, "password": "secret12",
            "real_name": name, "phone": f"1360000{counter['n']:04d}", "department": "技术部",
            "college": "学院", "class_name": "一班",
        }, follow_redirects=True)
        assert "等待超级管理员审批" in registered.text
        with database.SessionLocal() as db:
            uid = db.scalar(select(User.id).where(User.username == name))
        if name != "待审成员":
            assert "已通过注册" in post(admin, f"/members/{uid}/approve").text
            assert login(client, name).url.path == "/"
        return client, uid

    yield app, admin, member, tmp_path
    database.engine.dispose()
    database.engine, database.SessionLocal = previous


def test_login_page_points_forgot_password_to_admin(desk):
    app, _admin, _member, _root = desk
    guest = TestClient(app)
    assert "忘记密码请联系管理员" in guest.get("/login").text


def test_admin_reset_shows_password_once_and_forces_change(desk):
    app, admin, member, root = desk
    client, uid = member("林夏同学")
    assert "重置密码" in admin.get("/members").text
    assert client.get("/org").status_code == 200

    reset = post(admin, f"/members/{uid}/reset-password")
    match = re.search(r"临时密码只显示这一次：([A-Za-z0-9]+)", reset.text)
    assert match, reset.text
    temporary = match.group(1)
    assert len(temporary) == 12
    assert set(temporary) <= set(svc.TEMP_PASSWORD_ALPHABET)
    assert temporary not in admin.get("/members").text
    stored = (root / "club.db").read_bytes()
    wal = root / "club.db-wal"
    if wal.exists():
        stored += wal.read_bytes()
    assert temporary.encode() not in stored

    with database.SessionLocal() as db:
        columns = {column["name"] for column in inspect(database.engine).get_columns("password_resets")}
        assert "password" not in columns
        row = db.scalar(select(PasswordReset))
        admin_id = db.scalar(select(User.id).where(User.username == "admin"))
        target = db.get(User, uid)
        assert row.actor_id == admin_id
        assert row.target_id == uid
        assert row.created_at is not None
        assert target.must_change_password is True
        assert svc.verify_password(temporary, target.password_hash)
        assert not svc.verify_password("secret12", target.password_hash)

    blocked = client.get("/org", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/profile"
    profile = client.get("/profile")
    assert "不用再输入临时密码" in profile.text
    assert 'name="current"' not in profile.text

    mismatch = client.post("/profile/password", data={
        "csrf": csrf(profile), "new_password": "brandnew1", "confirm": "brandnew2",
    }, follow_redirects=True)
    assert "不一致" in mismatch.text
    assert client.get("/projects", follow_redirects=False).headers["location"] == "/profile"

    same = client.post("/profile/password", data={
        "csrf": csrf(client.get("/profile")), "new_password": temporary, "confirm": temporary,
    }, follow_redirects=True)
    assert "不能与临时密码相同" in same.text
    assert client.get("/finance", follow_redirects=False).headers["location"] == "/profile"

    logged_out = client.post("/logout", data={"csrf": csrf(client.get("/profile"))}, follow_redirects=False)
    assert logged_out.status_code == 303
    fresh = TestClient(app)
    denied = login(fresh, "林夏同学", "secret12", follow=True)
    assert "账号或密码不正确" in denied.text
    entered = login(fresh, "林夏同学", temporary, follow=False)
    assert entered.headers["location"] == "/profile"
    landed = login(fresh, "林夏同学", temporary, follow=True)
    assert landed.url.path == "/profile"
    assert 'name="current"' not in landed.text

    done = fresh.post("/profile/password", data={
        "csrf": csrf(landed), "new_password": "brandnew1", "confirm": "brandnew1",
    }, follow_redirects=True)
    assert done.url.path == "/"
    assert "密码已更新" in done.text
    assert fresh.get("/org").status_code == 200
    with database.SessionLocal() as db:
        assert db.get(User, uid).must_change_password is False
    again = TestClient(app)
    assert login(again, "林夏同学", temporary, follow=True).url.path == "/login"
    assert login(again, "林夏同学", "brandnew1").url.path == "/"


def test_only_admin_can_reset_active_or_disabled_members(desk):
    app, admin, member, _root = desk
    client, uid = member("周宁同学")
    other, _other = member("何琪同学")
    refused = post(other, f"/members/{uid}/reset-password")
    assert "只有超级管理员可以重置密码" in refused.text
    with database.SessionLocal() as db:
        assert db.scalar(select(PasswordReset.id)) is None
        assert db.get(User, uid).must_change_password is False

    with database.SessionLocal() as db:
        admin_id = db.scalar(select(User.id).where(User.username == "admin"))
    assert "不能重置超级管理员" in post(admin, f"/members/{admin_id}/reset-password").text

    _pending, pending_id = member("待审成员")
    assert "只能重置已启用或已停用" in post(admin, f"/members/{pending_id}/reset-password").text

    assert "已停用" in post(admin, f"/members/{uid}/disable").text
    disabled = post(admin, f"/members/{uid}/reset-password")
    assert "临时密码只显示这一次" in disabled.text
    with database.SessionLocal() as db:
        assert db.get(User, uid).status == "disabled"
        assert db.get(User, uid).must_change_password is True
    assert "账号已停用" in login(client, "周宁同学", re.search(r"临时密码只显示这一次：([A-Za-z0-9]+)", disabled.text).group(1)).text
