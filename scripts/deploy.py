#!/usr/bin/env python3
"""Update the existing Compose deployment without changing its physical data volume."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BRANCH = "cursor/club-system-04e1"
CONTAINER = "club-management"
HEALTH = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"


def run(*args, capture=False, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, text=capture,
                          stdout=subprocess.PIPE if capture else kwargs.pop("stdout", None), **kwargs)


def output(*args):
    return run(*args, capture=True).stdout.strip()


def inspect_container():
    ids = output("docker", "ps", "-aq", "--filter", "name=^/club-management$")
    return json.loads(output("docker", "inspect", CONTAINER))[0] if ids else None


def existing_storage(container):
    mounts = [m for m in container["Mounts"] if m["Destination"] == "/app/data"]
    if len(mounts) != 1 or mounts[0]["Type"] != "volume":
        raise RuntimeError("此脚本仅更新 /app/data 使用命名卷的部署；检测到自定义挂载，未修改服务")
    env = dict(item.split("=", 1) for item in container["Config"]["Env"] if "=" in item)
    if env.get("DATABASE_URL") != "sqlite:////app/data/club.db" or env.get("UPLOAD_DIR") != "/app/data/uploads":
        raise RuntimeError("检测到自定义数据库或上传目录，请按 README 的自定义部署说明处理")
    labels = container["Config"].get("Labels") or {}
    if not labels.get("com.docker.compose.project") or labels.get("com.docker.compose.service") != "club":
        raise RuntimeError("现有容器不是本项目的 Compose club 服务，停止更新")
    return labels["com.docker.compose.project"], mounts[0]["Name"]


def wait_healthy():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(["docker", "exec", CONTAINER, "python", "-c", HEALTH],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    raise RuntimeError("服务在 60 秒内未通过健康检查")


def deploy(args):
    if not (ROOT / ".env").is_file():
        raise RuntimeError("缺少 .env；首次部署请先复制 .env.example 并设置密钥及密码")
    if output("git", "branch", "--show-current") != BRANCH:
        raise RuntimeError(f"请先切换到 {BRANCH}")
    if output("git", "status", "--porcelain"):
        raise RuntimeError("工作区存在未提交修改，停止更新；不会覆盖服务器上的修改")
    run("docker", "compose", "version")
    old = inspect_container()
    if args.init and old:
        raise RuntimeError("已有部署，请去掉 --init，使用保留数据更新")
    if not args.init and not old:
        raise RuntimeError("未找到原容器，停止更新以防新建空卷。首次部署请显式使用 --init")
    project, volume = existing_storage(old) if old else ("club-management-system", "")
    if not args.local:
        run("git", "fetch", "origin", BRANCH)
        run("git", "merge", "--ff-only", f"origin/{BRANCH}")
        # 重新加载更新后的脚本；文件锁会随 exec 关闭，新进程重新获取。
        os.execv(sys.executable, [sys.executable, str(ROOT / "scripts/deploy.py"), "--local"] + (["--init"] if args.init else []))
    base = ["docker", "compose", "-p", project, "-f", str(ROOT / "docker-compose.yml")]
    backup_dir = None
    stopped = False
    replacement_started = False
    with tempfile.TemporaryDirectory(prefix="club-deploy-") as temp:
        override = Path(temp) / "storage.json"
        if old:
            # external/name 指向实际旧卷，目录名改变也不会创建另一个空卷。
            override.write_text(json.dumps({"volumes": {"club-data": {"external": True, "name": volume}}}))
            base += ["-f", str(override)]
        config = json.loads(output(*base, "config", "--format", "json"))
        service = config["services"]["club"]
        env = service.get("environment", {})
        if env.get("DATABASE_URL") != "sqlite:////app/data/club.db" or env.get("UPLOAD_DIR") != "/app/data/uploads":
            raise RuntimeError(".env 必须保留默认 SQLite 路径及 /app/data/uploads；未停止原服务")
        mounts = service.get("volumes", [])
        if len(mounts) != 1 or mounts[0].get("source") != "club-data" or mounts[0].get("target") != "/app/data" or mounts[0].get("type") != "volume":
            raise RuntimeError("Compose 挂载与标准配置不符，停止更新")
        if not old:
            physical = config["volumes"]["club-data"]["name"]
            if physical in output("docker", "volume", "ls", "--format", "{{.Name}}").splitlines():
                raise RuntimeError("首次部署的目标卷已存在，请先确认旧部署，不能使用 --init 覆盖或接管")
        old_tag = None
        if old:
            # 构建会移走 club-management:latest。Buildx 记录的镜像摘要在构建后可能无法再 tag，
            # 所以在构建前用容器当时的镜像名留下回滚标签。
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            old_tag = f"club-management:backup-{stamp.lower()}"
            run("docker", "tag", old["Config"].get("Image") or old["Image"], old_tag)
        print("构建镜像，原服务继续运行……", flush=True)
        run(*base, "build", "club")
        if old:
            backup_dir = ROOT / "backups" / stamp
            backup_dir.mkdir(parents=True, mode=0o700)
            shutil.copyfile(ROOT / ".env", backup_dir / ".env")
            os.chmod(backup_dir / ".env", 0o600)
            shutil.copyfile(ROOT / "docker-compose.yml", backup_dir / "docker-compose.yml")
            (backup_dir / "deployment.json").write_text(json.dumps({
                "project": project, "volume": volume, "image": old["Image"], "image_tag": old_tag,
                "target_commit": output("git", "rev-parse", "HEAD"),
            }, ensure_ascii=False, indent=2))
        try:
            if old:
                print("短暂停止服务，备份数据库和全部上传文件……", flush=True)
                stopped = True
                run("docker", "stop", "-t", "30", CONTAINER)
                partial = backup_dir / "data.tar.gz.partial"
                with partial.open("wb") as stream:
                    os.chmod(partial, 0o600)
                    run("docker", "run", "--rm", "--network", "none", "--volumes-from", f"{CONTAINER}:ro",
                        old_tag, "python", "-c", (ROOT / "scripts/backup_data.py").read_text(), stdout=stream)
                partial.rename(backup_dir / "data.tar.gz")
                print(f"备份完成：{backup_dir}", flush=True)
            replacement_started = True
            run(*base, "up", "-d", "--no-build", "--pull", "never", "club")
            current = inspect_container()
            if old and existing_storage(current)[1] != volume:
                run("docker", "stop", CONTAINER)
                raise RuntimeError("数据卷校验失败，已停止新服务")
            wait_healthy()
            print("更新完成，服务健康。账号、数据库、上传文件与 .env 均已保留。" if old else "首次部署完成，服务健康。", flush=True)
        except Exception:
            if old and stopped:
                print("更新失败，尝试恢复原镜像；不会自动覆盖数据库。", file=sys.stderr, flush=True)
                try:
                    if replacement_started:
                        rollback = Path(temp) / "rollback.json"
                        rollback.write_text(json.dumps({"services": {"club": {"image": old_tag}}}))
                        run(*base, "-f", str(rollback), "up", "-d", "--no-build", "--pull", "never", "club")
                    else:
                        run("docker", "start", CONTAINER)
                    wait_healthy()
                    print("原服务已恢复；请根据错误修复后重试。", file=sys.stderr)
                except Exception:
                    print("原服务恢复未成功，请按 README 检查日志并使用备份恢复。", file=sys.stderr)
                print(f"备份目录：{backup_dir}", file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description="保留数据的一键部署（默认更新已有部署）")
    parser.add_argument("--local", action="store_true", help="使用当前已提交代码，不拉取远端")
    parser.add_argument("--init", action="store_true", help="首次部署；已有容器或目标卷时拒绝执行")
    args = parser.parse_args()
    os.chdir(ROOT)
    lock_path = output("git", "rev-parse", "--git-path", "club-deploy.lock")
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有部署正在执行")
        deploy(args)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"部署未完成：{exc}", file=sys.stderr)
        sys.exit(1)
