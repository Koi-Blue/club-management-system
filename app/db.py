import shutil
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, delete, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    Activity,
    Announcement,
    Asset,
    Base,
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
)
from app.security import hash_password

engine = None
SessionLocal = None
DATA_REVISION = "3"


def setup(settings: Settings) -> None:
    global engine, SessionLocal
    url = settings.database_url
    if url.startswith("sqlite:///"):
        db_path = url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        if url.startswith("sqlite"):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _column_names(table: str) -> set[str]:
    if not inspect(engine).has_table(table):
        return set()
    return {column["name"] for column in inspect(engine).get_columns(table)}


def _add_column(db: Session, table: str, name: str, ddl: str) -> None:
    if name not in _column_names(table):
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db(settings: Settings) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _add_column(db, "users", "college", "VARCHAR(40) DEFAULT ''")
        _add_column(db, "users", "class_name", "VARCHAR(40) DEFAULT ''")
        _add_column(db, "projects", "department", "VARCHAR(40) DEFAULT ''")
        _add_column(db, "activities", "department", "VARCHAR(40) DEFAULT ''")
        db.commit()
        if db.get(Setting, "club_name") is None:
            db.add(Setting(key="club_name", value=settings.club_name))
        admin = db.scalar(select(User).where(User.username == settings.admin_username))
        if admin is None:
            admin = User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                real_name="超级管理员",
                phone="",
                college="",
                class_name="",
                department="",
                status="active",
                is_admin=True,
            )
            db.add(admin)
            db.flush()
        revision = db.get(Setting, "data_revision")
        if revision is None or revision.value != DATA_REVISION:
            _wipe_demo_data(db, settings.upload_dir, admin.id)
            if revision is None:
                db.add(Setting(key="data_revision", value=DATA_REVISION))
            else:
                revision.value = DATA_REVISION
        db.commit()
    finally:
        db.close()


def _wipe_demo_data(db: Session, upload_dir: str, admin_id: int) -> None:
    for model in (
        CheckIn,
        Signup,
        Borrow,
        ProjectFile,
        LibraryFile,
        Project,
        Activity,
        Announcement,
        Asset,
        Meeting,
        LedgerEntry,
        Reimbursement,
        LeaveRequest,
        DutyShift,
    ):
        db.execute(delete(model))
    db.execute(delete(UserRole).where(UserRole.user_id != admin_id))
    db.execute(delete(User).where(User.id != admin_id))
    root = Path(upload_dir)
    for name in ("projects", "library"):
        folder = root / name
        if folder.exists():
            shutil.rmtree(folder)
    folder = root
    folder.mkdir(parents=True, exist_ok=True)
