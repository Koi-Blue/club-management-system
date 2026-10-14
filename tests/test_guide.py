"""系统使用说明书入口、目录和示意图。"""
import re

from fastapi.testclient import TestClient

from app.main import app


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_guide_is_public_and_covers_the_menu():
    guest = TestClient(app)
    login_page = guest.get("/login")
    assert login_page.status_code == 200
    assert 'aria-label="系统使用说明书"' in login_page.text
    assert 'href="/guide"' in login_page.text
    register_page = guest.get("/register")
    assert 'href="/guide"' in register_page.text

    guide = guest.get("/guide")
    assert guide.status_code == 200
    assert "系统使用说明书" in guide.text
    assert "返回登录" in guide.text
    for anchor in (
        "login", "home", "approvals", "announcements", "org", "members",
        "activities", "leave", "duty", "projects", "files", "library",
        "meetings", "journal", "forum", "assets", "finance",
    ):
        assert f'href="#{anchor}"' in guide.text
        assert f'id="{anchor}"' in guide.text
    for image in ("login.svg", "approvals.svg", "project.svg", "files.svg", "activity.svg", "library.svg", "assets.svg", "finance.svg"):
        assert f'/static/guide/{image}' in guide.text
        assert guest.get(f"/static/guide/{image}").status_code == 200
    assert "仅" not in guide.text
    assert "只有" not in guide.text

    admin = TestClient(app)
    page = admin.get("/login")
    assert admin.post("/login", data={"csrf": csrf(page.text), "account": "admin", "password": "admin123"}, follow_redirects=True).status_code == 200
    home = admin.get("/")
    assert 'aria-label="系统使用说明书"' in home.text
    inside = admin.get("/guide")
    assert inside.status_code == 200
    assert 'aria-current="page"' in inside.text
    assert "调整成员" in inside.text
    assert "zip、rar、7z" in inside.text
    assert "登记归还" in inside.text
