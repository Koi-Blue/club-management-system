"""资产区分仪器与耗材：仪器借用要预计归还，耗材领用不用归还。"""
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text

import app.db as database
from app.config import load_settings
from app.db import init_db
from app.main import create_app
from app.models import Asset, Borrow, User

SHANGHAI = timezone(timedelta(hours=8))


def csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def login(client, username, password="secret12"):
    page = client.get("/login")
    return client.post(
        "/login",
        data={"csrf": csrf(page), "account": username, "password": password},
        follow_redirects=True,
    )


def post(client, url, data=None):
    payload = dict(data or {})
    payload["csrf"] = csrf(client.get("/"))
    return client.post(url, data=payload, follow_redirects=True)


def due_in(days=2):
    return (datetime.now(SHANGHAI) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def option_id(html, name):
    match = re.search(rf'<option value="(\d+)">{re.escape(name)}', html)
    assert match, html
    return match.group(1)


def form_html(html, action):
    match = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>([\s\S]*?)</form>', html)
    assert match, action
    return match.group(1)


def request_row(html, name, flow):
    for match in re.finditer(r"<tr>[\s\S]*?</tr>", html):
        row = match.group(0)
        if name in row and re.search(rf"<td>\s*{flow}\s*</td>", row):
            return row
    raise AssertionError(f"找不到{name}的{flow}记录\n{html}")


@pytest.fixture
def desk(tmp_path, monkeypatch):
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'club.db'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app()
    admin = TestClient(app)
    assert login(admin, "admin", "admin123").status_code == 200
    counter = {"n": 0}

    def member(name, roles=None):
        counter["n"] += 1
        client = TestClient(app)
        registered = client.post("/register", data={
            "csrf": csrf(client.get("/register")), "username": name, "password": "secret12",
            "real_name": name, "phone": f"1370000{counter['n']:04d}", "department": "技术部",
            "college": "学院", "class_name": "一班",
        }, follow_redirects=True)
        assert "等待超级管理员审批" in registered.text
        with database.SessionLocal() as db:
            uid = db.scalar(select(User.id).where(User.username == name))
        assert "已通过注册" in post(admin, f"/members/{uid}/approve").text
        if roles:
            assert post(admin, f"/members/{uid}/roles", {"roles": roles}).status_code == 200
        assert login(client, name).url.path == "/"
        return client, uid

    yield app, admin, member
    database.engine.dispose()
    database.engine, database.SessionLocal = previous


def test_register_form_defaults_to_instrument_and_list_shows_kind(desk):
    _app, admin, _member = desk
    page = admin.get("/assets")
    assert "申请借用" in page.text
    assert '<option value="instrument" selected>仪器</option>' in page.text
    assert '<option value="consumable">耗材</option>' in page.text
    assert "没有可借的仪器" in page.text
    assert "没有可领的耗材" in page.text

    assert "资产已登记" in post(admin, "/assets", {
        "name": "万用表", "category": "工具", "total_qty": "4", "location": "柜A", "description": "数字",
    }).text
    listed = admin.get("/assets")
    meter = re.search(r"<tr>(?:(?!</tr>).)*万用表(?:(?!</tr>).)*</tr>", listed.text).group(0)
    assert "仪器" in meter
    assert "4 / 4" in meter
    borrow_form = form_html(listed.text, "/borrows")
    assert "万用表" in borrow_form
    assert 'name="due_at"' in borrow_form
    assert "没有可领的耗材" in listed.text

    assert "资产已登记" in post(admin, "/assets", {
        "name": "焊锡丝", "kind": "consumable", "category": "材料", "total_qty": "5", "location": "柜B", "description": "",
    }).text
    both = admin.get("/assets")
    wire = re.search(r"<tr>(?:(?!</tr>).)*焊锡丝(?:(?!</tr>).)*</tr>", both.text).group(0)
    assert "耗材" in wire
    assert "焊锡丝" not in form_html(both.text, "/borrows")
    issue_form = form_html(both.text, "/issues")
    assert "焊锡丝" in issue_form
    assert "due_at" not in issue_form
    assert "预计归还" not in issue_form
    assert "万用表" not in issue_form
    assert "请选择仪器或耗材" in post(admin, "/assets", {
        "name": "坏类型", "kind": "tool", "category": "物资", "total_qty": "1", "location": "", "description": "",
    }).text


def test_instrument_borrow_requires_due_and_return_restores_stock(desk):
    _app, admin, member = desk
    person, _uid = member("林夏同学")
    assert "资产已登记" in post(admin, "/assets", {
        "name": "示波器", "kind": "instrument", "category": "仪器", "total_qty": "10", "location": "实验室", "description": "",
    }).text
    page = person.get("/assets")
    asset_id = option_id(form_html(page.text, "/borrows"), "示波器")
    missing = post(person, "/borrows", {"asset_id": asset_id, "qty": "2", "reason": "比赛", "due_at": ""})
    assert "请填写预计归还时间" in missing.text
    assert "借用申请已提交" in post(person, "/borrows", {
        "asset_id": asset_id, "qty": "2", "reason": "比赛", "due_at": due_in(),
    }).text
    pending = person.get("/assets")
    assert "8 / 10" not in pending.text
    assert "10 / 10" in pending.text
    row = request_row(pending.text, "示波器", "借用")
    assert "待审批" in row
    assert "登记归还" not in row

    review = admin.get("/assets")
    borrow_id = re.search(r"/borrows/(\d+)/review", review.text).group(1)
    assert "借用 · 2 件" in admin.get("/approvals").text
    assert "借用审批已保存" in post(admin, f"/borrows/{borrow_id}/review", {"decision": "approved", "comment": "可以"}).text
    approved = admin.get("/assets")
    assert "8 / 10" in approved.text
    held = request_row(approved.text, "示波器", "借用")
    assert "借用中" in held
    assert "登记归还" in held
    assert "—" not in held

    assert "已登记归还" in post(person, f"/borrows/{borrow_id}/return").text
    returned = person.get("/assets")
    assert "10 / 10" in returned.text
    done = request_row(returned.text, "示波器", "借用")
    assert "已归还" in done
    assert "登记归还" not in done


def test_consumable_issue_skips_due_and_cannot_be_returned(desk):
    _app, admin, member = desk
    person, _uid = member("周宁同学")
    officer, _officer_id = member("副社长周", ["副社长"])
    assert "资产已登记" in post(admin, "/assets", {
        "name": "热缩管", "kind": "consumable", "category": "材料", "total_qty": "5", "location": "柜", "description": "",
    }).text
    page = person.get("/assets")
    assert "没有可借的仪器" in page.text
    asset_id = option_id(form_html(page.text, "/issues"), "热缩管")
    assert "领用申请已提交" in post(person, "/issues", {"asset_id": asset_id, "qty": "2", "reason": "焊接"}).text
    pending = person.get("/assets")
    assert "5 / 5" in pending.text
    waiting = request_row(pending.text, "热缩管", "领用")
    assert "待审批" in waiting
    assert "—" in waiting
    assert "登记归还" not in waiting
    assert "领用 · 2 件" in admin.get("/approvals").text

    with database.SessionLocal() as db:
        issue_id = db.scalar(select(Borrow.id).where(Borrow.purpose == "issue", Borrow.status == "pending"))
        assert db.get(Borrow, issue_id).due_at is None
    assert "没有权限审批领用" in post(person, f"/borrows/{issue_id}/review", {"decision": "approved", "comment": ""}).text
    assert "领用审批已保存" in post(officer, f"/borrows/{issue_id}/review", {"decision": "approved", "comment": "拿去用"}).text

    approved = admin.get("/assets")
    assert "3 / 5" in approved.text
    taken = request_row(approved.text, "热缩管", "领用")
    assert "已领用" in taken
    assert "借用中" not in taken
    assert "登记归还" not in taken
    assert "耗材无需归还" in post(admin, f"/borrows/{issue_id}/return").text
    assert "3 / 5" in admin.get("/assets").text

    assert "领用申请已提交" in post(person, "/issues", {"asset_id": asset_id, "qty": "3", "reason": "先再领"}).text
    assert "领用申请已提交" in post(person, "/issues", {"asset_id": asset_id, "qty": "3", "reason": "后再领"}).text
    with database.SessionLocal() as db:
        pending_ids = list(db.scalars(select(Borrow.id).where(Borrow.purpose == "issue", Borrow.status == "pending").order_by(Borrow.id)))
    assert len(pending_ids) == 2
    assert "领用审批已保存" in post(officer, f"/borrows/{pending_ids[0]}/review", {"decision": "approved", "comment": ""}).text
    assert "0 / 5" in officer.get("/assets").text
    refused = post(officer, f"/borrows/{pending_ids[1]}/review", {"decision": "approved", "comment": ""})
    assert "可领数量不足，无法通过" in refused.text
    assert "0 / 5" in officer.get("/assets").text
    with database.SessionLocal() as db:
        assert db.get(Borrow, pending_ids[1]).status == "pending"


def test_cannot_borrow_consumable_or_issue_instrument(desk):
    _app, admin, member = desk
    person, _uid = member("何琪同学")
    assert "资产已登记" in post(admin, "/assets", {
        "name": "开发板", "kind": "instrument", "category": "物资", "total_qty": "3", "location": "", "description": "",
    }).text
    assert "资产已登记" in post(admin, "/assets", {
        "name": "杜邦线", "kind": "consumable", "category": "材料", "total_qty": "20", "location": "", "description": "",
    }).text
    page = person.get("/assets")
    board = option_id(form_html(page.text, "/borrows"), "开发板")
    wire = option_id(form_html(page.text, "/issues"), "杜邦线")
    assert "耗材不能借用，请走领用" in post(person, "/borrows", {
        "asset_id": wire, "qty": "1", "reason": "借用耗材", "due_at": due_in(),
    }).text
    assert "仪器请走借用，并填写预计归还时间" in post(person, "/issues", {
        "asset_id": board, "qty": "1", "reason": "当耗材领",
    }).text
    with database.SessionLocal() as db:
        assert db.scalar(select(Borrow.id)) is None
    honor, _honor_id = member("荣誉唐", ["荣誉社长"])
    assert "借用申请已提交" in post(person, "/borrows", {
        "asset_id": board, "qty": "1", "reason": "比赛", "due_at": due_in(),
    }).text
    with database.SessionLocal() as db:
        borrow_id = db.scalar(select(Borrow.id))
    assert "没有权限审批借用" in post(honor, f"/borrows/{borrow_id}/review", {"decision": "approved", "comment": ""}).text


def test_legacy_assets_without_kind_stay_instruments(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE assets (
            id INTEGER PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            category VARCHAR(40),
            total_qty INTEGER NOT NULL,
            location VARCHAR(80),
            description TEXT,
            created_at DATETIME
        )
        """
    )
    conn.execute(
        "INSERT INTO assets (id, name, category, total_qty, location, description, created_at) VALUES (1, '旧示波器', '物资', 2, '柜', '', '2026-01-01 00:00:00')"
    )
    conn.execute(
        """
        CREATE TABLE borrows (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            reason VARCHAR(200) DEFAULT '',
            status VARCHAR(20) DEFAULT 'pending',
            reviewer_id INTEGER,
            review_comment VARCHAR(200) DEFAULT '',
            due_at DATETIME NOT NULL,
            created_at DATETIME,
            approved_at DATETIME,
            returned_at DATETIME,
            FOREIGN KEY(asset_id) REFERENCES assets (id),
            FOREIGN KEY(user_id) REFERENCES users (id)
        )
        """
    )
    conn.execute("CREATE INDEX ix_borrows_status ON borrows (status)")
    conn.execute(
        "INSERT INTO borrows (id, asset_id, user_id, qty, reason, status, due_at, created_at) VALUES (1, 1, 1, 1, '实验', 'approved', '2026-03-01 08:00:00', '2026-01-02 00:00:00')"
    )
    conn.commit()
    conn.close()

    previous = database.engine, database.SessionLocal
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    try:
        app = create_app()
        init_db(load_settings())
        with database.engine.connect() as db:
            assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1
            asset_cols = {row[1]: row for row in db.execute(text("PRAGMA table_info(assets)")).all()}
            assert asset_cols["kind"][3] == 1
            assert db.execute(text("SELECT kind FROM assets WHERE id = 1")).scalar() == "instrument"
            borrow_cols = {row[1]: row for row in db.execute(text("PRAGMA table_info(borrows)")).all()}
            assert borrow_cols["due_at"][3] == 0
            assert "purpose" in borrow_cols
            legacy = db.execute(text("SELECT purpose, due_at, status, qty FROM borrows WHERE id = 1")).one()
            assert legacy.purpose == "borrow"
            assert legacy.due_at
            assert legacy.status == "approved"
            indexes = [row[0] for row in db.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'borrows'"
            )).all()]
            assert "ix_borrows_status" in indexes
        with database.SessionLocal() as db:
            asset = db.get(Asset, 1)
            assert asset.kind == "instrument"
            row = db.get(Borrow, 1)
            assert row.purpose == "borrow"
            assert row.due_at is not None

        admin = TestClient(app)
        assert login(admin, "admin", "admin123").url.path == "/"
        page = admin.get("/assets")
        assert "旧示波器" in page.text
        assert "1 / 2" in page.text
        legacy_row = request_row(page.text, "旧示波器", "借用")
        assert "借用中" in legacy_row
        assert "登记归还" in legacy_row
        assert "2026-03-01" in legacy_row

        assert "资产已登记" in post(admin, "/assets", {
            "name": "旧焊锡", "kind": "consumable", "category": "材料", "total_qty": "4", "location": "", "description": "",
        }).text
        issued = admin.get("/assets")
        consumable_id = option_id(form_html(issued.text, "/issues"), "旧焊锡")
        assert "领用申请已提交" in post(admin, "/issues", {"asset_id": consumable_id, "qty": "1", "reason": "维修"}).text
        with database.SessionLocal() as db:
            claim = db.scalar(select(Borrow).where(Borrow.purpose == "issue"))
            assert claim.due_at is None
            assert claim.asset_id == int(consumable_id)
    finally:
        database.engine.dispose()
        database.engine, database.SessionLocal = previous


def asset_row(html, name):
    match = re.search(rf"<tr>(?:(?!</tr>).)*{re.escape(name)}(?:(?!</tr>).)*</tr>", html)
    assert match, name
    return match.group(0)


def kind_action(html, name):
    match = re.search(r'action="(/assets/\d+/kind)"', asset_row(html, name))
    assert match, name
    return match.group(1)


def test_admin_can_change_kind_when_stock_is_clear(desk):
    _app, admin, member = desk
    person, _uid = member("林夏同学")
    assert "资产已登记" in post(admin, "/assets", {
        "name": "空万用表", "kind": "instrument", "category": "工具", "total_qty": "4", "location": "柜", "description": "",
    }).text
    listed = admin.get("/assets")
    action = kind_action(listed.text, "空万用表")
    assert "改类型" in asset_row(listed.text, "空万用表")
    assert "类型没有变化" in post(admin, action, {"kind": "instrument"}).text
    assert "请选择仪器或耗材" in post(admin, action, {"kind": "tool"}).text
    assert "找不到这项资产" in post(admin, "/assets/99999/kind", {"kind": "consumable"}).text
    changed = post(admin, action, {"kind": "consumable"})
    assert "资产类型已修改" in changed.text
    assert "没有可借的仪器" in changed.text
    assert "空万用表" in form_html(changed.text, "/issues")
    assert "4 / 4" in asset_row(changed.text, "空万用表")
    consumable_id = option_id(form_html(changed.text, "/issues"), "空万用表")
    assert "耗材不能借用，请走领用" in post(person, "/borrows", {
        "asset_id": consumable_id, "qty": "1", "reason": "当仪器借", "due_at": due_in(),
    }).text
    assert "领用申请已提交" in post(person, "/issues", {"asset_id": consumable_id, "qty": "1", "reason": "焊接"}).text


def test_open_borrow_blocks_turning_instrument_into_consumable(desk):
    _app, admin, member = desk
    person, _uid = member("周宁同学")
    assert "资产已登记" in post(admin, "/assets", {
        "name": "待批示波器", "kind": "instrument", "category": "仪器", "total_qty": "2", "location": "", "description": "",
    }).text
    assert "资产已登记" in post(admin, "/assets", {
        "name": "在借示波器", "kind": "instrument", "category": "仪器", "total_qty": "2", "location": "", "description": "",
    }).text
    page = person.get("/assets")
    borrow_form = form_html(page.text, "/borrows")
    pending_id = option_id(borrow_form, "待批示波器")
    held_id = option_id(borrow_form, "在借示波器")
    assert "借用申请已提交" in post(person, "/borrows", {
        "asset_id": pending_id, "qty": "1", "reason": "先申请", "due_at": due_in(),
    }).text
    assert "借用申请已提交" in post(person, "/borrows", {
        "asset_id": held_id, "qty": "1", "reason": "拿去用", "due_at": due_in(),
    }).text
    review = admin.get("/assets")
    pending_action = kind_action(review.text, "待批示波器")
    blocked = post(admin, pending_action, {"kind": "consumable"})
    assert "还有未归还或待审批的借用，不能改成耗材" in blocked.text
    with database.SessionLocal() as db:
        pending_asset = db.scalar(select(Asset).where(Asset.name == "待批示波器"))
        assert pending_asset.kind == "instrument"
        borrow_ids = list(db.scalars(select(Borrow.id).where(Borrow.status == "pending").order_by(Borrow.id)))
    assert "借用审批已保存" in post(admin, f"/borrows/{borrow_ids[0]}/review", {"decision": "rejected", "comment": "先不借"}).text
    changed = post(admin, pending_action, {"kind": "consumable"})
    assert "资产类型已修改" in changed.text
    assert "待批示波器" in form_html(changed.text, "/issues")
    assert "待批示波器" not in form_html(changed.text, "/borrows")

    held_action = kind_action(admin.get("/assets").text, "在借示波器")
    assert "借用审批已保存" in post(admin, f"/borrows/{borrow_ids[1]}/review", {"decision": "approved", "comment": "可以"}).text
    still = post(admin, held_action, {"kind": "consumable"})
    assert "还有未归还或待审批的借用，不能改成耗材" in still.text
    with database.SessionLocal() as db:
        assert db.scalar(select(Asset.kind).where(Asset.name == "在借示波器")) == "instrument"
    assert "已登记归还" in post(person, f"/borrows/{borrow_ids[1]}/return").text
    returned = post(admin, held_action, {"kind": "consumable"})
    assert "资产类型已修改" in returned.text
    done = request_row(returned.text, "在借示波器", "借用")
    assert "已归还" in done
    assert "2 / 2" in asset_row(returned.text, "在借示波器")
    assert "在借示波器" in form_html(returned.text, "/issues")


def test_open_issue_blocks_turning_consumable_into_instrument(desk):
    _app, admin, member = desk
    person, _uid = member("何琪同学")
    assert "资产已登记" in post(admin, "/assets", {
        "name": "待批焊锡", "kind": "consumable", "category": "材料", "total_qty": "3", "location": "", "description": "",
    }).text
    assert "资产已登记" in post(admin, "/assets", {
        "name": "已领热缩管", "kind": "consumable", "category": "材料", "total_qty": "3", "location": "", "description": "",
    }).text
    page = person.get("/assets")
    issue_form = form_html(page.text, "/issues")
    pending_id = option_id(issue_form, "待批焊锡")
    taken_id = option_id(issue_form, "已领热缩管")
    assert "领用申请已提交" in post(person, "/issues", {"asset_id": pending_id, "qty": "1", "reason": "先领"}).text
    assert "领用申请已提交" in post(person, "/issues", {"asset_id": taken_id, "qty": "1", "reason": "拿走"}).text
    review = admin.get("/assets")
    pending_action = kind_action(review.text, "待批焊锡")
    assert "还有已领用或待审批的领用，不能改成仪器" in post(admin, pending_action, {"kind": "instrument"}).text
    with database.SessionLocal() as db:
        assert db.scalar(select(Asset.kind).where(Asset.name == "待批焊锡")) == "consumable"
        issue_ids = list(db.scalars(select(Borrow.id).where(Borrow.purpose == "issue", Borrow.status == "pending").order_by(Borrow.id)))
    assert "领用审批已保存" in post(admin, f"/borrows/{issue_ids[0]}/review", {"decision": "rejected", "comment": "先不发"}).text
    changed = post(admin, pending_action, {"kind": "instrument"})
    assert "资产类型已修改" in changed.text
    assert "待批焊锡" in form_html(changed.text, "/borrows")
    assert "待批焊锡" not in form_html(changed.text, "/issues")
    board_id = option_id(form_html(changed.text, "/borrows"), "待批焊锡")
    assert "仪器请走借用，并填写预计归还时间" in post(person, "/issues", {"asset_id": board_id, "qty": "1", "reason": "当耗材"}).text
    assert "借用申请已提交" in post(person, "/borrows", {
        "asset_id": board_id, "qty": "1", "reason": "改成仪器后再借", "due_at": due_in(),
    }).text

    taken_action = kind_action(admin.get("/assets").text, "已领热缩管")
    assert "领用审批已保存" in post(admin, f"/borrows/{issue_ids[1]}/review", {"decision": "approved", "comment": "拿去"}).text
    blocked = post(admin, taken_action, {"kind": "instrument"})
    assert "还有已领用或待审批的领用，不能改成仪器" in blocked.text
    assert "耗材无需归还" in post(admin, f"/borrows/{issue_ids[1]}/return").text
    still = post(admin, taken_action, {"kind": "instrument"})
    assert "还有已领用或待审批的领用，不能改成仪器" in still.text
    with database.SessionLocal() as db:
        assert db.scalar(select(Asset.kind).where(Asset.name == "已领热缩管")) == "consumable"
    assert "2 / 3" in asset_row(admin.get("/assets").text, "已领热缩管")


def test_only_super_admin_can_change_asset_kind(desk):
    _app, admin, member = desk
    president, _president_id = member("社长周宁", ["社长", "宣传部成员"])
    minister, _minister_id = member("技术部部长", ["技术部部长"])
    assert "资产已登记" in post(president, "/assets", {
        "name": "社团电钻", "kind": "instrument", "category": "工具", "total_qty": "1", "location": "", "description": "",
    }).text
    admin_page = admin.get("/assets")
    action = kind_action(admin_page.text, "社团电钻")
    for client in (president, minister):
        page = client.get("/assets")
        row = asset_row(page.text, "社团电钻")
        assert "改类型" not in row
        assert "/kind" not in row
        assert "仪器" in row
        refused = post(client, action, {"kind": "consumable"})
        assert "只有超级管理员可以修改资产类型" in refused.text
    with database.SessionLocal() as db:
        assert db.scalar(select(Asset.kind).where(Asset.name == "社团电钻")) == "instrument"
    assert "资产类型已修改" in post(admin, action, {"kind": "consumable"}).text
