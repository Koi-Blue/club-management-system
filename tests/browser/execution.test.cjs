const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.CLUB_PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '../..');
const temp = mkdtempSync(path.join(tmpdir(), 'club-execution-browser-'));
const python = process.env.CLUB_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const base = 'http://127.0.0.1:18766';
const env = { ...process.env, SECRET_KEY: 'execution-browser-secret-0123456789', ADMIN_USERNAME: 'admin', ADMIN_PASSWORD: 'browser123',
  DATABASE_URL: `sqlite:///${temp}/club.db`, UPLOAD_DIR: `${temp}/uploads`, CAPTCHA_DISABLED: '1', PYTHONIOENCODING: 'utf-8' };
let server, browser, ids;
let logs = '';

before(async () => {
  const seed = spawnSync(python, ['-c', `
import json
from app.main import app
from app.db import SessionLocal
from app.models import User, UserRole, Project, ProjectMember
from app.security import hash_password
from app import project_execution as ex
with SessionLocal() as db:
    people = {}
    for name, label in [('manager', '项目负责人'), ('worker', '任务负责人'), ('reviewer', '独立验收人')]:
        user = User(username=name, password_hash=hash_password('testpass'), real_name=label, department='技术部', status='active')
        db.add(user); db.flush(); db.add(UserRole(user_id=user.id, role='技术部成员'))
        people[name] = user
    db.commit()
    manager, worker, reviewer = [people[k] for k in ['manager', 'worker', 'reviewer']]
    project = Project(title='力传感器研发项目', department='技术部', leader_id=manager.id, manager_id=manager.id, status='approved', execution_enabled=True)
    db.add(project); db.flush()
    for user in [worker, reviewer]: db.add(ProjectMember(project_id=project.id, user_id=user.id))
    db.commit()
    stage = ex.save_stage(db, manager, [], project.id, {'title':'测试与标定', 'position':1})
    data = {'stage_id':stage.id, 'owner_id':worker.id, 'title':'测试夹具安装与检查', 'planned_start':'2026-10-20', 'planned_end':'2026-10-25', 'deliverable':'安装检查记录', 'priority':'medium'}
    ordinary = ex.save_task(db, manager, [], project.id, data)
    data.update(title='静态标定与线性拟合验证', is_key='1', reviewer_id=reviewer.id, deliverable='标定数据和线性拟合报告', acceptance_criteria='满足规格书精度要求', predecessor_ids=[ordinary.id])
    key = ex.save_task(db, manager, [], project.id, data)
    milestone = ex.save_milestone(db, manager, [], project.id, {'title':'标定阶段通过', 'stage_id':stage.id, 'owner_id':worker.id, 'reviewer_id':reviewer.id, 'acceptance_criteria':'两个任务完成且结果符合精度要求', 'planned_date':'2026-10-28', 'task_ids':[ordinary.id,key.id]})
    print(json.dumps({'project':project.id,'stage':stage.id,'ordinary':ordinary.id,'key':key.id,'milestone':milestone.id,'manager':manager.id,'worker':worker.id,'reviewer':reviewer.id}))
`], { cwd: root, env, encoding: 'utf8', windowsHide: true });
  assert.equal(seed.status, 0, seed.stderr);
  ids = JSON.parse(seed.stdout.trim());
  server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '18766'], { cwd: root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stdout.on('data', chunk => { logs += chunk; });
  server.stderr.on('data', chunk => { logs += chunk; });
  let ready = false;
  for (let i=0; i<100; i++) {
    try { if ((await fetch(base + '/healthz')).ok) { ready=true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert(ready, logs);
  browser = await chromium.launch({ headless: true, executablePath: process.env.CLUB_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
});

after(async () => {
  await browser?.close();
  if (server && server.exitCode === null) {
    const closed = new Promise(resolve => server.once('exit', resolve));
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/PID', String(server.pid), '/T', '/F'], { windowsHide: true });
    } else {
      server.kill('SIGTERM');
    }
    await closed;
  }
  assert(path.resolve(temp).startsWith(path.resolve(tmpdir()) + path.sep));
  assert(path.basename(temp).startsWith('club-execution-browser-'));
  rmSync(temp, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
});

async function login(t, who, mobile=false) {
  const context = await browser.newContext(mobile ? { viewport:{width:390,height:844}, isMobile:true, hasTouch:true } : { viewport:{width:1440,height:1000} });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.goto(base + '/login');
  await page.locator('[name=account]').fill(who);
  await page.locator('[name=password]').fill('testpass');
  await page.locator('[name=captcha]').fill('0000');
  await page.locator('button[type=submit]').click();
  await page.waitForURL(base + '/');
  return page;
}

function taskUrl(id) { return `${base}/projects/${ids.project}/execution/tasks/${id}`; }
function milestoneUrl() { return `${base}/projects/${ids.project}/execution/milestones/${ids.milestone}`; }
async function submitForm(page, form, selector='button[type=submit]') {
  await Promise.all([page.waitForNavigation(), form.locator(selector).click()]);
  assert.equal(await page.locator('.flash.error').count(), 0, await page.locator('.main').innerText());
}
async function noHorizontalOverflow(page) {
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert(await page.locator('.main').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
}

test('desktop owner completes ordinary work, key submission is independently reviewed, milestone is accepted', async t => {
  const worker = await login(t, 'worker');
  await worker.goto(taskUrl(ids.ordinary));
  let form = worker.locator('form[action$="/progress"]').first();
  await form.locator('[name=status]').selectOption('done');
  await form.locator('[name=note]').fill('夹具安装完成，检查记录已确认');
  await submitForm(worker, form);
  assert((await worker.locator('.page-head').innerText()).includes('已完成'));
  await worker.goto(taskUrl(ids.key));
  form = worker.locator('form[action$="/submit"]');
  await form.locator('[name=note]').fill('静态标定与拟合结果满足精度要求');
  await submitForm(worker, form);
  assert((await worker.locator('.page-head').innerText()).includes('待验收'));
  assert.equal(await worker.locator('form[action$="/review"]').count(),0);
  if (process.env.CLUB_UI_SCREENSHOTS) await worker.screenshot({path:path.join(process.env.CLUB_UI_SCREENSHOTS,'execution-desktop.png')});
  await worker.getByText('撤回本次验收',{exact:true}).click();
  form = worker.locator('form[action$="/withdraw"]');
  await form.locator('[name=reason]').fill('补充测试环境说明');
  await submitForm(worker, form);
  assert((await worker.locator('.main').innerText()).includes('已撤回'));
  form = worker.locator('form[action$="/submit"]');
  await form.locator('[name=note]').fill('补充环境温度与采样频率，重新提交');
  await submitForm(worker, form);

  const reviewer = await login(t, 'reviewer');
  await reviewer.goto(base + '/project-todos');
  assert(await reviewer.locator(`a[href="/projects/${ids.project}/execution/tasks/${ids.key}"]`).count());
  await reviewer.goto(taskUrl(ids.key));
  form = reviewer.locator('form[action$="/review"]');
  await form.locator('[name=comment]').fill('验收通过：精度符合规格书');
  await submitForm(reviewer, form, 'button[value=approved]');
  assert((await reviewer.locator('.page-head').innerText()).includes('已完成'));
  await worker.goto(milestoneUrl());
  form = worker.locator('form[action$="/submit"]');
  await form.locator('[name=note]').fill('全部关联任务完成，申请阶段验收');
  await submitForm(worker, form);
  await reviewer.goto(milestoneUrl());
  form = reviewer.locator('form[action$="/review"]');
  await form.locator('[name=comment]').fill('阶段成果通过');
  await submitForm(reviewer, form, 'button[value=approved]');
  assert((await reviewer.locator('.page-head').innerText()).includes('已通过'));
  await noHorizontalOverflow(reviewer);
});

test('390px workspace cards, task filters and real plan form remain usable without horizontal overflow', async t => {
  const page = await login(t, 'manager', true);
  await page.goto(`${base}/projects/${ids.project}/execution`);
  await noHorizontalOverflow(page);
  assert.equal(await page.locator('.execution-task-table thead').isVisible(), false);
  await page.locator('.execution-filters [name=status]').selectOption('done');
  await page.locator('.execution-filters[method=get] button[type=submit]').click();
  await page.waitForURL(/status=done/);
  assert.equal(await page.locator('.execution-task-table tbody tr').count(), 2);
  if (process.env.CLUB_UI_SCREENSHOTS) {
    await page.screenshot({path:path.join(process.env.CLUB_UI_SCREENSHOTS,'execution-mobile.png')});
    await page.locator('.execution-task-table').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(process.env.CLUB_UI_SCREENSHOTS,'execution-mobile-tasks.png')});
  }
  await page.goto(`${base}/projects/${ids.project}/execution/tasks/new`);
  await noHorizontalOverflow(page);
  const form = page.locator('.execution-form');
  await form.locator('[name=title]').fill('手机新增交接任务');
  await form.locator('[name=stage_id]').selectOption(String(ids.stage));
  await form.locator('[name=owner_id]').selectOption(String(ids.worker));
  await form.locator('[name=planned_start]').fill('2026-10-29');
  await form.locator('[name=planned_end]').fill('2026-10-30');
  await form.locator('[name=deliverable]').fill('项目交接清单');
  await form.locator(`[name=collaborator_ids][value="${ids.reviewer}"]`).check();
  await form.locator(`[name=predecessor_ids][value="${ids.key}"]`).check();
  if (process.env.CLUB_UI_SCREENSHOTS) await page.screenshot({path:path.join(process.env.CLUB_UI_SCREENSHOTS,'execution-mobile-form.png')});
  await submitForm(page, form);
  assert((await page.locator('.main').innerText()).includes('手机新增交接任务'));
  await noHorizontalOverflow(page);
});
