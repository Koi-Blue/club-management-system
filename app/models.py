from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UtcDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(80), default="")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    real_name: Mapped[str] = mapped_column(String(40))
    phone: Mapped[str] = mapped_column(String(20), default="")
    college: Mapped[str] = mapped_column(String(40), default="")
    class_name: Mapped[str] = mapped_column(String(40), default="")
    department: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(40))


class Announcement(Base):
    __tablename__ = "announcements"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str] = mapped_column(String(40), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    leader_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    execution_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="0")
    execution_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)


class ProjectStage(Base):
    __tablename__ = "project_stages"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ProjectTask(Base):
    __tablename__ = "project_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("project_stages.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    planned_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    planned_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_start: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    actual_end: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    deliverable: Mapped[str] = mapped_column(Text, default="")
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="todo", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class TaskCollaborator(Base):
    __tablename__ = "task_collaborators"
    __table_args__ = (UniqueConstraint("task_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("project_tasks.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "predecessor_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("project_tasks.id"), index=True)
    predecessor_id: Mapped[int] = mapped_column(ForeignKey("project_tasks.id"), index=True)


class ProjectMilestone(Base):
    __tablename__ = "project_milestones"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    stage_id: Mapped[int | None] = mapped_column(ForeignKey("project_stages.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(160))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    planned_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_date: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    acceptance_criteria: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="todo")
    version: Mapped[int] = mapped_column(Integer, default=1)


class MilestoneTask(Base):
    __tablename__ = "milestone_tasks"
    __table_args__ = (UniqueConstraint("milestone_id", "task_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    milestone_id: Mapped[int] = mapped_column(ForeignKey("project_milestones.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("project_tasks.id"), index=True)


class ProjectSubmission(Base):
    __tablename__ = "project_submissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("project_tasks.id"), nullable=True, index=True)
    milestone_id: Mapped[int | None] = mapped_column(ForeignKey("project_milestones.id"), nullable=True, index=True)
    submitter_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(Text)
    criteria_snapshot: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    review_comment: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class SubmissionFile(Base):
    __tablename__ = "submission_files"
    __table_args__ = (UniqueConstraint("submission_id", "file_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("project_submissions.id"), index=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("project_files.id"), index=True)


class ProjectEvent(Base):
    __tablename__ = "project_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ProjectFile(Base):
    __tablename__ = "project_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    stored_name: Mapped[str] = mapped_column(String(80))
    original_name: Mapped[str] = mapped_column(String(180))
    size: Mapped[int] = mapped_column(Integer)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    # all：当前项目相关人可见；selected：上传者、立项人和 project_file_viewers
    visibility: Mapped[str] = mapped_column(String(20), default="all", server_default="'all'")


class ProjectFileViewer(Base):
    __tablename__ = "project_file_viewers"
    __table_args__ = (UniqueConstraint("file_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("project_files.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)


class LibraryFile(Base):
    __tablename__ = "library_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    department: Mapped[str] = mapped_column(String(40), index=True)
    stored_name: Mapped[str] = mapped_column(String(80))
    original_name: Mapped[str] = mapped_column(String(180))
    size: Mapped[int] = mapped_column(Integer)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    # approved：按原可见范围展示。旧行没有该列时迁移为 approved，避免突然不可见。
    status: Mapped[str] = mapped_column(String(20), default="approved", server_default="'approved'")
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    delete_requester_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    delete_requested_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(40), default="物资")
    kind: Mapped[str] = mapped_column(String(20), default="instrument", server_default="'instrument'")
    total_qty: Mapped[int] = mapped_column(Integer)
    location: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Borrow(Base):
    __tablename__ = "borrows"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str] = mapped_column(String(200), default="")
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    purpose: Mapped[str] = mapped_column(String(20), default="borrow", server_default="'borrow'")


class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str] = mapped_column(String(40), default="")
    location: Mapped[str] = mapped_column(String(80), default="")
    start_at: Mapped[datetime] = mapped_column(UtcDateTime())
    end_at: Mapped[datetime] = mapped_column(UtcDateTime())
    capacity: Mapped[int] = mapped_column(Integer, default=0)
    checkin_code: Mapped[str] = mapped_column(String(12), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    issued_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class Signup(Base):
    __tablename__ = "signups"
    __table_args__ = (UniqueConstraint("activity_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class CheckIn(Base):
    __tablename__ = "checkins"
    __table_args__ = (UniqueConstraint("activity_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    method: Mapped[str] = mapped_column(String(20))
    checked_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    department: Mapped[str] = mapped_column(String(40), default="")
    held_at: Mapped[str] = mapped_column(String(16), default="")
    location: Mapped[str] = mapped_column(String(80), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    amount_cents: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(40), default="")
    note: Mapped[str] = mapped_column(String(200), default="")
    happened_on: Mapped[str] = mapped_column(String(10))
    recorder_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Reimbursement(Base):
    __tablename__ = "reimbursements"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ReimbursementFile(Base):
    __tablename__ = "reimbursement_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    reimbursement_id: Mapped[int] = mapped_column(ForeignKey("reimbursements.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    stored_name: Mapped[str] = mapped_column(String(80))
    original_name: Mapped[str] = mapped_column(String(180))
    size: Mapped[int] = mapped_column(Integer)


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    start_on: Mapped[str] = mapped_column(String(10))
    end_on: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Journal(Base):
    __tablename__ = "journals"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(80))
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ForumPost(Base):
    __tablename__ = "forum_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(80))
    body: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(20), index=True)
    department: Mapped[str] = mapped_column(String(40), default="")
    direction: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ForumComment(Base):
    __tablename__ = "forum_comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("forum_posts.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class DutyShift(Base):
    __tablename__ = "duty_shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    duty_on: Mapped[str] = mapped_column(String(10), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    note: Mapped[str] = mapped_column(String(120), default="")
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
