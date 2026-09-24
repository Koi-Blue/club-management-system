import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

SHANGHAI = timezone(timedelta(hours=8))


def csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, html[:800]
    return match.group(1)


def login(client: TestClient, account: str, password: str):
    page = client.get("/login")
    return client.post("/login", data={"csrf": csrf(page.text), "account": account, "password": password}, follow_redirects=True)


def register(client: TestClient, **fields):
    page = client.get("/register")
    payload = {"csrf": csrf(page.text), "password": "secret12", "college": "计算机学院", "class_name": "软工2201"}
    payload.update(fields)
    return client.post("/register", data=payload, follow_redirects=True)


def test_restart_keeps_registered_account():
    from app.config import load_settings
    from app.db import init_db

    guest = TestClient(app)
    registered = register(guest, username="keepme", real_name="留存", phone="13800000009", department="技术部")
    assert "等待超级管理员审批" in registered.text
    init_db(load_settings())
    again = login(guest, "keepme", "secret12")
    assert "等待超级管理员审批" in again.text
    admin = TestClient(app)
    login(admin, "admin", "admin123")
    members = admin.get("/members")
    user_id = re.search(r"/members/(\d+)/approve", members.text).group(1)
    admin.post(f"/members/{user_id}/reject", data={"csrf": csrf(members.text)}, follow_redirects=True)


def test_permissions_org_finance_and_archive():
    admin = TestClient(app)
    assert login(admin, "admin", "admin123").status_code == 200
    assert "何琪" not in admin.get("/members").text
    assert "外联手册" not in admin.get("/projects").text

    guest = TestClient(app)
    registered = register(guest, username="lin", real_name="林夏", phone="13800000001", department="技术部")
    assert "等待超级管理员审批" in registered.text
    denied = login(guest, "13800000001", "secret12")
    assert "等待超级管理员审批" in denied.text

    members = admin.get("/members")
    user_id = re.search(r"/members/(\d+)/approve", members.text).group(1)
    approved = admin.post(f"/members/{user_id}/approve", data={"csrf": csrf(members.text)}, follow_redirects=True)
    assert "默认职务为所属部门成员" in approved.text
    assert 'value="技术部成员" checked' in approved.text

    tech = TestClient(app)
    home = login(tech, "13800000001", "secret12")
    assert "林夏" in home.text
    org = tech.get("/org")
    assert "软工2201" in org.text and "林夏" in org.text and "13800000001" in org.text
    assert "技术部嵌入式软件负责人" in org.text

    new_page = tech.get("/projects/new")
    missing = tech.post("/projects", data={"csrf": csrf(new_page.text), "title": "未关联", "summary": "", "department": "技术部"}, follow_redirects=True)
    assert "请勾选关联成员" in missing.text
    member_id = re.search(r'name="members" value="(\d+)"', new_page.text).group(1)
    created = tech.post("/projects", data={"csrf": csrf(new_page.text), "title": "巡线车", "summary": "嵌入式", "department": "技术部", "members": member_id}, follow_redirects=True)
    assert "草稿" in created.text
    project_path = created.url.path
    submitted = tech.post(project_path + "/submit", data={"csrf": csrf(tech.get(project_path).text)}, follow_redirects=True)
    assert "已提交审批" in submitted.text
    assert "巡线车" not in admin.get("/projects").text

    promo_guest = TestClient(app)
    register(promo_guest, username="zhou", real_name="周宁", phone="13800000002", department="宣传部", class_name="新闻2202")
    pending = admin.get("/members")
    promo_id = re.search(r"/members/(\d+)/approve", pending.text).group(1)
    admin.post(f"/members/{promo_id}/approve", data={"csrf": csrf(pending.text)}, follow_redirects=True)
    promo = TestClient(app)
    login(promo, "zhou", "secret12")
    assert "巡线车" not in promo.get("/projects").text
    zhou_org = promo.get("/org")
    assert "林夏" in zhou_org.text and "13800000001" not in zhou_org.text
    assert "没有权限登记财务" in promo.post("/finance/ledger", data={"csrf": csrf(promo.get("/finance").text), "kind": "income", "amount": "10", "category": "社费", "note": "", "happened_on": "2026-09-23"}, follow_redirects=True).text
    assert "收支账" not in promo.get("/finance").text

    admin.post(f"/members/{user_id}/roles", data={"csrf": csrf(admin.get("/members").text), "roles": ["技术部部长", "技术部算法负责人"]}, follow_redirects=True)
    assert "巡线车" in tech.get("/projects").text

    honor_guest = TestClient(app)
    register(honor_guest, username="tang", real_name="唐宁", phone="13800000003", department="人事部")
    honor_page = admin.get("/members")
    honor_id = re.search(r"/members/(\d+)/approve", honor_page.text).group(1)
    admin.post(f"/members/{honor_id}/approve", data={"csrf": csrf(honor_page.text)}, follow_redirects=True)
    admin.post(f"/members/{honor_id}/roles", data={"csrf": csrf(admin.get("/members").text), "roles": ["荣誉社长"]}, follow_redirects=True)
    honor = TestClient(app)
    login(honor, "tang", "secret12")
    assert "巡线车" in honor.get("/projects").text
    assert "13800000001" in honor.get("/org").text
    assert "收支账" not in honor.get("/finance").text
    announced = honor.post("/announcements", data={"csrf": csrf(honor.get("/announcements").text), "title": "招新说明", "content": "本周见面"}, follow_redirects=True)
    assert "公告已发布" in announced.text
    blocked = admin.post(project_path + "/review", data={"csrf": csrf(admin.get("/").text), "decision": "approved", "comment": "超管不能批"}, follow_redirects=True)
    assert "没有权限审批立项" in blocked.text

    admin.post(f"/members/{promo_id}/roles", data={"csrf": csrf(admin.get("/members").text), "roles": ["社长", "宣传部成员"]}, follow_redirects=True)
    finance = login(promo, "zhou", "secret12")
    assert "巡线车" not in promo.get("/projects").text
    president_review = promo.post(project_path + "/review", data={"csrf": csrf(finance.text), "decision": "approved", "comment": "社长不能批"}, follow_redirects=True)
    assert "没有权限审批立项" in president_review.text
    approved = honor.post(project_path + "/review", data={"csrf": csrf(honor.get(project_path).text), "decision": "approved", "comment": "同意立项"}, follow_redirects=True)
    assert "审批已保存" in approved.text
    assert "收支账" in finance.text or "收支账" in promo.get("/finance").text
    recorded = promo.post("/finance/ledger", data={"csrf": csrf(promo.get("/finance").text), "kind": "income", "amount": "20.50", "category": "社费", "note": "学期", "happened_on": "2026-09-23"}, follow_redirects=True)
    assert "收支已登记" in recorded.text
    claim = tech.post("/finance/claims", data={"csrf": csrf(tech.get("/finance").text), "amount": "12.00", "reason": "传感器"}, follow_redirects=True)
    assert "报销已提交" in claim.text
    assert "传感器" in promo.get("/finance").text
    assert "20.50" not in tech.get("/finance").text

    start = (datetime.now(SHANGHAI) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    end = (datetime.now(SHANGHAI) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    drafted = tech.post("/activities", data={"csrf": csrf(tech.get("/activities/new").text), "title": "焊接培训", "description": "实验室", "department": "技术部", "location": "A101", "start_at": start, "end_at": end, "capacity": "10"}, follow_redirects=True)
    activity_path = drafted.url.path
    outsider = TestClient(app)
    registered_outsider = register(outsider, username="heqi", real_name="何琪", phone="13800000004", department="财务部", class_name="会计2201")
    assert "等待超级管理员审批" in registered_outsider.text
    outsider_page = admin.get("/members")
    outsider_id = re.search(r"/members/(\d+)/approve", outsider_page.text).group(1)
    admin.post(f"/members/{outsider_id}/approve", data={"csrf": csrf(outsider_page.text)}, follow_redirects=True)
    other = TestClient(app)
    login(other, "heqi", "secret12")
    assert "焊接培训" not in other.get("/activities").text
    assert "焊接培训" not in honor.get("/activities").text
    issued = promo.post(activity_path + "/issue", data={"csrf": csrf(promo.get(activity_path).text)}, follow_redirects=True)
    assert "活动已发放" in issued.text
    code = re.search(r"签到码\s*(\d{6})", issued.text).group(1)
    public = other.get(activity_path)
    assert code not in public.text
    assert "报名成功" in other.post(activity_path + "/signup", data={"csrf": csrf(public.text)}, follow_redirects=True).text

    journal = tech.post("/journal", data={"csrf": csrf(tech.get("/journal/new").text), "title": "实验记录", "body": "# 今天\n\n完成了**巡线**"}, follow_redirects=True)
    assert "实验记录" in journal.text and "<h1>今天</h1>" in journal.text
    assert "实验记录" not in promo.get("/journal").text
    posted = tech.post("/forum", data={"csrf": csrf(tech.get("/forum").text), "title": "算法讨论", "body": "只给算法方向", "scope": "direction:算法"}, follow_redirects=True)
    assert "帖子已发布" in posted.text
    assert "算法讨论" in tech.get("/forum").text
    assert "算法讨论" not in promo.get("/forum").text
    assert "算法讨论" not in honor.get("/forum").text

    deleted = admin.post(f"/members/{user_id}/delete", data={"csrf": csrf(admin.get("/members").text)}, follow_redirects=True)
    assert "历史档案仍会保留" in deleted.text
    assert "已注销" in honor.get(project_path).text
    assert "账号已注销" in login(tech, "lin", "secret12").text
