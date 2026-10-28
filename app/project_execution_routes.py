"""Project execution pages share the existing session, CSRF and shell helpers."""
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import AppError
from app import services as svc
from app import project_execution as execution


TASK_STATUS = {"todo": "未开始", "doing": "进行中", "review": "待验收", "done": "已完成", "cancelled": "已取消"}
MILESTONE_STATUS = {"todo": "未开始", "review": "待验收", "done": "已通过", "approved": "已通过", "rejected": "待修改"}
PRIORITIES = {"high": "高", "medium": "中", "low": "低"}


def register_execution_routes(app, settings, guard, shell, render, redirect, flash, csrf_ok, fail):
    def base_url(project_id):
        return f"/projects/{project_id}/execution"

    def page(request, db, user, name, context, **extra):
        response = render(request, name, shell(request, db, user, "projects", execution=context,
            task_status=TASK_STATUS, milestone_status=MILESTONE_STATUS, priorities=PRIORITIES,
            base=base_url(context['project'].id), **extra))
        response.headers["Cache-Control"] = "private, no-store"
        return response

    def context_for(request, db, project_id):
        user, bounce = guard(request, db)
        if bounce:
            return None, None, bounce
        try:
            context = execution.project_context(db, user, svc.roles_of(db, user.id), project_id)
            context["people"] = context["participants"]
            context["names"] = {p["id"]: p["name"] for p in context["participants"] + context["reviewers"]}
            context["stage_names"] = {stage.id: stage.title for stage in context["stages"]}
            context["task_meta"] = context["task_info"]
            context["milestone_meta"] = context["milestone_info"]
            for task in context["tasks"]:
                info = context["task_info"][task.id]
                context["names"].setdefault(task.owner_id, info["owner"])
                context["names"].setdefault(task.reviewer_id, info["reviewer"])
                info["actual_start"] = svc.fmt_dt(task.actual_start)
                info["actual_end"] = svc.fmt_dt(task.actual_end)
            for milestone in context["milestones"]:
                info = context["milestone_info"][milestone.id]
                context["names"].setdefault(milestone.owner_id, info["owner"])
                context["names"].setdefault(milestone.reviewer_id, info["reviewer"])
            return user, context, None
        except AppError as exc:
            return user, None, fail(request, "/projects", exc)

    def find_row(context, key, row_id):
        for row in context[key]:
            if row.id == row_id:
                return row
        raise AppError("找不到这个任务或里程碑，或你没有查看权限")

    async def mutate(request, db, project_id, action, message, back=None):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        back = back or base_url(project_id)
        form = await request.form()
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect(back)
        data = dict(form)
        for key in ("collaborator_ids", "predecessor_ids", "file_ids", "task_ids"):
            data[key] = list(form.getlist(key))
        try:
            action(user, svc.roles_of(db, user.id), data)
        except AppError as exc:
            db.rollback()
            return fail(request, back, exc)
        except (ValueError, TypeError):
            db.rollback()
            return fail(request, back, AppError("表单内容不正确，请检查人员、日期及版本后重试"))
        flash(request, message)
        return redirect(back)

    @app.get("/projects/{project_id}/execution")
    def workspace(project_id: int, request: Request, db: Session = Depends(get_db)):
        user, context, bounce = context_for(request, db, project_id)
        if bounce:
            return bounce
        filters = {key: request.query_params.get(key, "") for key in ("stage", "owner", "status")}
        tasks = context["tasks"]
        if filters["stage"]:
            tasks = [row for row in tasks if str(row.stage_id or "") == filters["stage"]]
        if filters["owner"]:
            tasks = [row for row in tasks if str(row.owner_id) == filters["owner"]]
        if filters["status"]:
            tasks = [row for row in tasks if row.status == filters["status"]]
        return page(request, db, user, "project_execution.html", context, filtered_tasks=tasks, filters=filters)

    @app.post("/projects/{project_id}/execution/manager")
    async def manager(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.set_manager(
            db, user, roles, project_id, int(data.get("manager_id") or 0), str(data.get("reason") or ""), data.get("version")), "执行负责人已更新")

    @app.post("/projects/{project_id}/execution/enable")
    async def enable(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.enable_execution(
            db, user, roles, project_id, version=data.get("version")), "执行管理已启用，后续按任务及里程碑验收规则结项")

    @app.post("/projects/{project_id}/execution/stages")
    async def stage_create(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.save_stage(db, user, roles, project_id, data), "阶段已添加")

    @app.post("/projects/{project_id}/execution/stages/{stage_id}/edit")
    async def stage_edit(project_id: int, stage_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.save_stage(db, user, roles, project_id, data, stage_id=stage_id), "阶段已保存")

    @app.get("/projects/{project_id}/execution/tasks/new")
    def task_new(project_id: int, request: Request, db: Session = Depends(get_db)):
        user, context, bounce = context_for(request, db, project_id)
        if bounce:
            return bounce
        if not context["can_manage"]:
            return fail(request, base_url(project_id), AppError("只有执行负责人可以编制任务计划"))
        if not context["stages"]:
            return fail(request, base_url(project_id) + "#execution-stages", AppError("请先添加项目阶段，再建立任务计划"))
        return page(request, db, user, "project_execution_task_form.html", context, task=None)

    @app.get("/projects/{project_id}/execution/tasks/{task_id}/edit")
    def task_edit_page(project_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
        user, context, bounce = context_for(request, db, project_id)
        if bounce:
            return bounce
        if not context["can_manage"]:
            return fail(request, base_url(project_id), AppError("只有执行负责人可以编制任务计划"))
        try:
            task = find_row(context, "tasks", task_id)
        except AppError as exc:
            return fail(request, base_url(project_id), exc)
        return page(request, db, user, "project_execution_task_form.html", context, task=task)

    @app.get("/projects/{project_id}/execution/tasks/{task_id}")
    def task_detail(project_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
        user, context, bounce = context_for(request, db, project_id)
        if bounce:
            return bounce
        try:
            task = find_row(context, "tasks", task_id)
        except AppError as exc:
            return fail(request, base_url(project_id), exc)
        return page(request, db, user, "project_execution_task.html", context, task=task)

    @app.post("/projects/{project_id}/execution/tasks")
    async def task_create(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.save_task(db, user, roles, project_id, data), "任务已添加")

    @app.post("/projects/{project_id}/execution/tasks/{task_id}/edit")
    async def task_update(project_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.save_task(db, user, roles, project_id, data, task_id=task_id), "任务计划已保存", f"{base_url(project_id)}/tasks/{task_id}")

    @app.post("/projects/{project_id}/execution/tasks/{task_id}/progress")
    async def task_progress(project_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.update_task_progress(db, user, roles, project_id, task_id, data), "任务进展已记录", f"{base_url(project_id)}/tasks/{task_id}")

    @app.post("/projects/{project_id}/execution/tasks/{task_id}/submit")
    async def task_submit(project_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.submit_task(db, user, roles, project_id, task_id, data), "成果已提交验收", f"{base_url(project_id)}/tasks/{task_id}")

    @app.post("/projects/{project_id}/execution/tasks/{task_id}/review")
    async def task_review(project_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.review_task(db, user, roles, project_id, task_id, data), "任务验收结果已记录", f"{base_url(project_id)}/tasks/{task_id}")

    @app.post("/projects/{project_id}/execution/tasks/{task_id}/withdraw")
    async def task_withdraw(project_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.withdraw_submission(db, user, roles, project_id, task_id, data), "已撤回验收，可调整后重新提交", f"{base_url(project_id)}/tasks/{task_id}")

    @app.get("/projects/{project_id}/execution/milestones/new")
    def milestone_new(project_id: int, request: Request, db: Session = Depends(get_db)):
        user, context, bounce = context_for(request, db, project_id)
        if bounce:
            return bounce
        if not context["can_manage"]:
            return fail(request, base_url(project_id), AppError("只有执行负责人可以编制里程碑计划"))
        return page(request, db, user, "project_execution_milestone_form.html", context, milestone=None)

    @app.get("/projects/{project_id}/execution/milestones/{milestone_id}/edit")
    def milestone_edit_page(project_id: int, milestone_id: int, request: Request, db: Session = Depends(get_db)):
        user, context, bounce = context_for(request, db, project_id)
        if bounce:
            return bounce
        if not context["can_manage"]:
            return fail(request, base_url(project_id), AppError("只有执行负责人可以编制里程碑计划"))
        try:
            milestone = find_row(context, "milestones", milestone_id)
        except AppError as exc:
            return fail(request, base_url(project_id), exc)
        return page(request, db, user, "project_execution_milestone_form.html", context, milestone=milestone)

    @app.get("/projects/{project_id}/execution/milestones/{milestone_id}")
    def milestone_detail(project_id: int, milestone_id: int, request: Request, db: Session = Depends(get_db)):
        user, context, bounce = context_for(request, db, project_id)
        if bounce:
            return bounce
        try:
            milestone = find_row(context, "milestones", milestone_id)
        except AppError as exc:
            return fail(request, base_url(project_id), exc)
        return page(request, db, user, "project_execution_milestone.html", context, milestone=milestone)

    @app.post("/projects/{project_id}/execution/milestones")
    async def milestone_create(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.save_milestone(db, user, roles, project_id, data), "里程碑已添加")

    @app.post("/projects/{project_id}/execution/milestones/{milestone_id}/edit")
    async def milestone_update(project_id: int, milestone_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.save_milestone(db, user, roles, project_id, data, milestone_id=milestone_id), "里程碑已保存", f"{base_url(project_id)}/milestones/{milestone_id}")

    @app.post("/projects/{project_id}/execution/milestones/{milestone_id}/submit")
    async def milestone_submit(project_id: int, milestone_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.submit_milestone(db, user, roles, project_id, milestone_id, data), "里程碑已提交验收", f"{base_url(project_id)}/milestones/{milestone_id}")

    @app.post("/projects/{project_id}/execution/milestones/{milestone_id}/review")
    async def milestone_review(project_id: int, milestone_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.review_milestone(db, user, roles, project_id, milestone_id, data), "里程碑验收结果已记录", f"{base_url(project_id)}/milestones/{milestone_id}")

    @app.post("/projects/{project_id}/execution/milestones/{milestone_id}/withdraw")
    async def milestone_withdraw(project_id: int, milestone_id: int, request: Request, db: Session = Depends(get_db)):
        return await mutate(request, db, project_id, lambda user, roles, data: execution.withdraw_submission(db, user, roles, project_id, milestone_id, data, milestone=True), "已撤回里程碑验收", f"{base_url(project_id)}/milestones/{milestone_id}")

    @app.get("/project-todos")
    def todos(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        data = execution.personal_work(db, user, svc.roles_of(db, user.id))
        response = render(request, "project_execution_todos.html", shell(request, db, user, "projects", todo_data=data, task_status=TASK_STATUS))
        response.headers["Cache-Control"] = "private, no-store"
        return response
