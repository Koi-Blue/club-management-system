"""立项草稿/驳回可编辑，待批可撤回，废草稿可删除。"""
import re
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

import app.db as database
from app.models import Project, ProjectFile, ProjectMember, User


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


def form_html(html, action):
    match = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>([\s\S]*?)</form>', html)
    assert match, action
    return match.group(1)


def approval_badge(html):
    nav = re.search(r'href="/approvals"[^>]*>[\s\S]*?</a>', html)
    assert nav, html
    found = re.search(r"<em>(\d+)</em>", nav.group(0))
    return int(found.group(1)) if found else 0


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
            "real_name": name, "phone": f"1370001{counter['n']:04d}", "department": department,
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

    yield {
        "app": app,
        "admin": admin,
        "member": member,
        "uploads": tmp_path / "uploads",
    }
    database.engine.dispose()
    database.engine, database.SessionLocal = previous


def create_project(client, title, department, member_ids, summary="说明"):
    page = client.get("/projects/new")
    created = client.post("/projects", data={
        "csrf": csrf(page),
        "title": title,
        "summary": summary,
        "department": department,
        "members": member_ids,
    }, follow_redirects=True)
    assert "草稿" in created.text
    return created


def test_related_members_can_edit_draft_within_department_rules(club):
    admin = club["admin"]
    tech, tech_id = club["member"]("林夏同学", "技术部")
    promo, promo_id = club["member"]("周宁同学", "宣传部")
    honor, honor_id = club["member"]("唐宁同学", "人事部", ["荣誉社长"])
    president, _president_id = club["member"]("社长周宁", "宣传部", ["社长", "宣传部成员"])

    created = create_project(tech, "巡线车", "技术部", [tech_id], "嵌入式")
    project_path = created.url.path
    project_id = project_path.rsplit("/", 1)[-1]
    assert "编辑" in created.text
    assert "删除" in created.text
    assert "撤回为草稿" not in created.text

    assert "巡线车" not in promo.get("/projects").text
    hidden = promo.get(project_path)
    assert "没有查看权限" in hidden.text
    assert "巡线车" not in admin.get("/projects").text
    assert "没有权限修改这个立项" in post(admin, f"{project_path}/edit", {
        "title": "超管改名", "summary": "不行", "department": "技术部", "members": [tech_id],
    }).text
    with database.SessionLocal() as db:
        assert db.get(Project, int(project_id)).title == "巡线车"

    edit = tech.get(f"{project_path}/edit")
    edit_form = form_html(edit.text, f"{project_path}/edit")
    assert "巡线车" in edit_form
    assert "嵌入式" in edit_form
    assert ">技术部</option>" in edit_form
    assert "财务部" not in edit_form
    assert "宣传部" not in edit_form
    assert f'value="{tech_id}"' in edit_form and "checked" in edit_form
    assert "请勾选关联成员" in post(tech, f"{project_path}/edit", {
        "title": "巡线车", "summary": "嵌入式", "department": "技术部",
    }).text
    assert "只能在自己的部门立项" in post(tech, f"{project_path}/edit", {
        "title": "不该跨部门", "summary": "嵌入式", "department": "财务部", "members": [tech_id],
    }).text
    with database.SessionLocal() as db:
        kept = db.get(Project, int(project_id))
        assert kept.title == "巡线车"
        assert kept.department == "技术部"

    saved = post(tech, f"{project_path}/edit", {
        "title": "巡线车二代", "summary": "改过的说明", "department": "技术部", "members": [tech_id, promo_id],
    })
    assert "立项已保存" in saved.text
    assert "巡线车二代" in saved.text
    assert "改过的说明" in saved.text
    assert "草稿" in saved.text
    assert "周宁同学" in saved.text
    assert "巡线车二代" in promo.get("/projects").text

    promo_edit = promo.get(f"{project_path}/edit")
    promo_form = form_html(promo_edit.text, f"{project_path}/edit")
    assert ">技术部</option>" in promo_form
    assert ">宣传部</option>" in promo_form
    assert "财务部" not in promo_form
    assert "只能在自己的部门立项" in post(promo, f"{project_path}/edit", {
        "title": "巡线车二代", "summary": "改过的说明", "department": "财务部", "members": [tech_id, promo_id],
    }).text
    with database.SessionLocal() as db:
        assert db.get(Project, int(project_id)).department == "技术部"
    kept_by_member = post(promo, f"{project_path}/edit", {
        "title": "巡线车二代", "summary": "成员改的", "department": "技术部", "members": [tech_id, promo_id],
    })
    assert "立项已保存" in kept_by_member.text
    assert "成员改的" in kept_by_member.text
    moved = post(promo, f"{project_path}/edit", {
        "title": "巡线车二代", "summary": "成员改的", "department": "宣传部", "members": [tech_id, promo_id],
    })
    assert "立项已保存" in moved.text
    assert "宣传部" in moved.text
    back = post(tech, f"{project_path}/edit", {
        "title": "巡线车二代", "summary": "成员改的", "department": "技术部", "members": [tech_id, promo_id, honor_id],
    })
    assert "立项已保存" in back.text
    honor_moved = post(honor, f"{project_path}/edit", {
        "title": "巡线车二代", "summary": "荣誉改部门", "department": "财务部", "members": [tech_id, promo_id, honor_id],
    })
    assert "立项已保存" in honor_moved.text
    with database.SessionLocal() as db:
        row = db.get(Project, int(project_id))
        assert row.department == "财务部"
        assert row.status == "draft"
        assert row.summary == "荣誉改部门"

    removed = post(tech, f"{project_path}/edit", {
        "title": "巡线车二代", "summary": "荣誉改部门", "department": "技术部", "members": [tech_id],
    })
    assert "立项已保存" in removed.text
    assert "没有查看权限" in promo.get(project_path).text
    assert "已提交审批" in post(tech, f"{project_path}/submit").text
    pending = tech.get(project_path)
    assert "待审批" in pending.text
    assert f'href="{project_path}/edit"' not in pending.text
    assert "撤回为草稿" in pending.text
    assert "当前状态不能修改立项" in tech.get(f"{project_path}/edit", follow_redirects=True).text
    assert "当前状态不能修改立项" in post(tech, f"{project_path}/edit", {
        "title": "待批不能改", "summary": "荣誉改部门", "department": "技术部", "members": [tech_id],
    }).text
    with database.SessionLocal() as db:
        assert db.get(Project, int(project_id)).title == "巡线车二代"
    assert "没有权限审批立项" in post(admin, f"{project_path}/review", {"decision": "approved", "comment": "超管不能批"}).text
    assert "没有权限审批立项" in post(president, f"{project_path}/review", {"decision": "approved", "comment": "社长不能批"}).text
    assert "巡线车二代" not in president.get("/projects").text
    assert "巡线车二代" in honor.get("/projects").text


def test_pending_project_can_be_withdrawn_to_draft(club):
    tech, tech_id = club["member"]("林夏同学", "技术部")
    promo, promo_id = club["member"]("周宁同学", "宣传部")
    honor, honor_id = club["member"]("唐宁同学", "人事部", ["荣誉社长"])
    outsider, _outsider_id = club["member"]("何琪同学", "财务部")

    created = create_project(tech, "机械臂", "技术部", [tech_id, promo_id], "课程设计")
    project_path = created.url.path
    project_id = int(project_path.rsplit("/", 1)[-1])
    assert "只有待审批的立项可以撤回" in post(tech, f"{project_path}/withdraw").text
    assert "已提交审批" in post(tech, f"{project_path}/submit").text
    queued = honor.get("/approvals")
    assert "机械臂" in queued.text
    assert approval_badge(queued.text) == 1
    with database.SessionLocal() as db:
        row = db.get(Project, project_id)
        row.reviewer_id = honor_id
        row.review_comment = "先记一笔"
        db.commit()
    assert "没有权限撤回这个立项" in post(honor, f"{project_path}/withdraw").text
    assert "没有权限撤回这个立项" in post(outsider, f"{project_path}/withdraw").text
    with database.SessionLocal() as db:
        assert db.get(Project, project_id).status == "pending"
        assert db.get(Project, project_id).review_comment == "先记一笔"

    withdrawn = post(promo, f"{project_path}/withdraw")
    assert "已撤回为草稿" in withdrawn.text
    assert "草稿" in withdrawn.text
    assert "先记一笔" not in withdrawn.text
    assert "编辑" in withdrawn.text
    with database.SessionLocal() as db:
        row = db.get(Project, project_id)
        assert row.status == "draft"
        assert row.reviewer_id is None
        assert row.review_comment == ""
    cleared = honor.get("/approvals")
    assert "机械臂" not in cleared.text
    assert approval_badge(cleared.text) == 0
    assert "已提交审批" in post(tech, f"{project_path}/submit").text
    assert approval_badge(honor.get("/approvals").text) == 1


def test_delete_draft_or_rejected_removes_members_and_files(club):
    uploads: Path = club["uploads"]
    tech, tech_id = club["member"]("林夏同学", "技术部")
    honor, _honor_id = club["member"]("唐宁同学", "人事部", ["荣誉社长"])
    outsider, _outsider_id = club["member"]("何琪同学", "财务部")

    created = create_project(tech, "废草稿", "技术部", [tech_id], "还没交")
    project_path = created.url.path
    project_id = int(project_path.rsplit("/", 1)[-1])
    uploaded = post(tech, f"{project_path}/files", files={"file": ("note.txt", b"draft-note", "text/plain")})
    assert "资料已上传" in uploaded.text
    file_match = re.search(rf'href="({re.escape(project_path)}/files/\d+)"', uploaded.text)
    assert file_match
    assert tech.get(file_match.group(1)).content == b"draft-note"
    stored = list(uploads.joinpath("projects", str(project_id)).glob("*.txt"))
    assert len(stored) == 1
    assert "没有权限删除这个立项" in post(honor, f"{project_path}/delete").text
    assert "没有权限删除这个立项" in post(outsider, f"{project_path}/delete").text
    assert stored[0].exists()

    deleted = post(tech, f"{project_path}/delete")
    assert deleted.url.path == "/projects"
    assert "立项已删除" in deleted.text
    assert "废草稿" not in deleted.text
    assert not stored[0].exists()
    assert not uploads.joinpath("projects", str(project_id)).exists()
    with database.SessionLocal() as db:
        assert db.get(Project, project_id) is None
        assert db.scalar(select(func.count()).select_from(ProjectMember).where(ProjectMember.project_id == project_id)) == 0
        assert db.scalar(select(func.count()).select_from(ProjectFile).where(ProjectFile.project_id == project_id)) == 0
    assert "找不到这个文件" in tech.get(file_match.group(1), follow_redirects=True).text

    pending = create_project(tech, "待批项目", "技术部", [tech_id], "等审批")
    pending_path = pending.url.path
    pending_id = int(pending_path.rsplit("/", 1)[-1])
    assert "资料已上传" in post(tech, f"{pending_path}/files", files={"file": ("wait.txt", b"keep-me", "text/plain")}).text
    assert "已提交审批" in post(tech, f"{pending_path}/submit").text
    refused = post(tech, f"{pending_path}/delete")
    assert "只有草稿或已驳回的立项可以删除" in refused.text
    assert refused.url.path == pending_path
    waiting = list(uploads.joinpath("projects", str(pending_id)).glob("*.txt"))
    assert len(waiting) == 1
    assert tech.get(re.search(rf'href="({re.escape(pending_path)}/files/\d+)"', tech.get(pending_path).text).group(1)).content == b"keep-me"

    assert "审批已保存" in post(honor, f"{pending_path}/review", {"decision": "rejected", "comment": "补材料"}).text
    rejected = tech.get(pending_path)
    assert "已驳回" in rejected.text
    assert "补材料" in rejected.text
    assert "编辑" in rejected.text
    removed = post(tech, f"{pending_path}/delete")
    assert removed.url.path == "/projects"
    assert "立项已删除" in removed.text
    assert "待批项目" not in removed.text
    assert not waiting[0].exists()
    with database.SessionLocal() as db:
        assert db.get(Project, pending_id) is None
        assert db.scalar(select(func.count()).select_from(ProjectFile).where(ProjectFile.project_id == pending_id)) == 0


def test_reject_then_edit_resubmit_and_archive_upload_stay_bounded(club):
    admin = club["admin"]
    tech, tech_id = club["member"]("林夏同学", "技术部")
    promo, _promo_id = club["member"]("周宁同学", "宣传部")
    honor, _honor_id = club["member"]("唐宁同学", "人事部", ["荣誉社长"])
    president, _president_id = club["member"]("社长周宁", "宣传部", ["社长", "宣传部成员"])

    created = create_project(tech, "展示柜", "技术部", [tech_id], "第一版")
    project_path = created.url.path
    project_id = int(project_path.rsplit("/", 1)[-1])
    assert "展示柜" not in promo.get("/projects").text
    assert "没有查看权限" in promo.get(project_path).text
    assert "已提交审批" in post(tech, f"{project_path}/submit").text
    assert "没有权限审批立项" in post(admin, f"{project_path}/review", {"decision": "approved", "comment": "超管不能批"}).text
    assert "没有权限审批立项" in post(president, f"{project_path}/review", {"decision": "rejected", "comment": "社长不能批"}).text
    assert "审批已保存" in post(honor, f"{project_path}/review", {"decision": "rejected", "comment": "名称再具体些"}).text
    rejected = tech.get(project_path)
    assert "已驳回" in rejected.text
    assert "名称再具体些" in rejected.text
    revised = post(tech, f"{project_path}/edit", {
        "title": "展柜灯光", "summary": "补了范围", "department": "技术部", "members": [tech_id],
    })
    assert "立项已保存" in revised.text
    assert "展柜灯光" in revised.text
    assert "名称再具体些" in revised.text
    assert "已提交审批" in post(tech, f"{project_path}/submit").text
    pending = tech.get(project_path)
    assert "待审批" in pending.text
    assert "展柜灯光" in pending.text
    assert "审批已保存" in post(honor, f"{project_path}/review", {"decision": "approved", "comment": "可以做"}).text
    running = tech.get(project_path)
    assert "进行中" in running.text
    assert "当前状态不能修改立项" in tech.get(f"{project_path}/edit", follow_redirects=True).text
    assert "只有待审批的立项可以撤回" in post(tech, f"{project_path}/withdraw").text
    assert "只有草稿或已驳回的立项可以删除" in post(tech, f"{project_path}/delete").text
    uploaded = post(tech, f"{project_path}/files", files={"file": ("plan.txt", b"archive-me", "text/plain")})
    assert "资料已上传" in uploaded.text
    file_url = re.search(rf'href="({re.escape(project_path)}/files/\d+)"', uploaded.text).group(1)
    assert tech.get(file_url).content == b"archive-me"
    assert "找不到这个立项" in post(promo, f"{project_path}/files", files={"file": ("nope.txt", b"no", "text/plain")}).text
    assert "立项已归档" in post(tech, f"{project_path}/archive").text
    archived = tech.get(project_path)
    assert "已归档" in archived.text
    assert f'href="{project_path}/edit"' not in archived.text
    assert "撤回为草稿" not in archived.text
    assert "只有草稿或已驳回的立项可以删除" in post(tech, f"{project_path}/delete").text
    assert tech.get(file_url).content == b"archive-me"
    with database.SessionLocal() as db:
        assert db.get(Project, project_id).status == "archived"
        assert db.scalar(select(func.count()).select_from(ProjectFile).where(ProjectFile.project_id == project_id)) == 1
