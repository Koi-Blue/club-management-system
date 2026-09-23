# 社团管理系统

单个社团的内部管理站点。成员注册后由超级管理员审批，一个人可以同时担任多个职务。

## 能做什么

- 组织：社长、副社长、指导老师、荣誉社长、部长、成员
- 立项与资料留存：提交、审批、上传文件、归档
- 资产借用：登记库存、申请、审批、归还
- 活动：创建、发放、报名、签到码签到、补签
- 公告

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

修改 `.env` 里的 `SECRET_KEY` 和 `ADMIN_PASSWORD`，本地数据库可以写成：

```bash
DATABASE_URL=sqlite:///./data/club.db
UPLOAD_DIR=./data/uploads
```

```bash
set -a && source .env && set +a
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 服务器部署

服务器需要 Docker。在项目目录准备只属于这台机器的 `.env`，不要提交到仓库：

```bash
docker compose up -d --build
```

站点映射到宿主机 **80** 端口。数据库和上传文件在 Docker 卷 `club-data` 里，不对公网开放。

备份：

```bash
docker cp club-management:/app/data/club.db ./club-backup.db
```

## 开放端口

浏览器访问使用 **TCP 80**。管理服务器继续使用 **TCP 22**。不要把数据库暴露到公网。
