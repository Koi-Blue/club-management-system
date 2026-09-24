# 社团管理系统

单个社团的内部管理站点，使用 **Python 3.12 / FastAPI / SQLAlchemy / Jinja2 / SQLite**，通过 Docker Compose 部署。成员注册后由超级管理员审批，一个人可以同时担任多个职务。

## 功能与权限

- 组织架构：社长 → 副社长 → 部门部长 → 负责人/成员，以父子节点和连线展示。指导老师、荣誉社长在旁边独立展示，不参与上下级架构。
- 技术方向：嵌入式软件、算法、硬件、机械。技术成员位于对应方向负责人下面，方向负责人仍归技术部部长管理。
- 账号：手机号或用户名登录；注册填写学院、班级和部门，技术部可选择方向；超级管理员审批、分配职务、停用或注销账号。
- 公告、审批待办、请假、值班、立项及项目资料、部门资料库、会议纪要、资产借用。
- 活动：草稿、发放、报名、签到、结束。荣誉社长可创建、发放、管理**自己创建的活动**，不因此获得其他人的草稿审批权；原有社长等角色的权限保持不变。活动发放后全社可报名。
- 日记：仅本人可见，保留 Markdown 源文供编辑，阅读时渲染格式。
- 论坛：帖子、普通评论、匿名评论，正文和评论支持 Markdown；暂不支持多层回复。成员可删除本人评论，超级管理员可删除任意评论，删除帖子同时删除评论。
- 报销：金额和事由必填；1～10 份 PDF/JPG/PNG 发票、一张 JPG/PNG 收款二维码；单文件最多 20MB，总计最多 50MB。仅申请人、超级管理员、社长、财务部部长可查看相应报销及附件。保留“申请 → 审批 → 登记付款并记账”流程。

账本仅对超级管理员、社长、财务部部长开放。荣誉社长可查看全部项目并审批立项，不拥有全部业务或财务权限。电话展示沿用原有权限。历史项目参与关系不随论坛角色调整而改变。

### 技术方向与论坛可见范围

超级管理员在「账号与职务」中选择“技术部算法成员”等普通成员职务，可多选方向，也可兼任其他职务；这些成员职务**不授予负责人管理权限**。勾选方向成员后，“技术部成员”待分配职务会自动取消。旧账号没有方向时显示为“技术部成员（待分配方向）”，由超管分配，不自动猜测方向。

| 帖子范围 | 可见人员 |
| --- | --- |
| 全社 | 所有已通过审批、已启用的成员 |
| 部门 | 当前具有该部门职务的成员 |
| 整个技术部 | 当前具有技术部职务的成员 |
| 技术方向 | 该方向成员、对应负责人、技术部部长 |

超级管理员可查看全部帖子，作者始终可查看、删除本人历史帖子。多职务取可见范围并集；新成员审批通过后即可查看对应历史帖子。角色/方向修改后下次请求立即生效，无需重新登录；不再用注册时旧部门赋予论坛权限。发表评论、读评论和删除评论均需仍有帖子查看权限。

匿名评论对其他成员（包括发帖人）仅显示“匿名成员”，真实身份只有超级管理员可见；本人看到“我”标记。不要在评论正文中自行写入需要隐藏的身份信息。

Markdown 支持六级标题、嵌套/有序/无序列表、引用、表格、加粗/斜体/删除线、图片链接、普通链接、行内代码和围栏代码块。原始 HTML 不执行；不提供论坛图片上传，图片使用 Markdown 图片地址。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

修改 `.env` 的 `SECRET_KEY`（至少 16 字符）和 `ADMIN_PASSWORD`（至少 6 字符）。本地运行时改成：

```dotenv
DATABASE_URL=sqlite:///./data/club.db
UPLOAD_DIR=./data/uploads
```

```bash
set -a
source .env
set +a
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

浏览器打开 `http://localhost:8000`。启动会补充缺失的表/字段，不删除旧表或重置业务数据。已有管理员密码不因重启或 `.env` 中密码改变而重置。

## 服务器首次部署

适用环境：Linux、Git、Python 3、Docker Engine 和 Docker Compose v2（`docker compose`）。执行用户需有 Docker 使用权限，服务器需要可以拉取 Git 仓库及构建依赖。

```bash
git clone --branch cursor/club-system-04e1 --single-branch https://github.com/Koi-Blue/club-management-system.git
cd club-management-system
cp .env.example .env
# 编辑 .env，设置本机专用的 SECRET_KEY 和 ADMIN_PASSWORD
./deploy.sh --init
```

服务器 `.env` 保留这两个默认路径：

```dotenv
DATABASE_URL=sqlite:////app/data/club.db
UPLOAD_DIR=/app/data/uploads
```

访问 `http://服务器IP`，宿主机端口 **TCP 80**。SSH 管理使用 **TCP 22**，SQLite 不需要开放数据库端口。

`--init` 只用于首次部署；检测到原容器或目标数据卷已经存在时会拒绝执行。不要删除旧容器后使用 `--init` 尝试“更新”。

## 已有服务器保留数据的一键更新

**在原项目目录执行：**

```bash
./deploy.sh
```

如果服务器还是没有该脚本的旧版本，首次取得脚本：

```bash
cd /你的原项目目录/club-management-system
git status --short
# 有未提交修改时先妥善保存；.env 不应加入 Git。
git switch cursor/club-system-04e1
git pull --ff-only origin cursor/club-system-04e1
./deploy.sh --local
```

`--local` 使用当前已提交代码，不再次拉取；默认 `./deploy.sh` 会拉取目标分支，并在更新后重新加载脚本。仓库存在未提交修改、分支错误、拉取失败或无法快进时停止，不强行覆盖代码。

### 更新脚本具体保留什么

1. 识别现有 `club-management` 容器、原 Compose 项目名及 `/app/data` **实际数据卷名**；不是仅根据当前目录名推测卷名。
2. 构建新镜像时原服务仍运行；构建失败不停止原服务。
3. 保存旧镜像标签、部署信息及 `.env` 到 `backups/UTC时间/`。
4. 短暂停服，复制整个 `/app/data`，通过 SQLite backup API 生成包含已提交 WAL 数据的一致性数据库快照，验证数据库完整性，连同全部上传文件生成 `data.tar.gz`。
5. 使用原数据卷替换应用容器；启动时增量创建评论表、报销附件表，原账号、日记、论坛、报销等数据不重建。旧报销显示“历史报销：未上传附件”，仍可按原流程处理。
6. 再次核对数据卷，检查 `/healthz`。备份失败时恢复原容器；启动失败时尝试恢复旧镜像，并返回失败退出码。**不会自动用旧备份覆盖数据库**。

过程中有短暂维护窗口，时长取决于数据量和启动速度。请留出备份压缩文件及临时副本所需磁盘空间。备份包含账号数据、发票和二维码，应妥善保管；`backups/`、`.env`、本地数据和开发目录均排除在 Git/Docker 构建上下文之外。

### 为什么重建容器仍能保留账号

Compose 的逻辑卷为 `club-data`，实际卷名通常为 `<Compose项目名>_club-data`。数据库 `/app/data/club.db` 和上传文件 `/app/data/uploads` 在这个持久卷里，应用镜像不包含业务数据。一键脚本将本次部署明确绑定到原容器的实际卷，即使项目目录改变，也不会悄悄连到另一个空卷。

**不要执行 `docker compose down -v`，不要删除实际数据卷，也不要把新克隆目录中的普通 `docker compose up` 当作已有系统的数据迁移。** 单独复制运行中的 `club.db` 可能遗漏 WAL 中的事务；应使用上述停服一致性备份。

### 检查部署结果

```bash
docker ps --filter name=club-management
docker logs --tail=100 club-management
docker inspect club-management --format '{{range .Mounts}}{{println .Destination .Name}}{{end}}'
```

登录后检查旧账号、项目、上传资料及旧报销仍存在，再检查论坛评论和新增材料上传。

## 更新失败与备份恢复

脚本报错即表示更新未完成，不应仅凭容器存在判断成功。优先看终端错误和容器日志；若脚本已恢复旧镜像，修复错误后重新运行 `./deploy.sh`。旧镜像的 `club-management:backup-…` 标签和具体卷名保存在备份目录 `deployment.json` 中，不要在确认升级稳定之前清理旧镜像或备份。

确需恢复更新前的数据时，以下操作会把站点恢复到备份时间点；备份之后的数据不会出现在恢复后的站点中。**使用一个新数据卷恢复，原卷保留以便核对。** 在原项目目录执行，先替换备份目录：

```bash
BACKUP_DIR="$(realpath backups/替换为实际备份时间目录)"
OLD_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_tag"])' "$BACKUP_DIR/deployment.json")"
OLD_PROJECT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$BACKUP_DIR/deployment.json")"
RESTORE_VOLUME="club-recovery-$(date +%Y%m%d%H%M%S)"

test -s "$BACKUP_DIR/data.tar.gz" || exit 1
docker image inspect "$OLD_IMAGE" >/dev/null || exit 1
docker volume create "$RESTORE_VOLUME"
# 只对自己保管的可信备份执行解包。
docker run --rm --network none \
  --mount "type=volume,src=$RESTORE_VOLUME,dst=/app/data" \
  --mount "type=bind,src=$BACKUP_DIR,dst=/backup,readonly" \
  "$OLD_IMAGE" python -c 'import tarfile; tarfile.open("/backup/data.tar.gz").extractall("/app", filter="data")'
# 校验恢复的数据库；失败时不要继续。
docker run --rm --network none \
  --mount "type=volume,src=$RESTORE_VOLUME,dst=/app/data" \
  "$OLD_IMAGE" python -c 'import sqlite3; db=sqlite3.connect("file:/app/data/club.db?mode=ro", uri=True); assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"' || exit 1

cp .env "$BACKUP_DIR/env-before-restore"
chmod 600 "$BACKUP_DIR/env-before-restore"
cp "$BACKUP_DIR/.env" .env
chmod 600 .env
RESTORE_CONFIG="$(mktemp)"
python3 -c 'import json,sys; print(json.dumps({"services":{"club":{"image":sys.argv[1]}},"volumes":{"club-data":{"external":True,"name":sys.argv[2]}}}))' "$OLD_IMAGE" "$RESTORE_VOLUME" > "$RESTORE_CONFIG"
docker compose -p "$OLD_PROJECT" -f docker-compose.yml -f "$RESTORE_CONFIG" up -d --no-build --pull never club
rm -f "$RESTORE_CONFIG"
docker exec club-management python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/healthz").read().decode())'
```

恢复后下一次运行 `./deploy.sh` 会识别并继续使用恢复卷。原卷没有被删除，确认恢复正确后再由运维决定如何保留。

## 自定义部署与注意事项

一键脚本仅适配本仓库默认的 **SQLite + `/app/data` 命名卷 + 单个 Compose `club` 服务**。若使用外部数据库、绑定目录、自定义数据库/上传目录、额外挂载或非 Compose 容器，脚本会在替换服务前停止；此时应先确认现有挂载、备份数据库和上传目录，再按实际部署调整流程，不能改回默认路径掩盖差异。

运行脚本不需要也不会把服务器 `.env`、数据库或备份上传到 GitHub；未连接服务器时，代码仓库中的脚本不会自动在服务器执行。

## 开发验证

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
bash -n deploy.sh
```

测试覆盖主要业务回归、方向变更与历史帖子权限、匿名身份隔离、Markdown、报销材料权限、荣誉社长活动边界、旧数据库增量初始化及含 WAL 的备份恢复。部署控制流程通过模拟 Docker 命令验证；正式发布前仍应在实际 Docker 环境执行升级演练。
