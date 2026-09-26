from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  让全部表注册到 Base.metadata
from app.config import Settings
from app.models import Base, Setting, User
from app.security import hash_password

engine = None
SessionLocal = None
PLACEHOLDER_CLUB_NAMES = {"", "社团"}


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
        stored_name = db.get(Setting, "club_name")
        if stored_name is None:
            db.add(Setting(key="club_name", value=settings.club_name))
        elif stored_name.value in PLACEHOLDER_CLUB_NAMES and settings.club_name not in PLACEHOLDER_CLUB_NAMES:
            stored_name.value = settings.club_name
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
        db.commit()
    finally:
        db.close()
