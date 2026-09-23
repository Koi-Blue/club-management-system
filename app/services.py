import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    Announcement,
    Asset,
    Borrow,
    CheckIn,
    Project,
    ProjectFile,
    Setting,
    Signup,
    User,
    UserRole,
    utcnow,
)
from app.security import hash_password, verify_password

SHANGHAI = timezone(timedelta(hours=8))
CLUB_ROLES = ["社长", "副社长", "指导老师", "荣誉社长", "部长", "成员"]
ROLE_ORDER = {name: index for index, name in enumerate(CLUB_ROLES)}
ALLOWED_FILE_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".zip", ".txt", ".csv",
}
MAX_FILE_SIZE = 20 * 1024 * 1024
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")


class AppError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def parse_local_dt(raw: str, label: str) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise AppError(f"请填写{label}")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AppError(f"{label}格式不正确") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def club_name(db: Session) -> str:
    row = db.get(Setting, "club_name")
    return row.value if row and row.value else "社团"


def set_club_name(db: Session, name: str) -> None:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 40:
        raise AppError("社团名称需要在 1 到 40 个字之间")
    row = db.get(Setting, "club_name")
    if row is None:
        db.add(Setting(key="club_name", value=cleaned))
    else:
        row.value = cleaned
    db.commit()


def roles_of(db: Session, user_id: int) -> list[str]:
    rows = db.scalars(select(UserRole.role).where(UserRole.user_id == user_id)).all()
    return sorted(rows, key=lambda item: ROLE_ORDER.get(item, 99))


def has_role(roles: list[str], *names: str) -> bool:
    return any(name in roles for name in names)


def can_assign_roles(user: User, roles: list[str]) -> bool:
    return user.is_admin or has_role(roles, "社长", "副社长")


def can_review_project(user: User, roles: list[str]) -> bool:
    return user.is_admin or has_role(roles, "社长", "副社长", "指导老师")


def can_manage_asset(user: User, roles: list[str]) -> bool:
    return user.is_admin or has_role(roles, "社长", "副社长", "部长")


def can_create_activity(user: User, roles: list[str]) -> bool:
    return user.is_admin or has_role(roles, "社长", "副社长", "指导老师", "部长")


def can_issue_activity(user: User, roles: list[str]) -> bool:
    return user.is_admin or has_role(roles, "社长", "副社长", "指导老师")


def can_manual_checkin(user: User, roles: list[str]) -> bool:
    return user.is_admin or has_role(roles, "社长", "副社长", "部长")


def can_announce(user: User, roles: list[str]) -> bool:
    return user.is_admin or has_role(roles, "社长", "副社长", "部长")


def can_see_checkin_code(user: User, roles: list[str], activity: Activity) -> bool:
    return activity.creator_id == user.id or can_issue_activity(user, roles) or can_manual_checkin(user, roles)


def name_map(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = db.scalars(select(User).where(User.id.in_(user_ids))).all()
    return {row.id: row.real_name for row in rows}


def require_text(value: str, label: str, limit: int) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise AppError(f"请填写{label}")
    if len(cleaned) > limit:
        raise AppError(f"{label}不能超过 {limit} 个字")
    return cleaned


def optional_text(value: str, label: str, limit: int) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) > limit:
        raise AppError(f"{label}不能超过 {limit} 个字")
    return cleaned


def register_user(db: Session, username: str, password: str, real_name: str, phone: str, department: str) -> None:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise AppError("用户名需为 3 到 32 位中文、字母、数字或下划线")
    if len(password) < 6 or len(password) > 64:
        raise AppError("密码需要 6 到 64 位")
    real_name = require_text(real_name, "姓名", 40)
    phone = optional_text(phone, "手机", 20)
    if phone and not re.fullmatch(r"[0-9+\-]{6,20}", phone):
        raise AppError("手机号格式不正确")
    department = optional_text(department, "部门", 40)
    if db.scalar(select(User).where(User.username == username)):
        raise AppError("这个用户名已经注册")
    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            real_name=real_name,
            phone=phone,
            department=department,
            status="pending",
            is_admin=False,
        )
    )
    db.commit()


def authenticate(db: Session, username: str, password: str) -> User:
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not verify_password(password, user.password_hash):
        raise AppError("用户名或密码不正确")
    if user.status == "pending":
        raise AppError("账号正在等待超级管理员审批")
    if user.status == "rejected":
        raise AppError("账号未通过审批，请联系超级管理员")
    if user.status != "active":
        raise AppError("账号已停用")
    return user


def approve_user(db: Session, actor: User, user_id: int) -> None:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以审批注册")
    user = db.get(User, user_id)
    if user is None or user.is_admin:
        raise AppError("找不到这个账号")
    user.status = "active"
    if not roles_of(db, user.id):
        db.add(UserRole(user_id=user.id, role="成员"))
    db.commit()


def reject_user(db: Session, actor: User, user_id: int) -> None:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以审批注册")
    user = db.get(User, user_id)
    if user is None or user.is_admin:
        raise AppError("找不到这个账号")
    user.status = "rejected"
    db.commit()


def set_user_status(db: Session, actor: User, user_id: int, status: str) -> None:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以停用或启用账号")
    if status not in {"active", "disabled"}:
        raise AppError("状态不正确")
    user = db.get(User, user_id)
    if user is None or user.is_admin:
        raise AppError("不能停用超级管理员")
    user.status = status
    db.commit()


def set_roles(db: Session, actor: User, actor_roles: list[str], user_id: int, selected: list[str]) -> None:
    if not can_assign_roles(actor, actor_roles):
        raise AppError("没有权限分配职务")
    user = db.get(User, user_id)
    if user is None or user.status != "active" or user.is_admin:
        raise AppError("只能给已启用的成员分配职务")
    cleaned = []
    for role in selected:
        if role in CLUB_ROLES and role not in cleaned:
            cleaned.append(role)
    current = roles_of(db, user.id)
    if actor.is_admin or "社长" in actor_roles:
        final = cleaned
    elif "副社长" in actor_roles:
        if any(role not in {"部长", "成员"} for role in cleaned):
            raise AppError("副社长只能调整部长和成员")
        locked = [role for role in current if role not in {"部长", "成员"}]
        final = locked + [role for role in cleaned if role not in locked]
    else:
        raise AppError("没有权限分配职务")
    if not final:
        raise AppError("至少保留一个职务")
    db.execute(delete(UserRole).where(UserRole.user_id == user.id))
    db.flush()
    for role in final:
        db.add(UserRole(user_id=user.id, role=role))
    db.commit()


def change_password(db: Session, user: User, current: str, new_password: str, confirm: str) -> None:
    if not verify_password(current, user.password_hash):
        raise AppError("当前密码不正确")
    if new_password != confirm:
        raise AppError("两次输入的新密码不一致")
    if len(new_password) < 6 or len(new_password) > 64:
        raise AppError("新密码需要 6 到 64 位")
    user.password_hash = hash_password(new_password)
    db.commit()


def create_project(db: Session, user: User, title: str, summary: str) -> Project:
    project = Project(
        title=require_text(title, "项目名称", 80),
        summary=optional_text(summary, "项目说明", 4000),
        status="pending",
        leader_id=user.id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def review_project(db: Session, actor: User, roles: list[str], project_id: int, decision: str, comment: str) -> None:
    if not can_review_project(actor, roles):
        raise AppError("没有权限审批立项")
    project = db.get(Project, project_id)
    if project is None:
        raise AppError("找不到这个立项")
    if project.status not in {"pending", "rejected"}:
        raise AppError("当前状态不能审批")
    if decision not in {"approved", "rejected"}:
        raise AppError("审批结果不正确")
    project.status = decision
    project.reviewer_id = actor.id
    project.review_comment = optional_text(comment, "审批意见", 200)
    project.updated_at = utcnow()
    db.commit()


def resubmit_project(db: Session, actor: User, project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None or project.leader_id != actor.id:
        raise AppError("只有立项人可以重新提交")
    if project.status != "rejected":
        raise AppError("只有被驳回的立项可以重新提交")
    project.status = "pending"
    project.updated_at = utcnow()
    db.commit()


def archive_project(db: Session, actor: User, roles: list[str], project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None:
        raise AppError("找不到这个立项")
    if project.status != "approved":
        raise AppError("只有进行中的立项可以归档")
    if project.leader_id != actor.id and not can_review_project(actor, roles):
        raise AppError("没有权限归档")
    project.status = "archived"
    project.updated_at = utcnow()
    db.commit()


def save_project_file(db: Session, actor: User, roles: list[str], project_id: int, upload_dir: str, filename: str, content: bytes) -> None:
    project = db.get(Project, project_id)
    if project is None:
        raise AppError("找不到这个立项")
    if actor.id != project.leader_id and not can_review_project(actor, roles):
        raise AppError("没有权限上传资料")
    if not content:
        raise AppError("请选择文件")
    if len(content) > MAX_FILE_SIZE:
        raise AppError("单个文件不能超过 20MB")
    original = Path(filename or "").name
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_FILE_EXT:
        raise AppError("不支持这个文件类型")
    if len(original) > 180:
        original = original[-180:]
    stored = f"{secrets.token_hex(16)}{suffix}"
    folder = Path(upload_dir) / "projects" / str(project.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / stored).write_bytes(content)
    db.add(
        ProjectFile(
            project_id=project.id,
            stored_name=stored,
            original_name=original,
            size=len(content),
            uploader_id=actor.id,
        )
    )
    project.updated_at = utcnow()
    db.commit()


def project_file_path(upload_dir: str, project_id: int, stored_name: str) -> Path:
    path = (Path(upload_dir) / "projects" / str(project_id) / stored_name).resolve()
    root = (Path(upload_dir) / "projects" / str(project_id)).resolve()
    if root not in path.parents:
        raise AppError("文件路径不正确")
    return path


def delete_project_file(db: Session, actor: User, roles: list[str], upload_dir: str, project_id: int, file_id: int) -> None:
    row = db.get(ProjectFile, file_id)
    if row is None or row.project_id != project_id:
        raise AppError("找不到这个文件")
    if actor.id != row.uploader_id and not can_review_project(actor, roles):
        raise AppError("没有权限删除这个文件")
    path = project_file_path(upload_dir, project_id, row.stored_name)
    if path.exists():
        path.unlink()
    db.delete(row)
    db.commit()


def create_asset(db: Session, actor: User, roles: list[str], name: str, category: str, total_qty: int, location: str, description: str) -> None:
    if not can_manage_asset(actor, roles):
        raise AppError("没有权限登记资产")
    if total_qty < 1 or total_qty > 100000:
        raise AppError("数量需要在 1 到 100000 之间")
    db.add(
        Asset(
            name=require_text(name, "资产名称", 80),
            category=optional_text(category, "分类", 40) or "物资",
            total_qty=total_qty,
            location=optional_text(location, "存放位置", 80),
            description=optional_text(description, "说明", 1000),
        )
    )
    db.commit()


def borrowed_qty(db: Session, asset_id: int) -> int:
    used = db.scalar(
        select(func.coalesce(func.sum(Borrow.qty), 0)).where(
            Borrow.asset_id == asset_id,
            Borrow.status == "approved",
        )
    )
    return int(used or 0)


def available_qty(db: Session, asset: Asset) -> int:
    return asset.total_qty - borrowed_qty(db, asset.id)


def update_asset_qty(db: Session, actor: User, roles: list[str], asset_id: int, total_qty: int) -> None:
    if not can_manage_asset(actor, roles):
        raise AppError("没有权限修改资产")
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise AppError("找不到这项资产")
    if total_qty < 1 or total_qty > 100000:
        raise AppError("数量需要在 1 到 100000 之间")
    if total_qty < borrowed_qty(db, asset.id):
        raise AppError("总数不能少于当前借出数量")
    asset.total_qty = total_qty
    db.commit()


def request_borrow(db: Session, user: User, asset_id: int, qty: int, reason: str, due_raw: str) -> None:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise AppError("找不到这项资产")
    if qty < 1:
        raise AppError("借用数量至少为 1")
    due_at = parse_local_dt(due_raw, "预计归还时间")
    if due_at <= utcnow():
        raise AppError("预计归还时间需要晚于现在")
    if qty > available_qty(db, asset):
        raise AppError("可借数量不足")
    db.add(
        Borrow(
            asset_id=asset.id,
            user_id=user.id,
            qty=qty,
            reason=optional_text(reason, "借用事由", 200),
            status="pending",
            due_at=due_at,
        )
    )
    db.commit()


def review_borrow(db: Session, actor: User, roles: list[str], borrow_id: int, decision: str, comment: str) -> None:
    if not can_manage_asset(actor, roles):
        raise AppError("没有权限审批借用")
    row = db.get(Borrow, borrow_id)
    if row is None or row.status != "pending":
        raise AppError("这条借用申请不能审批")
    if decision not in {"approved", "rejected"}:
        raise AppError("审批结果不正确")
    asset = db.get(Asset, row.asset_id)
    if decision == "approved":
        if asset is None or row.qty > available_qty(db, asset):
            raise AppError("可借数量不足，无法通过")
        row.approved_at = utcnow()
    row.status = decision
    row.reviewer_id = actor.id
    row.review_comment = optional_text(comment, "审批意见", 200)
    db.commit()


def return_borrow(db: Session, actor: User, roles: list[str], borrow_id: int) -> None:
    row = db.get(Borrow, borrow_id)
    if row is None or row.status != "approved":
        raise AppError("这条记录不在借用中")
    if actor.id != row.user_id and not can_manage_asset(actor, roles):
        raise AppError("没有权限登记归还")
    row.status = "returned"
    row.returned_at = utcnow()
    db.commit()


def create_activity(
    db: Session,
    actor: User,
    roles: list[str],
    title: str,
    description: str,
    location: str,
    start_raw: str,
    end_raw: str,
    capacity: int,
) -> Activity:
    if not can_create_activity(actor, roles):
        raise AppError("没有权限创建活动")
    start_at = parse_local_dt(start_raw, "开始时间")
    end_at = parse_local_dt(end_raw, "结束时间")
    if end_at <= start_at:
        raise AppError("结束时间需要晚于开始时间")
    if capacity < 0 or capacity > 100000:
        raise AppError("人数上限不正确")
    activity = Activity(
        title=require_text(title, "活动名称", 80),
        description=optional_text(description, "活动说明", 4000),
        location=optional_text(location, "地点", 80),
        start_at=start_at,
        end_at=end_at,
        capacity=capacity,
        status="pending",
        creator_id=actor.id,
    )
    db.add(activity)
    db.commit()
    db.refresh(activity)
    return activity


def issue_activity(db: Session, actor: User, roles: list[str], activity_id: int) -> str:
    if not can_issue_activity(actor, roles):
        raise AppError("没有权限发放活动")
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status not in {"pending", "rejected"}:
        raise AppError("当前状态不能发放")
    activity.status = "published"
    activity.issuer_id = actor.id
    activity.issued_at = utcnow()
    activity.checkin_code = f"{secrets.randbelow(1000000):06d}"
    activity.review_comment = ""
    db.commit()
    return activity.checkin_code


def reject_activity(db: Session, actor: User, roles: list[str], activity_id: int, comment: str) -> None:
    if not can_issue_activity(actor, roles):
        raise AppError("没有权限驳回活动")
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status != "pending":
        raise AppError("当前状态不能驳回")
    activity.status = "rejected"
    activity.issuer_id = actor.id
    activity.review_comment = optional_text(comment, "驳回意见", 200)
    db.commit()


def close_activity(db: Session, actor: User, roles: list[str], activity_id: int) -> None:
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status != "published":
        raise AppError("只有已发放的活动可以结束")
    if activity.creator_id != actor.id and not can_issue_activity(actor, roles):
        raise AppError("没有权限结束活动")
    activity.status = "closed"
    db.commit()


def signup_count(db: Session, activity_id: int) -> int:
    return int(db.scalar(select(func.count()).select_from(Signup).where(Signup.activity_id == activity_id)) or 0)


def signup_activity(db: Session, user: User, activity_id: int) -> None:
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status != "published":
        raise AppError("活动还未发放，不能报名")
    if as_utc(activity.end_at) < utcnow():
        raise AppError("活动已结束，不能报名")
    existing = db.scalar(select(Signup).where(Signup.activity_id == activity.id, Signup.user_id == user.id))
    if existing:
        raise AppError("你已经报名了")
    if activity.capacity and signup_count(db, activity.id) >= activity.capacity:
        raise AppError("报名人数已满")
    db.add(Signup(activity_id=activity.id, user_id=user.id))
    db.commit()


def cancel_signup(db: Session, user: User, activity_id: int) -> None:
    row = db.scalar(select(Signup).where(Signup.activity_id == activity_id, Signup.user_id == user.id))
    if row is None:
        raise AppError("你还没有报名")
    checked = db.scalar(select(CheckIn).where(CheckIn.activity_id == activity_id, CheckIn.user_id == user.id))
    if checked:
        raise AppError("已签到后不能取消报名")
    db.delete(row)
    db.commit()


def checkin_open(activity: Activity) -> bool:
    if activity.status != "published":
        return False
    now = utcnow()
    start = as_utc(activity.start_at) - timedelta(minutes=30)
    end = as_utc(activity.end_at)
    return start <= now <= end


def self_checkin(db: Session, user: User, activity_id: int, code: str) -> None:
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status != "published":
        raise AppError("活动当前不能签到")
    if not checkin_open(activity):
        raise AppError("不在签到时间内")
    signup = db.scalar(select(Signup).where(Signup.activity_id == activity.id, Signup.user_id == user.id))
    if signup is None:
        raise AppError("请先报名再签到")
    if db.scalar(select(CheckIn).where(CheckIn.activity_id == activity.id, CheckIn.user_id == user.id)):
        raise AppError("你已经签到")
    if not activity.checkin_code or (code or "").strip() != activity.checkin_code:
        raise AppError("签到码不正确")
    db.add(CheckIn(activity_id=activity.id, user_id=user.id, method="code"))
    db.commit()


def manual_checkin(db: Session, actor: User, roles: list[str], activity_id: int, user_id: int) -> None:
    if not can_manual_checkin(actor, roles):
        raise AppError("没有权限补签")
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status not in {"published", "closed"}:
        raise AppError("活动当前不能补签")
    signup = db.scalar(select(Signup).where(Signup.activity_id == activity.id, Signup.user_id == user_id))
    if signup is None:
        raise AppError("对方还没有报名")
    if db.scalar(select(CheckIn).where(CheckIn.activity_id == activity.id, CheckIn.user_id == user_id)):
        raise AppError("对方已经签到")
    db.add(CheckIn(activity_id=activity.id, user_id=user_id, method="manual"))
    db.commit()


def create_announcement(db: Session, actor: User, roles: list[str], title: str, content: str) -> None:
    if not can_announce(actor, roles):
        raise AppError("没有权限发布公告")
    db.add(
        Announcement(
            title=require_text(title, "公告标题", 80),
            content=require_text(content, "公告内容", 4000),
            author_id=actor.id,
        )
    )
    db.commit()


def delete_announcement(db: Session, actor: User, announcement_id: int) -> None:
    row = db.get(Announcement, announcement_id)
    if row is None:
        raise AppError("找不到这条公告")
    if actor.id != row.author_id and not actor.is_admin:
        raise AppError("没有权限删除公告")
    db.delete(row)
    db.commit()
