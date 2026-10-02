"""报销材料只通过报销权限读取，文件和报销单一起提交。"""
import os
from pathlib import Path
import secrets

from sqlalchemy.orm import Session

from app.models import Reimbursement, ReimbursementFile, User
from app.services import (
    AppError,
    MAX_FILE_SIZE,
    invalidate_nav_badges,
    parse_cents,
    require_text,
    safe_path,
    spool_upload,
    upload_slot,
)

MAX_INVOICES = 10
MAX_TOTAL_SIZE = 50 * 1024 * 1024
MEDIA_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def inspect_upload_name(filename: str, kind: str) -> tuple[str, str]:
    original = Path((filename or "").replace("\\", "/")).name
    suffix = Path(original).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png"} | ({".pdf"} if kind == "invoice" else set())
    if suffix not in allowed:
        raise AppError("发票支持 PDF/JPG/PNG，收款二维码支持 JPG/PNG")
    return original[-180:], suffix


def assert_upload_magic(suffix: str, header: bytes) -> None:
    valid = (
        (suffix == ".pdf" and header.startswith(b"%PDF-"))
        or (suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
        or (suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
    )
    if not valid:
        raise AppError("文件内容与格式不符，请上传有效的发票或二维码图片")


def request_reimbursement(db: Session, user: User, upload_dir: str, amount: str, reason: str,
                          invoices: list[tuple[str, object]], qr: tuple[str, object]) -> None:
    cents = parse_cents(amount)
    reason = require_text(reason, "报销事由", 200)
    if not 1 <= len(invoices) <= MAX_INVOICES:
        raise AppError("请上传 1 到 10 份发票")
    items = [("invoice", name, source) for name, source in invoices] + [("qr", qr[0], qr[1])]
    staged: list[Path] = []
    folder = None
    written: list[Path] = []
    try:
        with upload_slot():
            try:
                staging = safe_path(upload_dir, ["_incoming"])
                prepared = []
                total = 0
                for kind, name, source in items:
                    original, suffix = inspect_upload_name(name, kind)
                    temporary, size, header = spool_upload(
                        staging, suffix, source, max_size=MAX_FILE_SIZE,
                        empty_message="文件不能为空，单个文件不能超过 20MB",
                        size_message="文件不能为空，单个文件不能超过 20MB",
                    )
                    staged.append(temporary)
                    total += size
                    if total > MAX_TOTAL_SIZE:
                        raise AppError("本次报销附件合计不能超过 50MB")
                    assert_upload_magic(suffix, header)
                    prepared.append((kind, original, suffix, size, temporary))
                row = Reimbursement(user_id=user.id, amount_cents=cents, reason=reason, status="pending")
                db.add(row)
                db.flush()
                folder = safe_path(upload_dir, ["reimbursements", str(row.id)])
                folder.mkdir(parents=True, exist_ok=True)
                for kind, original, suffix, size, temporary in prepared:
                    stored = secrets.token_hex(16) + suffix
                    destination = folder / stored
                    os.replace(temporary, destination)
                    written.append(destination)
                    db.add(ReimbursementFile(
                        reimbursement_id=row.id, kind=kind, stored_name=stored, original_name=original, size=size,
                    ))
                db.commit()
            except OSError as exc:
                db.rollback()
                for path in written:
                    path.unlink(missing_ok=True)
                if folder is not None and folder.exists() and not any(folder.iterdir()):
                    folder.rmdir()
                raise AppError("附件保存失败，报销未提交，请稍后重试") from exc
            except Exception:
                db.rollback()
                for path in written:
                    path.unlink(missing_ok=True)
                if folder is not None and folder.exists() and not any(folder.iterdir()):
                    folder.rmdir()
                raise
        invalidate_nav_badges(db)
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
