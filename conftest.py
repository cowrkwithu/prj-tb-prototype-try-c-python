import pytest


@pytest.fixture(autouse=True)
def _fast_hasher(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def demo_users(db):
    from apps.accounts.services import ensure_demo_accounts
    from apps.accounts.models import User

    ensure_demo_accounts()
    return {u.username: u for u in User.objects.all()}


@pytest.fixture
def researcher_client(client, demo_users):
    client.force_login(demo_users["researcher"])
    return client


@pytest.fixture
def admin_client_portal(client, demo_users):
    client.force_login(demo_users["admin"])
    return client


_PARSED = {}


@pytest.fixture
def loaded_dictionary(db, monkeypatch):
    """현행 데이터사전 적재 (엑셀 파싱 결과는 세션 동안 캐시)."""
    import copy

    from django.conf import settings

    from apps.dictionary import services

    real = services.parse_workbook

    def cached(path):
        key = str(path)
        if key not in _PARSED:
            _PARSED[key] = real(path)
        return copy.deepcopy(_PARSED[key])

    monkeypatch.setattr(services, "parse_workbook", cached)
    services.load_dictionary(settings.DICTIONARY_XLSX)
    return services.active_version()


@pytest.fixture
def seeded_patients(db):
    from apps.cases.services import load_patients

    load_patients()
