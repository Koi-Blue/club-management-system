DEPARTMENTS = ["技术部", "财务部", "人事部", "宣传部"]
TECH_LEADS = [
    "技术部嵌入式软件负责人",
    "技术部算法负责人",
    "技术部硬件负责人",
    "技术部机械负责人",
]
MINISTERS = [f"{dept}部长" for dept in DEPARTMENTS]
DEPT_MEMBERS = [f"{dept}成员" for dept in DEPARTMENTS]
OFFICERS = ["指导老师", "社长", "副社长", "荣誉社长"]
CLUB_ROLES = OFFICERS + MINISTERS + TECH_LEADS + DEPT_MEMBERS

# 指导老师在最上，其下是社长、副社长和各部门。荣誉社长单独成支，方便在架构里看到。
ORG_TREE = [
    (
        "指导老师",
        [
            (
                "社长",
                [
                    (
                        "副社长",
                        [
                            (
                                "技术部部长",
                                [(lead, []) for lead in TECH_LEADS] + [("技术部成员", [])],
                            ),
                            ("财务部部长", [("财务部成员", [])]),
                            ("人事部部长", [("人事部成员", [])]),
                            ("宣传部部长", [("宣传部成员", [])]),
                        ],
                    )
                ],
            )
        ],
    ),
    ("荣誉社长", []),
]


def role_department(role: str) -> str:
    for dept in DEPARTMENTS:
        if role.startswith(dept):
            return dept
    return ""


def has_role(roles: list[str], *names: str) -> bool:
    return any(name in roles for name in names)


def managed_departments(roles: list[str]) -> set[str]:
    found = set()
    for role in roles:
        if role in TECH_LEADS or role.endswith("部长"):
            dept = role_department(role)
            if dept:
                found.add(dept)
    return found


def sees_all_business(is_admin: bool, roles: list[str]) -> bool:
    return is_admin or "荣誉社长" in roles


def sees_finance(is_admin: bool, roles: list[str]) -> bool:
    return is_admin or has_role(roles, "社长", "财务部部长")


def sees_club_operations(is_admin: bool, roles: list[str]) -> bool:
    return is_admin or has_role(roles, "社长", "副社长", "指导老师", "荣誉社长")


def can_approve_project(is_admin: bool, roles: list[str]) -> bool:
    return is_admin or has_role(roles, "社长", "副社长", "指导老师")


def can_issue_activity(is_admin: bool, roles: list[str]) -> bool:
    return is_admin or has_role(roles, "社长", "副社长", "指导老师")


def can_create_activity(is_admin: bool, roles: list[str]) -> bool:
    return can_issue_activity(is_admin, roles) or bool(managed_departments(roles))


def can_approve_borrow(is_admin: bool, roles: list[str]) -> bool:
    return is_admin or has_role(roles, "社长", "副社长")


def can_edit_duty(is_admin: bool, roles: list[str]) -> bool:
    return is_admin or has_role(roles, "社长", "副社长", "人事部部长")


def can_announce(is_admin: bool, roles: list[str]) -> bool:
    return is_admin or has_role(roles, "社长", "副社长", "指导老师") or bool(managed_departments(roles))


def home_departments(user_department: str, roles: list[str]) -> set[str]:
    found = set(managed_departments(roles))
    if user_department in DEPARTMENTS:
        found.add(user_department)
    for role in roles:
        dept = role_department(role)
        if dept:
            found.add(dept)
    return found
