from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Base, Setting, User
from app.security import hash_password

engine = None
SessionLocal = None


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


def init_db(settings: Settings) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.scalar(select(Setting).where(Setting.key == "club_name")) is None:
            db.add(Setting(key="club_name", value=settings.club_name))
        admin = db.scalar(select(User).where(User.username == settings.admin_username))
        if admin is None:
            db.add(
                User(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    real_name="超级管理员",
                    phone="",
                    department="",
                    status="active",
                    is_admin=True,
                )
            )
        db.commit()
    finally:
        db.close()
