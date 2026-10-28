"""Project execution, kept separate from the historical application/approval flow."""
from datetime import date
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.models import (
    Project, ProjectMember, ProjectFile, ProjectStage, ProjectTask, TaskCollaborator,
    TaskDependency, ProjectMilestone, MilestoneTask, ProjectSubmission, SubmissionFile,
    ProjectEvent, User, UserRole, utcnow,
)
from app import services as svc
from app.services import AppError

TASK_STATUS = {"todo": "未开始", "doing": "进行中", "review": "待验收", "done": "已完成", "cancelled": "已取消"}
MILESTONE_STATUS = {"todo": "未提交", "review": "待验收", "done": "已通过"}
PRIORITIES = {"high": "高", "medium": "中", "low": "低"}


def _int(value, label, minimum=1):
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise AppError(f"{label}格式不正确")
    if result < minimum:
        raise AppError(f"{label}格式不正确")
    return result


def _ids(value):
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return list(dict.fromkeys(_int(v, "选择项") for v in value if v not in (None, "")))


def _text(value, label, limit=4000, required=False):
    return (svc.require_text if required else svc.optional_text)(str(value or ""), label, limit)


def _date(value, label):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise AppError(f"{label}格式不正确")


def lock_project(db, project):
    """Serialize all project writes before checking state, membership or dependency graphs.

    SQLite obtains its write reservation on this no-op UPDATE. Other engines take a
    row lock. The caller must commit/rollback the same transaction after its change.
    """
    try:
        db.execute(update(Project).where(Project.id == project.id)
                   .values(execution_version=Project.execution_version),
                   execution_options={"synchronize_session": False})
        db.refresh(project)
    except OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise AppError("项目正在被其他人修改，请刷新后重试") from exc
        raise
    return project


def _project(db, user, roles, project_id, write=False):
    project = db.get(Project, _int(project_id, "项目"))
    if user.status != "active" or project is None or not svc.can_view_project(db, user, roles, project):
        raise AppError("没有查看这个项目的权限")
    if write:
        lock_project(db, project)
        if not svc.can_view_project(db, user, roles, project):
            raise AppError("没有查看这个项目的权限")
    return project


def participant_ids(db, project):
    return {project.leader_id, *db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project.id)).all()}


def _participant(db, project, uid):
    uid = _int(uid, "项目成员")
    person = db.get(User, uid)
    if person is None or person.status != "active" or (person.is_admin and uid != project.leader_id) or uid not in participant_ids(db, project):
        raise AppError("请选择有效项目成员")
    return person


def _reviewer(db, project, uid, owner_id):
    person = db.get(User, _int(uid, "验收人"))
    if person is None or person.status != "active" or not svc.can_view_project(db, person, svc.roles_of(db, person.id), project):
        raise AppError("验收人必须是有项目访问权限的有效账号")
    if person.id == owner_id:
        raise AppError("负责人不能验收自己的任务或里程碑")
    return person


def can_manage_execution(db, user, project):
    return user.status == "active" and (not user.is_admin or user.id == project.leader_id) and user.id == project.manager_id and user.id in participant_ids(db, project)


def _manage(db, user, project):
    if not can_manage_execution(db, user, project):
        raise AppError("只有执行负责人可以管理执行计划")
    if project.status not in {"draft", "rejected", "approved"}:
        raise AppError("当前项目状态不能修改执行计划")


def _running(project):
    if not project.execution_enabled:
        raise AppError("请先启用执行管理")
    if project.status != "approved":
        raise AppError("立项通过且未归档的项目才可执行任务")


def _row(db, model, ident, project):
    row = db.get(model, _int(ident, "记录"))
    if row is None or row.project_id != project.id:
        raise AppError("记录不存在或不属于这个项目")
    return row


def _bump(db, row, version, field="version"):
    expected = _int(version, "版本")
    model = type(row)
    result = db.execute(update(model).where(model.id == row.id, getattr(model, field) == expected)
                        .values({field: expected + 1}), execution_options={"synchronize_session": False})
    if result.rowcount != 1:
        raise AppError("记录已被其他人修改，请刷新后重试")
    db.refresh(row, [field])


def _event(db, project, user, kind, message):
    db.add(ProjectEvent(project_id=project.id, actor_id=user.id, kind=kind, message=message))
    project.updated_at = utcnow()


def set_manager(db, user, roles, project_id, manager_id, reason="", version=None):
    project = _project(db, user, roles, project_id, write=True)
    if project.status == "archived":
        raise AppError("已归档项目不能更换执行负责人")
    if user.id != project.leader_id and "荣誉社长" not in roles:
        raise AppError("只有立项人或荣誉社长可以更换执行负责人")
    manager = _participant(db, project, manager_id)
    reason = _text(reason, "变更原因", required=True)
    _bump(db, project, version, "execution_version")
    old = db.get(User, project.manager_id) if project.manager_id else None
    project.manager_id = manager.id
    _event(db, project, user, "manager", f"执行负责人：{old.real_name if old else '待指定'} → {manager.real_name}；{reason}")
    db.commit()
    return project


def enable_execution(db, user, roles, project_id, version=None):
    project = _project(db, user, roles, project_id, write=True)
    _manage(db, user, project)
    if project.status != "approved":
        raise AppError("立项通过后才可启用执行管理")
    if project.execution_enabled:
        raise AppError("执行管理已经启用")
    tasks = db.scalars(select(ProjectTask).where(ProjectTask.project_id == project.id)).all()
    milestones = db.scalars(select(ProjectMilestone).where(ProjectMilestone.project_id == project.id)).all()
    if not tasks:
        raise AppError("请先建立至少一个任务的执行计划")
    for item in [*tasks, *milestones]:
        _participant(db, project, item.owner_id)
        if isinstance(item, ProjectMilestone) or item.is_key:
            _reviewer(db, project, item.reviewer_id, item.owner_id)
    _bump(db, project, version, "execution_version")
    project.execution_enabled = True
    _event(db, project, user, "enable", "启用执行管理；从此按任务和里程碑验收规则结项")
    db.commit()
    return project


def save_stage(db, user, roles, project_id, data, stage_id=None):
    project = _project(db, user, roles, project_id, write=True)
    _manage(db, user, project)
    title = _text(data.get("title"), "阶段名称", 100, True)
    position = _int(data.get("position") or 0, "排序", 0)
    if stage_id is None:
        stage = ProjectStage(project_id=project.id, title=title, position=position)
        db.add(stage)
    else:
        stage = _row(db, ProjectStage, stage_id, project)
        _bump(db, stage, data.get("version"))
        stage.title, stage.position = title, position
    _event(db, project, user, "stage", f"保存阶段：{title}")
    db.commit()
    return stage


def _dependency_check(db, project, task_id, predecessor_ids):
    for pid in predecessor_ids:
        _row(db, ProjectTask, pid, project)
        if pid == task_id:
            raise AppError("任务不能依赖自身")
    edges = {}
    pairs = db.execute(select(TaskDependency.task_id, TaskDependency.predecessor_id)
                       .join(ProjectTask, ProjectTask.id == TaskDependency.task_id)
                       .where(ProjectTask.project_id == project.id)).all()
    for child, parent in pairs:
        if child != task_id:
            edges.setdefault(child, []).append(parent)
    edges[task_id] = predecessor_ids
    visiting, visited = set(), set()
    def visit(node):
        if node in visiting:
            raise AppError("前置任务存在循环依赖")
        if node in visited:
            return
        visiting.add(node)
        for parent in edges.get(node, []):
            visit(parent)
        visiting.remove(node)
        visited.add(node)
    for node in edges:
        visit(node)


def save_task(db, user, roles, project_id, data, task_id=None):
    project = _project(db, user, roles, project_id, write=True)
    _manage(db, user, project)
    title = _text(data.get("title"), "任务名称", 160, True)
    stage = _row(db, ProjectStage, data.get("stage_id"), project)
    owner = _participant(db, project, data.get("owner_id"))
    collaborators = _ids(data.get("collaborator_ids"))
    for uid in collaborators:
        _participant(db, project, uid)
    is_key = str(data.get("is_key", "")).lower() in {"true", "1", "on", "yes"}
    reviewer_id = _reviewer(db, project, data.get("reviewer_id"), owner.id).id if is_key else None
    start, end = _date(data.get("planned_start"), "计划开始"), _date(data.get("planned_end"), "计划完成")
    if bool(start) != bool(end) or (start and end < start):
        raise AppError("请同时填写计划起止日期，且完成日期不能早于开始日期")
    priority = data.get("priority") or "medium"
    if priority not in PRIORITIES:
        raise AppError("优先级不正确")
    deliverable = _text(data.get("deliverable"), "交付物", required=True)
    criteria = _text(data.get("acceptance_criteria"), "验收标准", required=is_key)
    predecessors = _ids(data.get("predecessor_ids"))
    _dependency_check(db, project, task_id or 0, predecessors)
    if task_id is None:
        task = ProjectTask(project_id=project.id, title=title, stage_id=stage.id, owner_id=owner.id)
        db.add(task)
        db.flush()
        message = f"创建任务：{title}；负责人：{owner.real_name}"
    else:
        task = _row(db, ProjectTask, task_id, project)
        if task.status in {"review", "done", "cancelled"}:
            raise AppError("待验收、已完成或已取消任务不能直接修改计划")
        reason = _text(data.get("reason"), "修改原因", required=True)
        _bump(db, task, data.get("version"))
        message = (f"修改任务：{task.title} → {title}；计划 {task.planned_start or '未定'}～{task.planned_end or '未定'}"
                   f" → {start or '未定'}～{end or '未定'}；负责人 #{task.owner_id} → #{owner.id}；{reason}")
    task.title, task.stage_id, task.owner_id = title, stage.id, owner.id
    task.reviewer_id, task.is_key, task.priority = reviewer_id, is_key, priority
    task.planned_start, task.planned_end = start, end
    task.deliverable, task.acceptance_criteria = deliverable, criteria
    task.updated_at = utcnow()
    db.execute(delete(TaskCollaborator).where(TaskCollaborator.task_id == task.id))
    db.execute(delete(TaskDependency).where(TaskDependency.task_id == task.id))
    for uid in collaborators:
        db.add(TaskCollaborator(task_id=task.id, user_id=uid))
    for pid in predecessors:
        db.add(TaskDependency(task_id=task.id, predecessor_id=pid))
    _event(db, project, user, "task_plan", message)
    db.commit()
    return task


def update_task_progress(db, user, roles, project_id, task_id, data):
    project = _project(db, user, roles, project_id, write=True)
    _running(project)
    task = _row(db, ProjectTask, task_id, project)
    status = data.get("status") or task.status
    if status == "cancelled":
        _manage(db, user, project)
    elif user.id != task.owner_id:
        raise AppError("只有任务负责人可以更新执行进度")
    if task.status in {"review", "done", "cancelled"}:
        raise AppError("当前任务状态不能修改进度")
    if status not in {"todo", "doing", "done", "cancelled"}:
        raise AppError("任务状态不正确")
    if task.is_key and status == "done":
        raise AppError("关键任务必须提交并通过验收后完成")
    progress = _int(data.get("progress", task.progress), "进度", 0)
    if progress > 100:
        raise AppError("进度必须在 0 到 100 之间")
    note = _text(data.get("note"), "进展说明", required=True)
    blocked = _text(data.get("blocked_reason"), "受阻原因")
    _bump(db, task, data.get("version"))
    task.status, task.blocked_reason = status, blocked
    task.progress = 0 if status == "todo" else 100 if status == "done" else progress
    if status in {"doing", "done"} and task.actual_start is None:
        task.actual_start = utcnow()
    if status == "done":
        task.actual_end = utcnow()
        task.blocked_reason = ""
    task.updated_at = utcnow()
    _event(db, project, user, "progress", f"{task.title}：{TASK_STATUS[status]}，{task.progress}%；{note}" + (f"；受阻：{blocked}" if blocked else ""))
    db.commit()
    return task


def _submission_files(db, project, user, reviewer, ids):
    files = []
    for fid in _ids(ids):
        row = db.get(ProjectFile, fid)
        if row is None or row.project_id != project.id or not svc.can_view_project_file(db, user, svc.roles_of(db, user.id), project, row):
            raise AppError("交付材料不存在、无权查看或不属于这个项目")
        if not svc.can_view_project_file(db, reviewer, svc.roles_of(db, reviewer.id), project, row):
            raise AppError("验收人无权查看所选材料，请先调整材料授权或更换材料")
        files.append(row)
    return files


def _submit(db, user, roles, project_id, item_id, data, milestone=False):
    project = _project(db, user, roles, project_id, write=True)
    _running(project)
    model = ProjectMilestone if milestone else ProjectTask
    item = _row(db, model, item_id, project)
    if item.owner_id != user.id:
        raise AppError("只有负责人可以提交成果")
    if not milestone and not item.is_key:
        raise AppError("普通任务可直接登记完成，无需验收")
    if item.status not in {"todo", "doing"}:
        raise AppError("当前状态不能提交验收")
    if milestone:
        _milestone_ready(db, item)
    reviewer = _reviewer(db, project, item.reviewer_id, item.owner_id)
    note = _text(data.get("note"), "交付说明", required=True)
    files = _submission_files(db, project, user, reviewer, data.get("file_ids"))
    _bump(db, item, data.get("version"))
    submission = ProjectSubmission(project_id=project.id, task_id=None if milestone else item.id,
        milestone_id=item.id if milestone else None, submitter_id=user.id, reviewer_id=reviewer.id,
        note=note, criteria_snapshot=item.acceptance_criteria)
    db.add(submission)
    db.flush()
    for row in files:
        db.add(SubmissionFile(submission_id=submission.id, file_id=row.id))
    item.status = "review"
    if not milestone:
        item.progress = 100
        item.actual_start = item.actual_start or utcnow()
        item.actual_end = utcnow()
        item.updated_at = utcnow()
    _event(db, project, user, "submit", f"提交{'里程碑' if milestone else '关键任务'}：{item.title}；交付批次 #{submission.id}")
    db.commit()
    return submission


def submit_task(db, user, roles, project_id, task_id, data):
    return _submit(db, user, roles, project_id, task_id, data)


def _review(db, user, roles, project_id, item_id, data, milestone=False):
    project = _project(db, user, roles, project_id, write=True)
    _running(project)
    model = ProjectMilestone if milestone else ProjectTask
    item = _row(db, model, item_id, project)
    if user.id != item.reviewer_id or user.id == item.owner_id:
        raise AppError("只有指定的其他验收人可以验收")
    if item.status != "review":
        raise AppError("当前没有待验收成果")
    _reviewer(db, project, user.id, item.owner_id)
    decision = data.get("decision")
    if decision not in {"approved", "rejected"}:
        raise AppError("验收结果不正确")
    comment = _text(data.get("comment"), "验收意见", required=True)
    column = ProjectSubmission.milestone_id if milestone else ProjectSubmission.task_id
    submission = db.scalar(select(ProjectSubmission).where(column == item.id, ProjectSubmission.status == "pending").order_by(ProjectSubmission.id.desc()))
    if submission is None or submission.reviewer_id != user.id or submission.submitter_id == user.id:
        raise AppError("待验收批次无效，请刷新")
    fids = db.scalars(select(SubmissionFile.file_id).where(SubmissionFile.submission_id == submission.id)).all()
    _submission_files(db, project, user, user, fids)
    if milestone and decision == "approved":
        _milestone_ready(db, item)
    _bump(db, item, data.get("version"))
    submission.status, submission.review_comment, submission.reviewed_at = decision, comment, utcnow()
    item.status = "done" if decision == "approved" else "todo" if milestone else "doing"
    if milestone:
        item.actual_date = utcnow() if decision == "approved" else None
    elif decision == "rejected":
        item.actual_end = None
        item.updated_at = utcnow()
    else:
        item.blocked_reason = ""
        item.updated_at = utcnow()
    _event(db, project, user, "review", f"{item.title}：{'验收通过' if decision == 'approved' else '退回修改'}；批次 #{submission.id}；{comment}")
    db.commit()
    return submission


def review_task(db, user, roles, project_id, task_id, data):
    return _review(db, user, roles, project_id, task_id, data)


def save_milestone(db, user, roles, project_id, data, milestone_id=None):
    project = _project(db, user, roles, project_id, write=True)
    _manage(db, user, project)
    title = _text(data.get("title"), "里程碑名称", 160, True)
    owner = _participant(db, project, data.get("owner_id"))
    reviewer = _reviewer(db, project, data.get("reviewer_id"), owner.id)
    criteria = _text(data.get("acceptance_criteria"), "验收标准", required=True)
    planned = _date(data.get("planned_date"), "计划日期")
    stage_id = _row(db, ProjectStage, data["stage_id"], project).id if data.get("stage_id") else None
    tids = _ids(data.get("task_ids"))
    if not tids:
        raise AppError("里程碑至少关联一个任务")
    for tid in tids:
        _row(db, ProjectTask, tid, project)
    if milestone_id is None:
        item = ProjectMilestone(project_id=project.id, title=title, owner_id=owner.id, reviewer_id=reviewer.id, acceptance_criteria=criteria)
        db.add(item)
        db.flush()
        reason = "创建"
    else:
        item = _row(db, ProjectMilestone, milestone_id, project)
        if item.status != "todo":
            raise AppError("待验收或已通过里程碑不能修改")
        reason = _text(data.get("reason"), "修改原因", required=True)
        _bump(db, item, data.get("version"))
        reason = f"计划 {item.planned_date or '未定'} → {planned or '未定'}；{reason}"
    item.title, item.owner_id, item.reviewer_id = title, owner.id, reviewer.id
    item.acceptance_criteria, item.planned_date, item.stage_id = criteria, planned, stage_id
    db.execute(delete(MilestoneTask).where(MilestoneTask.milestone_id == item.id))
    for tid in tids:
        db.add(MilestoneTask(milestone_id=item.id, task_id=tid))
    _event(db, project, user, "milestone_plan", f"保存里程碑：{title}；{reason}")
    db.commit()
    return item


def _milestone_ready(db, item):
    tasks = db.scalars(select(ProjectTask).join(MilestoneTask, MilestoneTask.task_id == ProjectTask.id)
                      .where(MilestoneTask.milestone_id == item.id)).all()
    effective = [t for t in tasks if t.status != "cancelled"]
    if not effective or any(t.status != "done" for t in effective):
        raise AppError("关联的有效任务全部完成后才能提交或通过里程碑验收")


def submit_milestone(db, user, roles, project_id, milestone_id, data):
    return _submit(db, user, roles, project_id, milestone_id, data, milestone=True)


def review_milestone(db, user, roles, project_id, milestone_id, data):
    return _review(db, user, roles, project_id, milestone_id, data, milestone=True)


def withdraw_submission(db, user, roles, project_id, item_id, data, milestone=False):
    project = _project(db, user, roles, project_id, write=True)
    _running(project)
    item = _row(db, ProjectMilestone if milestone else ProjectTask, item_id, project)
    if user.id != item.owner_id and not can_manage_execution(db, user, project):
        raise AppError("只有负责人或执行负责人可以撤回验收")
    if item.status != "review":
        raise AppError("只有待验收成果可以撤回")
    reason = _text(data.get("reason"), "撤回原因", required=True)
    column = ProjectSubmission.milestone_id if milestone else ProjectSubmission.task_id
    submission = db.scalar(select(ProjectSubmission).where(column == item.id, ProjectSubmission.status == "pending").order_by(ProjectSubmission.id.desc()))
    if submission is None:
        raise AppError("没有待验收批次")
    _bump(db, item, data.get("version"))
    submission.status, submission.review_comment, submission.reviewed_at = "withdrawn", reason, utcnow()
    item.status = "todo" if milestone else "doing"
    if not milestone:
        item.actual_end = None
        item.updated_at = utcnow()
    _event(db, project, user, "withdraw_submission", f"撤回验收：{item.title}；批次 #{submission.id}；{reason}")
    db.commit()
    return submission


def guard_members_change(db, project, member_ids):
    retained = {project.leader_id, *member_ids}
    if project.manager_id and project.manager_id not in retained:
        raise AppError("请先更换执行负责人，再移除该成员")
    tasks = db.scalars(select(ProjectTask).where(ProjectTask.project_id == project.id, ProjectTask.status.notin_(["done", "cancelled"]))).all()
    for task in tasks:
        if task.owner_id not in retained or (task.reviewer_id and task.reviewer_id not in retained and task.reviewer_id in participant_ids(db, project)):
            raise AppError("请先交接未完成任务的负责人或验收人，再移除成员")
    milestones = db.scalars(select(ProjectMilestone).where(ProjectMilestone.project_id == project.id, ProjectMilestone.status != "done")).all()
    for item in milestones:
        if item.owner_id not in retained or (item.reviewer_id not in retained and item.reviewer_id in participant_ids(db, project)):
            raise AppError("请先交接未完成里程碑的负责人或验收人，再移除成员")


def assert_can_archive(db, user, roles, project):
    if not project.execution_enabled:
        return
    if not can_manage_execution(db, user, project) and "荣誉社长" not in roles:
        raise AppError("只有执行负责人或荣誉社长可以结项归档")
    tasks = db.scalars(select(ProjectTask).where(ProjectTask.project_id == project.id, ProjectTask.status != "cancelled")).all()
    milestones = db.scalars(select(ProjectMilestone).where(ProjectMilestone.project_id == project.id)).all()
    if not tasks or any(t.status != "done" for t in tasks):
        raise AppError("所有有效任务完成、关键任务验收通过后才能归档")
    if any(m.status != "done" for m in milestones):
        raise AppError("所有里程碑验收通过后才能归档")


def is_file_protected(db, file_id):
    # All submission evidence is retained, including rejected batches and pending reviews.
    return db.scalar(select(SubmissionFile.id).where(SubmissionFile.file_id == file_id).limit(1)) is not None


def delete_execution_data(db, project_id):
    task_ids = select(ProjectTask.id).where(ProjectTask.project_id == project_id)
    milestone_ids = select(ProjectMilestone.id).where(ProjectMilestone.project_id == project_id)
    submission_ids = select(ProjectSubmission.id).where(ProjectSubmission.project_id == project_id)
    db.execute(delete(SubmissionFile).where(SubmissionFile.submission_id.in_(submission_ids)))
    db.execute(delete(ProjectSubmission).where(ProjectSubmission.project_id == project_id))
    db.execute(delete(MilestoneTask).where(MilestoneTask.milestone_id.in_(milestone_ids)))
    db.execute(delete(TaskDependency).where(TaskDependency.task_id.in_(task_ids)))
    db.execute(delete(TaskCollaborator).where(TaskCollaborator.task_id.in_(task_ids)))
    for model in [ProjectTask, ProjectMilestone, ProjectStage, ProjectEvent]:
        db.execute(delete(model).where(model.project_id == project_id))


def _submission_context(db, project, user, roles, rows, names):
    result = []
    labels = {"pending": "待验收", "approved": "已通过", "rejected": "已退回", "withdrawn": "已撤回"}
    for row in rows:
        files = db.scalars(select(ProjectFile).join(SubmissionFile, SubmissionFile.file_id == ProjectFile.id)
                           .where(SubmissionFile.submission_id == row.id)).all()
        visible = svc.visible_project_files(db, user, roles, project, list(files))
        result.append({"id": row.id, "note": row.note, "status": row.status,
                       "status_label": labels.get(row.status, row.status),
                       "criteria_snapshot": row.criteria_snapshot,
                       "review_comment": row.review_comment,
                       "submitter": names.get(row.submitter_id, "历史成员"),
                       "reviewer": names.get(row.reviewer_id, "历史成员"),
                       "submitted_at": svc.fmt_dt(row.submitted_at), "reviewed_at": svc.fmt_dt(row.reviewed_at),
                       "files": [{"id": f.id, "name": f.original_name, "url": f"/projects/{project.id}/files/{f.id}"} for f in visible],
                       "hidden_file_count": len(files) - len(visible)})
    return result


def project_context(db, user, roles, project_id):
    project = _project(db, user, roles, project_id)
    stages = db.scalars(select(ProjectStage).where(ProjectStage.project_id == project.id)
                        .order_by(ProjectStage.position, ProjectStage.id)).all()
    stage_order = {s.id: i for i, s in enumerate(stages)}
    tasks = db.scalars(select(ProjectTask).where(ProjectTask.project_id == project.id).order_by(ProjectTask.id)).all()
    tasks.sort(key=lambda t: (stage_order.get(t.stage_id, 0), t.id))
    milestones = db.scalars(select(ProjectMilestone).where(ProjectMilestone.project_id == project.id).order_by(ProjectMilestone.id)).all()
    users = db.scalars(select(User)).all()
    names = {u.id: u.real_name for u in users}
    audience_ids = participant_ids(db, project)
    participants = [{"id": u.id, "name": u.real_name} for u in users
                    if u.status == "active" and (not u.is_admin or u.id == project.leader_id) and u.id in audience_ids]
    honor_ids = set(db.scalars(select(UserRole.user_id).where(UserRole.role == "荣誉社长")).all())
    reviewer_ids = {p["id"] for p in participants} | honor_ids
    reviewers = [{"id": u.id, "name": u.real_name} for u in users if u.status == "active" and u.id in reviewer_ids]
    manage = can_manage_execution(db, user, project) and project.status in {"draft", "rejected", "approved"}
    running = bool(project.execution_enabled and project.status == "approved")
    today = utcnow().astimezone(svc.SHANGHAI).date()
    task_map = {t.id: t for t in tasks}
    submissions = db.scalars(select(ProjectSubmission).where(ProjectSubmission.project_id == project.id)
                             .order_by(ProjectSubmission.id.desc())).all()
    task_info, milestone_info = {}, {}
    for task in tasks:
        collaborators = list(db.scalars(select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == task.id)).all())
        predecessor_ids = list(db.scalars(select(TaskDependency.predecessor_id).where(TaskDependency.task_id == task.id)).all())
        predecessors = [task_map[i] for i in predecessor_ids if i in task_map]
        warnings = []
        if any(p.status not in {"done", "cancelled"} for p in predecessors):
            warnings.append("前置任务尚未完成")
        if task.planned_start and any(p.planned_end and p.planned_end >= task.planned_start for p in predecessors):
            warnings.append("计划与前置任务日期存在衔接冲突")
        can_work = running and task.owner_id == user.id and task.status in {"todo", "doing"}
        task_info[task.id] = {
            "owner": names.get(task.owner_id, "历史成员"), "reviewer": names.get(task.reviewer_id, "无需验收"),
            "collaborator_ids": collaborators, "predecessor_ids": predecessor_ids,
            "collaborators": [names.get(i, "历史成员") for i in collaborators],
            "predecessors": [p.title for p in predecessors],
            "overdue": bool(task.planned_end and task.planned_end < today and task.status not in {"done", "cancelled"}),
            "duration": (task.planned_end-task.planned_start).days + 1 if task.planned_start and task.planned_end else None,
            "warnings": warnings, "can_update": can_work,
            "can_edit": manage and task.status in {"todo", "doing"},
            "can_submit": can_work and task.is_key,
            "can_review": running and task.status == "review" and task.reviewer_id == user.id and task.owner_id != user.id,
            "can_withdraw": running and task.status == "review" and (task.owner_id == user.id or manage),
            "can_cancel": running and manage and task.status in {"todo", "doing"},
            "submissions": _submission_context(db, project, user, roles, [s for s in submissions if s.task_id == task.id], names),
        }
    for item in milestones:
        tids = list(db.scalars(select(MilestoneTask.task_id).where(MilestoneTask.milestone_id == item.id)).all())
        linked = [task_map[i] for i in tids if i in task_map]
        effective_linked = [t for t in linked if t.status != "cancelled"]
        ready = bool(effective_linked) and all(t.status == "done" for t in effective_linked)
        milestone_info[item.id] = {
            "owner": names.get(item.owner_id, "历史成员"), "reviewer": names.get(item.reviewer_id, "历史成员"),
            "task_ids": tids, "task_titles": [t.title for t in linked], "ready": ready,
            "overdue": bool(item.planned_date and item.planned_date < today and item.status != "done"),
            "can_edit": manage and item.status == "todo",
            "can_submit": running and item.owner_id == user.id and item.status == "todo" and ready,
            "can_review": running and item.reviewer_id == user.id and item.owner_id != user.id and item.status == "review",
            "can_withdraw": running and item.status == "review" and (item.owner_id == user.id or manage),
            "submissions": _submission_context(db, project, user, roles, [s for s in submissions if s.milestone_id == item.id], names),
        }
    effective = [t for t in tasks if t.status != "cancelled"]
    keys = [t for t in effective if t.is_key]
    stats = {"progress": round(sum(t.progress for t in effective)/len(effective), 1) if effective else None,
             "task_total": len(effective), "task_done": sum(t.status == "done" for t in effective),
             "key_total": len(keys), "key_done": sum(t.status == "done" for t in keys),
             "milestone_total": len(milestones), "milestone_done": sum(m.status == "done" for m in milestones),
             "overdue": sum(i["overdue"] for i in task_info.values())}
    events = db.scalars(select(ProjectEvent).where(ProjectEvent.project_id == project.id)
                        .order_by(ProjectEvent.id.desc()).limit(100)).all()
    files = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project.id).order_by(ProjectFile.uploaded_at.desc())).all()
    visible_files = svc.visible_project_files(db, user, roles, project, list(files))
    return {
        "project": project, "stages": stages, "tasks": tasks, "milestones": milestones,
        "participants": participants, "reviewers": reviewers,
        "manager_name": names.get(project.manager_id, "待指定"),
        "can_assign_manager": project.status != "archived" and (project.leader_id == user.id or "荣誉社长" in roles),
        "can_manage": manage, "can_enable": manage and project.status == "approved" and not project.execution_enabled,
        "can_execute": running, "task_info": task_info, "milestone_info": milestone_info,
        "files": [{"id": f.id, "name": f.original_name} for f in visible_files], "stats": stats,
        "events": [{"id": e.id, "actor": names.get(e.actor_id, "历史成员"), "message": e.message, "created_at": svc.fmt_dt(e.created_at)} for e in events],
        "task_status": TASK_STATUS, "milestone_status": MILESTONE_STATUS, "priorities": PRIORITIES,
    }


def personal_work(db, user, roles):
    """No preview from projects that this user can no longer access."""
    result = {"tasks": [], "reviews": [], "milestones": []}
    if user.status != "active":
        return result
    today = utcnow().astimezone(svc.SHANGHAI).date()
    projects = db.scalars(select(Project).where(Project.status == "approved", Project.execution_enabled.is_(True))).all()
    collab_ids = set(db.scalars(select(TaskCollaborator.task_id).where(TaskCollaborator.user_id == user.id)).all())
    for project in projects:
        if not svc.can_view_project(db, user, roles, project):
            continue
        tasks = db.scalars(select(ProjectTask).where(ProjectTask.project_id == project.id, ProjectTask.status.notin_(["done", "cancelled"]))).all()
        for task in tasks:
            row = {"id": task.id, "title": task.title, "project_id": project.id, "project_title": project.title,
                   "status": task.status, "status_label": TASK_STATUS[task.status], "progress": task.progress,
                   "planned_end": task.planned_end, "overdue": bool(task.planned_end and task.planned_end < today),
                   "href": f"/projects/{project.id}/execution/tasks/{task.id}", "kind": "task"}
            if task.owner_id == user.id or task.id in collab_ids:
                result["tasks"].append(row)
            if task.status == "review" and task.reviewer_id == user.id and task.owner_id != user.id:
                result["reviews"].append(row)
        milestones = db.scalars(select(ProjectMilestone).where(ProjectMilestone.project_id == project.id, ProjectMilestone.status != "done")).all()
        for item in milestones:
            row = {"id": item.id, "title": item.title, "project_id": project.id, "project_title": project.title,
                   "status": item.status, "status_label": MILESTONE_STATUS[item.status],
                   "planned_end": item.planned_date, "overdue": bool(item.planned_date and item.planned_date < today),
                   "href": f"/projects/{project.id}/execution/milestones/{item.id}", "kind": "milestone"}
            if item.owner_id == user.id:
                result["milestones"].append(row)
            if item.status == "review" and item.reviewer_id == user.id and item.owner_id != user.id:
                result["reviews"].append(row)
    return result
