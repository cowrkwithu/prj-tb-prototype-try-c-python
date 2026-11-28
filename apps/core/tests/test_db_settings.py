"""TB_DATABASE_URL → DATABASES 변환(PostgreSQL 전환은 환경변수로만, 기본은 SQLite)."""
import pytest

from config import settings as cfg


def test_default_is_sqlite():
    assert cfg.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3" or cfg._DB_URL


def test_url_parsed(monkeypatch):
    monkeypatch.setenv("TB_DB_LOCK_TIMEOUT_MS", "3000")
    d = cfg._database_from_url("postgresql://tb%40u:p%3Aw%40d@db.local:6543/tbrs")
    assert (d["ENGINE"], d["NAME"], d["USER"], d["PASSWORD"], d["HOST"], d["PORT"]) == (
        "django.db.backends.postgresql", "tbrs", "tb@u", "p:w@d", "db.local", "6543")
    assert d["OPTIONS"] == {"options": "-c lock_timeout=3000"}


@pytest.mark.parametrize("url", ["mysql://a:b@h/db", "sqlite:///x"])
def test_unsupported_scheme(url):
    with pytest.raises(ValueError):
        cfg._database_from_url(url)


def test_bad_lock_timeout(monkeypatch):
    monkeypatch.setenv("TB_DB_LOCK_TIMEOUT_MS", "abc")
    with pytest.raises(ValueError):
        cfg._database_from_url("postgresql://a:b@h:1/db")


@pytest.mark.parametrize("ms", ["0", "-1", "", " "])
def test_lock_timeout_must_be_positive(monkeypatch, ms):
    """0 은 PostgreSQL 에서 '무제한 대기' — 503 안내가 동작하지 않으므로 거부한다."""
    monkeypatch.setenv("TB_DB_LOCK_TIMEOUT_MS", ms)
    with pytest.raises(ValueError, match="1 이상"):
        cfg._database_from_url("postgresql://a:b@h:1/db")


def test_known_libpq_options_passed(monkeypatch):
    monkeypatch.delenv("TB_DB_LOCK_TIMEOUT_MS", raising=False)
    d = cfg._database_from_url("postgresql://a:b@h:1/db?sslmode=verify-full&sslrootcert=%2Fetc%2Fca.pem"
                               "&connect_timeout=10&application_name=tbportal&sslcert=c.pem&sslkey=k.pem")
    assert d["OPTIONS"] == {"options": "-c lock_timeout=5000", "sslmode": "verify-full", "sslrootcert": "/etc/ca.pem",
                            "connect_timeout": "10", "application_name": "tbportal", "sslcert": "c.pem", "sslkey": "k.pem"}
    assert d["NAME"] == "db"


@pytest.mark.parametrize("query", ["sslmod=require", "options=-c%20statement_timeout%3D0", "sslmode=require&sslmode=disable",
                                   "garbage", "a=1"])
def test_unknown_or_bad_query_rejected(monkeypatch, query):
    monkeypatch.delenv("TB_DB_LOCK_TIMEOUT_MS", raising=False)
    with pytest.raises(ValueError, match="TB_DATABASE_URL"):
        cfg._database_from_url(f"postgresql://a:b@h:1/db?{query}")
