import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
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
    Journal,
    LeaveRequest,
    LedgerEntry,
    LibraryFile,
    Meeting,
    PasswordReset,
    Project,
    ProjectFile,
    ProjectFileViewer,
    ProjectMember,
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
    TECH_DIRECTIONS,
    TECH_MEMBERS,
    can_announce,
    can_approve_borrow,
    can_approve_project,
    can_create_activity,
    can_edit_duty,
    can_issue_activity,
    can_see_member_phone,
    has_role,
    home_departments,
    managed_departments,
    sees_all_business,
    sees_all_projects,
    sees_club_operations,
    sees_finance,
)
from app.security import hash_password, verify_password

SHANGHAI = timezone(timedelta(hours=8))
ALLOWED_FILE_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".txt", ".csv",
    ".zip", ".rar", ".7z",
}
MAX_FILE_SIZE = 20 * 1024 * 1024
UPLOAD_CHUNK = 64 * 1024
MAX_CONCURRENT_UPLOADS = 2
BADGE_TTL_SECONDS = 45
CLUB_NAME_TTL_SECONDS = 45
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")
PHONE_RE = re.compile(r"^1\d{10}$")
_cache_lock = threading.Lock()
_upload_slots = threading.BoundedSemaphore(MAX_CONCURRENT_UPLOADS)
_club_name_cache: dict[str, tuple[int, float, str]] = {}
_club_name_gen: dict[str, int] = {}
_badge_cache: dict[str, tuple[int, float, dict]] = {}
_badge_gen: dict[str, int] = {}


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
    if not amount.is_finite() or amount <= 0 or amount > Decimal("10000000"):
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


def _db_scope(db: Session) -> str:
    bind = db.get_bind()
    url = getattr(bind, "url", None)
    if url is None and hasattr(bind, "engine"):
        url = bind.engine.url
    return str(url)


def invalidate_club_name_cache(db: Session) -> None:
    scope = _db_scope(db)
    with _cache_lock:
        _club_name_gen[scope] = _club_name_gen.get(scope, 0) + 1
        _club_name_cache.pop(scope, None)


def invalidate_nav_badges(db: Session) -> None:
    scope = _db_scope(db)
    with _cache_lock:
        _badge_gen[scope] = _badge_gen.get(scope, 0) + 1
        _badge_cache.pop(scope, None)


def club_name(db: Session) -> str:
    scope = _db_scope(db)
    now = time.monotonic()
    with _cache_lock:
        generation = _club_name_gen.get(scope, 0)
        cached = _club_name_cache.get(scope)
        if cached and cached[0] == generation and now - cached[1] < CLUB_NAME_TTL_SECONDS:
            return cached[2]
    row = db.get(Setting, "club_name")
    value = row.value if row and row.value else "社团"
    with _cache_lock:
        if _club_name_gen.get(scope, 0) == generation:
            _club_name_cache[scope] = (generation, time.monotonic(), value)
    return value


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
    invalidate_club_name_cache(db)


def _sort_roles(roles: list[str]) -> list[str]:
    order = {name: index for index, name in enumerate(CLUB_ROLES)}
    return sorted(roles, key=lambda item: order.get(item, 99))


def _role_cache(db: Session) -> dict[int, list[str]]:
    cache = db.info.get("role_cache")
    if cache is None:
        cache = {}
        db.info["role_cache"] = cache
    return cache


def forget_cached_roles(db: Session, user_id: int) -> None:
    cache = db.info.get("role_cache")
    if cache is not None:
        cache.pop(user_id, None)


def roles_of(db: Session, user_id: int) -> list[str]:
    cache = _role_cache(db)
    cached = cache.get(user_id)
    if cached is not None:
        return list(cached)
    rows = db.scalars(select(UserRole.role).where(UserRole.user_id == user_id)).all()
    result = _sort_roles(list(rows))
    cache[user_id] = result
    return list(result)


def roles_of_many(db: Session, user_ids) -> dict[int, list[str]]:
    ids: list[int] = []
    seen: set[int] = set()
    for raw in user_ids:
        if not raw or raw in seen:
            continue
        seen.add(int(raw))
        ids.append(int(raw))
    if not ids:
        return {}
    cache = _role_cache(db)
    missing = [user_id for user_id in ids if user_id not in cache]
    if missing:
        grouped: dict[int, list[str]] = {user_id: [] for user_id in missing}
        rows = db.execute(select(UserRole.user_id, UserRole.role).where(UserRole.user_id.in_(missing))).all()
        for user_id, role in rows:
            grouped.setdefault(int(user_id), []).append(role)
        for user_id in missing:
            cache[user_id] = _sort_roles(grouped.get(user_id, []))
    return {user_id: list(cache.get(user_id, [])) for user_id in ids}


def nav_badge_snapshot(db: Session) -> dict:
    scope = _db_scope(db)
    now = time.monotonic()
    with _cache_lock:
        generation = _badge_gen.get(scope, 0)
        cached = _badge_cache.get(scope)
        if cached and cached[0] == generation and now - cached[1] < BADGE_TTL_SECONDS:
            return cached[2]
    pending_users = int(db.scalar(select(func.count()).select_from(User).where(User.status == "pending")) or 0)
    pending_projects = int(db.scalar(select(func.count()).select_from(Project).where(Project.status == "pending")) or 0)
    pending_borrows = int(db.scalar(select(func.count()).select_from(Borrow).where(Borrow.status == "pending")) or 0)
    pending_reimbursements = int(db.scalar(select(func.count()).select_from(Reimbursement).where(Reimbursement.status == "pending")) or 0)
    pending_leaves = int(db.scalar(select(func.count()).select_from(LeaveRequest).where(LeaveRequest.status == "pending")) or 0)
    draft_rows = db.execute(
        select(Activity.creator_id, func.count()).where(Activity.status == "draft").group_by(Activity.creator_id)
    ).all()
    draft_by_creator = {int(creator_id): int(count) for creator_id, count in draft_rows}
    data = {
        "pending_users": pending_users,
        "pending_projects": pending_projects,
        "pending_borrows": pending_borrows,
        "pending_reimbursements": pending_reimbursements,
        "pending_leaves": pending_leaves,
        "draft_activities": sum(draft_by_creator.values()),
        "draft_by_creator": draft_by_creator,
    }
    with _cache_lock:
        if _badge_gen.get(scope, 0) == generation:
            _badge_cache[scope] = (generation, time.monotonic(), data)
    return data


def pending_member_count(db: Session, user: User) -> int:
    if not user.is_admin:
        return 0
    return nav_badge_snapshot(db)["pending_users"]


def approval_count(db: Session, user: User, roles: list[str]) -> int:
    snap = nav_badge_snapshot(db)
    total = 0
    if user.is_admin:
        total += snap["pending_users"]
    if can_approve_project(user.is_admin, roles):
        total += snap["pending_projects"]
    if can_issue_activity(user.is_admin, roles) or "荣誉社长" in roles:
        if can_issue_activity(user.is_admin, roles):
            total += snap["draft_activities"]
        else:
            total += snap["draft_by_creator"].get(user.id, 0)
    if can_approve_borrow(user.is_admin, roles):
        total += snap["pending_borrows"]
    if sees_finance(user.is_admin, roles):
        total += snap["pending_reimbursements"]
    if user.is_admin or "社长" in roles or "副社长" in roles or "指导老师" in roles or managed_departments(roles):
        total += snap["pending_leaves"]
    return total


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


def project_related(db: Session, user: User, project: Project) -> bool:
    if project.leader_id == user.id:
        return True
    return db.scalar(select(ProjectMember.id).where(ProjectMember.project_id == project.id, ProjectMember.user_id == user.id)) is not None


def can_view_project(db: Session, user: User, roles: list[str], project: Project) -> bool:
    return sees_all_projects(roles) or project_related(db, user, project)


def is_project_leader(user: User, project: Project) -> bool:
    return project.leader_id == user.id


def can_manage_project(db: Session, user: User, roles: list[str], project: Project) -> bool:
    if project.status not in {"draft", "rejected"}:
        return False
    return is_project_leader(user, project)


def can_edit_project_members(db: Session, user: User, roles: list[str], project: Project) -> bool:
    return project.status == "approved" and is_project_leader(user, project)


def can_withdraw_project(db: Session, user: User, project: Project) -> bool:
    return project.status == "pending" and is_project_leader(user, project)


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


def register_user(db: Session, username: str, password: str, real_name: str, phone: str, college: str, class_name: str, department: str, direction: str = "") -> None:
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
    if direction and (department != "技术部" or direction not in dict(TECH_DIRECTIONS)):
        raise AppError("请选择有效的技术方向")
    user = User(
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
    db.add(user)
    if direction:
        db.flush()
        db.add(UserRole(user_id=user.id, role=f"技术部{direction}成员"))
    db.commit()
    invalidate_nav_badges(db)


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
    forget_cached_roles(db, user.id)
    invalidate_nav_badges(db)


def reject_user(db: Session, actor: User, user_id: int) -> None:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以审批注册")
    user = db.get(User, user_id)
    if user is None or user.is_admin:
        raise AppError("找不到这个账号")
    user.status = "rejected"
    db.commit()
    invalidate_nav_badges(db)


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
    invalidate_nav_badges(db)


def delete_user(db: Session, actor: User, user_id: int) -> None:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以删除成员")
    user = db.get(User, user_id)
    if user is None or user.is_admin:
        raise AppError("不能删除超级管理员")
    user.status = "deleted"
    db.commit()
    invalidate_nav_badges(db)


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
    if any(role in TECH_MEMBERS for role in final) and "技术部成员" in final:
        final.remove("技术部成员")
    db.execute(delete(UserRole).where(UserRole.user_id == user.id))
    db.flush()
    for role in final:
        db.add(UserRole(user_id=user.id, role=role))
    db.commit()
    forget_cached_roles(db, user.id)


TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def generate_temporary_password() -> str:
    return "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(12))


def reset_member_password(db: Session, actor: User, user_id: int) -> str:
    if not actor.is_admin:
        raise AppError("只有超级管理员可以重置密码")
    user = db.get(User, user_id)
    if user is None or user.is_admin:
        raise AppError("不能重置超级管理员")
    if user.status not in {"active", "disabled"}:
        raise AppError("只能重置已启用或已停用成员的密码")
    temporary = generate_temporary_password()
    user.password_hash = hash_password(temporary)
    user.must_change_password = True
    db.add(PasswordReset(actor_id=actor.id, target_id=user.id))
    db.commit()
    return temporary


def change_password(db: Session, user: User, current: str, new_password: str, confirm: str, *, skip_current: bool = False) -> None:
    if not skip_current and not verify_password(current, user.password_hash):
        raise AppError("当前密码不正确")
    if new_password != confirm:
        raise AppError("两次输入的新密码不一致")
    if len(new_password) < 6 or len(new_password) > 64:
        raise AppError("新密码需要 6 到 64 位")
    if skip_current and verify_password(new_password, user.password_hash):
        raise AppError("新密码不能与临时密码相同")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()


def project_department_choices(user: User, roles: list[str], current: str = "") -> list[str]:
    if "荣誉社长" in roles or user.is_admin:
        found = list(DEPARTMENTS)
    else:
        found = visible_library_departments(user, roles) or ([user.department] if user.department in DEPARTMENTS else [])
    allowed = set(found)
    if current in DEPARTMENTS:
        allowed.add(current)
    return [dept for dept in DEPARTMENTS if dept in allowed]


def ensure_project_department(user: User, roles: list[str], department: str, *, keeping: str | None = None) -> None:
    if department not in DEPARTMENTS:
        raise AppError("请选择部门")
    if keeping is not None and department == keeping:
        return
    if department not in user_departments(user, roles) and "荣誉社长" not in roles and not user.is_admin:
        raise AppError("只能在自己的部门立项")


def active_member_ids(db: Session, member_ids: list[int]) -> list[int]:
    selected = []
    for raw in member_ids:
        if raw in selected:
            continue
        member = db.get(User, raw)
        if member is None or member.status != "active" or member.is_admin:
            raise AppError("关联成员里有无效账号")
        selected.append(raw)
    if not selected:
        raise AppError("请勾选关联成员")
    return selected


def create_project(db: Session, user: User, roles: list[str], title: str, summary: str, department: str, member_ids: list[int]) -> Project:
    ensure_project_department(user, roles, department)
    selected = active_member_ids(db, member_ids)
    project = Project(
        title=require_text(title, "项目名称", 80),
        summary=optional_text(summary, "项目说明", 4000),
        department=department,
        status="draft",
        leader_id=user.id,
    )
    db.add(project)
    db.flush()
    for member_id in selected:
        db.add(ProjectMember(project_id=project.id, user_id=member_id))
    db.commit()
    db.refresh(project)
    return project


def submit_project(db: Session, user: User, roles: list[str], project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None or not can_manage_project(db, user, roles, project):
        raise AppError("没有权限提交这个立项")
    project.status = "pending"
    project.updated_at = utcnow()
    db.commit()
    invalidate_nav_badges(db)


def _replace_project_members(db: Session, project: Project, member_ids: list[int]) -> None:
    db.execute(delete(ProjectMember).where(ProjectMember.project_id == project.id))
    db.flush()
    for member_id in member_ids:
        db.add(ProjectMember(project_id=project.id, user_id=member_id))


def update_project(db: Session, user: User, roles: list[str], project_id: int, title: str, summary: str, department: str, member_ids: list[int]) -> None:
    project = db.get(Project, project_id)
    if project is None or not is_project_leader(user, project):
        raise AppError("没有权限修改这个立项")
    selected = active_member_ids(db, member_ids)
    if project.status == "approved":
        _replace_project_members(db, project, selected)
        project.updated_at = utcnow()
        db.commit()
        return
    if project.status not in {"draft", "rejected"}:
        raise AppError("当前状态不能修改立项")
    ensure_project_department(user, roles, department, keeping=project.department)
    project.title = require_text(title, "项目名称", 80)
    project.summary = optional_text(summary, "项目说明", 4000)
    project.department = department
    project.updated_at = utcnow()
    _replace_project_members(db, project, selected)
    db.commit()


def withdraw_project(db: Session, user: User, project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None or not is_project_leader(user, project):
        raise AppError("没有权限撤回这个立项")
    if project.status != "pending":
        raise AppError("只有待审批的立项可以撤回")
    project.status = "draft"
    project.reviewer_id = None
    project.review_comment = ""
    project.updated_at = utcnow()
    db.commit()
    invalidate_nav_badges(db)


def delete_project(db: Session, user: User, roles: list[str], upload_dir: str, project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None or not is_project_leader(user, project):
        raise AppError("没有权限删除这个立项")
    if not can_manage_project(db, user, roles, project):
        raise AppError("只有草稿或已驳回的立项可以删除")
    files = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project.id)).all()
    file_ids = [row.id for row in files]
    if file_ids:
        db.execute(delete(ProjectFileViewer).where(ProjectFileViewer.file_id.in_(file_ids)))
    paths = [safe_path(upload_dir, ["projects", str(project.id), row.stored_name]) for row in files]
    for path in paths:
        path.unlink(missing_ok=True)
    for row in files:
        db.delete(row)
    db.execute(delete(ProjectMember).where(ProjectMember.project_id == project.id))
    db.flush()
    folder = safe_path(upload_dir, ["projects", str(project.id)])
    db.delete(project)
    db.commit()
    if folder.is_dir():
        try:
            folder.rmdir()
        except OSError:
            pass


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
    invalidate_nav_badges(db)


def archive_project(db: Session, user: User, roles: list[str], project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None or project.status != "approved":
        raise AppError("只有进行中的立项可以归档")
    if not project_related(db, user, project) and not can_approve_project(user.is_admin, roles):
        raise AppError("没有权限归档")
    project.status = "archived"
    project.updated_at = utcnow()
    db.commit()


@contextmanager
def upload_slot():
    if not _upload_slots.acquire(blocking=False):
        raise AppError("同时上传的文件较多，请稍后再试")
    try:
        yield
    finally:
        _upload_slots.release()


def write_upload_body(path: Path, source, max_size: int) -> tuple[int, bytes]:
    """按固定块把 source 写入 path，超过上限后停止，避免整文件进内存。"""
    header = bytearray()
    size = 0
    with path.open("wb") as handle:
        while True:
            chunk = source.read(UPLOAD_CHUNK)
            if not chunk:
                break
            if isinstance(chunk, str):
                raise AppError("文件读取失败")
            chunk = bytes(chunk)
            size += len(chunk)
            if len(header) < 16:
                header += chunk[: 16 - len(header)]
            handle.write(chunk)
            if size > max_size:
                break
    return size, bytes(header)


def spool_upload(directory: Path, suffix: str, source, *, max_size: int, empty_message: str, size_message: str) -> tuple[Path, int, bytes]:
    seek = getattr(source, "seek", None)
    if callable(seek):
        try:
            seek(0)
        except OSError:
            pass
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, raw_name = tempfile.mkstemp(prefix=".up-", suffix=suffix or ".bin", dir=directory)
    os.close(descriptor)
    path = Path(raw_name)
    try:
        size, header = write_upload_body(path, source, max_size)
        if size <= 0:
            raise AppError(empty_message)
        if size > max_size:
            raise AppError(size_message)
        return path, size, header
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _store_file(upload_dir: str, folder_parts: list[str], filename: str, source) -> tuple[str, str, int]:
    original = Path(filename or "").name
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_FILE_EXT:
        raise AppError("不支持这个文件类型")
    if len(original) > 180:
        original = original[-180:]
    stored = f"{secrets.token_hex(16)}{suffix}"
    folder = Path(upload_dir).joinpath(*folder_parts)
    temporary, size, _header = spool_upload(
        folder, suffix, source, max_size=MAX_FILE_SIZE, empty_message="请选择文件", size_message="单个文件不能超过 20MB",
    )
    try:
        os.replace(temporary, folder / stored)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return stored, original, size


def safe_path(upload_dir: str, parts: list[str]) -> Path:
    root = Path(upload_dir).resolve()
    path = root.joinpath(*parts).resolve()
    if root != path and root not in path.parents:
        raise AppError("文件路径不正确")
    return path


def project_audience_ids(db: Session, project: Project) -> list[int]:
    ids: list[int] = []
    if project.leader_id:
        ids.append(project.leader_id)
    for user_id in project_member_ids(db, project.id):
        if user_id not in ids:
            ids.append(user_id)
    return ids


def project_audience(db: Session, project: Project) -> list[dict]:
    ids = project_audience_ids(db, project)
    rows = db.scalars(select(User).where(User.id.in_(ids))).all() if ids else []
    by_id = {row.id: row for row in rows}
    audience = []
    for user_id in ids:
        person = by_id.get(user_id)
        if person is None:
            label = "已注销"
        else:
            label = f"{person.class_name} {name_of(person)}".strip()
        audience.append({"id": user_id, "name": label})
    return audience


def _file_visible_to(user: User, project: Project, row: ProjectFile, allowed_ids: set[int]) -> bool:
    if user.is_admin or row.uploader_id == user.id or project.leader_id == user.id:
        return True
    if (row.visibility or "all") != "selected":
        return True
    return row.id in allowed_ids


def can_view_project_file(db: Session, user: User, roles: list[str], project: Project, row: ProjectFile) -> bool:
    if row.project_id != project.id or not can_view_project(db, user, roles, project):
        return False
    if user.is_admin or sees_all_projects(roles):
        return True
    if row.uploader_id == user.id or project.leader_id == user.id:
        return True
    if (row.visibility or "all") != "selected":
        return True
    return db.scalar(
        select(ProjectFileViewer.id).where(ProjectFileViewer.file_id == row.id, ProjectFileViewer.user_id == user.id)
    ) is not None


def visible_project_files(db: Session, user: User, roles: list[str], project: Project, files: list[ProjectFile]) -> list[ProjectFile]:
    if not can_view_project(db, user, roles, project):
        return []
    if user.is_admin or sees_all_projects(roles):
        return list(files)
    needs = [
        row.id for row in files
        if (row.visibility or "all") == "selected" and row.uploader_id != user.id and project.leader_id != user.id
    ]
    allowed: set[int] = set()
    if needs:
        allowed = set(db.scalars(select(ProjectFileViewer.file_id).where(
            ProjectFileViewer.file_id.in_(needs), ProjectFileViewer.user_id == user.id,
        )).all())
    return [row for row in files if _file_visible_to(user, project, row, allowed)]


def project_files_for(db: Session, user: User, roles: list[str], project: Project) -> list[dict]:
    rows = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project.id).order_by(ProjectFile.uploaded_at.desc())).all()
    visible = visible_project_files(db, user, roles, project, list(rows))
    selected_ids = [row.id for row in visible if (row.visibility or "all") == "selected"]
    viewer_map: dict[int, list[int]] = {}
    if selected_ids:
        pairs = db.execute(select(ProjectFileViewer.file_id, ProjectFileViewer.user_id).where(ProjectFileViewer.file_id.in_(selected_ids))).all()
        for file_id, user_id in pairs:
            viewer_map.setdefault(file_id, []).append(user_id)
    name_ids = {project.leader_id, *(row.uploader_id for row in visible)}
    for group in viewer_map.values():
        name_ids.update(group)
    names = name_map(db, name_ids)
    listed = []
    for row in visible:
        if (row.visibility or "all") == "selected":
            ordered: list[int] = []
            for user_id in [row.uploader_id, project.leader_id, *viewer_map.get(row.id, [])]:
                if user_id and user_id not in ordered:
                    ordered.append(user_id)
            scope = "、".join(names.get(user_id, "已注销") for user_id in ordered)
        else:
            scope = "全体"
        listed.append({
            "id": row.id,
            "name": row.original_name,
            "uploader": names.get(row.uploader_id, "已注销"),
            "uploaded_at": fmt_dt(row.uploaded_at),
            "size": row.size,
            "scope": scope,
            "can_delete": row.uploader_id == user.id or user.is_admin or sees_all_projects(roles),
        })
    return listed


def save_project_file(db: Session, user: User, roles: list[str], upload_dir: str, project_id: int, filename: str, source, viewer_ids: list[int] | None = None) -> None:
    project = db.get(Project, project_id)
    if project is None or not can_view_project(db, user, roles, project):
        raise AppError("找不到这个立项")
    if not project_related(db, user, project) and not sees_all_projects(roles):
        raise AppError("没有权限上传资料")
    audience = project_audience_ids(db, project)
    allowed = set(audience)
    selected: list[int] = []
    if viewer_ids is not None:
        for raw in viewer_ids:
            if raw not in allowed:
                raise AppError("可见人必须是当前项目成员")
            if raw not in selected:
                selected.append(raw)
        visibility = "all" if set(selected) >= allowed else "selected"
    else:
        visibility = "all"
    with upload_slot():
        stored, original, size = _store_file(upload_dir, ["projects", str(project.id)], filename, source)
    row = ProjectFile(
        project_id=project.id, stored_name=stored, original_name=original, size=size,
        uploader_id=user.id, visibility=visibility,
    )
    db.add(row)
    db.flush()
    if visibility == "selected":
        for user_id in selected:
            db.add(ProjectFileViewer(file_id=row.id, user_id=user_id))
    project.updated_at = utcnow()
    db.commit()


def delete_project_file(db: Session, user: User, roles: list[str], upload_dir: str, project_id: int, file_id: int) -> None:
    row = db.get(ProjectFile, file_id)
    project = db.get(Project, project_id)
    if row is None or project is None or row.project_id != project.id or not can_view_project_file(db, user, roles, project, row):
        raise AppError("找不到这个文件")
    if user.id != row.uploader_id and not user.is_admin and not sees_all_projects(roles):
        raise AppError("没有权限删除这个文件")
    path = safe_path(upload_dir, ["projects", str(project_id), row.stored_name])
    if path.exists():
        path.unlink()
    db.execute(delete(ProjectFileViewer).where(ProjectFileViewer.file_id == row.id))
    db.delete(row)
    db.commit()


def save_library_file(db: Session, user: User, roles: list[str], upload_dir: str, department: str, filename: str, source) -> None:
    if department not in DEPARTMENTS or not can_upload_library(user, roles, department):
        raise AppError("没有权限上传这个部门的资料")
    with upload_slot():
        stored, original, size = _store_file(upload_dir, ["library", department], filename, source)
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


ASSET_KINDS = {"instrument": "仪器", "consumable": "耗材"}


def asset_kind_of(asset: Asset) -> str:
    kind = (getattr(asset, "kind", None) or "").strip()
    if kind not in ASSET_KINDS:
        return "instrument"
    return kind


def normalize_asset_kind(kind: str) -> str:
    value = (kind or "").strip()
    if not value:
        return "instrument"
    if value not in ASSET_KINDS:
        raise AppError("请选择仪器或耗材")
    return value


def require_asset_kind(kind: str) -> str:
    value = (kind or "").strip()
    if value not in ASSET_KINDS:
        raise AppError("请选择仪器或耗材")
    return value


def open_asset_flow_count(db: Session, asset_id: int, purpose: str) -> int:
    total = db.scalar(
        select(func.count()).select_from(Borrow).where(
            Borrow.asset_id == asset_id,
            Borrow.purpose == purpose,
            Borrow.status.in_(("pending", "approved")),
        )
    )
    return int(total or 0)


def change_asset_kind(db: Session, user: User, asset_id: int, kind: str) -> None:
    if not user.is_admin:
        raise AppError("只有超级管理员可以修改资产类型")
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise AppError("找不到这项资产")
    new_kind = require_asset_kind(kind)
    current = asset_kind_of(asset)
    if new_kind == current:
        raise AppError("类型没有变化")
    if new_kind == "consumable" and open_asset_flow_count(db, asset.id, "borrow"):
        raise AppError("还有未归还或待审批的借用，不能改成耗材")
    if new_kind == "instrument" and open_asset_flow_count(db, asset.id, "issue"):
        raise AppError("还有已领用或待审批的领用，不能改成仪器")
    asset.kind = new_kind
    db.commit()


def create_asset(db: Session, user: User, roles: list[str], name: str, category: str, total_qty: int, location: str, description: str, kind: str = "") -> None:
    if not (user.is_admin or has_role(roles, "社长", "副社长") or managed_departments(roles)):
        raise AppError("没有权限登记资产")
    if total_qty < 1 or total_qty > 100000:
        raise AppError("数量需要在 1 到 100000 之间")
    db.add(Asset(
        name=require_text(name, "资产名称", 80),
        category=optional_text(category, "分类", 40) or "物资",
        kind=normalize_asset_kind(kind),
        total_qty=total_qty,
        location=optional_text(location, "存放位置", 80),
        description=optional_text(description, "说明", 1000),
    ))
    db.commit()


def borrowed_qty(db: Session, asset_id: int) -> int:
    # 仪器借用中、耗材已领用都保持 approved，继续占用库存。耗材不归还。
    used = db.scalar(select(func.coalesce(func.sum(Borrow.qty), 0)).where(Borrow.asset_id == asset_id, Borrow.status == "approved"))
    return int(used or 0)


def borrowed_quantities(db: Session, asset_ids) -> dict[int, int]:
    ids: list[int] = []
    seen: set[int] = set()
    for raw in asset_ids:
        if not raw or raw in seen:
            continue
        seen.add(int(raw))
        ids.append(int(raw))
    if not ids:
        return {}
    rows = db.execute(
        select(Borrow.asset_id, func.coalesce(func.sum(Borrow.qty), 0))
        .where(Borrow.asset_id.in_(ids), Borrow.status == "approved")
        .group_by(Borrow.asset_id)
    ).all()
    found = {int(asset_id): int(total or 0) for asset_id, total in rows}
    return {asset_id: found.get(asset_id, 0) for asset_id in ids}


def available_qty(db: Session, asset: Asset) -> int:
    return asset.total_qty - borrowed_qty(db, asset.id)


def available_quantities(db: Session, assets) -> dict[int, int]:
    borrowed = borrowed_quantities(db, [asset.id for asset in assets])
    return {asset.id: asset.total_qty - borrowed.get(asset.id, 0) for asset in assets}


def request_borrow(db: Session, user: User, asset_id: int, qty: int, reason: str, due_raw: str) -> None:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise AppError("找不到这项资产")
    if asset_kind_of(asset) != "instrument":
        raise AppError("耗材不能借用，请走领用")
    if qty < 1:
        raise AppError("借用数量至少为 1")
    due_at = parse_local_dt(due_raw, "预计归还时间")
    if due_at <= utcnow():
        raise AppError("预计归还时间需要晚于现在")
    if qty > available_qty(db, asset):
        raise AppError("可借数量不足")
    db.add(Borrow(
        asset_id=asset.id,
        user_id=user.id,
        qty=qty,
        reason=optional_text(reason, "借用事由", 200),
        status="pending",
        due_at=due_at,
        purpose="borrow",
    ))
    db.commit()
    invalidate_nav_badges(db)


def request_issue(db: Session, user: User, asset_id: int, qty: int, reason: str) -> None:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise AppError("找不到这项资产")
    if asset_kind_of(asset) != "consumable":
        raise AppError("仪器请走借用，并填写预计归还时间")
    if qty < 1:
        raise AppError("领用数量至少为 1")
    if qty > available_qty(db, asset):
        raise AppError("可领数量不足")
    db.add(Borrow(
        asset_id=asset.id,
        user_id=user.id,
        qty=qty,
        reason=optional_text(reason, "领用事由", 200),
        status="pending",
        due_at=None,
        purpose="issue",
    ))
    db.commit()
    invalidate_nav_badges(db)


def review_borrow(db: Session, user: User, roles: list[str], borrow_id: int, decision: str, comment: str) -> str:
    row = db.get(Borrow, borrow_id)
    purpose = (row.purpose if row is not None else None) or "borrow"
    word = "领用" if purpose == "issue" else "借用"
    if not can_approve_borrow(user.is_admin, roles):
        raise AppError(f"没有权限审批{word}")
    if row is None or row.status != "pending":
        raise AppError(f"这条{word}申请不能审批")
    if decision not in {"approved", "rejected"}:
        raise AppError("审批结果不正确")
    asset = db.get(Asset, row.asset_id)
    if decision == "approved":
        stock = "可领" if purpose == "issue" else "可借"
        if asset is None or row.qty > available_qty(db, asset):
            raise AppError(f"{stock}数量不足，无法通过")
        row.approved_at = utcnow()
    row.status = decision
    row.reviewer_id = user.id
    row.review_comment = optional_text(comment, "审批意见", 200)
    db.commit()
    invalidate_nav_badges(db)
    return purpose


def return_borrow(db: Session, user: User, roles: list[str], borrow_id: int) -> None:
    row = db.get(Borrow, borrow_id)
    if row is None or row.status != "approved":
        raise AppError("这条记录不在借用中")
    if (row.purpose or "borrow") == "issue":
        raise AppError("耗材无需归还")
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
    if department != "全社" and department not in managed_departments(roles) and not can_issue_activity(user.is_admin, roles) and "荣誉社长" not in roles and not user.is_admin:
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
    invalidate_nav_badges(db)
    db.refresh(activity)
    return activity


def may_issue_activity(user: User, roles: list[str], activity: Activity) -> bool:
    return can_issue_activity(user.is_admin, roles) or ("荣誉社长" in roles and activity.creator_id == user.id)


def issue_activity(db: Session, user: User, roles: list[str], activity_id: int) -> str:
    activity = db.get(Activity, activity_id)
    if activity is None or not may_issue_activity(user, roles, activity):
        raise AppError("没有权限发放活动")
    if activity.status not in {"draft", "rejected"}:
        raise AppError("当前状态不能发放")
    activity.status = "published"
    activity.issuer_id = user.id
    activity.issued_at = utcnow()
    activity.checkin_code = f"{secrets.randbelow(1000000):06d}"
    activity.review_comment = ""
    db.commit()
    invalidate_nav_badges(db)
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
    invalidate_nav_badges(db)


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
    invalidate_nav_badges(db)


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
    invalidate_nav_badges(db)


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
    invalidate_nav_badges(db)


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


def build_org_tree(db: Session, viewer: User, viewer_roles: list[str] | None = None) -> dict:
    users = db.scalars(select(User).where(User.status == "active", User.is_admin.is_(False)).order_by(User.class_name, User.real_name)).all()
    grouped: dict[str, list[dict]] = {role: [] for role in CLUB_ROLES}
    role_cache = roles_of_many(db, [user.id for user in users])
    if viewer_roles is None:
        viewer_roles = role_cache.get(viewer.id)
        if viewer_roles is None:
            viewer_roles = roles_of(db, viewer.id)
    for user in users:
        target_roles = role_cache[user.id]
        visible_phone = user.phone if can_see_member_phone(viewer.is_admin, viewer_roles, target_roles, viewer.id == user.id) else ""
        person = {
            "real_name": user.real_name,
            "class_name": user.class_name,
            "college": user.college,
            "phone": visible_phone,
            "department": user.department,
        }
        for role in target_roles:
            grouped.setdefault(role, []).append(person)

    def walk(node: tuple) -> dict:
        title, children = node
        return {"title": "技术部成员（待分配方向）" if title == "技术部成员" else title, "people": grouped.get(title, []), "children": [walk(child) for child in children]}

    from app.org import ORG_TREE, ORG_INDEPENDENT
    return {"tree": [walk(node) for node in ORG_TREE], "independent": [walk(node) for node in ORG_INDEPENDENT]}


def project_member_ids(db: Session, project_id: int) -> list[int]:
    return list(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)).all())


def project_member_names(db: Session, project_id: int) -> list[str]:
    ids = project_member_ids(db, project_id)
    names = name_map(db, set(ids))
    return [names.get(item, "已注销") for item in ids]


def save_journal(db: Session, user: User, title: str, body: str, journal_id: int | None = None) -> Journal:
    if journal_id:
        row = db.get(Journal, journal_id)
        if row is None or row.user_id != user.id:
            raise AppError("找不到这篇日记")
    else:
        row = Journal(user_id=user.id, title="", body="")
        db.add(row)
    row.title = require_text(title, "标题", 80)
    row.body = optional_text(body, "正文", 8000)
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return row


def delete_journal(db: Session, user: User, journal_id: int) -> None:
    row = db.get(Journal, journal_id)
    if row is None or row.user_id != user.id:
        raise AppError("找不到这篇日记")
    db.delete(row)
    db.commit()
