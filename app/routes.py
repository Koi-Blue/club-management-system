import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.models import (
    Activity,
    Announcement,
    Asset,
    Borrow,
    CheckIn,
    DutyShift,
    LeaveRequest,
    LedgerEntry,
    LibraryFile,
    Meeting,
    Project,
    ProjectFile,
    Reimbursement,
    Signup,
    User,
    utcnow,
)
from app.org import (
    CLUB_ROLES,
    DEPARTMENTS,
    DEPT_MEMBERS,
    MINISTERS,
    TECH_LEADS,
    can_announce,
    can_approve_borrow,
    can_approve_project,
    can_create_activity,
    can_edit_duty,
    can_issue_activity,
    sees_finance,
)
from app.security import verify_password
from app.services import AppError
import app.services as svc

PROJECT_STATUS = {"draft": "草稿", "pending": "待审批", "approved": "进行中", "rejected": "已驳回", "archived": "已归档"}
ACTIVITY_STATUS = {"draft": "待发放", "published": "已发放", "rejected": "已驳回", "closed": "已结束"}
BORROW_STATUS = {"pending": "待审批", "approved": "借用中", "rejected": "已驳回", "returned": "已归还"}
USER_STATUS = {"pending": "待审批", "active": "已启用", "rejected": "已驳回", "disabled": "已停用", "deleted": "已注销"}
LEAVE_STATUS = {"pending": "待审批", "approved": "已通过", "rejected": "已驳回"}
CLAIM_STATUS = {"pending": "待审批", "approved": "待付款", "rejected": "已驳回", "paid": "已付款"}


def register_routes(app: FastAPI, settings: Settings) -> None:
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    templates.env.globals.update(
        project_status=PROJECT_STATUS,
        activity_status=ACTIVITY_STATUS,
        borrow_status=BORROW_STATUS,
        user_status=USER_STATUS,
        leave_status=LEAVE_STATUS,
        claim_status=CLAIM_STATUS,
        departments=DEPARTMENTS,
    )

    def render(request: Request, name: str, context: dict, status_code: int = 200):
        context["request"] = request
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def redirect(url: str):
        return RedirectResponse(url, status_code=303)

    def flash(request: Request, message: str, level: str = "ok") -> None:
        request.session["flash"] = {"message": message, "level": level}

    def ensure_csrf(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_hex(16)
            request.session["csrf"] = token
        return token

    def csrf_ok(request: Request, token: str | None) -> bool:
        expected = request.session.get("csrf") or ""
        given = token or ""
        return bool(expected and given and secrets.compare_digest(expected, given))

    def active_user(request: Request, db: Session) -> User | None:
        uid = request.session.get("uid")
        if not uid:
            return None
        user = db.get(User, uid)
        if user is None or user.status != "active":
            request.session.clear()
            return None
        return user

    def guard(request: Request, db: Session):
        user = active_user(request, db)
        if user is None:
            return None, redirect("/login")
        return user, None

    def fail(request: Request, url: str, exc: AppError):
        flash(request, exc.message, "error")
        return redirect(url)

    def shell(request: Request, db: Session, user: User, nav: str, **extra) -> dict:
        roles = svc.roles_of(db, user.id)
        finance = sees_finance(user.is_admin, roles)
        labels = ["超级管理员"] if user.is_admin else []
        labels.extend(roles)
        pending_users = int(db.scalar(select(func.count()).select_from(User).where(User.status == "pending")) or 0) if user.is_admin else 0
        me = {
            "id": user.id,
            "username": user.username,
            "real_name": user.real_name,
            "is_admin": user.is_admin,
            "roles": roles,
            "role_text": "、".join(labels) if labels else "未分配职务",
            "department": user.department,
            "warn_password": user.is_admin and verify_password(settings.admin_password, user.password_hash),
            "finance": finance,
            "can_announce": can_announce(user.is_admin, roles),
            "can_create_activity": can_create_activity(user.is_admin, roles),
            "can_issue": can_issue_activity(user.is_admin, roles),
            "can_approve_project": can_approve_project(user.is_admin, roles),
            "can_approve_borrow": can_approve_borrow(user.is_admin, roles),
            "can_edit_duty": can_edit_duty(user.is_admin, roles),
            "can_manage_asset": user.is_admin or "社长" in roles or "副社长" in roles or bool(svc.managed_departments(roles)),
            "library_departments": svc.visible_library_departments(user, roles),
            "upload_departments": [dept for dept in DEPARTMENTS if svc.can_upload_library(user, roles, dept)],
        }
        context = {
            "csrf": ensure_csrf(request),
            "flash": request.session.pop("flash", None),
            "club_name": svc.club_name(db),
            "me": me,
            "nav": nav,
            "badges": {"members": pending_users, "approvals": approval_count(db, user, roles)},
            "club_roles": CLUB_ROLES,
            "role_groups": [
                ("社团职务", ["指导老师", "社长", "副社长", "荣誉社长"]),
                ("部长", MINISTERS),
                ("技术负责人", TECH_LEADS),
                ("部门成员", DEPT_MEMBERS),
            ],
        }
        context.update(extra)
        return context

    def approval_count(db: Session, user: User, roles: list[str]) -> int:
        total = 0
        if user.is_admin:
            total += int(db.scalar(select(func.count()).select_from(User).where(User.status == "pending")) or 0)
        if can_approve_project(user.is_admin, roles):
            total += int(db.scalar(select(func.count()).select_from(Project).where(Project.status == "pending")) or 0)
        if can_issue_activity(user.is_admin, roles):
            total += int(db.scalar(select(func.count()).select_from(Activity).where(Activity.status == "draft")) or 0)
        if can_approve_borrow(user.is_admin, roles):
            total += int(db.scalar(select(func.count()).select_from(Borrow).where(Borrow.status == "pending")) or 0)
        if sees_finance(user.is_admin, roles):
            total += int(db.scalar(select(func.count()).select_from(Reimbursement).where(Reimbursement.status == "pending")) or 0)
        if user.is_admin or "社长" in roles or "副社长" in roles or "指导老师" in roles or svc.managed_departments(roles):
            total += int(db.scalar(select(func.count()).select_from(LeaveRequest).where(LeaveRequest.status == "pending")) or 0)
        return total

    async def form_of(request: Request):
        return await request.form()

    def auth_page(request: Request, db: Session, name: str):
        if active_user(request, db):
            return redirect("/")
        return render(request, name, {"csrf": ensure_csrf(request), "flash": request.session.pop("flash", None), "club_name": svc.club_name(db)})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login")
    def login_page(request: Request, db: Session = Depends(get_db)):
        return auth_page(request, db, "login.html")

    @app.post("/login")
    async def login_post(request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/login")
        try:
            user = svc.authenticate(db, str(form.get("account") or ""), str(form.get("password") or ""))
        except AppError as exc:
            return fail(request, "/login", exc)
        request.session.clear()
        request.session["uid"] = user.id
        request.session["csrf"] = secrets.token_hex(16)
        return redirect("/")

    @app.get("/register")
    def register_page(request: Request, db: Session = Depends(get_db)):
        return auth_page(request, db, "register.html")

    @app.post("/register")
    async def register_post(request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/register")
        try:
            svc.register_user(db, str(form.get("username") or ""), str(form.get("password") or ""), str(form.get("real_name") or ""), str(form.get("phone") or ""), str(form.get("college") or ""), str(form.get("class_name") or ""), str(form.get("department") or ""))
        except AppError as exc:
            return fail(request, "/register", exc)
        flash(request, "注册已提交，请等待超级管理员审批后再登录")
        return redirect("/login")

    @app.post("/logout")
    async def logout(request: Request):
        form = await form_of(request)
        if csrf_ok(request, str(form.get("csrf") or "")):
            request.session.clear()
        return redirect("/login")

    @app.get("/")
    def dashboard(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        news = db.scalars(select(Announcement).order_by(Announcement.created_at.desc()).limit(4)).all()
        names = svc.name_map(db, {row.author_id for row in news})
        activities = [row for row in db.scalars(select(Activity).where(Activity.status == "published", Activity.end_at >= utcnow()).order_by(Activity.start_at).limit(8)).all()]
        duties = db.scalars(select(DutyShift).order_by(DutyShift.duty_on).limit(6)).all()
        duty_names = svc.name_map(db, {row.user_id for row in duties})
        todos = []
        if user.is_admin and db.scalar(select(func.count()).select_from(User).where(User.status == "pending")):
            todos.append({"href": "/members", "text": "有注册账号待审批"})
        if can_approve_project(user.is_admin, roles) and db.scalar(select(func.count()).select_from(Project).where(Project.status == "pending")):
            todos.append({"href": "/approvals", "text": "有立项待审批"})
        if can_issue_activity(user.is_admin, roles) and db.scalar(select(func.count()).select_from(Activity).where(Activity.status == "draft")):
            todos.append({"href": "/approvals", "text": "有活动待发放"})
        if sees_finance(user.is_admin, roles) and db.scalar(select(func.count()).select_from(Reimbursement).where(Reimbursement.status == "pending")):
            todos.append({"href": "/finance", "text": "有报销待审批"})
        return render(request, "dashboard.html", shell(
            request, db, user, "home",
            todos=todos,
            news=[{"title": row.title, "content": row.content, "author": names.get(row.author_id, ""), "created_at": svc.fmt_dt(row.created_at)} for row in news],
            upcoming=[{"id": row.id, "title": row.title, "start_at": svc.fmt_dt(row.start_at), "location": row.location} for row in activities],
            duties=[{"duty_on": row.duty_on, "name": duty_names.get(row.user_id, ""), "note": row.note} for row in duties],
            my_claims=[{"amount": svc.money(row.amount_cents), "reason": row.reason, "status": row.status} for row in db.scalars(select(Reimbursement).where(Reimbursement.user_id == user.id).order_by(Reimbursement.created_at.desc()).limit(3)).all()],
        ))

    @app.get("/org")
    def org_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        return render(request, "org.html", shell(request, db, user, "org", tree=svc.build_org_tree(db)))

    @app.get("/members")
    def members_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not user.is_admin:
            return redirect("/org")
        rows = db.scalars(select(User).where(User.is_admin.is_(False)).order_by(User.created_at.desc())).all()
        people = [{
            "id": row.id, "username": row.username, "real_name": row.real_name, "phone": row.phone,
            "college": row.college, "class_name": row.class_name, "department": row.department,
            "status": row.status, "roles": svc.roles_of(db, row.id), "created_at": svc.fmt_dt(row.created_at),
        } for row in rows]
        return render(request, "members.html", shell(
            request, db, user, "members",
            pending=[row for row in people if row["status"] == "pending"],
            active=[row for row in people if row["status"] == "active"],
            inactive=[row for row in people if row["status"] in {"rejected", "disabled", "deleted"}],
        ))

    async def member_post(user_id: int, request: Request, db: Session, action, message: str):
        form = await form_of(request)
        actor, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/members")
        try:
            action(actor, user_id, form)
        except AppError as exc:
            return fail(request, "/members", exc)
        flash(request, message)
        return redirect("/members")

    @app.post("/members/{user_id}/approve")
    async def members_approve(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_post(user_id, request, db, lambda actor, uid, _form: svc.approve_user(db, actor, uid), "已通过注册，默认职务为所属部门成员")

    @app.post("/members/{user_id}/reject")
    async def members_reject(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_post(user_id, request, db, lambda actor, uid, _form: svc.reject_user(db, actor, uid), "已驳回注册")

    @app.post("/members/{user_id}/disable")
    async def members_disable(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_post(user_id, request, db, lambda actor, uid, _form: svc.set_user_status(db, actor, uid, "disabled"), "已停用账号")

    @app.post("/members/{user_id}/enable")
    async def members_enable(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_post(user_id, request, db, lambda actor, uid, _form: svc.set_user_status(db, actor, uid, "active"), "已启用账号")

    @app.post("/members/{user_id}/delete")
    async def members_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_post(user_id, request, db, lambda actor, uid, _form: svc.delete_user(db, actor, uid), "成员已删除，历史档案仍会保留")

    @app.post("/members/{user_id}/roles")
    async def members_roles(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_post(user_id, request, db, lambda actor, uid, form: svc.set_roles(db, actor, uid, [str(item) for item in form.getlist("roles")]), "职务已保存")

    @app.get("/projects")
    def projects_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        rows = [row for row in db.scalars(select(Project).order_by(Project.updated_at.desc())).all() if svc.can_view_project(user, roles, row)]
        names = svc.name_map(db, {row.leader_id for row in rows})
        projects = [{"id": row.id, "title": row.title, "department": row.department, "status": row.status, "leader": names.get(row.leader_id, "已注销"), "updated_at": svc.fmt_dt(row.updated_at)} for row in rows]
        return render(request, "projects.html", shell(request, db, user, "projects", projects=projects))

    @app.get("/projects/new")
    def project_new(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        choices = svc.visible_library_departments(user, roles) or ([user.department] if user.department in DEPARTMENTS else DEPARTMENTS)
        return render(request, "project_new.html", shell(request, db, user, "projects", choices=choices))

    @app.post("/projects")
    async def project_create(request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/projects/new")
        try:
            project = svc.create_project(db, user, svc.roles_of(db, user.id), str(form.get("title") or ""), str(form.get("summary") or ""), str(form.get("department") or ""))
        except AppError as exc:
            return fail(request, "/projects/new", exc)
        flash(request, "立项草稿已保存，确认后可以提交审批")
        return redirect(f"/projects/{project.id}")

    @app.get("/projects/{project_id}")
    def project_detail(project_id: int, request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        project = db.get(Project, project_id)
        roles = svc.roles_of(db, user.id)
        if project is None or not svc.can_view_project(user, roles, project):
            flash(request, "找不到这个立项，或你没有查看权限", "error")
            return redirect("/projects")
        files = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project.id).order_by(ProjectFile.uploaded_at.desc())).all()
        names = svc.name_map(db, {project.leader_id, project.reviewer_id or 0, *(row.uploader_id for row in files)})
        detail = {
            "id": project.id, "title": project.title, "summary": project.summary, "department": project.department,
            "status": project.status, "leader": names.get(project.leader_id, "已注销"), "reviewer": names.get(project.reviewer_id or 0, ""),
            "review_comment": project.review_comment, "updated_at": svc.fmt_dt(project.updated_at),
            "can_submit": svc.can_manage_project(user, roles, project),
            "can_review": can_approve_project(user.is_admin, roles) and project.status == "pending",
            "can_archive": project.status == "approved" and (project.leader_id == user.id or can_approve_project(user.is_admin, roles) or project.department in svc.managed_departments(roles)),
            "can_upload": project.leader_id == user.id or user.is_admin or project.department in svc.managed_departments(roles) or can_approve_project(user.is_admin, roles),
            "files": [{"id": row.id, "name": row.original_name, "uploader": names.get(row.uploader_id, "已注销"), "uploaded_at": svc.fmt_dt(row.uploaded_at), "size": row.size, "can_delete": row.uploader_id == user.id or user.is_admin or project.department in svc.managed_departments(roles)} for row in files],
        }
        return render(request, "project_detail.html", shell(request, db, user, "projects", project=detail))

    async def project_action(project_id: int, request: Request, db: Session, action, message: str):
        form = await form_of(request)
        user, bounce = guard(request, db)
        back = f"/projects/{project_id}"
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect(back)
        try:
            action(user, svc.roles_of(db, user.id), form)
        except AppError as exc:
            return fail(request, back, exc)
        flash(request, message)
        return redirect(back)

    @app.post("/projects/{project_id}/submit")
    async def project_submit(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await project_action(project_id, request, db, lambda actor, roles, _form: svc.submit_project(db, actor, roles, project_id), "已提交审批")

    @app.post("/projects/{project_id}/review")
    async def project_review(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await project_action(project_id, request, db, lambda actor, roles, form: svc.review_project(db, actor, roles, project_id, str(form.get("decision") or ""), str(form.get("comment") or "")), "审批已保存")

    @app.post("/projects/{project_id}/archive")
    async def project_archive(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await project_action(project_id, request, db, lambda actor, roles, _form: svc.archive_project(db, actor, roles, project_id), "立项已归档")

    @app.post("/projects/{project_id}/files")
    async def project_upload(project_id: int, request: Request, db: Session = Depends(get_db)):
        def action(actor, roles, form):
            upload = form.get("file")
            if upload is None or not getattr(upload, "filename", ""):
                raise AppError("请选择文件")
            svc.save_project_file(db, actor, roles, settings.upload_dir, project_id, upload.filename, upload.file.read())
        return await project_action(project_id, request, db, action, "资料已上传")

    @app.post("/projects/{project_id}/files/{file_id}/delete")
    async def project_file_delete(project_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
        return await project_action(project_id, request, db, lambda actor, roles, _form: svc.delete_project_file(db, actor, roles, settings.upload_dir, project_id, file_id), "文件已删除")

    @app.get("/projects/{project_id}/files/{file_id}")
    def project_download(project_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        project = db.get(Project, project_id)
        row = db.get(ProjectFile, file_id)
        roles = svc.roles_of(db, user.id)
        if project is None or row is None or row.project_id != project.id or not svc.can_view_project(user, roles, project):
            flash(request, "找不到这个文件", "error")
            return redirect("/projects")
        path = svc.safe_path(settings.upload_dir, ["projects", str(project_id), row.stored_name])
        if not path.exists():
            flash(request, "文件已丢失", "error")
            return redirect(f"/projects/{project_id}")
        return FileResponse(path, filename=row.original_name)

    @app.get("/library")
    def library_page(request: Request, db: Session = Depends(get_db), department: str = ""):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        visible = svc.visible_library_departments(user, roles)
        current = department if department in visible else (visible[0] if visible else "")
        rows = db.scalars(select(LibraryFile).where(LibraryFile.department == current).order_by(LibraryFile.uploaded_at.desc())).all() if current else []
        names = svc.name_map(db, {row.uploader_id for row in rows})
        files = [{"id": row.id, "name": row.original_name, "size": row.size, "uploader": names.get(row.uploader_id, "已注销"), "uploaded_at": svc.fmt_dt(row.uploaded_at), "can_delete": row.uploader_id == user.id or svc.can_upload_library(user, roles, current)} for row in rows]
        return render(request, "library.html", shell(request, db, user, "library", current=current, files=files, can_upload=svc.can_upload_library(user, roles, current) if current else False))

    @app.post("/library")
    async def library_upload(request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        user, bounce = guard(request, db)
        department = str(form.get("department") or "")
        back = f"/library?department={department}"
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect(back)
        upload = form.get("file")
        try:
            if upload is None or not getattr(upload, "filename", ""):
                raise AppError("请选择文件")
            svc.save_library_file(db, user, svc.roles_of(db, user.id), settings.upload_dir, department, upload.filename, upload.file.read())
        except AppError as exc:
            return fail(request, back, exc)
        flash(request, "资料已放入部门库")
        return redirect(back)

    @app.get("/library/{file_id}")
    def library_download(file_id: int, request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        row = db.get(LibraryFile, file_id)
        roles = svc.roles_of(db, user.id)
        if row is None or not svc.can_view_library(user, roles, row.department):
            flash(request, "找不到这个文件", "error")
            return redirect("/library")
        path = svc.safe_path(settings.upload_dir, ["library", row.department, row.stored_name])
        if not path.exists():
            flash(request, "文件已丢失", "error")
            return redirect(f"/library?department={row.department}")
        return FileResponse(path, filename=row.original_name)

    @app.post("/library/{file_id}/delete")
    async def library_delete(file_id: int, request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        user, bounce = guard(request, db)
        row = db.get(LibraryFile, file_id) if user else None
        back = f"/library?department={row.department}" if row else "/library"
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect(back)
        try:
            svc.delete_library_file(db, user, svc.roles_of(db, user.id), settings.upload_dir, file_id)
        except AppError as exc:
            return fail(request, back, exc)
        flash(request, "资料已删除")
        return redirect(back)

    @app.get("/meetings")
    def meetings_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        rows = [row for row in db.scalars(select(Meeting).order_by(Meeting.created_at.desc())).all() if svc.can_view_meeting(user, roles, row)]
        names = svc.name_map(db, {row.author_id for row in rows})
        items = [{"id": row.id, "title": row.title, "department": row.department or "全社", "held_at": row.held_at, "location": row.location, "content": row.content, "author": names.get(row.author_id, "已注销"), "can_delete": row.author_id == user.id or user.is_admin or "社长" in roles} for row in rows]
        return render(request, "meetings.html", shell(request, db, user, "meetings", items=items, can_create=user.is_admin or svc.sees_club_operations(user.is_admin, roles) or bool(svc.managed_departments(roles))))

    @app.post("/meetings")
    async def meeting_create(request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/meetings")
        try:
            svc.create_meeting(db, user, svc.roles_of(db, user.id), str(form.get("title") or ""), str(form.get("department") or ""), str(form.get("held_at") or ""), str(form.get("location") or ""), str(form.get("content") or ""))
        except AppError as exc:
            return fail(request, "/meetings", exc)
        flash(request, "会议纪要已保存")
        return redirect("/meetings")

    @app.post("/meetings/{meeting_id}/delete")
    async def meeting_delete(meeting_id: int, request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/meetings")
        try:
            svc.delete_meeting(db, user, svc.roles_of(db, user.id), meeting_id)
        except AppError as exc:
            return fail(request, "/meetings", exc)
        flash(request, "纪要已删除")
        return redirect("/meetings")

    @app.get("/activities")
    def activities_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        rows = [row for row in db.scalars(select(Activity).order_by(Activity.start_at.desc())).all() if svc.can_view_activity(user, roles, row)]
        names = svc.name_map(db, {row.creator_id for row in rows})
        signed = set(db.scalars(select(Signup.activity_id).where(Signup.user_id == user.id)).all())
        items = [{"id": row.id, "title": row.title, "department": row.department or "全社", "location": row.location, "status": row.status, "start_at": svc.fmt_dt(row.start_at), "creator": names.get(row.creator_id, "已注销"), "signed": row.id in signed, "signup_count": svc.signup_count(db, row.id), "capacity": row.capacity} for row in rows]
        return render(request, "activities.html", shell(request, db, user, "activities", activities=items))

    @app.get("/activities/new")
    def activity_new(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        if not can_create_activity(user.is_admin, roles):
            flash(request, "没有权限创建活动", "error")
            return redirect("/activities")
        scopes = ["全社"] + (DEPARTMENTS if can_issue_activity(user.is_admin, roles) or user.is_admin else sorted(svc.managed_departments(roles)))
        return render(request, "activity_new.html", shell(request, db, user, "activities", scopes=scopes))

    @app.post("/activities")
    async def activity_create(request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/activities/new")
        try:
            capacity = str(form.get("capacity") or "0").strip() or "0"
            activity = svc.create_activity(db, user, svc.roles_of(db, user.id), str(form.get("title") or ""), str(form.get("description") or ""), str(form.get("department") or ""), str(form.get("location") or ""), str(form.get("start_at") or ""), str(form.get("end_at") or ""), int(capacity))
        except (AppError, ValueError) as exc:
            return fail(request, "/activities/new", exc if isinstance(exc, AppError) else AppError("人数上限不正确"))
        flash(request, "活动草稿已保存，发放后成员才能报名")
        return redirect(f"/activities/{activity.id}")

    @app.get("/activities/{activity_id}")
    def activity_detail(activity_id: int, request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        activity = db.get(Activity, activity_id)
        roles = svc.roles_of(db, user.id)
        if activity is None or not svc.can_view_activity(user, roles, activity):
            flash(request, "活动还未发放，或你没有查看权限", "error")
            return redirect("/activities")
        signups = db.scalars(select(Signup).where(Signup.activity_id == activity.id).order_by(Signup.created_at)).all()
        checkins = {row.user_id: row for row in db.scalars(select(CheckIn).where(CheckIn.activity_id == activity.id)).all()}
        names = svc.name_map(db, {activity.creator_id, activity.issuer_id or 0, *(row.user_id for row in signups)})
        show_code = bool(activity.checkin_code) and svc.can_see_checkin_code(user, roles, activity)
        people = []
        for row in signups:
            checked = checkins.get(row.user_id)
            people.append({"user_id": row.user_id, "name": names.get(row.user_id, "已注销"), "checked": checked is not None, "checked_at": svc.fmt_dt(checked.checked_at) if checked else "", "method": "签到码" if checked and checked.method == "code" else ("补签" if checked else "")})
        detail = {
            "id": activity.id, "title": activity.title, "description": activity.description, "department": activity.department or "全社",
            "location": activity.location, "start_at": svc.fmt_dt(activity.start_at), "end_at": svc.fmt_dt(activity.end_at),
            "capacity": activity.capacity, "status": activity.status, "creator": names.get(activity.creator_id, "已注销"),
            "issuer": names.get(activity.issuer_id or 0, ""), "issued_at": svc.fmt_dt(activity.issued_at), "review_comment": activity.review_comment,
            "signup_count": len(signups), "checkin_count": len(checkins), "signed": any(row.user_id == user.id for row in signups),
            "checked": user.id in checkins, "checkin_open": svc.checkin_open(activity), "show_code": show_code,
            "code": activity.checkin_code if show_code else "",
            "can_issue": can_issue_activity(user.is_admin, roles) and activity.status in {"draft", "rejected"},
            "can_reject": can_issue_activity(user.is_admin, roles) and activity.status == "draft",
            "can_close": activity.status == "published" and (activity.creator_id == user.id or can_issue_activity(user.is_admin, roles)),
            "can_manual": activity.status in {"published", "closed"} and (user.is_admin or can_issue_activity(user.is_admin, roles) or activity.department in svc.managed_departments(roles) or activity.creator_id == user.id),
            "people": people, "unchecked": [row for row in people if not row["checked"]],
        }
        return render(request, "activity_detail.html", shell(request, db, user, "activities", activity=detail))

    async def activity_action(activity_id: int, request: Request, db: Session, action, message: str):
        form = await form_of(request)
        user, bounce = guard(request, db)
        back = f"/activities/{activity_id}"
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect(back)
        try:
            result = action(user, svc.roles_of(db, user.id), form)
        except AppError as exc:
            return fail(request, back, exc)
        except ValueError:
            return fail(request, back, AppError("数字填写不正确"))
        flash(request, result or message)
        return redirect(back)

    @app.post("/activities/{activity_id}/issue")
    async def activity_issue(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_action(activity_id, request, db, lambda actor, roles, _form: f"活动已发放，签到码 {svc.issue_activity(db, actor, roles, activity_id)}", "")

    @app.post("/activities/{activity_id}/reject")
    async def activity_reject(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_action(activity_id, request, db, lambda actor, roles, form: svc.reject_activity(db, actor, roles, activity_id, str(form.get("comment") or "")), "活动已驳回")

    @app.post("/activities/{activity_id}/close")
    async def activity_close(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_action(activity_id, request, db, lambda actor, roles, _form: svc.close_activity(db, actor, roles, activity_id), "活动已结束")

    @app.post("/activities/{activity_id}/signup")
    async def activity_signup(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_action(activity_id, request, db, lambda actor, _roles, _form: svc.signup_activity(db, actor, activity_id), "报名成功")

    @app.post("/activities/{activity_id}/cancel")
    async def activity_cancel(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_action(activity_id, request, db, lambda actor, _roles, _form: svc.cancel_signup(db, actor, activity_id), "已取消报名")

    @app.post("/activities/{activity_id}/checkin")
    async def activity_checkin(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_action(activity_id, request, db, lambda actor, _roles, form: svc.self_checkin(db, actor, activity_id, str(form.get("code") or "")), "签到成功")

    @app.post("/activities/{activity_id}/checkin-manual")
    async def activity_manual(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_action(activity_id, request, db, lambda actor, roles, form: svc.manual_checkin(db, actor, roles, activity_id, int(str(form.get("user_id") or "0"))), "已补签")

    @app.get("/assets")
    def assets_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        asset_rows = db.scalars(select(Asset).order_by(Asset.created_at.desc())).all()
        borrow_rows = [row for row in db.scalars(select(Borrow).order_by(Borrow.created_at.desc()).limit(100)).all() if svc.can_view_borrow(user, roles, row)]
        names = svc.name_map(db, {row.user_id for row in borrow_rows})
        asset_names = {row.id: row.name for row in asset_rows}
        assets = [{"id": row.id, "name": row.name, "category": row.category, "total_qty": row.total_qty, "available": svc.available_qty(db, row), "location": row.location, "description": row.description} for row in asset_rows]
        borrows = [{"id": row.id, "asset": asset_names.get(row.asset_id, "资产"), "user": names.get(row.user_id, "已注销"), "qty": row.qty, "reason": row.reason, "status": row.status, "due_at": svc.fmt_dt(row.due_at), "comment": row.review_comment, "can_review": can_approve_borrow(user.is_admin, roles) and row.status == "pending", "can_return": row.status == "approved" and (row.user_id == user.id or can_approve_borrow(user.is_admin, roles))} for row in borrow_rows]
        return render(request, "assets.html", shell(request, db, user, "assets", assets=assets, borrows=borrows))

    async def simple_post(request: Request, db: Session, back: str, action, message: str):
        form = await form_of(request)
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect(back)
        try:
            action(user, svc.roles_of(db, user.id), form)
        except AppError as exc:
            return fail(request, back, exc)
        except ValueError:
            return fail(request, back, AppError("数字填写不正确"))
        flash(request, message)
        return redirect(back)

    @app.post("/assets")
    async def asset_create(request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/assets", lambda actor, roles, form: svc.create_asset(db, actor, roles, str(form.get("name") or ""), str(form.get("category") or ""), int(str(form.get("total_qty") or "0")), str(form.get("location") or ""), str(form.get("description") or "")), "资产已登记")

    @app.post("/borrows")
    async def borrow_create(request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/assets", lambda actor, _roles, form: svc.request_borrow(db, actor, int(str(form.get("asset_id") or "0")), int(str(form.get("qty") or "0")), str(form.get("reason") or ""), str(form.get("due_at") or "")), "借用申请已提交")

    @app.post("/borrows/{borrow_id}/review")
    async def borrow_review(borrow_id: int, request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/assets", lambda actor, roles, form: svc.review_borrow(db, actor, roles, borrow_id, str(form.get("decision") or ""), str(form.get("comment") or "")), "借用审批已保存")

    @app.post("/borrows/{borrow_id}/return")
    async def borrow_return(borrow_id: int, request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/assets", lambda actor, roles, _form: svc.return_borrow(db, actor, roles, borrow_id), "已登记归还")

    @app.get("/finance")
    def finance_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        finance = sees_finance(user.is_admin, roles)
        claims = db.scalars(select(Reimbursement).order_by(Reimbursement.created_at.desc())).all()
        claims = [row for row in claims if svc.can_view_reimbursement(user, roles, row)]
        names = svc.name_map(db, {row.user_id for row in claims})
        ledger = []
        balance = 0
        if finance:
            entries = db.scalars(select(LedgerEntry).order_by(LedgerEntry.happened_on.desc(), LedgerEntry.id.desc())).all()
            recorders = svc.name_map(db, {row.recorder_id for row in entries})
            for row in entries:
                signed = row.amount_cents if row.kind == "income" else -row.amount_cents
                balance += signed
                ledger.append({"happened_on": row.happened_on, "kind": "收入" if row.kind == "income" else "支出", "amount": svc.money(row.amount_cents), "category": row.category, "note": row.note, "recorder": recorders.get(row.recorder_id, "已注销")})
            balance = sum(row.amount_cents if row.kind == "income" else -row.amount_cents for row in entries)
        return render(request, "finance.html", shell(
            request, db, user, "finance", finance=finance, balance=svc.money(balance), ledger=ledger,
            claims=[{"id": row.id, "user": names.get(row.user_id, "已注销"), "amount": svc.money(row.amount_cents), "reason": row.reason, "status": row.status, "comment": row.review_comment, "mine": row.user_id == user.id} for row in claims],
        ))

    @app.post("/finance/ledger")
    async def finance_ledger(request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/finance", lambda actor, roles, form: svc.add_ledger(db, actor, roles, str(form.get("kind") or ""), str(form.get("amount") or ""), str(form.get("category") or ""), str(form.get("note") or ""), str(form.get("happened_on") or "")), "收支已登记")

    @app.post("/finance/claims")
    async def finance_claim(request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/finance", lambda actor, _roles, form: svc.request_reimbursement(db, actor, str(form.get("amount") or ""), str(form.get("reason") or "")), "报销已提交")

    @app.post("/finance/claims/{item_id}/review")
    async def finance_review(item_id: int, request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/finance", lambda actor, roles, form: svc.review_reimbursement(db, actor, roles, item_id, str(form.get("decision") or ""), str(form.get("comment") or "")), "报销审批已保存")

    @app.post("/finance/claims/{item_id}/pay")
    async def finance_pay(item_id: int, request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/finance", lambda actor, roles, _form: svc.pay_reimbursement(db, actor, roles, item_id), "已登记付款并记入支出")

    @app.get("/leave")
    def leave_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        rows = db.scalars(select(LeaveRequest).order_by(LeaveRequest.created_at.desc())).all()
        owners = {row.id: db.get(User, row.user_id) for row in rows}
        visible = [row for row in rows if svc.can_view_leave(user, roles, row, owners.get(row.id))]
        names = svc.name_map(db, {row.user_id for row in visible})
        items = [{"id": row.id, "user": names.get(row.user_id, "已注销"), "start_on": row.start_on, "end_on": row.end_on, "reason": row.reason, "status": row.status, "comment": row.review_comment, "can_review": row.status == "pending" and row.user_id != user.id and svc.can_approve_leave(user, roles, owners.get(row.id))} for row in visible]
        return render(request, "leave.html", shell(request, db, user, "leave", items=items))

    @app.post("/leave")
    async def leave_create(request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/leave", lambda actor, _roles, form: svc.request_leave(db, actor, str(form.get("start_on") or ""), str(form.get("end_on") or ""), str(form.get("reason") or "")), "请假已提交")

    @app.post("/leave/{item_id}/review")
    async def leave_review(item_id: int, request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/leave", lambda actor, roles, form: svc.review_leave(db, actor, roles, item_id, str(form.get("decision") or ""), str(form.get("comment") or "")), "请假审批已保存")

    @app.get("/duty")
    def duty_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        rows = db.scalars(select(DutyShift).order_by(DutyShift.duty_on, DutyShift.id)).all()
        names = svc.name_map(db, {row.user_id for row in rows})
        members = db.scalars(select(User).where(User.status == "active", User.is_admin.is_(False)).order_by(User.real_name)).all()
        return render(request, "duty.html", shell(
            request, db, user, "duty",
            shifts=[{"id": row.id, "duty_on": row.duty_on, "name": names.get(row.user_id, "已注销"), "note": row.note, "class_name": (db.get(User, row.user_id).class_name if db.get(User, row.user_id) else ""), "phone": (db.get(User, row.user_id).phone if db.get(User, row.user_id) and db.get(User, row.user_id).status == "active" else "")} for row in rows],
            members=[{"id": row.id, "real_name": row.real_name, "class_name": row.class_name} for row in members],
        ))

    @app.post("/duty")
    async def duty_create(request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/duty", lambda actor, roles, form: svc.create_duty(db, actor, roles, str(form.get("duty_on") or ""), int(str(form.get("user_id") or "0")), str(form.get("note") or "")), "值班已安排")

    @app.post("/duty/{duty_id}/delete")
    async def duty_delete(duty_id: int, request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/duty", lambda actor, roles, _form: svc.delete_duty(db, actor, roles, duty_id), "值班已删除")

    @app.get("/announcements")
    def announcements_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        rows = db.scalars(select(Announcement).order_by(Announcement.created_at.desc()).limit(100)).all()
        names = svc.name_map(db, {row.author_id for row in rows})
        items = [{"id": row.id, "title": row.title, "content": row.content, "author": names.get(row.author_id, "已注销"), "created_at": svc.fmt_dt(row.created_at), "can_delete": row.author_id == user.id or user.is_admin} for row in rows]
        return render(request, "announcements.html", shell(request, db, user, "announcements", items=items))

    @app.post("/announcements")
    async def announcement_create(request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/announcements", lambda actor, roles, form: svc.create_announcement(db, actor, roles, str(form.get("title") or ""), str(form.get("content") or "")), "公告已发布")

    @app.post("/announcements/{announcement_id}/delete")
    async def announcement_delete(announcement_id: int, request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/announcements", lambda actor, _roles, _form: svc.delete_announcement(db, actor, announcement_id), "公告已删除")

    @app.get("/approvals")
    def approvals_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        items = []
        if user.is_admin:
            for row in db.scalars(select(User).where(User.status == "pending")).all():
                items.append({"href": "/members", "kind": "注册", "title": row.real_name, "meta": f"{row.college} {row.class_name} · {row.phone}"})
        if can_approve_project(user.is_admin, roles):
            for row in db.scalars(select(Project).where(Project.status == "pending")).all():
                items.append({"href": f"/projects/{row.id}", "kind": "立项", "title": row.title, "meta": row.department})
        if can_issue_activity(user.is_admin, roles):
            for row in db.scalars(select(Activity).where(Activity.status == "draft")).all():
                items.append({"href": f"/activities/{row.id}", "kind": "活动发放", "title": row.title, "meta": row.department or "全社"})
        if can_approve_borrow(user.is_admin, roles):
            for row in db.scalars(select(Borrow).where(Borrow.status == "pending")).all():
                asset = db.get(Asset, row.asset_id)
                items.append({"href": "/assets", "kind": "借用", "title": asset.name if asset else "资产", "meta": f"{row.qty} 件"})
        if sees_finance(user.is_admin, roles):
            for row in db.scalars(select(Reimbursement).where(Reimbursement.status == "pending")).all():
                items.append({"href": "/finance", "kind": "报销", "title": svc.money(row.amount_cents), "meta": row.reason})
        if user.is_admin or "社长" in roles or "副社长" in roles or "指导老师" in roles or svc.managed_departments(roles):
            for row in db.scalars(select(LeaveRequest).where(LeaveRequest.status == "pending")).all():
                owner = db.get(User, row.user_id)
                if svc.can_approve_leave(user, roles, owner) and row.user_id != user.id:
                    items.append({"href": "/leave", "kind": "请假", "title": svc.name_of(owner), "meta": f"{row.start_on} 至 {row.end_on}"})
        return render(request, "approvals.html", shell(request, db, user, "approvals", items=items))

    @app.get("/profile")
    def profile_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        return render(request, "profile.html", shell(
            request, db, user, "profile",
            profile={"phone": user.phone, "college": user.college, "class_name": user.class_name, "department": user.department},
        ))

    @app.post("/profile/password")
    async def profile_password(request: Request, db: Session = Depends(get_db)):
        return await simple_post(request, db, "/profile", lambda actor, _roles, form: svc.change_password(db, actor, str(form.get("current") or ""), str(form.get("new_password") or ""), str(form.get("confirm") or "")), "密码已更新")

    @app.post("/profile/club")
    async def profile_club(request: Request, db: Session = Depends(get_db)):
        form = await form_of(request)
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not user.is_admin:
            flash(request, "只有超级管理员可以修改社团名称", "error")
            return redirect("/profile")
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/profile")
        try:
            svc.set_club_name(db, str(form.get("club_name") or ""))
        except AppError as exc:
            return fail(request, "/profile", exc)
        flash(request, "社团名称已更新")
        return redirect("/profile")

    @app.exception_handler(404)
    async def not_found(request: Request, _exc):
        return render(request, "not_found.html", {"club_name": "社团", "flash": None}, status_code=404)
