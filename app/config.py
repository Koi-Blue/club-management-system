import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    secret_key: str
    database_url: str
    upload_dir: str
    admin_username: str
    admin_password: str
    club_name: str


def load_settings() -> Settings:
    secret = os.environ.get("SECRET_KEY", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if len(secret) < 16:
        raise RuntimeError("SECRET_KEY 至少需要 16 个字符")
    if len(password) < 6:
        raise RuntimeError("ADMIN_PASSWORD 至少需要 6 个字符")
    username = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
    club_name = os.environ.get("CLUB_NAME", "智机逐梦创新协会").strip() or "智机逐梦创新协会"
    return Settings(
        secret_key=secret,
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./data/club.db"),
        upload_dir=os.environ.get("UPLOAD_DIR", "./data/uploads"),
        admin_username=username,
        admin_password=password,
        club_name=club_name,
    )
