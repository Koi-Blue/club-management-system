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


def _relax_borrow_due_at() -> None:
    """SQLite 不能直接去掉 NOT NULL。旧库的预计归还时间改为可空，耗材领用才不必填写。"""
    if engine is None or not inspect(engine).has_table("borrows"):
        return
    with engine.connect() as conn:
        info = list(conn.execute(text("PRAGMA table_info(borrows)")).mappings())
        index_rows = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='borrows' AND sql IS NOT NULL"
        )).all()
    due = next((row for row in info if row["name"] == "due_at"), None)
    if due is None or not int(due["notnull"]):
        return
    names = {row["name"] for row in info}
    required = {
        "id", "asset_id", "user_id", "qty", "reason", "status",
        "reviewer_id", "review_comment", "due_at", "created_at",
        "approved_at", "returned_at",
    }
    missing = sorted(required - names)
    if missing:
        raise RuntimeError("borrows 表缺少字段，无法迁移预计归还时间：" + "、".join(missing))
    purpose_expr = "purpose" if "purpose" in names else "'borrow'"
    index_sql = [row[0] for row in index_rows if row[0]]
    raw = engine.raw_connection()
    cur = raw.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        if cur.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError("无法暂时关闭外键检查，停止迁移借用表")
        cur.execute("BEGIN")
        cur.execute("DROP TABLE IF EXISTS borrows__due_nullable")
        cur.execute(
            """
            CREATE TABLE borrows__due_nullable (
                id INTEGER NOT NULL PRIMARY KEY,
                asset_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                qty INTEGER NOT NULL,
                reason VARCHAR(200) NOT NULL DEFAULT '',
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                reviewer_id INTEGER,
                review_comment VARCHAR(200) NOT NULL DEFAULT '',
                due_at DATETIME,
                created_at DATETIME NOT NULL,
                approved_at DATETIME,
                returned_at DATETIME,
                purpose VARCHAR(20) NOT NULL DEFAULT 'borrow',
                FOREIGN KEY(asset_id) REFERENCES assets (id),
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(reviewer_id) REFERENCES users (id)
            )
            """
        )
        cur.execute(
            f"""
            INSERT INTO borrows__due_nullable (
                id, asset_id, user_id, qty, reason, status, reviewer_id, review_comment,
                due_at, created_at, approved_at, returned_at, purpose
            )
            SELECT
                id, asset_id, user_id, qty,
                COALESCE(reason, ''),
                COALESCE(status, 'pending'),
                reviewer_id,
                COALESCE(review_comment, ''),
                due_at,
                COALESCE(created_at, CURRENT_TIMESTAMP),
                approved_at,
                returned_at,
                COALESCE({purpose_expr}, 'borrow')
            FROM borrows
            """
        )
        cur.execute("DROP TABLE borrows")
        cur.execute("ALTER TABLE borrows__due_nullable RENAME TO borrows")
        for statement in index_sql:
            cur.execute(statement)
        cur.execute("COMMIT")
    except Exception:
        try:
            raw.dbapi_connection.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            raw.cursor().execute("PRAGMA foreign_keys=ON")
        finally:
            raw.close()
    inspect(engine).clear_cache()
    engine.dispose()


def init_db(settings: Settings) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _add_column(db, "users", "college", "VARCHAR(40) DEFAULT ''")
        _add_column(db, "users", "class_name", "VARCHAR(40) DEFAULT ''")
        _add_column(db, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
        _add_column(db, "projects", "department", "VARCHAR(40) DEFAULT ''")
        _add_column(db, "project_files", "visibility", "VARCHAR(20) NOT NULL DEFAULT 'all'")
        _add_column(db, "activities", "department", "VARCHAR(40) DEFAULT ''")
        _add_column(db, "assets", "kind", "VARCHAR(20) NOT NULL DEFAULT 'instrument'")
        _add_column(db, "borrows", "purpose", "VARCHAR(20) NOT NULL DEFAULT 'borrow'")
        _add_column(db, "library_files", "status", "VARCHAR(20) NOT NULL DEFAULT 'approved'")
        _add_column(db, "library_files", "reviewer_id", "INTEGER")
        _add_column(db, "library_files", "reviewed_at", "DATETIME")
        _add_column(db, "library_files", "delete_requester_id", "INTEGER")
        _add_column(db, "library_files", "delete_requested_at", "DATETIME")
        db.commit()
    finally:
        db.close()
    _relax_borrow_due_at()
    db = SessionLocal()
    try:
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
        from app.services import invalidate_club_name_cache
        invalidate_club_name_cache(db)
    finally:
        db.close()
