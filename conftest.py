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


class _FrozenClock:
    """time 모듈 대역 — time() 만 고정값(now)을 돌려주고 나머지는 실제 time 으로 넘긴다."""

    def __init__(self, now: float):
        import time as _time

        self._time, self.now = _time, now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(self._time, name)


@pytest.fixture
def frozen_api_clock(monkeypatch):
    """API 요청 제한(분 단위 고정 창)의 시계를 분 중간(…:30초)에 고정한다 — 실제 시각의 분 경계에 따라
    한 창의 요청이 두 창으로 나뉘어 흔들리던 테스트를 결정적으로 만든다. clock.now 를 바꿔 창 경계를 시험할 수 있다."""
    from apps.datasharing import services

    clock = _FrozenClock(1_800_000_030.0)  # 60 으로 나누어떨어지는 시각 + 30초
    monkeypatch.setattr(services, "time", clock)
    return clock
