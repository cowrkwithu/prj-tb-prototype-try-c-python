"""데모 계정 (원본 contexts/demoAccounts.ts)."""
from django.db import transaction

from apps.core.seeding import is_seeded, mark_seeded

from .models import User

DATASET = "accounts"

DEMO_ACCOUNTS = {
    "admin": {
        "password": "admin1234", "display_name": "관리자", "role": User.Role.ADMIN,
        "organization": "TBRS Platform", "email": "admin@tb-portal.org",
    },
    "dmanager": {
        "password": "dmng1234", "display_name": "데이터관리자", "role": User.Role.ADMIN,
        "organization": "TBRS Platform", "email": "dmanager@tb-portal.org",
    },
    "researcher": {
        "password": "res1234", "display_name": "김연구원", "role": User.Role.RESEARCHER,
        "organization": "분당서울대병원", "email": "kim@snubh.org",
    },
}


# Django 관리 사이트(/django-admin/) 접근 권한은 admin 계정에만 준다.
# dmanager 는 포털 관리자(role=admin)이지만 staff/superuser 가 아니다(포털 /admin/* 는 role 로 충분).
DJANGO_SUPERUSERS = {"admin"}


def ensure_demo_accounts(force: bool = False) -> dict:
    """데모 계정을 1회 생성한다.

    이미 적재 기록이 있으면 건너뛴다(사용자가 지운 계정이 되살아나지 않게). force=True 면 없는 계정만 다시 만든다.
    이미 있는 계정의 비밀번호·프로필은 건드리지 않는다.
    """
    if is_seeded(DATASET) and not force:
        return {"created": [], "existing": [], "skipped": True}
    created, existing = [], []
    with transaction.atomic():
        _create_missing(created, existing)
        mark_seeded(DATASET, {"created": created, "existing": existing})
    return {"created": created, "existing": existing, "skipped": False}


def _create_missing(created, existing):
    for username, spec in DEMO_ACCOUNTS.items():
        if User.objects.filter(username=username).exists():
            existing.append(username)
            continue
        superuser = username in DJANGO_SUPERUSERS
        user = User(
            username=username, display_name=spec["display_name"], role=spec["role"],
            organization=spec["organization"], email=spec["email"],
            is_staff=superuser, is_superuser=superuser,
        )
        user.set_password(spec["password"])
        user.save()
        created.append(username)
