import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def catalog(seeded_patients):
    """시드 환자 50명 + 유전자 10·변이 카탈로그 20."""
    from apps.genomics.services import load_genomics

    load_genomics()


@pytest.fixture
def other_user(db):
    from apps.accounts.models import User

    return User.objects.create_user("other", password="x", role="researcher", organization="몽골국립의료원")


def approve_genomics(user):
    from apps.datasharing.tests.conftest import make_dar

    return make_dar(user, ["genomics"], "approved")
