from sqlalchemy.orm import Session

from app.models import ForumPost, ForumComment, User
from app.org import DEPARTMENTS, TECH_DIRECTIONS, role_department
from app.services import AppError, require_text


def in_tech(user: User, roles: list[str]) -> bool:
    return any(role.startswith("技术部") for role in roles)


def forum_choices(user: User, roles: list[str]) -> list[dict]:
    choices = []
    departments = {role_department(role) for role in roles} - {""}
    if user.is_admin:
        departments = set(DEPARTMENTS)
    for department in DEPARTMENTS:
        if department not in departments:
            continue
        if department == "技术部":
            for name, lead in TECH_DIRECTIONS:
                if user.is_admin or "技术部部长" in roles or lead in roles or f"技术部{name}成员" in roles:
                    choices.append({"value": f"direction:{name}", "label": f"仅{name}"})
            choices.append({"value": "tech", "label": "整个技术部"})
        else:
            choices.append({"value": f"dept:{department}", "label": f"仅{department}"})
    choices.append({"value": "club", "label": "全社"})
    return choices


def can_view_post(user: User, roles: list[str], post: ForumPost) -> bool:
    if user.is_admin or post.author_id == user.id or post.scope == "club":
        return True
    if post.scope == "tech":
        return in_tech(user, roles)
    if post.scope == "dept":
        return post.department in {role_department(role) for role in roles}
    if post.scope == "direction":
        lead = dict(TECH_DIRECTIONS).get(post.direction, "")
        return lead in roles or "技术部部长" in roles or f"技术部{post.direction}成员" in roles
    return False


def create_post(db: Session, user: User, roles: list[str], title: str, body: str, scope_value: str) -> ForumPost:
    scope = scope_value
    direction = ""
    department = ""
    allowed = {item["value"] for item in forum_choices(user, roles)}
    # 兼容旧页面的 dept 值，但只允许当前角色的唯一非技术部门。
    if scope == "dept":
        departments = [item.split(":", 1)[1] for item in allowed if item.startswith("dept:")]
        if len(departments) == 1:
            scope = f"dept:{departments[0]}"
    if scope not in allowed:
        raise AppError("不能选择这个查看范围")
    if scope.startswith("direction:"):
        direction = scope.split(":", 1)[1]
        scope = "direction"
        department = "技术部"
    elif scope == "tech":
        department = "技术部"
    elif scope.startswith("dept:"):
        department = scope.split(":", 1)[1]
        scope = "dept"
    post = ForumPost(
        author_id=user.id,
        title=require_text(title, "标题", 80),
        body=require_text(body, "正文", 8000),
        scope=scope,
        department=department,
        direction=direction,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def delete_post(db: Session, user: User, roles: list[str], post_id: int) -> None:
    post = db.get(ForumPost, post_id)
    if post is None or not can_view_post(user, roles, post):
        raise AppError("找不到这篇帖子")
    if post.author_id != user.id and not user.is_admin:
        raise AppError("没有权限删除")
    db.delete(post)
    db.commit()


def scope_label(post: ForumPost) -> str:
    if post.scope == "club":
        return "全社"
    if post.scope == "tech":
        return "整个技术部"
    if post.scope == "direction":
        return f"仅{post.direction}"
    if post.scope == "dept":
        return f"仅{post.department}"
    return post.scope


def add_comment(db: Session, user: User, roles: list[str], post_id: int, body: str, anonymous: bool) -> None:
    post = db.get(ForumPost, post_id)
    if post is None or not can_view_post(user, roles, post):
        raise AppError("找不到这篇帖子，或不在你的查看范围内")
    db.add(ForumComment(post_id=post.id, author_id=user.id, body=require_text(body, "评论", 8000), anonymous=anonymous))
    db.commit()


def delete_comment(db: Session, user: User, roles: list[str], post_id: int, comment_id: int) -> None:
    post = db.get(ForumPost, post_id)
    comment = db.get(ForumComment, comment_id)
    if post is None or not can_view_post(user, roles, post) or comment is None or comment.post_id != post.id:
        raise AppError("找不到这条评论")
    if comment.author_id != user.id and not user.is_admin:
        raise AppError("只能删除自己的评论")
    db.delete(comment)
    db.commit()
