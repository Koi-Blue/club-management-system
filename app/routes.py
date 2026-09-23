import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.models import Activity, Announcement, Asset, Borrow, CheckIn, Project, ProjectFile, Signup, User, utcnow
from app.security import verify_password
from app.services import AppError
import app.services as svc

USER_STATUS = {"pending": "待审批", "active": "已启用", "rejected": "已驳回", "disabled": "已停用"}
PROJECT_STATUS = {"pending": "待审批", "approved": "进行中", "rejected": "已驳回", "archived": "已归档"}
BORROW_STATUS = {"pending": "待审批", "approved": "借用中", "rejected": "已驳回", "returned": "已归还"}
ACTIVITY_STATUS = {"pending": "待发放", "published": "已发放", "rejected": "已驳回", "closed": "已结束"}


def register_routes(app: FastAPI, settings: Settings) -> None:
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    templates.env.globals.update(
        user_status=USER_STATUS,
        project_status=PROJECT_STATUS,
        borrow_status=BORROW_STATUS,
        activity_status=ACTIVITY_STATUS,
    )

    def render(request: Request, name: str, context: dict, status_code: int = 200):
        context["request"] = request
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def redirect(url: str):
        return RedirectResponse(url, status_code=303)

    def flash(request: Request, message: str, level: str = "ok") -> None:
        request.session["flash"] = {"message": message, "level": level}

    def pop_flash(request: Request):
        return request.session.pop("flash", None)

    def ensure_csrf(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_hex(16)
            request.session["csrf"] = token
        return token

    def csrf_ok(request: Request, token: str | None) -> bool:
        expected = request.session.get("csrf") or ""
        given = token or ""
        if not expected or not given:
            return False
        return secrets.compare_digest(expected, given)

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

    def me_of(db: Session, user: User, roles: list[str]) -> dict:
        labels = ["超级管理员"] if user.is_admin else []
        labels.extend(roles)
        return {
            "id": user.id,
            "username": user.username,
            "real_name": user.real_name,
            "is_admin": user.is_admin,
            "roles": roles,
            "role_text": "、".join(labels) if labels else "未分配职务",
            "department": user.department,
            "warn_password": user.is_admin and verify_password(settings.admin_password, user.password_hash),
            "can_assign": svc.can_assign_roles(user, roles),
            "can_review_project": svc.can_review_project(user, roles),
            "can_manage_asset": svc.can_manage_asset(user, roles),
            "can_create_activity": svc.can_create_activity(user, roles),
            "can_issue_activity": svc.can_issue_activity(user, roles),
            "can_manual_checkin": svc.can_manual_checkin(user, roles),
            "can_announce": svc.can_announce(user, roles),
            "editable_roles": editable_roles(user, roles),
        }

    def editable_roles(user: User, roles: list[str]) -> list[str]:
        if user.is_admin or "社长" in roles:
            return list(svc.CLUB_ROLES)
        if "副社长" in roles:
            return ["部长", "成员"]
        return []

    def shell(request: Request, db: Session, user: User, nav: str, **extra) -> dict:
        roles = svc.roles_of(db, user.id)
        me = me_of(db, user, roles)
        badges = {
            "members": int(db.scalar(select(func.count()).select_from(User).where(User.status == "pending")) or 0) if user.is_admin else 0,
            "projects": int(db.scalar(select(func.count()).select_from(Project).where(Project.status == "pending")) or 0) if me["can_review_project"] else 0,
            "assets": int(db.scalar(select(func.count()).select_from(Borrow).where(Borrow.status == "pending")) or 0) if me["can_manage_asset"] else 0,
            "activities": int(db.scalar(select(func.count()).select_from(Activity).where(Activity.status == "pending")) or 0) if me["can_issue_activity"] else 0,
        }
        context = {
            "csrf": ensure_csrf(request),
            "flash": pop_flash(request),
            "club_name": svc.club_name(db),
            "me": me,
            "nav": nav,
            "badges": badges,
            "club_roles": svc.CLUB_ROLES,
        }
        context.update(extra)
        return context

    def fail(request: Request, url: str, exc: AppError):
        flash(request, exc.message, "error")
        return redirect(url)

    def show_person(db: Session, user: User) -> dict:
        return {
            "id": user.id,
            "username": user.username,
            "real_name": user.real_name,
            "phone": user.phone,
            "department": user.department,
            "status": user.status,
            "is_admin": user.is_admin,
            "roles": svc.roles_of(db, user.id),
            "created_at": svc.fmt_dt(user.created_at),
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login")
    def login_page(request: Request, db: Session = Depends(get_db)):
        if active_user(request, db):
            return redirect("/")
        return render(request, "login.html", {
            "csrf": ensure_csrf(request),
            "flash": pop_flash(request),
            "club_name": svc.club_name(db),
        })

    @app.post("/login")
    async def login_post(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/login")
        try:
            user = svc.authenticate(db, str(form.get("username") or ""), str(form.get("password") or ""))
        except AppError as exc:
            return fail(request, "/login", exc)
        request.session.clear()
        request.session["uid"] = user.id
        request.session["csrf"] = secrets.token_hex(16)
        return redirect("/")

    @app.get("/register")
    def register_page(request: Request, db: Session = Depends(get_db)):
        if active_user(request, db):
            return redirect("/")
        return render(request, "register.html", {
            "csrf": ensure_csrf(request),
            "flash": pop_flash(request),
            "club_name": svc.club_name(db),
        })

    @app.post("/register")
    async def register_post(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/register")
        try:
            svc.register_user(
                db,
                str(form.get("username") or ""),
                str(form.get("password") or ""),
                str(form.get("real_name") or ""),
                str(form.get("phone") or ""),
                str(form.get("department") or ""),
            )
        except AppError as exc:
            return fail(request, "/register", exc)
        flash(request, "注册已提交，请等待超级管理员审批后再登录")
        return redirect("/login")

    @app.post("/logout")
    async def logout(request: Request):
        form = await request.form()
        if csrf_ok(request, str(form.get("csrf") or "")):
            request.session.clear()
        return redirect("/login")

    @app.get("/")
    def dashboard(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        upcoming_rows = db.scalars(
            select(Activity).where(Activity.status == "published", Activity.end_at >= utcnow()).order_by(Activity.start_at).limit(6)
        ).all()
        news_rows = db.scalars(select(Announcement).order_by(Announcement.created_at.desc()).limit(5)).all()
        author_ids = {row.author_id for row in news_rows}
        names = svc.name_map(db, author_ids)
        todos = []
        if user.is_admin:
            count = int(db.scalar(select(func.count()).select_from(User).where(User.status == "pending")) or 0)
            if count:
                todos.append({"href": "/members", "text": f"{count} 个注册账号待审批"})
        if svc.can_review_project(user, roles):
            count = int(db.scalar(select(func.count()).select_from(Project).where(Project.status == "pending")) or 0)
            if count:
                todos.append({"href": "/projects?status=pending", "text": f"{count} 个立项待审批"})
        if svc.can_manage_asset(user, roles):
            count = int(db.scalar(select(func.count()).select_from(Borrow).where(Borrow.status == "pending")) or 0)
            if count:
                todos.append({"href": "/assets", "text": f"{count} 条借用申请待审批"})
        if svc.can_issue_activity(user, roles):
            count = int(db.scalar(select(func.count()).select_from(Activity).where(Activity.status == "pending")) or 0)
            if count:
                todos.append({"href": "/activities?status=pending", "text": f"{count} 场活动待发放"})
        my_signup_ids = set(db.scalars(select(Signup.activity_id).where(Signup.user_id == user.id)).all())
        context = shell(
            request, db, user, "home",
            stats={
                "members": int(db.scalar(select(func.count()).select_from(User).where(User.status == "active", User.is_admin.is_(False))) or 0),
                "projects": int(db.scalar(select(func.count()).select_from(Project).where(Project.status.in_(["pending", "approved", "archived"]))) or 0),
                "assets": int(db.scalar(select(func.count()).select_from(Asset)) or 0),
                "on_loan": int(db.scalar(select(func.coalesce(func.sum(Borrow.qty), 0)).where(Borrow.status == "approved")) or 0),
            },
            todos=todos,
            upcoming=[{
                "id": row.id,
                "title": row.title,
                "location": row.location,
                "start_at": svc.fmt_dt(row.start_at),
                "signed": row.id in my_signup_ids,
            } for row in upcoming_rows],
            news=[{
                "id": row.id,
                "title": row.title,
                "content": row.content,
                "author": names.get(row.author_id, ""),
                "created_at": svc.fmt_dt(row.created_at),
            } for row in news_rows],
        )
        return render(request, "dashboard.html", context)

    @app.get("/members")
    def members_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        rows = db.scalars(select(User).order_by(User.created_at)).all()
        people = [show_person(db, row) for row in rows if not row.is_admin]
        return render(request, "members.html", shell(
            request, db, user, "members",
            pending=[row for row in people if row["status"] == "pending"],
            active=[row for row in people if row["status"] == "active"],
            inactive=[row for row in people if row["status"] in {"rejected", "disabled"}],
        ))

    @app.post("/members/{user_id}/approve")
    async def members_approve(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_action(request, db, user_id, lambda actor, _roles: svc.approve_user(db, actor, user_id), "已通过注册")

    @app.post("/members/{user_id}/reject")
    async def members_reject(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_action(request, db, user_id, lambda actor, _roles: svc.reject_user(db, actor, user_id), "已驳回注册")

    @app.post("/members/{user_id}/disable")
    async def members_disable(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_action(request, db, user_id, lambda actor, _roles: svc.set_user_status(db, actor, user_id, "disabled"), "已停用账号")

    @app.post("/members/{user_id}/enable")
    async def members_enable(user_id: int, request: Request, db: Session = Depends(get_db)):
        return await member_action(request, db, user_id, lambda actor, _roles: svc.set_user_status(db, actor, user_id, "active"), "已启用账号")

    @app.post("/members/{user_id}/roles")
    async def members_roles(user_id: int, request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        actor, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/members")
        selected = [str(item) for item in form.getlist("roles")]
        try:
            svc.set_roles(db, actor, svc.roles_of(db, actor.id), user_id, selected)
        except AppError as exc:
            return fail(request, "/members", exc)
        flash(request, "职务已保存")
        return redirect("/members")

    async def member_action(request: Request, db: Session, user_id: int, action, ok_message: str):
        form = await request.form()
        actor, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/members")
        try:
            action(actor, svc.roles_of(db, actor.id))
        except AppError as exc:
            return fail(request, "/members", exc)
        flash(request, ok_message)
        return redirect("/members")

    @app.get("/projects")
    def projects_page(request: Request, db: Session = Depends(get_db), status: str = ""):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        stmt = select(Project).order_by(Project.created_at.desc())
        if status in PROJECT_STATUS:
            stmt = stmt.where(Project.status == status)
        rows = db.scalars(stmt).all()
        names = svc.name_map(db, {row.leader_id for row in rows})
        projects = [{
            "id": row.id,
            "title": row.title,
            "summary": row.summary,
            "status": row.status,
            "leader": names.get(row.leader_id, ""),
            "created_at": svc.fmt_dt(row.created_at),
        } for row in rows]
        return render(request, "projects.html", shell(request, db, user, "projects", projects=projects, status=status))

    @app.get("/projects/new")
    def project_new_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        return render(request, "project_new.html", shell(request, db, user, "projects"))

    @app.post("/projects")
    async def project_create(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        back = "/projects/new"
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect(back)
        try:
            project = svc.create_project(db, user, str(form.get("title") or ""), str(form.get("summary") or ""))
        except AppError as exc:
            return fail(request, back, exc)
        flash(request, "立项已提交，等待审批")
        return redirect(f"/projects/{project.id}")

    @app.get("/projects/{project_id}")
    def project_detail(project_id: int, request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        project = db.get(Project, project_id)
        if project is None:
            flash(request, "找不到这个立项", "error")
            return redirect("/projects")
        roles = svc.roles_of(db, user.id)
        files = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project.id).order_by(ProjectFile.uploaded_at.desc())).all()
        names = svc.name_map(db, {project.leader_id, project.reviewer_id or 0, *(row.uploader_id for row in files)} - {0})
        can_review = svc.can_review_project(user, roles)
        detail = {
            "id": project.id,
            "title": project.title,
            "summary": project.summary,
            "status": project.status,
            "leader": names.get(project.leader_id, ""),
            "leader_id": project.leader_id,
            "reviewer": names.get(project.reviewer_id or 0, ""),
            "review_comment": project.review_comment,
            "created_at": svc.fmt_dt(project.created_at),
            "updated_at": svc.fmt_dt(project.updated_at),
            "can_review": can_review and project.status == "pending",
            "can_resubmit": project.leader_id == user.id and project.status == "rejected",
            "can_archive": project.status == "approved" and (project.leader_id == user.id or can_review),
            "can_upload": project.leader_id == user.id or can_review,
            "files": [{
                "id": row.id,
                "name": row.original_name,
                "size": row.size,
                "uploader": names.get(row.uploader_id, ""),
                "uploaded_at": svc.fmt_dt(row.uploaded_at),
                "can_delete": row.uploader_id == user.id or can_review,
            } for row in files],
        }
        return render(request, "project_detail.html", shell(request, db, user, "projects", project=detail))

    @app.post("/projects/{project_id}/review")
    async def project_review(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await project_post(project_id, request, db, lambda actor, roles, form: svc.review_project(
            db, actor, roles, project_id, str(form.get("decision") or ""), str(form.get("comment") or "")
        ), "审批已保存")

    @app.post("/projects/{project_id}/resubmit")
    async def project_resubmit(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await project_post(project_id, request, db, lambda actor, _roles, _form: svc.resubmit_project(db, actor, project_id), "已重新提交")

    @app.post("/projects/{project_id}/archive")
    async def project_archive(project_id: int, request: Request, db: Session = Depends(get_db)):
        return await project_post(project_id, request, db, lambda actor, roles, _form: svc.archive_project(db, actor, roles, project_id), "立项已归档")

    @app.post("/projects/{project_id}/files")
    async def project_upload(project_id: int, request: Request, db: Session = Depends(get_db)):
        form = await request.form()

        def action(actor, roles, current):
            upload = current.get("file")
            if upload is None or not getattr(upload, "filename", ""):
                raise AppError("请选择文件")
            content = upload.file.read()
            svc.save_project_file(db, actor, roles, project_id, settings.upload_dir, upload.filename, content)

        return await project_post(project_id, request, db, action, "资料已上传", form)

    @app.post("/projects/{project_id}/files/{file_id}/delete")
    async def project_file_delete(project_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
        return await project_post(
            project_id, request, db,
            lambda actor, roles, _form: svc.delete_project_file(db, actor, roles, settings.upload_dir, project_id, file_id),
            "文件已删除",
        )

    @app.get("/projects/{project_id}/files/{file_id}")
    def project_download(project_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        row = db.get(ProjectFile, file_id)
        back = f"/projects/{project_id}"
        if row is None or row.project_id != project_id:
            flash(request, "找不到这个文件", "error")
            return redirect("/projects")
        path = svc.project_file_path(settings.upload_dir, project_id, row.stored_name)
        if not path.exists():
            flash(request, "文件已丢失", "error")
            return redirect(back)
        return FileResponse(path, filename=row.original_name)

    async def project_post(project_id: int, request: Request, db: Session, action, ok_message: str, form=None):
        if form is None:
            form = await request.form()
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
        flash(request, ok_message)
        return redirect(back)

    @app.get("/assets")
    def assets_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        asset_rows = db.scalars(select(Asset).order_by(Asset.created_at.desc())).all()
        borrow_rows = db.scalars(select(Borrow).order_by(Borrow.created_at.desc()).limit(100)).all()
        names = svc.name_map(db, {row.user_id for row in borrow_rows})
        asset_names = {row.id: row.name for row in asset_rows}
        missing_assets = {row.asset_id for row in borrow_rows} - set(asset_names)
        if missing_assets:
            for row in db.scalars(select(Asset).where(Asset.id.in_(missing_assets))).all():
                asset_names[row.id] = row.name
        roles = svc.roles_of(db, user.id)
        can_manage = svc.can_manage_asset(user, roles)
        assets = [{
            "id": row.id,
            "name": row.name,
            "category": row.category,
            "total_qty": row.total_qty,
            "available": svc.available_qty(db, row),
            "location": row.location,
            "description": row.description,
        } for row in asset_rows]
        borrows = [{
            "id": row.id,
            "asset": asset_names.get(row.asset_id, "已删除资产"),
            "user": names.get(row.user_id, ""),
            "mine": row.user_id == user.id,
            "qty": row.qty,
            "reason": row.reason,
            "status": row.status,
            "due_at": svc.fmt_dt(row.due_at),
            "created_at": svc.fmt_dt(row.created_at),
            "comment": row.review_comment,
            "can_review": can_manage and row.status == "pending",
            "can_return": row.status == "approved" and (row.user_id == user.id or can_manage),
        } for row in borrow_rows]
        return render(request, "assets.html", shell(
            request, db, user, "assets", assets=assets, borrows=borrows, can_manage=can_manage,
        ))

    @app.post("/assets")
    async def asset_create(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/assets")
        try:
            svc.create_asset(
                db, user, svc.roles_of(db, user.id),
                str(form.get("name") or ""),
                str(form.get("category") or ""),
                parse_int(form.get("total_qty"), "数量"),
                str(form.get("location") or ""),
                str(form.get("description") or ""),
            )
        except AppError as exc:
            return fail(request, "/assets", exc)
        flash(request, "资产已登记")
        return redirect("/assets")

    @app.post("/borrows")
    async def borrow_create(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/assets")
        try:
            svc.request_borrow(
                db, user,
                parse_int(form.get("asset_id"), "资产"),
                parse_int(form.get("qty"), "借用数量"),
                str(form.get("reason") or ""),
                str(form.get("due_at") or ""),
            )
        except AppError as exc:
            return fail(request, "/assets", exc)
        flash(request, "借用申请已提交")
        return redirect("/assets")

    @app.post("/borrows/{borrow_id}/review")
    async def borrow_review(borrow_id: int, request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/assets")
        try:
            svc.review_borrow(db, user, svc.roles_of(db, user.id), borrow_id, str(form.get("decision") or ""), str(form.get("comment") or ""))
        except AppError as exc:
            return fail(request, "/assets", exc)
        flash(request, "借用审批已保存")
        return redirect("/assets")

    @app.post("/borrows/{borrow_id}/return")
    async def borrow_return(borrow_id: int, request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/assets")
        try:
            svc.return_borrow(db, user, svc.roles_of(db, user.id), borrow_id)
        except AppError as exc:
            return fail(request, "/assets", exc)
        flash(request, "已登记归还")
        return redirect("/assets")

    @app.get("/activities")
    def activities_page(request: Request, db: Session = Depends(get_db), status: str = ""):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        stmt = select(Activity).order_by(Activity.start_at.desc())
        if not (svc.can_create_activity(user, roles) or svc.can_issue_activity(user, roles)):
            stmt = stmt.where(Activity.status.in_(["published", "closed"]))
        if status in ACTIVITY_STATUS:
            stmt = stmt.where(Activity.status == status)
        rows = db.scalars(stmt).all()
        names = svc.name_map(db, {row.creator_id for row in rows})
        my_ids = set(db.scalars(select(Signup.activity_id).where(Signup.user_id == user.id)).all())
        activities = [{
            "id": row.id,
            "title": row.title,
            "location": row.location,
            "status": row.status,
            "start_at": svc.fmt_dt(row.start_at),
            "end_at": svc.fmt_dt(row.end_at),
            "creator": names.get(row.creator_id, ""),
            "signed": row.id in my_ids,
            "signup_count": svc.signup_count(db, row.id),
            "capacity": row.capacity,
        } for row in rows]
        return render(request, "activities.html", shell(request, db, user, "activities", activities=activities, status=status))

    @app.get("/activities/new")
    def activity_new_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        roles = svc.roles_of(db, user.id)
        if not svc.can_create_activity(user, roles):
            flash(request, "没有权限创建活动", "error")
            return redirect("/activities")
        return render(request, "activity_new.html", shell(request, db, user, "activities"))

    @app.post("/activities")
    async def activity_create(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/activities/new")
        try:
            capacity_raw = str(form.get("capacity") or "").strip()
            activity = svc.create_activity(
                db, user, svc.roles_of(db, user.id),
                str(form.get("title") or ""),
                str(form.get("description") or ""),
                str(form.get("location") or ""),
                str(form.get("start_at") or ""),
                str(form.get("end_at") or ""),
                parse_int(capacity_raw or "0", "人数上限"),
            )
        except AppError as exc:
            return fail(request, "/activities/new", exc)
        flash(request, "活动已提交，等待发放")
        return redirect(f"/activities/{activity.id}")

    @app.get("/activities/{activity_id}")
    def activity_detail(activity_id: int, request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        activity = db.get(Activity, activity_id)
        if activity is None:
            flash(request, "找不到这场活动", "error")
            return redirect("/activities")
        roles = svc.roles_of(db, user.id)
        if activity.status not in {"published", "closed"} and activity.creator_id != user.id and not svc.can_issue_activity(user, roles):
            flash(request, "活动还未发放", "error")
            return redirect("/activities")
        signups = db.scalars(select(Signup).where(Signup.activity_id == activity.id).order_by(Signup.created_at)).all()
        checkins = db.scalars(select(CheckIn).where(CheckIn.activity_id == activity.id)).all()
        check_map = {row.user_id: row for row in checkins}
        names = svc.name_map(db, {activity.creator_id, activity.issuer_id or 0, *(row.user_id for row in signups)} - {0})
        show_code = bool(activity.checkin_code) and svc.can_see_checkin_code(user, roles, activity)
        people = []
        for row in signups:
            checked = check_map.get(row.user_id)
            people.append({
                "user_id": row.user_id,
                "name": names.get(row.user_id, ""),
                "signed_at": svc.fmt_dt(row.created_at),
                "checked": checked is not None,
                "checked_at": svc.fmt_dt(checked.checked_at) if checked else "",
                "method": "签到码" if checked and checked.method == "code" else ("补签" if checked else ""),
            })
        detail = {
            "id": activity.id,
            "title": activity.title,
            "description": activity.description,
            "location": activity.location,
            "start_at": svc.fmt_dt(activity.start_at),
            "end_at": svc.fmt_dt(activity.end_at),
            "capacity": activity.capacity,
            "status": activity.status,
            "creator": names.get(activity.creator_id, ""),
            "issuer": names.get(activity.issuer_id or 0, ""),
            "issued_at": svc.fmt_dt(activity.issued_at),
            "review_comment": activity.review_comment,
            "signup_count": len(signups),
            "checkin_count": len(checkins),
            "signed": any(row.user_id == user.id for row in signups),
            "checked": user.id in check_map,
            "checkin_open": svc.checkin_open(activity),
            "show_code": show_code,
            "code": activity.checkin_code if show_code else "",
            "can_issue": svc.can_issue_activity(user, roles) and activity.status in {"pending", "rejected"},
            "can_reject": svc.can_issue_activity(user, roles) and activity.status == "pending",
            "can_close": activity.status == "published" and (activity.creator_id == user.id or svc.can_issue_activity(user, roles)),
            "can_manual": svc.can_manual_checkin(user, roles) and activity.status in {"published", "closed"},
            "people": people,
            "unchecked": [row for row in people if not row["checked"]],
        }
        return render(request, "activity_detail.html", shell(request, db, user, "activities", activity=detail))

    @app.post("/activities/{activity_id}/issue")
    async def activity_issue(activity_id: int, request: Request, db: Session = Depends(get_db)):
        def action(actor, roles, _form):
            code = svc.issue_activity(db, actor, roles, activity_id)
            flash(request, f"活动已发放，签到码 {code}")
        return await activity_post(activity_id, request, db, action, "")

    @app.post("/activities/{activity_id}/reject")
    async def activity_reject(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_post(
            activity_id, request, db,
            lambda actor, roles, form: svc.reject_activity(db, actor, roles, activity_id, str(form.get("comment") or "")),
            "活动已驳回",
        )

    @app.post("/activities/{activity_id}/close")
    async def activity_close(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_post(activity_id, request, db, lambda actor, roles, _form: svc.close_activity(db, actor, roles, activity_id), "活动已结束")

    @app.post("/activities/{activity_id}/signup")
    async def activity_signup(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_post(activity_id, request, db, lambda actor, _roles, _form: svc.signup_activity(db, actor, activity_id), "报名成功")

    @app.post("/activities/{activity_id}/cancel")
    async def activity_cancel(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_post(activity_id, request, db, lambda actor, _roles, _form: svc.cancel_signup(db, actor, activity_id), "已取消报名")

    @app.post("/activities/{activity_id}/checkin")
    async def activity_checkin(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_post(
            activity_id, request, db,
            lambda actor, _roles, form: svc.self_checkin(db, actor, activity_id, str(form.get("code") or "")),
            "签到成功",
        )

    @app.post("/activities/{activity_id}/checkin-manual")
    async def activity_manual(activity_id: int, request: Request, db: Session = Depends(get_db)):
        return await activity_post(
            activity_id, request, db,
            lambda actor, roles, form: svc.manual_checkin(db, actor, roles, activity_id, parse_int(form.get("user_id"), "成员")),
            "已补签",
        )

    async def activity_post(activity_id: int, request: Request, db: Session, action, ok_message: str):
        form = await request.form()
        user, bounce = guard(request, db)
        back = f"/activities/{activity_id}"
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect(back)
        try:
            action(user, svc.roles_of(db, user.id), form)
        except AppError as exc:
            return fail(request, back, exc)
        if ok_message:
            flash(request, ok_message)
        return redirect(back)

    @app.get("/announcements")
    def announcements_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        rows = db.scalars(select(Announcement).order_by(Announcement.created_at.desc()).limit(100)).all()
        names = svc.name_map(db, {row.author_id for row in rows})
        items = [{
            "id": row.id,
            "title": row.title,
            "content": row.content,
            "author": names.get(row.author_id, ""),
            "created_at": svc.fmt_dt(row.created_at),
            "can_delete": row.author_id == user.id or user.is_admin,
        } for row in rows]
        return render(request, "announcements.html", shell(request, db, user, "announcements", items=items))

    @app.post("/announcements")
    async def announcement_create(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/announcements")
        try:
            svc.create_announcement(db, user, svc.roles_of(db, user.id), str(form.get("title") or ""), str(form.get("content") or ""))
        except AppError as exc:
            return fail(request, "/announcements", exc)
        flash(request, "公告已发布")
        return redirect("/announcements")

    @app.post("/announcements/{announcement_id}/delete")
    async def announcement_delete(announcement_id: int, request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/announcements")
        try:
            svc.delete_announcement(db, user, announcement_id)
        except AppError as exc:
            return fail(request, "/announcements", exc)
        flash(request, "公告已删除")
        return redirect("/announcements")

    @app.get("/profile")
    def profile_page(request: Request, db: Session = Depends(get_db)):
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        return render(request, "profile.html", shell(request, db, user, "profile"))

    @app.post("/profile/password")
    async def profile_password(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        user, bounce = guard(request, db)
        if bounce:
            return bounce
        if not csrf_ok(request, str(form.get("csrf") or "")):
            flash(request, "页面已过期，请刷新后重试", "error")
            return redirect("/profile")
        try:
            svc.change_password(
                db, user,
                str(form.get("current") or ""),
                str(form.get("new_password") or ""),
                str(form.get("confirm") or ""),
            )
        except AppError as exc:
            return fail(request, "/profile", exc)
        flash(request, "密码已更新")
        return redirect("/profile")

    @app.post("/profile/club")
    async def profile_club(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
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
        db = next(get_db())
        try:
            name = svc.club_name(db)
        finally:
            db.close()
        return render(request, "not_found.html", {
            "club_name": name,
            "csrf": request.session.get("csrf", ""),
            "flash": None,
        }, status_code=404)


def parse_int(raw, label: str) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise AppError(f"{label}不正确") from exc
