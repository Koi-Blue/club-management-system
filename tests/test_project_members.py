"""立项关联成员可以来自任意部门，但不能是超管或未启用账号。"""
import re

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

import app.db as database
from app.models import Project, ProjectMember, User
from app.services import AppError
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


def post(client, url, data=None):
    payload = dict(data or {})
    payload["csrf"] = csrf(client.get("/"))
    return client.post(url, data=payload, follow_redirects=True)


def form_html(html, action):
    match = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>([\s\S]*?)</form>', html)
    assert match, action
    return match.group(1)


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
            "real_name": name, "phone": f"1370002{counter['n']:04d}", "department": department,
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

    yield {"app": app, "admin": admin, "member": member}
    database.engine.dispose()
    database.engine, database.SessionLocal = previous


def test_project_members_can_come_from_any_department(club):
    admin = club["admin"]
    tech, tech_id = club["member"]("林夏同学", "技术部")
    finance, finance_id = club["member"]("何琪同学", "财务部")
    promo, promo_id = club["member"]("周宁同学", "宣传部")
    person, person_id = club["member"]("陈一同学", "人事部")
    stopped, stopped_id = club["member"]("停用同学", "财务部")
    assert "已停用账号" in post(admin, f"/members/{stopped_id}/disable").text

    guest = TestClient(club["app"])
    pending = guest.post("/register", data={
        "csrf": csrf(guest.get("/register")), "username": "pendingmember", "password": "secret12",
        "real_name": "待审同学", "phone": "13700029999", "department": "技术部",
        "college": "学院", "class_name": "一班",
    }, follow_redirects=True)
    assert "等待超级管理员审批" in pending.text
    with database.SessionLocal() as db:
        admin_id = db.scalar(select(User.id).where(User.is_admin.is_(True)))
        pending_id = db.scalar(select(User.id).where(User.username == "pendingmember"))

    form = form_html(tech.get("/projects/new").text, "/projects")
    assert "<option>技术部</option>" in form
    assert "<option>财务部</option>" not in form
    assert "<option>宣传部</option>" not in form
    for uid, dept, name in (
        (tech_id, "技术部", "林夏同学"),
        (finance_id, "财务部", "何琪同学"),
        (promo_id, "宣传部", "周宁同学"),
        (person_id, "人事部", "陈一同学"),
    ):
        assert f'name="members" value="{uid}"' in form
        assert f"{dept} · 一班 {name}" in form
    assert f'value="{admin_id}"' not in form
    assert f'value="{stopped_id}"' not in form
    assert f'value="{pending_id}"' not in form
    assert "停用同学" not in form
    assert "待审同学" not in form
    assert "可勾选任意部门的已启用成员" in form

    blocked = post(tech, "/projects", {
        "title": "不该跨部门立项", "summary": "说明", "department": "财务部", "members": [tech_id, finance_id],
    })
    assert "只能在自己的部门立项" in blocked.text
    with database.SessionLocal() as db:
        assert db.scalar(select(Project.id).where(Project.title == "不该跨部门立项")) is None
        tech_user = db.get(User, tech_id)
        with pytest.raises(AppError, match="无效账号"):
            svc.active_member_ids(db, [tech_id, admin_id])
        with pytest.raises(AppError, match="无效账号"):
            svc.active_member_ids(db, [stopped_id])
        with pytest.raises(AppError, match="无效账号"):
            svc.active_member_ids(db, [pending_id])
        selected = svc.active_member_ids(db, [finance_id, tech_id, promo_id, finance_id])
        assert selected == [finance_id, tech_id, promo_id]
        created = svc.create_project(
            db, tech_user, svc.roles_of(db, tech_id), "跨部门项目", "说明", "技术部",
            [tech_id, finance_id, promo_id, person_id],
        )
        project_id = created.id

    page = tech.get("/projects/new")
    saved = tech.post("/projects", data={
        "csrf": csrf(page),
        "title": "巡线车",
        "summary": "嵌入式",
        "department": "技术部",
        "members": [tech_id, finance_id, promo_id],
    }, follow_redirects=True)
    assert "草稿" in saved.text
    assert "何琪同学" in saved.text
    assert "周宁同学" in saved.text
    assert "巡线车" in finance.get("/projects").text
    assert "跨部门项目" in finance.get("/projects").text
    assert "巡线车" not in stopped.get("/projects").text
    project_path = saved.url.path
    edit = form_html(tech.get(f"{project_path}/edit").text, f"{project_path}/edit")
    assert "<option>财务部</option>" not in edit
    assert f"{finance_id}" in edit and "财务部 · 一班 何琪同学" in edit
    assert f'name="members" value="{person_id}"' in edit
    revised = post(tech, f"{project_path}/edit", {
        "title": "巡线车", "summary": "嵌入式", "department": "技术部",
        "members": [tech_id, person_id],
    })
    assert "立项已保存" in revised.text
    assert "陈一同学" in revised.text
    with database.SessionLocal() as db:
        row = db.scalar(select(Project).where(Project.title == "巡线车"))
        assert row.department == "技术部"
        assert row.leader_id == tech_id
        members = set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == row.id)).all())
        assert members == {tech_id, person_id}
        service_members = set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)).all())
        assert service_members == {tech_id, finance_id, promo_id, person_id}
    assert "跨部门项目" in promo.get("/projects").text
    assert "巡线车" not in finance.get("/projects").text
