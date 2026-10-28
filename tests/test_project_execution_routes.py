"""Browser-facing workflow, permissions, and CSRF regression coverage."""
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import ProjectMember
from test_project_execution import execution_club, task


def csrf(response):
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, response.text[:1500]
    return match.group(1)


def client_as(club, name):
    client = TestClient(club["app"])
    page = client.get("/login")
    response = client.post("/login", data={"csrf": csrf(page), "account": name, "password": "secret12"}, follow_redirects=True)
    assert response.status_code == 200
    assert response.url.path == "/"
    return client


def post(client, path, data):
    return client.post(path, data=dict(data, csrf=csrf(client.get("/"))), follow_redirects=True)


def test_workspace_forms_and_private_cache_headers(execution_club):
    c = execution_club
    client = client_as(c, "founder")
    base = f'/projects/{c["project"].id}/execution'
    row = task(c, title="页面中的任务")
    for suffix in ("", "/tasks/new", f"/tasks/{row.id}", f"/tasks/{row.id}/edit", "/milestones/new"):
        response = client.get(base + suffix)
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        assert 'name="csrf"' in response.text
    response = client.get(base)
    assert "页面中的任务" in response.text
    assert "里程碑" in response.text
    assert base in client.get(f'/projects/{c["project"].id}').text


def test_login_required_and_nonparticipant_cannot_see_workspace(execution_club):
    c = execution_club
    base = f'/projects/{c["project"].id}/execution'
    row = task(c, title="受保护的执行任务")
    anonymous = TestClient(c["app"])
    assert anonymous.get(base).url.path == "/login"
    outsider = client_as(c, "outsider")
    for suffix in ("", "/tasks/new", f"/tasks/{row.id}", f"/tasks/{row.id}/edit", "/milestones/new"):
        response = outsider.get(base + suffix, follow_redirects=True)
        assert "受保护的执行任务" not in response.text
        assert response.url.path == "/projects"


def test_forged_progress_and_csrf_cannot_mutate_task(execution_club):
    c = execution_club
    row = task(c)
    path = f'/projects/{c["project"].id}/execution/tasks/{row.id}/progress'
    worker = client_as(c, "worker")
    payload = dict(status="done", progress="100", note="伪造提交", version=row.version)
    response = worker.post(path, data=payload, follow_redirects=True)
    assert "页面已过期" in response.text
    c["db"].refresh(row)
    assert row.status == "todo"
    outsider = client_as(c, "outsider")
    post(outsider, path, payload)
    c["db"].refresh(row)
    assert row.status == "todo"
    accepted = post(worker, path, payload)
    assert "任务进展已记录" in accepted.text
    c["db"].refresh(row)
    assert row.status == "done"


def test_key_task_route_submit_review_and_personal_todos(execution_club):
    c = execution_club
    row = task(c, title="关键交付任务", key=True)
    base = f'/projects/{c["project"].id}/execution/tasks/{row.id}'
    worker = client_as(c, "worker")
    reviewer = client_as(c, "reviewer")
    outsider = client_as(c, "outsider")
    assert "关键交付任务" in worker.get("/project-todos").text
    assert "关键交付任务" not in outsider.get("/project-todos").text
    submitted = post(worker, base + "/submit", dict(note="成果已经完成", version=row.version))
    assert "成果已提交验收" in submitted.text
    c["db"].refresh(row)
    assert row.status == "review"
    assert "关键交付任务" in reviewer.get("/project-todos").text
    rejected = post(worker, base + "/review", dict(decision="approved", comment="本人批准", version=row.version))
    c["db"].refresh(row)
    assert row.status == "review"
    approved = post(reviewer, base + "/review", dict(decision="approved", comment="验收符合标准", version=row.version))
    assert "任务验收结果已记录" in approved.text
    c["db"].refresh(row)
    assert row.status == "done"
    assert "关键交付任务" not in reviewer.get("/project-todos").text


def test_form_create_task_with_collaborators_and_filtered_list(execution_club):
    c = execution_club
    client = client_as(c, "founder")
    base = f'/projects/{c["project"].id}/execution'
    stage = c["ex"].save_stage(c["db"], c["users"]["founder"], [], c["project"].id, {"title": "设计阶段", "position": 0})
    response = post(client, base + "/tasks", dict(title="新表单任务", stage_id=stage.id, owner_id=c["users"]["worker"].id,
        collaborator_ids=[c["users"]["manager"].id], planned_start="2026-10-10", planned_end="2026-10-11",
        deliverable="设计图纸", acceptance_criteria="", priority="medium"))
    assert "任务已添加" in response.text
    assert "新表单任务" in client.get(base + "?status=todo").text
    from app.models import ProjectTask, TaskCollaborator
    row = c["db"].scalar(select(ProjectTask).where(ProjectTask.title == "新表单任务"))
    assert f'href="{base}/tasks/{row.id}"' not in client.get(base + "?status=done").text
    assert row.owner_id == c["users"]["worker"].id
    assert c["db"].scalar(select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == row.id)) == c["users"]["manager"].id


def test_member_removal_rechecked_on_task_and_todo_requests(execution_club):
    c = execution_club
    row = task(c, title="移除前的任务")
    worker = client_as(c, "worker")
    assert "移除前的任务" in worker.get("/project-todos").text
    link = c["db"].scalar(select(ProjectMember).where(ProjectMember.project_id == c["project"].id,
        ProjectMember.user_id == c["users"]["worker"].id))
    c["db"].delete(link)
    c["db"].commit()
    assert "移除前的任务" not in worker.get("/project-todos").text
    response = worker.get(f'/projects/{c["project"].id}/execution/tasks/{row.id}', follow_redirects=True)
    assert response.url.path == "/projects"
    assert "移除前的任务" not in response.text
