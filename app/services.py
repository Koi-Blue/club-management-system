import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

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
    Setting,
    Signup,
    User,
    UserRole,
    utcnow,
)
from app.org import (
    CLUB_ROLES,
    DEPARTMENTS,
    can_announce,
    can_approve_borrow,
    can_approve_project,
    can_create_activity,
    can_edit_duty,
    can_issue_activity,
    has_role,
    home_departments,
    managed_departments,
    sees_all_business,
    sees_club_operations,
    sees_finance,
)
from app.security import hash_password, verify_password

SHANGHAI = timezone(timedelta(hours=8))
ALLOWED_FILE_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".zip", ".txt", ".csv",
}
MAX_FILE_SIZE = 20 * 1024 * 1024
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")
PHONE_RE = re.compile(r"^1\d{10}$")


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


def money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def parse_cents(raw: str) -> int:
    text = (raw or "").strip().replace(",", "")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise AppError("金额格式不正确") from exc
    if amount <= 0 or amount > Decimal("10000000"):
        raise AppError("金额需要大于 0")
    quantized = amount.quantize(Decimal("0.01"))
    if quantized != amount:
        raise AppError("金额最多保留两位小数")
    return int(quantized * 100)


def parse_day(raw: str, label: str) -> str:
    text = (raw or "").strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise AppError(f"{label}格式不正确") from exc
    return text


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
    order = {name: index for index, name in enumerate(CLUB_ROLES)}
    return sorted(rows, key=lambda item: order.get(item, 99))


def name_of(user: User | None) -> str:
    if user is None:
        return "已注销"
    if user.status == "deleted":
        return f"{user.real_name}（已注销）"
    return user.real_name


def name_map(db: Session, user_ids: set[int]) -> dict[int, str]:
    ids = {item for item in user_ids if item}
    if not ids:
        return {}
    rows = db.scalars(select(User).where(User.id.in_(ids))).all()
    return {row.id: name_of(row) for row in rows}


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


def user_departments(user: User, roles: list[str]) -> set[str]:
    return home_departments(user.department, roles)


def can_view_project(user: User, roles: list[str], project: Project) -> bool:
    if sees_all_business(user.is_admin, roles) or project.leader_id == user.id:
        return True
    if project.department in managed_departments(roles):
        return True
    if sees_club_operations(user.is_admin, roles) and project.status != "draft":
        return True
    return project.department in user_departments(user, roles) and project.status in {"approved", "archived"}


def can_manage_project(user: User, roles: list[str], project: Project) -> bool:
    if user.is_admin:
        return True
    if project.status not in {"draft", "rejected"}:
        return False
    return project.leader_id == user.id or project.department in managed_departments(roles)


def can_view_activity(user: User, roles: list[str], activity: Activity) -> bool:
    if activity.status in {"published", "closed"}:
        return True
    if sees_all_business(user.is_admin, roles) or activity.creator_id == user.id:
        return True
    if activity.department in managed_departments(roles):
        return True
    return sees_club_operations(user.is_admin, roles)


def can_view_library(user: User, roles: list[str], department: str) -> bool:
    if sees_all_business(user.is_admin, roles) or has_role(roles, "社长", "副社长", "指导老师"):
        return True
    return department in user_departments(user, roles) or department in managed_departments(roles)


def can_upload_library(user: User, roles: list[str], department: str) -> bool:
    return user.is_admin or "社长" in roles or department in managed_departments(roles)


def can_view_meeting(user: User, roles: list[str], meeting: Meeting) -> bool:
    if not meeting.department or sees_all_business(user.is_admin, roles) or has_role(roles, "社长", "副社长", "指导老师"):
        return True
    return meeting.department in user_departments(user, roles) or meeting.department in managed_departments(roles)


def can_view_leave(user: User, roles: list[str], row: LeaveRequest, owner: User | None) -> bool:
    if sees_all_business(user.is_admin, roles) or row.user_id == user.id or has_role(roles, "社长", "副社长", "指导老师"):
        return True
    department = owner.department if owner else ""
    return department in managed_departments(roles)


def can_approve_leave(user: User, roles: list[str], owner: User | None) -> bool:
    if user.is_admin or has_role(roles, "社长", "副社长", "指导老师"):
        return True
    department = owner.department if owner else ""
    return department in managed_departments(roles)


def visible_library_departments(user: User, roles: list[str]) -> list[str]:
    if sees_all_business(user.is_admin, roles) or has_role(roles, "社长", "副社长", "指导老师"):
        return list(DEPARTMENTS)
    found = user_departments(user, roles) | managed_departments(roles)
    return [dept for dept in DEPARTMENTS if dept in found]


def register_user(db: Session, username: str, password: str, real_name: str, phone: str, college: str, class_name: str, department: str) -> None:
    username = username.strip()
    phone = phone.strip()
    if not USERNAME_RE.match(username):
        raise AppError("用户名需为 3 到 32 位中文、字母、数字或下划线")
    if not PHONE_RE.match(phone):
        raise AppError("请填写 11 位手机号")
    if len(password) < 6 or len(password) > 64:
        raise AppError("密码需要 6 到 64 位")
    if department not in DEPARTMENTS:
        raise AppError("请选择部门")
    if db.scalar(select(User).where(User.username == username)):
        raise AppError("这个用户名已经注册")
    if db.scalar(select(User).where(User.phone == phone)):
        raise AppError("这个手机号已经注册")
    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            real_name=require_text(real_name, "姓名", 40),
            phone=phone,
            college=require_text(college, "学院", 40),
            class_name=require_text(class_name, "班级", 40),
            department=department,
            status="pending",
            is_admin=False,
        )
    )
    db.commit()


def authenticate(db: Session, account: str, password: str) -> User:
    account = account.strip()
    user = db.scalar(select(User).where(User.username == account))
    if user is None and account:
        user = db.scalar(select(User).where(User.phone == account))
    if user is None or not verify_password(password, user.password_hash):
        raise AppError("账号或密码不正确")
    if user.status == "pending":
        raise AppError("账号正在等待超级管理员审批")
    if user.status == "rejected":
        raise AppError("账号未通过审批，请联系超级管理员")
    if user.status == "deleted":
        raise AppError("账号已注销")
    if user.status != "active":
        raise AppError("账号已停用")
    return user


def approve_user(db: Session, actor: User, user_id: int) -> None:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以审批注册")
    user = db.get(User, user_id)
    if user is None or user.is_admin or user.status == "deleted":
        raise AppError("找不到这个账号")
    user.status = "active"
    if user.department in DEPARTMENTS and not roles_of(db, user.id):
        db.add(UserRole(user_id=user.id, role=f"{user.department}成员"))
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
    user = db.get(User, user_id)
    if user is None or user.is_admin:
        raise AppError("不能修改超级管理员")
    if status not in {"active", "disabled"}:
        raise AppError("状态不正确")
    user.status = status
    db.commit()


def delete_user(db: Session, actor: User, user_id: int) -> None:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以删除成员")
    user = db.get(User, user_id)
    if user is None or user.is_admin:
        raise AppError("不能删除超级管理员")
    user.status = "deleted"
    db.commit()


def set_roles(db: Session, actor: User, user_id: int, selected: list[str]) -> None:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以分配职务")
    user = db.get(User, user_id)
    if user is None or user.status != "active" or user.is_admin:
        raise AppError("只能给已启用的成员分配职务")
    final = []
    for role in selected:
        if role in CLUB_ROLES and role not in final:
            final.append(role)
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


def create_project(db: Session, user: User, roles: list[str], title: str, summary: str, department: str) -> Project:
    if department not in DEPARTMENTS:
        raise AppError("请选择部门")
    if department not in user_departments(user, roles) and not sees_club_operations(user.is_admin, roles) and not user.is_admin:
        raise AppError("只能在自己的部门立项")
    project = Project(
        title=require_text(title, "项目名称", 80),
        summary=optional_text(summary, "项目说明", 4000),
        department=department,
        status="draft",
        leader_id=user.id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def submit_project(db: Session, user: User, roles: list[str], project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None or not can_manage_project(user, roles, project):
        raise AppError("没有权限提交这个立项")
    project.status = "pending"
    project.updated_at = utcnow()
    db.commit()


def review_project(db: Session, user: User, roles: list[str], project_id: int, decision: str, comment: str) -> None:
    if not can_approve_project(user.is_admin, roles):
        raise AppError("没有权限审批立项")
    project = db.get(Project, project_id)
    if project is None or project.status != "pending":
        raise AppError("当前状态不能审批")
    if decision not in {"approved", "rejected"}:
        raise AppError("审批结果不正确")
    project.status = decision
    project.reviewer_id = user.id
    project.review_comment = optional_text(comment, "审批意见", 200)
    project.updated_at = utcnow()
    db.commit()


def archive_project(db: Session, user: User, roles: list[str], project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None or project.status != "approved":
        raise AppError("只有进行中的立项可以归档")
    if project.leader_id != user.id and not can_approve_project(user.is_admin, roles) and project.department not in managed_departments(roles):
        raise AppError("没有权限归档")
    project.status = "archived"
    project.updated_at = utcnow()
    db.commit()


def _store_file(upload_dir: str, folder_parts: list[str], filename: str, content: bytes) -> tuple[str, str, int]:
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
    folder = Path(upload_dir).joinpath(*folder_parts)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / stored).write_bytes(content)
    return stored, original, len(content)


def safe_path(upload_dir: str, parts: list[str]) -> Path:
    root = Path(upload_dir).resolve()
    path = root.joinpath(*parts).resolve()
    if root != path and root not in path.parents:
        raise AppError("文件路径不正确")
    return path


def save_project_file(db: Session, user: User, roles: list[str], upload_dir: str, project_id: int, filename: str, content: bytes) -> None:
    project = db.get(Project, project_id)
    if project is None or not can_view_project(user, roles, project):
        raise AppError("找不到这个立项")
    if not can_manage_project(user, roles, project) and project.status == "draft":
        raise AppError("没有权限上传资料")
    if project.leader_id != user.id and project.department not in managed_departments(roles) and not user.is_admin and not can_approve_project(user.is_admin, roles):
        if project.status == "draft":
            raise AppError("没有权限上传资料")
    stored, original, size = _store_file(upload_dir, ["projects", str(project.id)], filename, content)
    db.add(ProjectFile(project_id=project.id, stored_name=stored, original_name=original, size=size, uploader_id=user.id))
    project.updated_at = utcnow()
    db.commit()


def delete_project_file(db: Session, user: User, roles: list[str], upload_dir: str, project_id: int, file_id: int) -> None:
    row = db.get(ProjectFile, file_id)
    project = db.get(Project, project_id)
    if row is None or project is None or row.project_id != project.id or not can_view_project(user, roles, project):
        raise AppError("找不到这个文件")
    if user.id != row.uploader_id and not user.is_admin and project.department not in managed_departments(roles):
        raise AppError("没有权限删除这个文件")
    path = safe_path(upload_dir, ["projects", str(project_id), row.stored_name])
    if path.exists():
        path.unlink()
    db.delete(row)
    db.commit()


def save_library_file(db: Session, user: User, roles: list[str], upload_dir: str, department: str, filename: str, content: bytes) -> None:
    if department not in DEPARTMENTS or not can_upload_library(user, roles, department):
        raise AppError("没有权限上传这个部门的资料")
    stored, original, size = _store_file(upload_dir, ["library", department], filename, content)
    db.add(LibraryFile(department=department, stored_name=stored, original_name=original, size=size, uploader_id=user.id))
    db.commit()


def delete_library_file(db: Session, user: User, roles: list[str], upload_dir: str, file_id: int) -> None:
    row = db.get(LibraryFile, file_id)
    if row is None or not can_view_library(user, roles, row.department):
        raise AppError("找不到这个文件")
    if user.id != row.uploader_id and not can_upload_library(user, roles, row.department):
        raise AppError("没有权限删除这个文件")
    path = safe_path(upload_dir, ["library", row.department, row.stored_name])
    if path.exists():
        path.unlink()
    db.delete(row)
    db.commit()


def create_asset(db: Session, user: User, roles: list[str], name: str, category: str, total_qty: int, location: str, description: str) -> None:
    if not (user.is_admin or has_role(roles, "社长", "副社长") or managed_departments(roles)):
        raise AppError("没有权限登记资产")
    if total_qty < 1 or total_qty > 100000:
        raise AppError("数量需要在 1 到 100000 之间")
    db.add(Asset(
        name=require_text(name, "资产名称", 80),
        category=optional_text(category, "分类", 40) or "物资",
        total_qty=total_qty,
        location=optional_text(location, "存放位置", 80),
        description=optional_text(description, "说明", 1000),
    ))
    db.commit()


def borrowed_qty(db: Session, asset_id: int) -> int:
    used = db.scalar(select(func.coalesce(func.sum(Borrow.qty), 0)).where(Borrow.asset_id == asset_id, Borrow.status == "approved"))
    return int(used or 0)


def available_qty(db: Session, asset: Asset) -> int:
    return asset.total_qty - borrowed_qty(db, asset.id)


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
    db.add(Borrow(asset_id=asset.id, user_id=user.id, qty=qty, reason=optional_text(reason, "借用事由", 200), status="pending", due_at=due_at))
    db.commit()


def review_borrow(db: Session, user: User, roles: list[str], borrow_id: int, decision: str, comment: str) -> None:
    if not can_approve_borrow(user.is_admin, roles):
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
    row.reviewer_id = user.id
    row.review_comment = optional_text(comment, "审批意见", 200)
    db.commit()


def return_borrow(db: Session, user: User, roles: list[str], borrow_id: int) -> None:
    row = db.get(Borrow, borrow_id)
    if row is None or row.status != "approved":
        raise AppError("这条记录不在借用中")
    if user.id != row.user_id and not can_approve_borrow(user.is_admin, roles):
        raise AppError("没有权限登记归还")
    row.status = "returned"
    row.returned_at = utcnow()
    db.commit()


def can_view_borrow(user: User, roles: list[str], row: Borrow) -> bool:
    return row.user_id == user.id or sees_all_business(user.is_admin, roles) or can_approve_borrow(user.is_admin, roles)


def create_activity(db: Session, user: User, roles: list[str], title: str, description: str, department: str, location: str, start_raw: str, end_raw: str, capacity: int) -> Activity:
    if not can_create_activity(user.is_admin, roles):
        raise AppError("没有权限创建活动")
    if department not in DEPARTMENTS and department != "全社":
        raise AppError("请选择活动范围")
    if department != "全社" and department not in managed_departments(roles) and not can_issue_activity(user.is_admin, roles) and not user.is_admin:
        raise AppError("只能创建本部门或全社活动")
    start_at = parse_local_dt(start_raw, "开始时间")
    end_at = parse_local_dt(end_raw, "结束时间")
    if end_at <= start_at:
        raise AppError("结束时间需要晚于开始时间")
    if capacity < 0 or capacity > 100000:
        raise AppError("人数上限不正确")
    activity = Activity(
        title=require_text(title, "活动名称", 80),
        description=optional_text(description, "活动说明", 4000),
        department=department,
        location=optional_text(location, "地点", 80),
        start_at=start_at,
        end_at=end_at,
        capacity=capacity,
        status="draft",
        creator_id=user.id,
    )
    db.add(activity)
    db.commit()
    db.refresh(activity)
    return activity


def issue_activity(db: Session, user: User, roles: list[str], activity_id: int) -> str:
    if not can_issue_activity(user.is_admin, roles):
        raise AppError("没有权限发放活动")
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status not in {"draft", "rejected"}:
        raise AppError("当前状态不能发放")
    activity.status = "published"
    activity.issuer_id = user.id
    activity.issued_at = utcnow()
    activity.checkin_code = f"{secrets.randbelow(1000000):06d}"
    activity.review_comment = ""
    db.commit()
    return activity.checkin_code


def reject_activity(db: Session, user: User, roles: list[str], activity_id: int, comment: str) -> None:
    if not can_issue_activity(user.is_admin, roles):
        raise AppError("没有权限驳回活动")
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status != "draft":
        raise AppError("当前状态不能驳回")
    activity.status = "rejected"
    activity.issuer_id = user.id
    activity.review_comment = optional_text(comment, "驳回意见", 200)
    db.commit()


def close_activity(db: Session, user: User, roles: list[str], activity_id: int) -> None:
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status != "published":
        raise AppError("只有已发放的活动可以结束")
    if activity.creator_id != user.id and not can_issue_activity(user.is_admin, roles):
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
    if db.scalar(select(Signup).where(Signup.activity_id == activity.id, Signup.user_id == user.id)):
        raise AppError("你已经报名了")
    if activity.capacity and signup_count(db, activity.id) >= activity.capacity:
        raise AppError("报名人数已满")
    db.add(Signup(activity_id=activity.id, user_id=user.id))
    db.commit()


def cancel_signup(db: Session, user: User, activity_id: int) -> None:
    row = db.scalar(select(Signup).where(Signup.activity_id == activity_id, Signup.user_id == user.id))
    if row is None:
        raise AppError("你还没有报名")
    if db.scalar(select(CheckIn).where(CheckIn.activity_id == activity_id, CheckIn.user_id == user.id)):
        raise AppError("已签到后不能取消报名")
    db.delete(row)
    db.commit()


def checkin_open(activity: Activity) -> bool:
    if activity.status != "published":
        return False
    now = utcnow()
    return as_utc(activity.start_at) - timedelta(minutes=30) <= now <= as_utc(activity.end_at)


def self_checkin(db: Session, user: User, activity_id: int, code: str) -> None:
    activity = db.get(Activity, activity_id)
    if activity is None or activity.status != "published":
        raise AppError("活动当前不能签到")
    if not checkin_open(activity):
        raise AppError("不在签到时间内")
    if db.scalar(select(Signup).where(Signup.activity_id == activity.id, Signup.user_id == user.id)) is None:
        raise AppError("请先报名再签到")
    if db.scalar(select(CheckIn).where(CheckIn.activity_id == activity.id, CheckIn.user_id == user.id)):
        raise AppError("你已经签到")
    if (code or "").strip() != activity.checkin_code:
        raise AppError("签到码不正确")
    db.add(CheckIn(activity_id=activity.id, user_id=user.id, method="code"))
    db.commit()


def manual_checkin(db: Session, user: User, roles: list[str], activity_id: int, user_id: int) -> None:
    activity = db.get(Activity, activity_id)
    if activity is None or not can_view_activity(user, roles, activity):
        raise AppError("找不到这场活动")
    if not (user.is_admin or can_issue_activity(user.is_admin, roles) or activity.department in managed_departments(roles) or activity.creator_id == user.id):
        raise AppError("没有权限补签")
    if activity.status not in {"published", "closed"}:
        raise AppError("活动当前不能补签")
    if db.scalar(select(Signup).where(Signup.activity_id == activity.id, Signup.user_id == user_id)) is None:
        raise AppError("对方还没有报名")
    if db.scalar(select(CheckIn).where(CheckIn.activity_id == activity.id, CheckIn.user_id == user_id)):
        raise AppError("对方已经签到")
    db.add(CheckIn(activity_id=activity.id, user_id=user_id, method="manual"))
    db.commit()


def can_see_checkin_code(user: User, roles: list[str], activity: Activity) -> bool:
    return activity.creator_id == user.id or can_issue_activity(user.is_admin, roles) or activity.department in managed_departments(roles)


def create_announcement(db: Session, user: User, roles: list[str], title: str, content: str) -> None:
    if not can_announce(user.is_admin, roles):
        raise AppError("没有权限发布公告")
    db.add(Announcement(title=require_text(title, "公告标题", 80), content=require_text(content, "公告内容", 4000), author_id=user.id))
    db.commit()


def delete_announcement(db: Session, user: User, announcement_id: int) -> None:
    row = db.get(Announcement, announcement_id)
    if row is None:
        raise AppError("找不到这条公告")
    if user.id != row.author_id and not user.is_admin:
        raise AppError("没有权限删除公告")
    db.delete(row)
    db.commit()


def create_meeting(db: Session, user: User, roles: list[str], title: str, department: str, held_at: str, location: str, content: str) -> None:
    if department not in DEPARTMENTS and department != "":
        raise AppError("请选择会议范围")
    if department and department not in managed_departments(roles) and not sees_club_operations(user.is_admin, roles) and not user.is_admin:
        raise AppError("没有权限记录这个范围的会议")
    if not department and not (user.is_admin or sees_club_operations(user.is_admin, roles) or managed_departments(roles)):
        raise AppError("没有权限记录全社会议")
    db.add(Meeting(
        title=require_text(title, "会议主题", 80),
        department=department,
        held_at=require_text(held_at, "会议时间", 16),
        location=optional_text(location, "地点", 80),
        content=require_text(content, "纪要", 4000),
        author_id=user.id,
    ))
    db.commit()


def delete_meeting(db: Session, user: User, roles: list[str], meeting_id: int) -> None:
    row = db.get(Meeting, meeting_id)
    if row is None or not can_view_meeting(user, roles, row):
        raise AppError("找不到这条纪要")
    if user.id != row.author_id and not user.is_admin and "社长" not in roles:
        raise AppError("没有权限删除纪要")
    db.delete(row)
    db.commit()


def add_ledger(db: Session, user: User, roles: list[str], kind: str, amount: str, category: str, note: str, happened_on: str) -> None:
    if not sees_finance(user.is_admin, roles):
        raise AppError("没有权限登记财务")
    if kind not in {"income", "expense"}:
        raise AppError("请选择收入或支出")
    db.add(LedgerEntry(
        kind=kind,
        amount_cents=parse_cents(amount),
        category=optional_text(category, "分类", 40) or "其他",
        note=optional_text(note, "备注", 200),
        happened_on=parse_day(happened_on, "日期"),
        recorder_id=user.id,
    ))
    db.commit()


def request_reimbursement(db: Session, user: User, amount: str, reason: str) -> None:
    db.add(Reimbursement(user_id=user.id, amount_cents=parse_cents(amount), reason=require_text(reason, "报销事由", 200), status="pending"))
    db.commit()


def review_reimbursement(db: Session, user: User, roles: list[str], item_id: int, decision: str, comment: str) -> None:
    if not sees_finance(user.is_admin, roles):
        raise AppError("没有权限审批报销")
    row = db.get(Reimbursement, item_id)
    if row is None or row.status != "pending":
        raise AppError("这条报销不能审批")
    if decision not in {"approved", "rejected"}:
        raise AppError("审批结果不正确")
    row.status = decision
    row.reviewer_id = user.id
    row.review_comment = optional_text(comment, "审批意见", 200)
    db.commit()


def pay_reimbursement(db: Session, user: User, roles: list[str], item_id: int) -> None:
    if not sees_finance(user.is_admin, roles):
        raise AppError("没有权限登记付款")
    row = db.get(Reimbursement, item_id)
    if row is None or row.status != "approved":
        raise AppError("只有已通过的报销可以登记付款")
    row.status = "paid"
    row.reviewer_id = user.id
    db.add(LedgerEntry(
        kind="expense",
        amount_cents=row.amount_cents,
        category="报销",
        note=row.reason[:200],
        happened_on=datetime.now(SHANGHAI).strftime("%Y-%m-%d"),
        recorder_id=user.id,
    ))
    db.commit()


def can_view_reimbursement(user: User, roles: list[str], row: Reimbursement) -> bool:
    return row.user_id == user.id or sees_finance(user.is_admin, roles)


def request_leave(db: Session, user: User, start_on: str, end_on: str, reason: str) -> None:
    start = parse_day(start_on, "开始日期")
    end = parse_day(end_on, "结束日期")
    if end < start:
        raise AppError("结束日期不能早于开始日期")
    db.add(LeaveRequest(user_id=user.id, start_on=start, end_on=end, reason=require_text(reason, "请假事由", 200), status="pending"))
    db.commit()


def review_leave(db: Session, user: User, roles: list[str], item_id: int, decision: str, comment: str) -> None:
    row = db.get(LeaveRequest, item_id)
    owner = db.get(User, row.user_id) if row else None
    if row is None or not can_view_leave(user, roles, row, owner):
        raise AppError("找不到这条请假")
    if not can_approve_leave(user, roles, owner):
        raise AppError("没有权限审批请假")
    if row.status != "pending":
        raise AppError("这条请假已经处理")
    if row.user_id == user.id and not user.is_admin:
        raise AppError("不能审批自己的请假")
    if decision not in {"approved", "rejected"}:
        raise AppError("审批结果不正确")
    row.status = decision
    row.reviewer_id = user.id
    row.review_comment = optional_text(comment, "审批意见", 200)
    db.commit()


def create_duty(db: Session, user: User, roles: list[str], duty_on: str, user_id: int, note: str) -> None:
    if not can_edit_duty(user.is_admin, roles):
        raise AppError("没有权限安排值班")
    target = db.get(User, user_id)
    if target is None or target.status != "active" or target.is_admin:
        raise AppError("请选择在册成员")
    db.add(DutyShift(duty_on=parse_day(duty_on, "值班日期"), user_id=target.id, note=optional_text(note, "说明", 120), creator_id=user.id))
    db.commit()


def delete_duty(db: Session, user: User, roles: list[str], duty_id: int) -> None:
    if not can_edit_duty(user.is_admin, roles):
        raise AppError("没有权限删除值班")
    row = db.get(DutyShift, duty_id)
    if row is None:
        raise AppError("找不到这条值班")
    db.delete(row)
    db.commit()


def build_org_tree(db: Session) -> list[dict]:
    users = db.scalars(select(User).where(User.status == "active", User.is_admin.is_(False)).order_by(User.class_name, User.real_name)).all()
    grouped: dict[str, list[dict]] = {role: [] for role in CLUB_ROLES}
    for user in users:
        person = {
            "real_name": user.real_name,
            "class_name": user.class_name,
            "college": user.college,
            "phone": user.phone,
            "department": user.department,
        }
        for role in roles_of(db, user.id):
            grouped.setdefault(role, []).append(person)

    def walk(node: tuple) -> dict:
        title, children = node
        return {"title": title, "people": grouped.get(title, []), "children": [walk(child) for child in children]}

    from app.org import ORG_TREE
    return [walk(node) for node in ORG_TREE]
