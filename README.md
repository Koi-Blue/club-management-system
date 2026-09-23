# 社团管理系统

单个社团的内部管理站点。成员注册后由超级管理员审批，一个人可以同时担任多个职务。

## 能做什么

- 组织架构：指导老师、社长、副社长、四个部门的部长和成员、技术部四位负责人、荣誉社长。可兼任，按层级展开，显示班级、姓名和电话
- 账号：手机号或用户名登录；注册填写学院、班级和部门；超级管理员审批、分配职务和删除成员
- 审批待办、公告、请假、值班
- 立项与项目资料：草稿、审批、归档
- 部门资料库、会议纪要
- 资产借用：库存、申请、审批、归还
- 活动：草稿、发放、报名、签到
- 财务：收支账和报销。账本只对超级管理员、社长、财务部部长开放

可见范围：荣誉社长可看全部业务；部长和技术负责人管理本部门立项、资料和活动草稿；普通成员看本部门资料和自己的项目，可报名全社活动、申请借用和报销。财务账只对超级管理员、社长和财务部部长可见。

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
