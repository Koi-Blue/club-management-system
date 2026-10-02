const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.CLUB_PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '../..');
const temp = mkdtempSync(path.join(tmpdir(), 'club-browser-'));
const python = process.env.CLUB_TEST_PYTHON || path.join(root, '.venv/bin/python');
const base = 'http://127.0.0.1:18765';
const env = { ...process.env, SECRET_KEY: 'browser-test-secret-0123456789', ADMIN_USERNAME: 'admin', ADMIN_PASSWORD: 'browser123',
  DATABASE_URL: `sqlite:///${temp}/club.db`, UPLOAD_DIR: `${temp}/uploads`, CAPTCHA_DISABLED: '1' };
let server;
let browser;
let logs = '';

before(async () => {
  const seed = spawnSync(python, ['-c', `
from app.main import app
from app.db import SessionLocal
from app.models import User, UserRole, ForumPost, Journal
from app.org import CLUB_ROLES
from app.security import hash_password
from app.services import save_library_file
import os
with SessionLocal() as db:
    for i, role in enumerate(CLUB_ROLES):
        user = User(username=f'person{i}', password_hash=hash_password('testpass'), real_name=f'测试成员{i}', college='信息学院', class_name='机器人2401', phone='13800138000', department='技术部', status='active')
        db.add(user); db.flush(); db.add(UserRole(user_id=user.id, role=role))
    for i in range(40):
        db.add(ForumPost(author_id=1, title=f'技术讨论 {i}', body='## 计划\\n\\n- 硬件\\n- 软件', scope='club'))
        db.add(Journal(user_id=1, title=f'实验日记 {i}', body='**实验内容**'))
    db.commit()
    save_library_file(db, db.get(User, 1), [], os.environ['UPLOAD_DIR'], '技术部', '资料.txt', b'test document')
`], { cwd: root, env, encoding: 'utf8' });
  assert.equal(seed.status, 0, seed.stderr);
  server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '18765'], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stdout.on('data', chunk => { logs += chunk; });
  server.stderr.on('data', chunk => { logs += chunk; });
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(base + '/healthz')).ok) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert(ready, logs);
  browser = await chromium.launch({ headless: true, executablePath: process.env.CLUB_BROWSER_EXECUTABLE || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', ...(process.env.CLUB_BROWSER_EXECUTABLE ? ['--use-gl=angle', '--use-angle=swiftshader'] : [])] });
});

after(async () => {
  await browser?.close();
  if (server) {
    const closed = new Promise(resolve => server.once('exit', resolve));
    server.kill('SIGTERM');
    await closed;
  }
  rmSync(temp, { recursive: true, force: true });
});

async function loggedIn(t, mobile = false) {
  const context = await browser.newContext(mobile ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } : { viewport: { width: 1280, height: 620 } });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.goto(base + '/login');
  await page.locator('[name=account]').fill('admin');
  await page.locator('[name=password]').fill('browser123');
  await page.locator('[name=captcha]').fill('0000');
  await page.locator('button[type=submit]').click();
  await page.waitForURL(base + '/');
  await page.waitForFunction(() => !!history.state?.clubNavigation);
  await page.evaluate(() => { window.shellMarker = 71; });
  return page;
}

async function settled(page, pathname) {
  await page.waitForURL(base + pathname);
  await page.waitForFunction(() => document.querySelectorAll('.page-viewport > .main').length === 1 && !document.querySelector('.page-viewport').hasAttribute('aria-busy'));
}

async function module(page, pathname) {
  await page.locator(`.nav a[href="${pathname}"]`).click();
  await settled(page, pathname);
}

async function touchSwipe(page, dx) {
  const client = await page.context().newCDPSession(page);
  await page.locator('.main').evaluate(el => { el.scrollTop = 0; });
  const rect = await page.locator('.main .page-head').boundingBox();
  const x = dx < 0 ? 310 : 70;
  const y = rect.y + rect.height - 5;
  await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
  for (let i = 1; i <= 5; i++) await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + dx * i / 5, y }] });
  await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await client.detach();
}

test('desktop menu position, shell identity and history content position survive navigation/reload', async t => {
  const page = await loggedIn(t);
  await module(page, '/forum');
  await page.locator('.main').evaluate(el => { el.scrollTop = 600; });
  await module(page, '/forum');
  assert.equal(await page.evaluate(() => window.shellMarker), 71);
  assert.equal(await page.locator('.main').evaluate(el => el.scrollTop), 600);
  await page.locator('.nav').evaluate(el => { el.scrollTop = el.scrollHeight; });
  const menu = await page.locator('.nav').evaluate(el => el.scrollTop);
  assert(menu > 100);
  await module(page, '/finance');
  assert.equal(await page.evaluate(() => window.shellMarker), 71);
  assert(Math.abs(await page.locator('.nav').evaluate(el => el.scrollTop) - menu) < 2);
  await page.goBack();
  await settled(page, '/forum');
  assert.equal(await page.locator('.main').evaluate(el => el.scrollTop), 600);
  await page.goForward();
  await settled(page, '/finance');
  await page.reload();
  assert(Math.abs(await page.locator('.nav').evaluate(el => el.scrollTop) - menu) < 2);
});

test('mobile modules slide simultaneously while header stays fixed, strip position survives a POST', async t => {
  const page = await loggedIn(t, true);
  await module(page, '/forum');
  const header = await page.locator('.sidebar').boundingBox();
  const menu = await page.locator('.nav').evaluate(el => el.scrollLeft);
  await page.locator('.nav a[href="/journal"]').click();
  await page.waitForFunction(() => location.pathname === '/journal' && document.querySelectorAll('.page-viewport > .main').length === 2);
  const motion = await page.locator('.page-viewport').evaluate(el => [...el.querySelectorAll('.main')].map(page => page.getAnimations()[0]?.effect.getKeyframes().map(frame => frame.transform)));
  assert.equal(motion.length, 2);
  assert(motion.every(frames => frames.some(value => value.includes('100%'))));
  assert.deepEqual(await page.locator('.sidebar').boundingBox(), header);
  await settled(page, '/journal');
  assert.equal(await page.evaluate(() => window.shellMarker), 71);
  assert(Math.abs(await page.locator('.nav').evaluate(el => el.scrollLeft) - menu) < 2);
  await page.locator('a[href="/journal/new"]').click();
  await settled(page, '/journal/new');
  await page.locator('[name=title]').fill('手机保存日记');
  await page.locator('[name=body]').fill('**表单保持可用**');
  const before = await page.locator('.nav').evaluate(el => el.scrollLeft);
  await page.locator('.main button[type=submit]').first().click();
  await page.waitForURL(/\/journal\/\d+$/);
  assert(await page.locator('.prose strong').isVisible());
  assert(Math.abs(await page.locator('.nav').evaluate(el => el.scrollLeft) - before) < 2);
});

test('mobile swipe changes adjacent modules, but edited forms and chart panning do not navigate', async t => {
  const page = await loggedIn(t, true);
  await module(page, '/activities');
  await touchSwipe(page, -180);
  await settled(page, '/leave');
  await touchSwipe(page, 180);
  await settled(page, '/activities');
  await module(page, '/forum');
  await page.locator('[name=title]').fill('未提交的草稿');
  await touchSwipe(page, -180);
  await page.waitForTimeout(300);
  assert.equal(new URL(page.url()).pathname, '/forum');
  assert.equal(await page.locator('[name=title]').inputValue(), '未提交的草稿');
  await module(page, '/org');
  const chart = await page.locator('.org-chart').boundingBox();
  const client = await page.context().newCDPSession(page);
  const before = await page.locator('.org-chart').evaluate(el => el.scrollLeft);
  await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: chart.x + 280, y: chart.y + 40 }] });
  await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: chart.x + 70, y: chart.y + 40 }] });
  await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(250);
  assert.equal(new URL(page.url()).pathname, '/org');
  assert((await page.locator('.org-chart').evaluate(el => el.scrollLeft)) > before);
  await client.detach();
});

test('organization nodes flow down, siblings align, and the mobile document does not overflow', async t => {
  const page = await loggedIn(t, true);
  await module(page, '/org');
  const geometry = await page.locator('.org-tree').evaluate(tree => {
    const president = tree.querySelector(':scope > li');
    const vice = president.querySelector(':scope > ul > li');
    const departments = [...vice.querySelectorAll(':scope > ul > li > .org-node')];
    const rect = el => { const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, x: r.x, center: r.x + r.width / 2 }; };
    return { president: rect(president.querySelector(':scope > .org-node')), vice: rect(vice.querySelector(':scope > .org-node')), departments: departments.map(rect) };
  });
  assert(geometry.vice.top > geometry.president.bottom);
  assert(geometry.departments.every(node => node.top > geometry.vice.bottom));
  assert(Math.abs(geometry.president.center - geometry.vice.center) < 1);
  assert(geometry.departments.every(node => Math.abs(node.top - geometry.departments[0].top) < 1));
  assert(new Set(geometry.departments.map(node => node.x)).size === 4);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert(!await page.locator('.org-tree').getByText('指导老师', { exact: true }).count());
  assert(await page.locator('.org-independent').getByText('指导老师').count());
  if (process.env.CLUB_UI_SCREENSHOTS) {
    await page.screenshot({ path: path.join(process.env.CLUB_UI_SCREENSHOTS, 'org-mobile.png') });
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.screenshot({ path: path.join(process.env.CLUB_UI_SCREENSHOTS, 'org-desktop.png') });
  }
});

test('rapid navigation keeps the latest response; reduced motion and unavailable storage remain usable', async t => {
  const page = await loggedIn(t, true);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.evaluate(() => { Storage.prototype.setItem = () => { throw new Error('disabled'); }; });
  await page.route('**/journal', async route => {
    if (route.request().resourceType() === 'fetch') await new Promise(resolve => setTimeout(resolve, 300));
    try { await route.continue(); } catch { /* superseded request */ }
  });
  await page.locator('.nav a[href="/journal"]').click();
  await page.locator('.nav a[href="/finance"]').click();
  await settled(page, '/finance');
  await page.waitForTimeout(400);
  assert.equal(new URL(page.url()).pathname, '/finance');
  assert.equal(await page.locator('.main').count(), 1);
  assert.equal(await page.locator('.main').evaluate(el => el.getAnimations().length), 0);
  assert.equal(await page.evaluate(() => window.shellMarker), 71);
});

test('downloads remain downloads and expired sessions return to login', async t => {
  const page = await loggedIn(t);
  await module(page, '/library');
  const downloadEvent = page.waitForEvent('download');
  await page.locator('a[href="/library/1"]').click();
  const download = await downloadEvent;
  assert.equal(download.suggestedFilename(), '资料.txt');
  assert.equal(new URL(page.url()).pathname, '/library');
  await page.context().clearCookies();
  await page.locator('.nav a[href="/members"]').click();
  await page.waitForURL(base + '/login');
  assert(await page.locator('[name=account]').isVisible());
});
