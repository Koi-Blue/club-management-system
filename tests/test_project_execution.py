"""Execution management must preserve old teams and enforce the delivery workflow."""
import io
import sqlite3
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text

import app.db as database
import app.services as svc
from app.config import load_settings
from app.models import Project, ProjectFile, ProjectFileViewer, ProjectMember, User, UserRole
from app.security import hash_password


@pytest.fixture
def execution_club(tmp_path, monkeypatch):
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'execution.db'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    from app.main import create_app
    import app.project_execution as execution

    app = create_app()
    db = database.SessionLocal()
    password = hash_password("secret12")
    users = {}
    for name in ("founder", "worker", "reviewer", "manager", "outsider", "honor"):
        user = User(username=name, real_name=name, password_hash=password,
                    department="技术部", status="active", phone="")
        db.add(user)
        db.flush()
        users[name] = user
    db.add(UserRole(user_id=users["honor"].id, role="荣誉社长"))
    db.commit()
    project = svc.create_project(db, users["founder"], [], "执行项目", "验证兼容", "技术部",
                                 [users[key].id for key in ("worker", "reviewer", "manager")])
    project.status = "approved"
    db.commit()
    yield dict(app=app, db=db, ex=execution, project=project, users=users, uploads=str(tmp_path / "uploads"))
    db.close()
    database.engine.dispose()
    database.engine, database.SessionLocal = previous


def task(club, *, title="任务", key=False, owner="worker", reviewer="reviewer", **changes):
    from app.models import ProjectStage
    stage = club["db"].scalar(select(ProjectStage).where(ProjectStage.project_id == club["project"].id))
    if stage is None:
        stage = club["ex"].save_stage(club["db"], club["users"]["founder"], [], club["project"].id,
                                      {"title": "研发阶段", "position": 0})
    data = dict(title=title, stage_id=stage.id, owner_id=club["users"][owner].id, collaborator_ids=[],
                planned_start="2026-10-01", planned_end="2026-10-02", deliverable="成果说明",
                acceptance_criteria="达到规定精度", is_key=key,
                reviewer_id=club["users"][reviewer].id if key else "",
                priority="medium", predecessor_ids=[])
    data.update(changes)
    return club["ex"].save_task(club["db"], club["users"]["founder"], [], club["project"].id, data)


def progress(club, row, status="doing", value=50, actor="worker", **changes):
    data = dict(status=status, progress=value, blocked_reason="", note="已更新执行情况", version=row.version)
    data.update(changes)
    return club["ex"].update_task_progress(club["db"], club["users"][actor], [], club["project"].id, row.id, data)


def submit(club, row, **changes):
    data = dict(note="提交成果", file_ids=[], version=row.version)
    data.update(changes)
    return club["ex"].submit_task(club["db"], club["users"]["worker"], [], club["project"].id, row.id, data)


def review(club, row, decision="approved", actor="reviewer", **changes):
    data = dict(decision=decision, comment="指标核验通过" if decision == "approved" else "请补充测试数据", version=row.version)
    data.update(changes)
    return club["ex"].review_task(club["db"], club["users"][actor], [], club["project"].id, row.id, data)


def edited_task_data(row, **changes):
    data = dict(title=row.title, stage_id=row.stage_id or "", owner_id=row.owner_id,
                collaborator_ids=[], planned_start=str(row.planned_start or ""),
                planned_end=str(row.planned_end or ""), deliverable=row.deliverable,
                acceptance_criteria=row.acceptance_criteria, is_key=row.is_key,
                reviewer_id=row.reviewer_id or "", priority=row.priority,
                predecessor_ids=[], version=row.version, reason="调整工作安排")
    data.update(changes)
    return data


def edit_task(club, row, **changes):
    return club["ex"].save_task(club["db"], club["users"]["founder"], [], club["project"].id,
                               edited_task_data(row, **changes), task_id=row.id)


def test_new_project_defaults_and_creator_not_in_members(execution_club):
    c = execution_club
    p, db, u = c["project"], c["db"], c["users"]
    assert p.execution_enabled is True
    assert p.manager_id == u["founder"].id
    assert u["founder"].id not in set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == p.id)))
    assert c["ex"].project_context(db, u["founder"], [], p.id)
    row = task(c)
    p.status = "draft"
    db.commit()
    with pytest.raises(svc.AppError):
        progress(c, row)


def test_admin_creator_is_default_manager_without_global_project_access(execution_club):
    c = execution_club
    db, ex = c["db"], c["ex"]
    admin = db.scalar(select(User).where(User.is_admin.is_(True)))
    with pytest.raises(svc.AppError):
        ex.project_context(db, admin, [], c["project"].id)
    db.rollback()
    own = svc.create_project(db, admin, [], "管理员本人立项", "", "技术部", [c["users"]["worker"].id])
    assert own.manager_id == admin.id
    context = ex.project_context(db, admin, [], own.id)
    assert context["can_manage"]
    assert admin.id in {p["id"] for p in context["participants"]}
    stage = ex.save_stage(db, admin, [], own.id, {"title": "测试", "position": 1})
    row = ex.save_task(db, admin, [], own.id, dict(title="独立验收", stage_id=stage.id,
        owner_id=c["users"]["worker"].id, is_key=True, reviewer_id=admin.id,
        deliverable="测试数据", acceptance_criteria="指标满足要求"))
    assert row.reviewer_id == admin.id


def test_manager_assignment_preserves_creator_and_existing_file_acl(execution_club):
    c = execution_club
    db, p, u, ex = c["db"], c["project"], c["users"], c["ex"]
    svc.save_project_file(db, u["founder"], [], c["uploads"], p.id, "private.txt", io.BytesIO(b"private"), [u["worker"].id])
    file = db.scalar(select(ProjectFile).where(ProjectFile.project_id == p.id))
    with pytest.raises(svc.AppError):
        ex.set_manager(db, u["worker"], [], p.id, u["manager"].id, "越权", p.execution_version)
    db.rollback()
    ex.set_manager(db, u["founder"], [], p.id, u["manager"].id, "委派执行负责人", p.execution_version)
    assert p.leader_id == u["founder"].id
    assert p.manager_id == u["manager"].id
    assert not svc.can_view_project_file(db, u["manager"], [], p, file)
    assert set(db.scalars(select(ProjectFileViewer.user_id).where(ProjectFileViewer.file_id == file.id))) == {u["worker"].id}
    with pytest.raises(svc.AppError):
        ex.set_manager(db, u["manager"], [], p.id, u["worker"].id, "自己转交", p.execution_version)
    db.rollback()
    with pytest.raises(svc.AppError):
        ex.set_manager(db, u["honor"], ["荣誉社长"], p.id, u["outsider"].id, "不在项目", p.execution_version)
    db.rollback()
    ex.set_manager(db, u["honor"], ["荣誉社长"], p.id, u["worker"].id, "监督交接", p.execution_version)
    assert p.manager_id == u["worker"].id


def test_inactive_member_cannot_become_manager(execution_club):
    c = execution_club
    c["users"]["manager"].status = "disabled"
    c["db"].commit()
    with pytest.raises(svc.AppError):
        c["ex"].set_manager(c["db"], c["users"]["founder"], [], c["project"].id,
                            c["users"]["manager"].id, "委派", c["project"].execution_version)


def test_ordinary_task_completes_without_review_but_key_task_cannot(execution_club):
    c = execution_club
    ordinary = task(c)
    progress(c, ordinary, "done", 100)
    assert ordinary.status == "done" and ordinary.progress == 100
    key = task(c, title="关键任务", key=True)
    with pytest.raises(svc.AppError):
        progress(c, key, "done", 100)
    c["db"].rollback()
    assert key.status != "done"
    with pytest.raises(svc.AppError):
        task(c, title="自验收", key=True, reviewer="worker")


def test_key_task_submission_rejection_and_resubmission(execution_club):
    c = execution_club
    row = task(c, key=True)
    progress(c, row)
    submit(c, row)
    assert row.status == "review" and row.progress == 100
    with pytest.raises(svc.AppError):
        review(c, row, actor="worker")
    c["db"].rollback()
    review(c, row, "rejected")
    assert row.status == "doing"
    submit(c, row)
    review(c, row)
    assert row.status == "done"
    from app.models import ProjectSubmission
    submissions = c["db"].scalars(select(ProjectSubmission).where(ProjectSubmission.task_id == row.id).order_by(ProjectSubmission.id)).all()
    assert len(submissions) == 2
    assert submissions[0].status == "rejected"
    assert submissions[1].status == "approved"


def test_nonparticipant_cannot_read_update_or_review(execution_club):
    c = execution_club
    row = task(c, key=True)
    with pytest.raises(svc.AppError):
        c["ex"].project_context(c["db"], c["users"]["outsider"], [], c["project"].id)
    with pytest.raises(svc.AppError):
        progress(c, row, actor="outsider")
    with pytest.raises(svc.AppError):
        progress(c, row, actor="reviewer")
    submit(c, row)
    with pytest.raises(svc.AppError):
        review(c, row, actor="outsider")


def test_invalid_dates_and_cross_project_predecessor_are_rejected(execution_club):
    c = execution_club
    with pytest.raises(svc.AppError):
        task(c, planned_end="2026-09-01")
    c["db"].rollback()
    foreign = svc.create_project(c["db"], c["users"]["founder"], [], "另一项目", "", "技术部", [c["users"]["worker"].id])
    foreign.status = "approved"
    c["db"].commit()
    foreign_club = dict(c, project=foreign)
    predecessor = task(foreign_club)
    with pytest.raises(svc.AppError):
        task(c, predecessor_ids=[predecessor.id])


def test_dependencies_reject_self_and_cycles(execution_club):
    c = execution_club
    first = task(c, title="第一项")
    second = task(c, title="第二项", predecessor_ids=[first.id])
    with pytest.raises(svc.AppError):
        edit_task(c, first, predecessor_ids=[first.id])
    c["db"].rollback()
    with pytest.raises(svc.AppError):
        edit_task(c, first, predecessor_ids=[second.id])


def test_plan_changes_require_reason_and_reject_stale_version(execution_club):
    c = execution_club
    row = task(c)
    stale = row.version
    with pytest.raises(svc.AppError):
        edit_task(c, row, planned_end="2026-10-03", reason="")
    c["db"].rollback()
    edit_task(c, row, planned_end="2026-10-03")
    assert row.planned_end == date(2026, 10, 3)
    with pytest.raises(svc.AppError):
        edit_task(c, row, title="过期覆盖", version=stale)
    c["db"].rollback()
    assert row.title != "过期覆盖"


def test_submission_requires_reviewer_file_access_and_retains_accepted_evidence(execution_club):
    c = execution_club
    db, p, u = c["db"], c["project"], c["users"]
    row = task(c, key=True)
    svc.save_project_file(db, u["worker"], [], c["uploads"], p.id, "limited.txt", io.BytesIO(b"evidence"), [u["worker"].id])
    file = db.scalar(select(ProjectFile).where(ProjectFile.project_id == p.id))
    with pytest.raises(svc.AppError):
        submit(c, row, file_ids=[file.id])
    db.rollback()
    assert file.visibility == "selected"
    db.add(ProjectFileViewer(file_id=file.id, user_id=u["reviewer"].id))
    db.commit()
    submit(c, row, file_ids=[file.id])
    review(c, row)
    with pytest.raises(svc.AppError):
        svc.delete_project_file(db, u["worker"], [], c["uploads"], p.id, file.id)
    db.rollback()
    assert db.get(ProjectFile, file.id) is not None


def test_legacy_can_plan_but_cannot_execute_until_enabled(execution_club):
    c = execution_club
    c["project"].execution_enabled = False
    c["db"].commit()
    row = task(c)
    with pytest.raises(svc.AppError):
        progress(c, row)
    c["db"].rollback()
    with pytest.raises(svc.AppError):
        c["ex"].enable_execution(c["db"], c["users"]["worker"], [], c["project"].id, c["project"].execution_version)
    c["db"].rollback()
    c["ex"].enable_execution(c["db"], c["users"]["founder"], [], c["project"].id, c["project"].execution_version)
    assert c["project"].execution_enabled
    progress(c, row)
    assert row.status == "doing"


def test_new_archive_gate_and_old_archive_behavior(execution_club):
    c = execution_club
    row = task(c)
    with pytest.raises(svc.AppError):
        svc.archive_project(c["db"], c["users"]["founder"], [], c["project"].id)
    c["db"].rollback()
    progress(c, row, "done", 100)
    with pytest.raises(svc.AppError):
        svc.archive_project(c["db"], c["users"]["worker"], [], c["project"].id)
    c["db"].rollback()
    svc.archive_project(c["db"], c["users"]["founder"], [], c["project"].id)
    assert c["project"].status == "archived"
    old = svc.create_project(c["db"], c["users"]["founder"], [], "原流程", "", "技术部", [c["users"]["worker"].id])
    old.execution_enabled = False
    old.status = "approved"
    c["db"].commit()
    svc.archive_project(c["db"], c["users"]["worker"], [], old.id)
    assert old.status == "archived"


def test_member_edit_preserves_ids_and_requires_work_handoff(execution_club):
    c = execution_club
    db, p, u = c["db"], c["project"], c["users"]
    before = dict(db.execute(select(ProjectMember.user_id, ProjectMember.id).where(ProjectMember.project_id == p.id)).all())
    row = task(c)
    with pytest.raises(svc.AppError):
        svc.update_project(db, u["founder"], [], p.id, p.title, p.summary, p.department, [u["reviewer"].id, u["manager"].id])
    db.rollback()
    svc.update_project(db, u["founder"], [], p.id, p.title, p.summary, p.department, list(before) + [u["outsider"].id])
    after = dict(db.execute(select(ProjectMember.user_id, ProjectMember.id).where(ProjectMember.project_id == p.id)).all())
    assert {key: after[key] for key in before} == before
    progress(c, row, "done", 100)
    svc.update_project(db, u["founder"], [], p.id, p.title, p.summary, p.department, [u["reviewer"].id, u["manager"].id])
    assert row.owner_id == u["worker"].id
    with pytest.raises(svc.AppError):
        c["ex"].project_context(db, u["worker"], [], p.id)


def milestone(club, rows, **changes):
    data = dict(title="阶段验收", owner_id=club["users"]["worker"].id,
                reviewer_id=club["users"]["reviewer"].id, planned_date="2026-10-03",
                acceptance_criteria="所有指标满足要求", task_ids=[r.id for r in rows])
    data.update(changes)
    return club["ex"].save_milestone(club["db"], club["users"]["founder"], [], club["project"].id, data)


def test_milestone_needs_completed_tasks_and_independent_acceptance(execution_club):
    c = execution_club
    row = task(c)
    checkpoint = milestone(c, [row])
    with pytest.raises(svc.AppError):
        c["ex"].submit_milestone(c["db"], c["users"]["worker"], [], c["project"].id, checkpoint.id,
                                 {"note": "提前提交", "version": checkpoint.version})
    c["db"].rollback()
    progress(c, row, "done", 100)
    assert checkpoint.status == "todo"
    with pytest.raises(svc.AppError):
        svc.archive_project(c["db"], c["users"]["founder"], [], c["project"].id)
    c["db"].rollback()
    c["ex"].submit_milestone(c["db"], c["users"]["worker"], [], c["project"].id, checkpoint.id,
                             {"note": "提交里程碑成果", "version": checkpoint.version})
    with pytest.raises(svc.AppError):
        c["ex"].review_milestone(c["db"], c["users"]["worker"], [], c["project"].id, checkpoint.id,
                                 {"decision": "approved", "comment": "自己验收", "version": checkpoint.version})
    c["db"].rollback()
    c["ex"].review_milestone(c["db"], c["users"]["reviewer"], [], c["project"].id, checkpoint.id,
                             {"decision": "approved", "comment": "阶段成果通过", "version": checkpoint.version})
    assert checkpoint.status == "done"
    svc.archive_project(c["db"], c["users"]["honor"], ["荣誉社长"], c["project"].id)
    assert c["project"].status == "archived"


def test_progress_is_equal_weight_and_cancellation_is_excluded(execution_club, monkeypatch):
    c = execution_club
    monkeypatch.setattr(c["ex"], "utcnow", lambda: datetime(2026, 10, 28, tzinfo=timezone.utc))
    stats = lambda: c["ex"].project_context(c["db"], c["users"]["founder"], [], c["project"].id)["stats"]
    assert stats()["progress"] is None
    first = task(c, title="普通完成")
    second = task(c, title="部分完成")
    cancelled = task(c, title="取消")
    progress(c, first, "done", 100)
    progress(c, second, "doing", 40)
    progress(c, cancelled, "cancelled", 0, actor="founder")
    checkpoint = milestone(c, [first])
    result = stats()
    assert result["progress"] == 70
    assert result["task_total"] == 2 and result["task_done"] == 1
    assert result["milestone_total"] == 1 and result["milestone_done"] == 0
    assert result["overdue"] == 1


def test_reviewer_access_revocation_prevents_acceptance(execution_club):
    c = execution_club
    row = task(c, key=True)
    submit(c, row)
    c["users"]["reviewer"].status = "disabled"
    c["db"].commit()
    with pytest.raises(svc.AppError):
        review(c, row)
    c["db"].rollback()
    assert row.status == "review"


def test_pending_review_can_be_withdrawn_and_reassigned_without_losing_history(execution_club):
    c = execution_club
    row = task(c, key=True)
    first = submit(c, row)
    c["users"]["reviewer"].status = "disabled"
    c["db"].commit()
    with pytest.raises(svc.AppError):
        c["ex"].withdraw_submission(c["db"], c["users"]["outsider"], [], c["project"].id, row.id,
                                     {"reason": "无权撤回", "version": row.version})
    c["db"].rollback()
    c["ex"].withdraw_submission(c["db"], c["users"]["founder"], [], c["project"].id, row.id,
                                 {"reason": "原验收人停用，重新指派", "version": row.version})
    assert first.status == "withdrawn" and row.status == "doing"
    edit_task(c, row, reviewer_id=c["users"]["manager"].id)
    second = submit(c, row)
    review(c, row, actor="manager")
    assert row.status == "done"
    assert first.id != second.id
    assert first.status == "withdrawn" and second.status == "approved"
    with pytest.raises(svc.AppError):
        c["ex"].withdraw_submission(c["db"], c["users"]["founder"], [], c["project"].id, row.id,
                                     {"reason": "不能覆盖已验收批次", "version": row.version})


def test_independent_session_cannot_overwrite_newer_plan(execution_club):
    c = execution_club
    row = task(c)
    old_version = row.version
    from app.models import ProjectTask
    with database.SessionLocal() as second:
        stale = second.get(ProjectTask, row.id)
        actor = second.get(User, c["users"]["founder"].id)
        edit_task(c, row, title="新的分工计划")
        with pytest.raises(svc.AppError):
            c["ex"].save_task(second, actor, [], c["project"].id,
                              edited_task_data(stale, title="过期会话试图覆盖", version=old_version), task_id=row.id)
        second.rollback()
    c["db"].refresh(row)
    assert row.title == "新的分工计划"


def test_inclusive_natural_days_and_dependency_warnings_do_not_reschedule(execution_club):
    c = execution_club
    first = task(c, planned_start="2026-10-01", planned_end="2026-10-02")
    second = task(c, planned_start="2026-10-02", planned_end="2026-10-02", predecessor_ids=[first.id])
    context = c["ex"].project_context(c["db"], c["users"]["founder"], [], c["project"].id)
    assert context["task_info"][first.id]["duration"] == 2
    assert context["task_info"][second.id]["duration"] == 1
    assert context["task_info"][second.id]["warnings"]
    assert second.planned_start == date(2026, 10, 2)


def test_cross_project_task_url_and_file_cannot_be_reused(execution_club):
    c = execution_club
    row = task(c, key=True)
    other = svc.create_project(c["db"], c["users"]["founder"], [], "其他项目", "", "技术部",
                               [c["users"]["worker"].id, c["users"]["reviewer"].id])
    other.status = "approved"
    c["db"].commit()
    with pytest.raises(svc.AppError):
        c["ex"].update_task_progress(c["db"], c["users"]["worker"], [], other.id, row.id,
                                     {"status": "doing", "progress": 50, "note": "跨项目", "version": row.version})
    c["db"].rollback()
    svc.save_project_file(c["db"], c["users"]["worker"], [], c["uploads"], other.id, "foreign.txt", io.BytesIO(b"other"))
    foreign_file = c["db"].scalar(select(ProjectFile).where(ProjectFile.project_id == other.id))
    with pytest.raises(svc.AppError):
        submit(c, row, file_ids=[foreign_file.id])


def test_legacy_upgrade_preserves_team_identity_and_selected_files(tmp_path, monkeypatch):
    path = tmp_path / "legacy-execution.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL, password_hash TEXT NOT NULL,
          real_name TEXT NOT NULL, phone TEXT NOT NULL, college TEXT NOT NULL, class_name TEXT NOT NULL,
          department TEXT NOT NULL, status TEXT NOT NULL, is_admin BOOLEAN NOT NULL, created_at DATETIME NOT NULL);
        INSERT INTO users VALUES (101,'original','unused','原立项人','','','','技术部','active',0,'2026-01-01');
        INSERT INTO users VALUES (102,'member','unused','原跨部门成员','','','','宣传部','active',0,'2026-01-01');
        INSERT INTO users VALUES (103,'inactive','unused','停用立项人','','','','技术部','disabled',0,'2026-01-01');
        CREATE TABLE projects (id INTEGER PRIMARY KEY,title TEXT NOT NULL,summary TEXT NOT NULL,department TEXT NOT NULL,
          status TEXT NOT NULL,leader_id INTEGER NOT NULL,reviewer_id INTEGER,review_comment TEXT NOT NULL,created_at DATETIME,updated_at DATETIME);
        INSERT INTO projects VALUES (201,'已立项项目','已有说明','技术部','approved',101,NULL,'原审批意见','2026-01-01','2026-01-02');
        INSERT INTO projects VALUES (202,'历史归档','归档说明','技术部','archived',103,NULL,'历史审批','2026-01-01','2026-01-03');
        CREATE TABLE project_members (id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL,user_id INTEGER NOT NULL);
        INSERT INTO project_members VALUES (301,201,102);
        CREATE TABLE project_files (id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL,stored_name TEXT NOT NULL,
          original_name TEXT NOT NULL,size INTEGER NOT NULL,uploader_id INTEGER NOT NULL,uploaded_at DATETIME,visibility TEXT NOT NULL);
        INSERT INTO project_files VALUES (401,201,'original.txt','旧资料.txt',4,101,'2026-01-01','selected');
        CREATE TABLE project_file_viewers (id INTEGER PRIMARY KEY,file_id INTEGER NOT NULL,user_id INTEGER NOT NULL);
        INSERT INTO project_file_viewers VALUES (501,401,102);
        """)
        old_project = conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
        old_members = conn.execute("SELECT * FROM project_members").fetchall()
        old_files = conn.execute("SELECT * FROM project_files").fetchall()
        old_viewers = conn.execute("SELECT * FROM project_file_viewers").fetchall()
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    try:
        settings = load_settings()
        database.setup(settings)
        database.init_db(settings)
        database.init_db(settings)
        with database.engine.connect() as db:
            assert [tuple(r) for r in db.execute(text("SELECT id,title,summary,department,status,leader_id,reviewer_id,review_comment,created_at,updated_at FROM projects ORDER BY id"))] == old_project
            assert [tuple(r) for r in db.execute(text("SELECT * FROM project_members"))] == old_members
            assert [tuple(r) for r in db.execute(text("SELECT * FROM project_files"))] == old_files
            assert [tuple(r) for r in db.execute(text("SELECT * FROM project_file_viewers"))] == old_viewers
            assert db.execute(text("SELECT execution_enabled,manager_id FROM projects WHERE id=201")).one() == (0, 101)
            assert db.execute(text("SELECT execution_enabled,manager_id FROM projects WHERE id=202")).one() == (0, None)
    finally:
        database.engine.dispose()
        database.engine, database.SessionLocal = previous
