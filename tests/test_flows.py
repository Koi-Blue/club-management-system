import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

SHANGHAI = timezone(timedelta(hours=8))


def csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, html[:1000]
    return match.group(1)


def login(client: TestClient, username: str, password: str):
    page = client.get("/login")
    return client.post(
        "/login",
        data={"csrf": csrf(page.text), "username": username, "password": password},
        follow_redirects=True,
    )


def post(client: TestClient, url: str, **data):
    page = client.get(data.pop("page"))
    data["csrf"] = csrf(page.text)
    return client.post(url, data=data, follow_redirects=True)


def test_health_and_registration_approval():
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        page = client.get("/register")
        response = client.post(
            "/register",
            data={
                "csrf": csrf(page.text),
                "username": "lin",
                "password": "secret12",
                "real_name": "林夏",
                "phone": "13800000000",
                "department": "宣传部",
            },
            follow_redirects=True,
        )
        assert "等待超级管理员审批" in response.text
        denied = login(client, "lin", "secret12")
        assert "等待超级管理员审批" in denied.text
        assert "首页" not in denied.text or "进入系统" in denied.text


def test_admin_assigns_multiple_roles_and_member_cannot_issue():
    admin = TestClient(app)
    login(admin, "admin", "admin123")
    assert "初始密码" in admin.get("/").text
    listed = admin.get("/members")
    assert "林夏" in listed.text
    user_id = re.search(r"/members/(\d+)/approve", listed.text).group(1)
    approved = post(admin, f"/members/{user_id}/approve", page="/members")
    assert "已通过注册" in approved.text
    roles_page = admin.get("/members")
    saved = admin.post(
        f"/members/{user_id}/roles",
        data={"csrf": csrf(roles_page.text), "roles": ["社长", "部长"]},
        follow_redirects=True,
    )
    assert "职务已保存" in saved.text
    assert 'value="社长" checked' in saved.text
    assert 'value="部长" checked' in saved.text
    narrowed = admin.post(
        f"/members/{user_id}/roles",
        data={"csrf": csrf(admin.get("/members").text), "roles": ["部长"]},
        follow_redirects=True,
    )
    assert 'value="社长" checked' not in narrowed.text
    assert 'value="部长" checked' in narrowed.text

    guest = TestClient(app)
    register_page = guest.get("/register")
    guest.post(
        "/register",
        data={"csrf": csrf(register_page.text), "username": "zhou", "password": "secret12", "real_name": "周宁", "phone": "", "department": ""},
        follow_redirects=True,
    )
    pending = admin.get("/members")
    zhou_id = re.search(r"/members/(\d+)/approve", pending.text).group(1)
    post(admin, f"/members/{zhou_id}/approve", page="/members")

    member = TestClient(app)
    home = login(member, "lin", "secret12")
    assert "林夏" in home.text
    assert "部长" in home.text
    start = (datetime.now(SHANGHAI) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    end = (datetime.now(SHANGHAI) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    created = member.post(
        "/activities",
        data={
            "csrf": csrf(member.get("/activities/new").text),
            "title": "迎新说明会",
            "description": "介绍社团",
            "location": "活动室",
            "start_at": start,
            "end_at": end,
            "capacity": "2",
        },
        follow_redirects=True,
    )
    assert "等待发放" in created.text
    activity_url = created.url.path
    assert re.fullmatch(r"/activities/\d+", activity_url)
    blocked = member.post(activity_url + "/issue", data={"csrf": csrf(member.get(activity_url).text)}, follow_redirects=True)
    assert "没有权限发放活动" in blocked.text

    issued = admin.post(activity_url + "/issue", data={"csrf": csrf(admin.get(activity_url).text)}, follow_redirects=True)
    assert "活动已发放" in issued.text
    code = re.search(r"签到码\s*(\d{6})", issued.text).group(1)

    attendee = TestClient(app)
    login(attendee, "zhou", "secret12")
    public_page = attendee.get(activity_url)
    assert code not in public_page.text
    joined = attendee.post(activity_url + "/signup", data={"csrf": csrf(public_page.text)}, follow_redirects=True)
    assert "报名成功" in joined.text
    wrong = attendee.post(
        activity_url + "/checkin",
        data={"csrf": csrf(attendee.get(activity_url).text), "code": "000000"},
        follow_redirects=True,
    )
    assert "签到码不正确" in wrong.text
    done = attendee.post(
        activity_url + "/checkin",
        data={"csrf": csrf(attendee.get(activity_url).text), "code": code},
        follow_redirects=True,
    )
    assert "签到成功" in done.text


def test_project_files_and_asset_borrow():
    admin = TestClient(app)
    login(admin, "admin", "admin123")
    member = TestClient(app)
    login(member, "lin", "secret12")

    created = member.post(
        "/projects",
        data={"csrf": csrf(member.get("/projects/new").text), "title": "秋季招新", "summary": "海报和名单"},
        follow_redirects=True,
    )
    assert "等待审批" in created.text
    project_id = re.search(r"/projects/(\d+)", created.text).group(1)
    uploaded = member.post(
        f"/projects/{project_id}/files",
        data={"csrf": csrf(member.get(f"/projects/{project_id}").text)},
        files={"file": ("名单.txt", b"member-a\nmember-b\n", "text/plain")},
        follow_redirects=True,
    )
    assert "资料已上传" in uploaded.text
    reviewed = admin.post(
        f"/projects/{project_id}/review",
        data={"csrf": csrf(admin.get(f"/projects/{project_id}").text), "decision": "approved", "comment": "可以做"},
        follow_redirects=True,
    )
    assert "审批已保存" in reviewed.text
    download = member.get(f"/projects/{project_id}/files/{re.search(r'/files/(\d+)', reviewed.text).group(1)}")
    assert download.status_code == 200
    assert download.content == b"member-a\nmember-b\n"

    stored = admin.post(
        "/assets",
        data={
            "csrf": csrf(admin.get("/assets").text),
            "name": "投影仪",
            "category": "设备",
            "total_qty": "1",
            "location": "库房",
            "description": "招新使用",
        },
        follow_redirects=True,
    )
    assert "资产已登记" in stored.text
    due = (datetime.now(SHANGHAI) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    asset_id = re.search(r'name="asset_id".*?value="(\d+)"', member.get("/assets").text, re.S)
    if not asset_id:
        asset_id = re.search(r'<option value="(\d+)">投影仪', member.get("/assets").text)
    assert asset_id
    requested = member.post(
        "/borrows",
        data={
            "csrf": csrf(member.get("/assets").text),
            "asset_id": asset_id.group(1),
            "qty": "1",
            "reason": "招新夜",
            "due_at": due,
        },
        follow_redirects=True,
    )
    assert "借用申请已提交" in requested.text
    borrow_id = re.search(r"/borrows/(\d+)/review", admin.get("/assets").text).group(1)
    approved = admin.post(
        f"/borrows/{borrow_id}/review",
        data={"csrf": csrf(admin.get("/assets").text), "decision": "approved", "comment": ""},
        follow_redirects=True,
    )
    assert "借用审批已保存" in approved.text
    second = member.post(
        "/borrows",
        data={
            "csrf": csrf(member.get("/assets").text),
            "asset_id": asset_id.group(1),
            "qty": "1",
            "reason": "再借一台",
            "due_at": due,
        },
        follow_redirects=True,
    )
    assert "可借数量不足" in second.text
    returned = member.post(
        f"/borrows/{borrow_id}/return",
        data={"csrf": csrf(member.get("/assets").text)},
        follow_redirects=True,
    )
    assert "已登记归还" in returned.text
