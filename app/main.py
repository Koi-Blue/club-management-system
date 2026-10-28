from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import load_settings
from app.db import init_db, setup
from app.routes import register_routes


def create_app() -> FastAPI:
    settings = load_settings()
    setup(settings)
    init_db(settings)
    app = FastAPI(title="社团管理系统", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="club_session",
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 24 * 14,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
    register_routes(app, settings)
    return app


app = create_app()
