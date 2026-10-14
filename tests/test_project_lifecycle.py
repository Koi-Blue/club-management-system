"""立项修改只归立项人；进行中可改成员；资料可限制可见人。"""
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text

import app.db as database
from app.models import Project, ProjectFile, ProjectFileViewer, ProjectMember, User


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


def upload_project_file(client, url, filename, body, viewers=None):
    fields = {"csrf": csrf(client.get("/"))}
    if viewers is not None:
        fields["viewers_set"] = "1"
        fields["viewers"] = [str(item) for item in viewers]
    return client.post(
        url,
        data=fields,
        files={"file": (filename, body, "application/octet-stream")},
        follow_redirects=True,
    )


def file_link(html, project_path, name):
    match = re.search(rf'href="({re.escape(project_path)}/files/\d+)">{re.escape(name)}', html)
    assert match, name
    return match.group(1)


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


def test_only_leader_can_edit_submit_or_delete_draft(club):
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
    assert "<option>财务部</option>" not in edit_form
    assert "<option>宣传部</option>" not in edit_form
    assert "宣传部 ·" in edit_form
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
    assert "编辑" in saved.text
    assert "提交审批" in saved.text
    promo_page = promo.get(project_path)
    assert f'href="{project_path}/edit"' not in promo_page.text
    assert "提交审批" not in promo_page.text
    assert "删除" not in promo_page.text
    assert "没有权限修改这个立项" in promo.get(f"{project_path}/edit", follow_redirects=True).text
    assert "没有权限修改这个立项" in post(promo, f"{project_path}/edit", {
        "title": "成员改名", "summary": "成员改的", "department": "宣传部", "members": [tech_id, promo_id],
    }).text
    assert "没有权限提交这个立项" in post(promo, f"{project_path}/submit").text
    assert "没有权限删除这个立项" in post(promo, f"{project_path}/delete").text
    assert "没有权限修改这个立项" in post(honor, f"{project_path}/edit", {
        "title": "荣誉改名", "summary": "荣誉改部门", "department": "财务部", "members": [tech_id, promo_id, honor_id],
    }).text
    with database.SessionLocal() as db:
        kept = db.get(Project, int(project_id))
        assert kept.title == "巡线车二代"
        assert kept.summary == "改过的说明"
        assert kept.department == "技术部"
        assert kept.status == "draft"
        assert kept.leader_id == tech_id

    honor_created = create_project(honor, "荣誉项目", "人事部", [honor_id], "跨部门")
    honor_moved = post(honor, f"{honor_created.url.path}/edit", {
        "title": "荣誉项目", "summary": "跨部门", "department": "财务部", "members": [honor_id],
    })
    assert "立项已保存" in honor_moved.text
    with database.SessionLocal() as db:
        honor_row = db.scalar(select(Project).where(Project.title == "荣誉项目"))
        assert honor_row.department == "财务部"
        assert honor_row.leader_id == honor_id
        assert honor_row.status == "draft"

    removed = post(tech, f"{project_path}/edit", {
        "title": "巡线车二代", "summary": "改过的说明", "department": "技术部", "members": [tech_id],
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
    assert "撤回为草稿" in tech.get(project_path).text
    assert "撤回为草稿" not in promo.get(project_path).text
    assert "没有权限撤回这个立项" in post(promo, f"{project_path}/withdraw").text
    with database.SessionLocal() as db:
        assert db.get(Project, project_id).status == "pending"
        assert db.get(Project, project_id).review_comment == "先记一笔"

    withdrawn = post(tech, f"{project_path}/withdraw")
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
    promo, promo_id = club["member"]("周宁同学", "宣传部")
    honor, _honor_id = club["member"]("唐宁同学", "人事部", ["荣誉社长"])
    outsider, _outsider_id = club["member"]("何琪同学", "财务部")

    created = create_project(tech, "废草稿", "技术部", [tech_id, promo_id], "还没交")
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
    assert "没有权限删除这个立项" in post(promo, f"{project_path}/delete").text
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
    promo, promo_id = club["member"]("周宁同学", "宣传部")
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
    assert "调整成员" in running.text
    assert f'href="{project_path}/edit"' in running.text
    assert "找不到这个立项" in post(promo, f"{project_path}/files", files={"file": ("nope.txt", b"no", "text/plain")}).text
    member_form = form_html(tech.get(f"{project_path}/edit").text, f"{project_path}/edit")
    assert "调整成员" in tech.get(f"{project_path}/edit").text
    assert 'name="title"' not in member_form
    assert 'name="summary"' not in member_form
    assert 'name="department"' not in member_form
    assert f'value="{tech_id}"' in member_form
    saved_members = post(tech, f"{project_path}/edit", {
        "title": "不该改标题", "summary": "不该改说明", "department": "财务部", "members": [tech_id, promo_id],
    })
    assert "立项已保存" in saved_members.text
    assert "展柜灯光" in saved_members.text
    assert "不该改标题" not in saved_members.text
    assert "周宁同学" in saved_members.text
    with database.SessionLocal() as db:
        row = db.get(Project, project_id)
        assert row.title == "展柜灯光"
        assert row.summary == "补了范围"
        assert row.department == "技术部"
        assert row.leader_id == tech_id
        assert row.status == "approved"
        members = set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)).all())
        assert members == {tech_id, promo_id}
    assert "调整成员" not in promo.get(project_path).text
    assert "没有权限修改这个立项" in post(promo, f"{project_path}/edit", {"members": [promo_id]}).text
    with database.SessionLocal() as db:
        members = set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)).all())
        assert members == {tech_id, promo_id}
    assert "只有待审批的立项可以撤回" in post(tech, f"{project_path}/withdraw").text
    assert "只有草稿或已驳回的立项可以删除" in post(tech, f"{project_path}/delete").text
    uploaded = post(tech, f"{project_path}/files", files={"file": ("plan.txt", b"archive-me", "text/plain")})
    assert "资料已上传" in uploaded.text
    file_url = re.search(rf'href="({re.escape(project_path)}/files/\d+)"', uploaded.text).group(1)
    assert tech.get(file_url).content == b"archive-me"
    assert "立项已归档" in post(tech, f"{project_path}/archive").text
    archived = tech.get(project_path)
    assert "已归档" in archived.text
    assert f'href="{project_path}/edit"' not in archived.text
    assert "撤回为草稿" not in archived.text
    assert "当前状态不能修改立项" in tech.get(f"{project_path}/edit", follow_redirects=True).text
    assert "没有权限修改这个立项" in post(promo, f"{project_path}/edit", {"members": [tech_id, promo_id]}).text
    assert "只有草稿或已驳回的立项可以删除" in post(tech, f"{project_path}/delete").text
    assert tech.get(file_url).content == b"archive-me"
    with database.SessionLocal() as db:
        assert db.get(Project, project_id).status == "archived"
        assert db.scalar(select(func.count()).select_from(ProjectFile).where(ProjectFile.project_id == project_id)) == 1


def test_project_files_support_archives_and_selected_viewers(club):
    tech, tech_id = club["member"]("林夏同学", "技术部")
    promo, promo_id = club["member"]("周宁同学", "宣传部")
    other, other_id = club["member"]("何琪同学", "财务部")
    honor, _honor_id = club["member"]("唐宁同学", "人事部", ["荣誉社长"])
    created = create_project(tech, "资料库", "技术部", [tech_id, promo_id, other_id], "附件")
    path = created.url.path
    project_id = int(path.rsplit("/", 1)[-1])
    assert "资料库" not in club["admin"].get("/projects").text

    refused = post(tech, f"{path}/files", files={"file": ("run.exe", b"nope", "application/octet-stream")})
    assert "不支持这个文件类型" in refused.text
    for name, body in (
        ("pack.zip", b"PK\x03\x04zip-body"),
        ("pack.rar", b"Rar!\x1a\x07\x00"),
        ("pack.7z", b"7z\xbc\xaf\x27\x1c"),
        ("brief.pdf", b"%PDF-1.4"),
    ):
        saved = upload_project_file(tech, f"{path}/files", name, body, viewers=[tech_id, promo_id, other_id])
        assert "资料已上传" in saved.text
        assert tech.get(file_link(saved.text, path, name)).content == body

    form = form_html(tech.get(path).text, f"{path}/files")
    assert 'name="viewers_set" value="1"' in form
    for uid in (tech_id, promo_id, other_id):
        assert re.search(rf'name="viewers" value="{uid}"[^>]*checked', form)

    shared = upload_project_file(tech, f"{path}/files", "note.txt", b"hello-all")
    assert "资料已上传" in shared.text
    note = file_link(shared.text, path, "note.txt")
    assert "全体" in re.search(r'note\.txt</a><p class="muted">([^<]*)</p>', shared.text).group(1)
    assert promo.get(note).content == b"hello-all"
    assert other.get(note).content == b"hello-all"
    assert honor.get(note).content == b"hello-all"
    with database.SessionLocal() as db:
        note_row = db.scalar(select(ProjectFile).where(ProjectFile.original_name == "note.txt"))
        assert note_row.visibility == "all"

    everyone = upload_project_file(tech, f"{path}/files", "everyone.txt", b"all-people", viewers=[tech_id, promo_id, other_id])
    assert "资料已上传" in everyone.text
    with database.SessionLocal() as db:
        row = db.scalar(select(ProjectFile).where(ProjectFile.original_name == "everyone.txt"))
        assert row.visibility == "all"
        assert db.scalar(select(func.count()).select_from(ProjectFileViewer).where(ProjectFileViewer.file_id == row.id)) == 0

    limited = upload_project_file(tech, f"{path}/files", "secret.txt", b"only-promo", viewers=[promo_id])
    assert "资料已上传" in limited.text
    secret = file_link(limited.text, path, "secret.txt")
    meta = re.search(r'secret\.txt</a><p class="muted">([^<]*)</p>', limited.text).group(1)
    assert "林夏同学" in meta and "周宁同学" in meta and "何琪同学" not in meta
    assert tech.get(secret).content == b"only-promo"
    assert promo.get(secret).content == b"only-promo"
    assert honor.get(secret).content == b"only-promo"
    assert "secret.txt" not in other.get(path).text
    hidden = other.get(secret, follow_redirects=True)
    assert "找不到这个文件" in hidden.text
    assert b"only-promo" not in hidden.content
    with database.SessionLocal() as db:
        secret_row = db.scalar(select(ProjectFile).where(ProjectFile.original_name == "secret.txt"))
        assert secret_row.visibility == "selected"
        assert secret_row.project_id == project_id
        secret_file_id = secret_row.id
        viewers = set(db.scalars(select(ProjectFileViewer.user_id).where(ProjectFileViewer.file_id == secret_file_id)).all())
        assert viewers == {promo_id}

    private = upload_project_file(promo, f"{path}/files", "mine.txt", b"leader-and-me", viewers=[])
    assert "资料已上传" in private.text
    mine = file_link(tech.get(path).text, path, "mine.txt")
    assert tech.get(mine).content == b"leader-and-me"
    assert promo.get(mine).content == b"leader-and-me"
    assert "mine.txt" not in other.get(path).text
    assert "找不到这个文件" in other.get(mine, follow_redirects=True).text
    assert honor.get(mine).content == b"leader-and-me"

    assert "可见人必须是当前项目成员" in upload_project_file(
        tech, f"{path}/files", "x.txt", b"x", viewers=[999999],
    ).text
    with database.SessionLocal() as db:
        assert db.scalar(select(ProjectFile.id).where(ProjectFile.original_name == "x.txt")) is None

    lone, _lone_id = club["member"]("外来同学", "宣传部")
    assert "找不到这个立项" in post(lone, f"{path}/files", files={"file": ("nope.txt", b"no", "text/plain")}).text
    from_honor = upload_project_file(honor, f"{path}/files", "from-honor.txt", b"honor-file", viewers=[tech_id, promo_id, other_id])
    assert "资料已上传" in from_honor.text
    assert promo.get(file_link(from_honor.text, path, "from-honor.txt")).content == b"honor-file"

    newbie, newbie_id = club["member"]("新成员", "技术部")
    assert "立项已保存" in post(tech, f"{path}/edit", {
        "title": "资料库", "summary": "附件", "department": "技术部",
        "members": [tech_id, promo_id, other_id, newbie_id],
    }).text
    assert newbie.get(file_link(newbie.get(path).text, path, "everyone.txt")).content == b"all-people"
    assert newbie.get(note).content == b"hello-all"
    assert "secret.txt" not in newbie.get(path).text
    assert "找不到这个文件" in newbie.get(secret, follow_redirects=True).text

    note_id = note.rsplit("/", 1)[-1]
    assert "没有权限删除这个文件" in post(promo, f"{path}/files/{note_id}/delete").text
    assert "找不到这个文件" in post(other, f"{path}/files/{secret_file_id}/delete").text
    assert "文件已删除" in post(honor, f"{path}/files/{secret_file_id}/delete").text
    with database.SessionLocal() as db:
        assert db.scalar(select(ProjectFile.id).where(ProjectFile.original_name == "secret.txt")) is None
        assert db.scalar(select(func.count()).select_from(ProjectFileViewer).where(ProjectFileViewer.file_id == secret_file_id)) == 0
    assert tech.get(note).content == b"hello-all"


def test_legacy_project_file_without_visibility_defaults_to_all(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE project_files (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            stored_name VARCHAR(80) NOT NULL,
            original_name VARCHAR(180) NOT NULL,
            size INTEGER NOT NULL,
            uploader_id INTEGER NOT NULL,
            uploaded_at DATETIME
        )
        """
    )
    conn.execute(
        "INSERT INTO project_files (id, project_id, stored_name, original_name, size, uploader_id, uploaded_at) "
        "VALUES (7, 3, 'legacy.txt', '旧资料.txt', 4, 1, '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    try:
        from app.main import create_app

        create_app()
        with database.engine.connect() as db:
            cols = {row[1]: row for row in db.execute(text("PRAGMA table_info(project_files)")).all()}
            assert cols["visibility"][3] == 1
            assert db.execute(text("SELECT visibility, original_name FROM project_files WHERE id = 7")).one() == ("all", "旧资料.txt")
            tables = {row[0] for row in db.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()}
            assert "project_file_viewers" in tables
        with database.SessionLocal() as db:
            row = db.get(ProjectFile, 7)
            assert row.visibility == "all"
            assert row.original_name == "旧资料.txt"
    finally:
        database.engine.dispose()
        database.engine, database.SessionLocal = previous
