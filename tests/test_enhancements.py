import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

import app.db as database
from app.main import create_app
from app.markdown import render_markdown
from app.models import Activity, ForumComment, ForumPost, Reimbursement, ReimbursementFile, User
from app import services as svc

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
PDF = b'%PDF-1.4\ninvoice'


def csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def login(client, username, password='secret12'):
    return client.post('/login', data={'csrf': csrf(client.get('/login')), 'account': username, 'password': password}, follow_redirects=True)


def post(client, url, data=None, files=None):
    data = dict(data or {})
    data['csrf'] = csrf(client.get('/'))
    return client.post(url, data=data, files=files, follow_redirects=True)


@pytest.fixture
def site(tmp_path, monkeypatch):
    previous = database.engine, database.SessionLocal
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "club.db"}')
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path / 'uploads'))
    app = create_app()
    admin = TestClient(app)
    login(admin, 'admin', 'admin123')
    counter = 0

    def member(name, roles=None, department='技术部', direction=''):
        nonlocal counter
        counter += 1
        client = TestClient(app)
        result = client.post('/register', data={
            'csrf': csrf(client.get('/register')), 'username': name, 'password': 'secret12',
            'real_name': name, 'phone': f'1390000{counter:04d}', 'department': department,
            'college': '学院', 'class_name': '一班', 'direction': direction,
        }, follow_redirects=True)
        assert '等待超级管理员审批' in result.text
        with database.SessionLocal() as db:
            uid = db.scalar(select(User.id).where(User.username == name))
        post(admin, f'/members/{uid}/approve')
        if roles:
            post(admin, f'/members/{uid}/roles', {'roles': roles})
        assert login(client, name).url.path == '/'
        return client, uid

    yield app, admin, member, tmp_path
    database.engine.dispose()
    database.engine, database.SessionLocal = previous


def new_topic(client, title='话题', scope='club', body='正文'):
    response = post(client, '/forum', {'title': title, 'body': body, 'scope': scope})
    assert '帖子已发布' in response.text
    with database.SessionLocal() as db:
        return db.scalar(select(ForumPost.id).where(ForumPost.title == title))


def test_current_roles_directions_and_new_members_see_history(site):
    _, admin, member, _ = site
    algo = new_topic(admin, '历史算法话题', 'direction:算法')
    tech = new_topic(admin, '历史技术话题', 'tech')
    promo = new_topic(admin, '历史宣传话题', 'dept:宣传部')
    client, uid = member('newmember', direction='算法')
    page = client.get('/forum').text
    assert '历史算法话题' in page and '历史技术话题' in page and '历史宣传话题' not in page
    assert '创建活动' not in client.get('/activities').text  # ordinary direction has no manager authority
    own = new_topic(client, '本人历史话题', 'direction:算法')
    post(admin, f'/members/{uid}/roles', {'roles': ['宣传部成员']})
    page = client.get('/forum').text
    assert '历史算法话题' not in page and '历史技术话题' not in page
    assert '历史宣传话题' in page and '本人历史话题' in page
    for forbidden in [algo, tech]:
        assert client.get(f'/forum/{forbidden}', follow_redirects=True).url.path == '/forum'
        post(client, f'/forum/{forbidden}/comments', {'body': '禁止写入'})
    with database.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ForumComment)) == 0
        assert db.get(User, uid).department == '技术部'  # unchanged registration data cannot grant old forum access
    post(admin, f'/members/{uid}/roles', {'roles': ['宣传部成员', '技术部硬件成员', '技术部算法成员', '技术部成员']})
    assert '历史算法话题' in client.get('/forum').text and '历史宣传话题' in client.get('/forum').text
    with database.SessionLocal() as db:
        roles = svc.roles_of(db, uid)
        assert '技术部成员' not in roles
        tree = svc.build_org_tree(db, db.get(User, uid), roles)
    assert [node['title'] for node in tree['independent']] == ['指导老师', '荣誉社长']
    leaders = tree['tree'][0]['children'][0]['children'][0]['children']
    for label in ['技术部算法负责人', '技术部硬件负责人']:
        leader = next(node for node in leaders if node['title'] == label)
        assert leader['children'][0]['people'][0]['real_name'] == 'newmember'
    org = client.get('/org').text
    chart, independent = org.split('<aside class="org-independent"', 1)
    # header/sidebar may contain role names; the actual chart must not contain independent nodes.
    chart = chart.split('aria-label="社团上下级架构"', 1)[1]
    assert '指导老师' not in chart and '荣誉社长' not in chart
    assert '指导老师' in independent and '荣誉社长' in independent


def test_comments_anonymity_deletion_and_post_cascade(site):
    _, admin, member, _ = site
    writer, _ = member('anonymouswriter', direction='算法')
    reader, _ = member('reader', direction='算法')
    topic = new_topic(admin)
    response = post(writer, f'/forum/{topic}/comments', {'body': '**匿名意见**', 'anonymous': '1'})
    assert '<strong>匿名意见</strong>' in response.text
    assert '真实作者：' not in response.text
    visible = reader.get(f'/forum/{topic}')
    assert '匿名成员' in visible.text and 'anonymouswriter' not in visible.text
    assert '真实作者：anonymouswriter' in admin.get(f'/forum/{topic}').text
    assert visible.headers['cache-control'] == 'private, no-store'
    with database.SessionLocal() as db:
        comment_id = db.scalar(select(ForumComment.id))
    denied = post(reader, f'/forum/{topic}/comments/{comment_id}/delete')
    assert '只能删除自己的评论' in denied.text
    bad_csrf = writer.post(f'/forum/{topic}/comments', data={'csrf': 'bad', 'body': 'bad'})
    with database.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ForumComment)) == 1
    post(writer, f'/forum/{topic}/comments/{comment_id}/delete')
    post(writer, f'/forum/{topic}/comments', {'body': '普通意见'})
    assert 'anonymouswriter' in reader.get(f'/forum/{topic}').text
    post(admin, f'/forum/{topic}/delete')
    with database.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ForumComment)) == 0


def test_markdown_formats_and_html_safety(site):
    _, admin, member, _ = site
    source = '''#### 标题

> 引用

1. 第一项
2. 第二项

| 项目 | 值 |
| --- | --- |
| A | 1 |

**粗体** ~~删除~~ [链接](https://example.com) ![图](https://example.com/a.png)

```python
print("**保持原样**")
```

<script>alert(1)</script>

[危险](javascript:alert%281%29)

![危险](data:text/html;base64,abc)
'''
    rendered = render_markdown(source)
    for tag in ['<h4>', '<blockquote>', '<ol>', '<table>', '<strong>', '<s>', '<img ', '<pre><code class="language-python">']:
        assert tag in rendered
    assert '<script>' not in rendered and 'href="javascript:' not in rendered and 'src="data:text/html' not in rendered
    assert '**保持原样**' in rendered
    client, _ = member('journalwriter')
    journal = post(client, '/journal', {'title': '富格式日记', 'body': source})
    assert '<table>' in journal.text
    assert admin.get(journal.url.path, follow_redirects=True).url.path == '/journal'
    topic = new_topic(client, body=source)
    assert '<table>' in client.get(f'/forum/{topic}').text


def attachments():
    return [('invoices', ('发票.pdf', PDF, 'application/pdf')), ('invoices', ('发票.png', PNG, 'image/png')), ('qr', ('收款.png', PNG, 'image/png'))]


def test_claim_materials_access_validation_and_existing_workflow(site):
    _, admin, member, root = site
    applicant, _ = member('applicant')
    other, _ = member('outsider')
    missing = post(applicant, '/finance/claims', {'amount': '12.30', 'reason': '材料'})
    assert '请上传' in missing.text
    fake = post(applicant, '/finance/claims', {'amount': '12.30', 'reason': '材料'}, files=[('invoices', ('x.pdf', b'not a pdf', 'application/pdf')), ('qr', ('qr.png', PNG, 'image/png'))])
    assert '文件内容与格式不符' in fake.text
    for amount in ['NaN', 'Infinity', '-1', '0', '1.234']:
        post(applicant, '/finance/claims', {'amount': amount, 'reason': '无效金额'}, files=attachments())
    with database.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Reimbursement)) == 0
    assert not list((root / 'uploads').rglob('*.pdf'))
    done = post(applicant, '/finance/claims', {'amount': '12.30', 'reason': '材料费用'}, files=attachments())
    assert '报销已提交' in done.text
    with database.SessionLocal() as db:
        claim = db.scalar(select(Reimbursement))
        claim_id = claim.id
        assert claim.amount_cents == 1230
        files = list(db.scalars(select(ReimbursementFile)))
    assert len(files) == 3
    for file in files:
        path = f'/finance/claims/{claim_id}/files/{file.id}'
        assert applicant.get(path).status_code == 200
        assert admin.get(path).status_code == 200
        assert other.get(path).status_code == 404
        assert applicant.get(f'/finance/claims/{claim_id + 1}/files/{file.id}').status_code == 404
        assert applicant.get(path).headers['cache-control'] == 'private, no-store'
    assert '材料费用' not in other.get('/finance').text
    post(admin, f'/finance/claims/{claim_id}/review', {'decision': 'approved', 'comment': '同意'})
    post(admin, f'/finance/claims/{claim_id}/pay')
    with database.SessionLocal() as db:
        assert db.get(Reimbursement, claim_id).status == 'paid'
    assert '12.30' in admin.get('/finance').text


def test_claim_file_failure_rolls_back_database_and_disk(site, monkeypatch):
    _, _, member, root = site
    client, _ = member('diskfailure')
    original = Path.write_bytes
    def broken(path, content):
        if path.suffix == '.png':
            raise OSError('disk full')
        return original(path, content)
    monkeypatch.setattr(Path, 'write_bytes', broken)
    result = post(client, '/finance/claims', {'amount': '1', 'reason': '异常上传'}, files=attachments())
    assert '报销未提交' in result.text
    with database.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Reimbursement)) == 0
        assert db.scalar(select(func.count()).select_from(ReimbursementFile)) == 0
    assert not list((root / 'uploads').rglob('*.pdf'))


def test_honor_only_issues_own_activity(site):
    _, admin, member, _ = site
    honor, _ = member('honorpresident', ['荣誉社长'])
    minister, _ = member('minister', ['技术部部长'])
    now = datetime.now(timezone.utc)
    fields = {'title': '荣誉社长活动', 'department': '全社', 'start_at': now.isoformat(), 'end_at': (now + timedelta(days=1)).isoformat(), 'capacity': '0'}
    created = post(honor, '/activities', fields)
    assert created.url.path.startswith('/activities/')
    own = created.url.path
    assert '发放活动' in created.text
    assert '荣誉社长活动' in honor.get('/approvals').text
    other = post(minister, '/activities', {**fields, 'title': '别人草稿', 'department': '技术部'}).url.path
    assert '别人草稿' not in honor.get('/activities').text
    post(honor, other + '/issue')
    post(honor, other + '/reject', {'comment': '越权'})
    with database.SessionLocal() as db:
        assert db.get(Activity, int(other.rsplit('/', 1)[1])).status == 'draft'
    issued = post(honor, own + '/issue')
    assert '活动已发放' in issued.text
    assert '结束活动' in issued.text
    assert '荣誉社长活动' in minister.get('/activities').text
    assert '荣誉社长活动' not in honor.get('/approvals').text


def test_old_database_upgrade_keeps_data_and_is_repeatable(tmp_path):
    # DDL captured from the pre-change fd26612 schema; no Git history is needed in CI.
    import os
    import sqlite3
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{tmp_path / "legacy.db"}', 'UPLOAD_DIR': str(tmp_path / 'uploads')}
    with sqlite3.connect(tmp_path / 'legacy.db') as db:
        db.executescript((repo / 'tests' / 'fixtures' / 'legacy_schema.sql').read_text())
        db.execute("INSERT INTO users (id,username,password_hash,real_name,phone,college,class_name,department,status,is_admin,created_at) VALUES (1,'admin','retained-hash','原管理员','','','','','active',1,'2026-09-01 00:00:00')")
        db.execute("INSERT INTO reimbursements VALUES (1,1,9876,'历史报销','approved',NULL,'','2026-09-01 00:00:00')")
        db.execute("INSERT INTO journals VALUES (1,1,'历史日记','**不丢失**','2026-09-01 00:00:00','2026-09-01 00:00:00')")
        db.execute("INSERT INTO forum_posts VALUES (1,1,'历史帖','旧内容','club','','','2026-09-01 00:00:00')")
    (tmp_path / 'uploads').mkdir()
    saved = tmp_path / 'uploads' / 'old.pdf'
    saved.write_bytes(PDF)
    subprocess.run([sys.executable, '-c', '''from app.main import app
from app.db import init_db, SessionLocal
from app.config import load_settings
from app.models import Reimbursement, Journal, ForumPost, ForumComment, ReimbursementFile
from sqlalchemy import select, func
init_db(load_settings())
init_db(load_settings())
with SessionLocal() as db:
    from app.models import User
    assert db.get(User, 1).password_hash == "retained-hash"
    assert db.scalar(select(Reimbursement)).amount_cents == 9876
    assert db.scalar(select(Journal)).body == "**不丢失**"
    assert db.scalar(select(ForumPost)).body == "旧内容"
    assert db.scalar(select(func.count()).select_from(ForumComment)) == 0
    assert db.scalar(select(func.count()).select_from(ReimbursementFile)) == 0
from fastapi.testclient import TestClient
from app.security import issue_token
client=TestClient(app)
client.cookies.set("club_jwt", issue_token(load_settings().secret_key, 1))
assert "历史报销：未上传附件" in client.get("/finance").text
'''], cwd=repo, env=env, check=True)
    assert saved.read_bytes() == PDF
