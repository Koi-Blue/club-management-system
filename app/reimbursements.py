"""报销材料只通过报销权限读取，文件和报销单一起提交。"""
from pathlib import Path
import secrets

from sqlalchemy.orm import Session

from app.models import Reimbursement, ReimbursementFile, User
from app.services import AppError, MAX_FILE_SIZE, parse_cents, require_text, safe_path

MAX_INVOICES = 10
MAX_TOTAL_SIZE = 50 * 1024 * 1024
MEDIA_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def validate_file(filename: str, content: bytes, kind: str) -> tuple[str, str]:
    original = Path((filename or "").replace("\\", "/")).name
    suffix = Path(original).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png"} | ({".pdf"} if kind == "invoice" else set())
    if suffix not in allowed:
        raise AppError("发票支持 PDF/JPG/PNG，收款二维码支持 JPG/PNG")
    if not content or len(content) > MAX_FILE_SIZE:
        raise AppError("文件不能为空，单个文件不能超过 20MB")
    valid = (suffix == ".pdf" and content.startswith(b"%PDF-")) or (suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n")) or (suffix in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"))
    if not valid:
        raise AppError("文件内容与格式不符，请上传有效的发票或二维码图片")
    return original[-180:], suffix


def request_reimbursement(db: Session, user: User, upload_dir: str, amount: str, reason: str,
                          invoices: list[tuple[str, bytes]], qr: tuple[str, bytes]) -> None:
    cents = parse_cents(amount)
    reason = require_text(reason, "报销事由", 200)
    if not 1 <= len(invoices) <= MAX_INVOICES:
        raise AppError("请上传 1 到 10 份发票")
    files = [("invoice", *item) for item in invoices] + [("qr", *qr)]
    if sum(len(content) for _, _, content in files) > MAX_TOTAL_SIZE:
        raise AppError("本次报销附件合计不能超过 50MB")
    validated = [(kind, *validate_file(name, content, kind), content) for kind, name, content in files]
    row = Reimbursement(user_id=user.id, amount_cents=cents, reason=reason, status="pending")
    written: list[Path] = []
    folder = None
    try:
        db.add(row)
        db.flush()
        folder = safe_path(upload_dir, ["reimbursements", str(row.id)])
        folder.mkdir(parents=True, exist_ok=True)
        for kind, original, suffix, content in validated:
            stored = secrets.token_hex(16) + suffix
            path = folder / stored
            written.append(path)
            path.write_bytes(content)
            db.add(ReimbursementFile(reimbursement_id=row.id, kind=kind, stored_name=stored,
                                     original_name=original, size=len(content)))
        db.commit()
    except Exception as exc:
        db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        if folder is not None and folder.exists() and not any(folder.iterdir()):
            folder.rmdir()
        if isinstance(exc, OSError):
            raise AppError("附件保存失败，报销未提交，请稍后重试") from exc
        raise
