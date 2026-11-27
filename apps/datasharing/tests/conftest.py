import pytest

VALID_DAR = {
    "name": "김연구", "institution": "분당서울대병원", "email": "kim@snubh.org", "orcid": "0000-0002-1825-009X",
    "approver_name": "김교수", "approver_title": "교수 / PI", "approver_email": "prof@snubh.org",
    "purpose": "다제내성 결핵 환자의 치료 결과와 유전체 변이의 연관성을 분석하기 위한 후향적 코호트 연구입니다.",
    "dua_agreed": "1",
}


@pytest.fixture(autouse=True)
def _media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()
    return settings.MEDIA_ROOT


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def make_dar(user, types, status="pending", approved=None, reviewer=None):
    """서비스로 신청을 만들고(필요하면) 다른 관리자가 승인/반려한다."""
    from apps.accounts.models import User
    from apps.datasharing import services

    dar = services.submit_request(user, {**VALID_DAR, "data_types": list(types)})
    if status == "pending":
        return dar
    reviewer = reviewer or User.objects.filter(role="admin").exclude(pk=user.pk).order_by("id").first()
    if status == "approved":
        return services.approve_request(dar, reviewer, approved)
    return services.reject_request(dar, reviewer, "연구 목적 불충분")


@pytest.fixture
def other_researcher(db):
    from apps.accounts.models import User

    return User.objects.create_user("other", password="x", role="researcher", organization="몽골국립의료원")


@pytest.fixture
def approved_all(demo_users):
    return make_dar(demo_users["researcher"], ["clinical", "genomics", "imaging"], "approved")


@pytest.fixture
def api_key(demo_users, approved_all):
    from apps.datasharing import services

    key, raw = services.issue_key(demo_users["researcher"], "kim@snubh.org", "분당서울대병원")
    return raw


def auth(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
