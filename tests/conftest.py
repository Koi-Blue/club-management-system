import os
import tempfile
from pathlib import Path

_root = Path(tempfile.mkdtemp(prefix="club-test-"))
os.environ["SECRET_KEY"] = "test-secret-key-0123456789"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin123"
os.environ["DATABASE_URL"] = f"sqlite:///{_root / 'club.db'}"
os.environ["UPLOAD_DIR"] = str(_root / "uploads")
os.environ["CLUB_NAME"] = "青禾社"
