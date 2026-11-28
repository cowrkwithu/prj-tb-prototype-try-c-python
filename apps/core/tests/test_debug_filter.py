"""DEBUG 오류 화면에 DB 접속 정보(비밀번호)가 나오지 않는다 — runserver 는 프로세스 환경변수를 META 로 복사한다."""
import os
import sys

from django.core.handlers.wsgi import WSGIRequest
from django.views.debug import ExceptionReporter, get_default_exception_reporter_filter
from wsgiref.util import setup_testing_defaults

SECRET = "PWx9q7zSecret"


def _report(monkeypatch, settings):
    settings.DEBUG = True
    monkeypatch.setenv("TB_DATABASE_URL", f"postgresql://tbportal:{SECRET}@127.0.0.1:5462/tbportal")
    monkeypatch.setenv("TB_REDIS_DSN", f"redis://:{SECRET}@h/0")
    env = dict(os.environ)  # wsgiref BaseHandler.setup_environ 와 같은 상태(os_environ 복사)
    setup_testing_defaults(env)
    req = WSGIRequest(env)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        et, ev, tb = sys.exc_info()
    rep = ExceptionReporter(req, et, ev, tb)
    return rep.get_traceback_html(), rep.get_traceback_text()


def test_filter_is_configured():
    from apps.core.debug import PortalExceptionReporterFilter

    assert isinstance(get_default_exception_reporter_filter(), PortalExceptionReporterFilter)


def test_database_url_hidden_in_debug_page(monkeypatch, settings):
    html, text = _report(monkeypatch, settings)
    assert SECRET not in html and SECRET not in text
    assert "TB_DATABASE_URL" in html  # 이름은 보이고 값만 가린다
    assert "TB_REDIS_DSN" in html


def test_pwd_style_names_hidden():
    from apps.core.debug import PortalExceptionReporterFilter

    f = PortalExceptionReporterFilter()
    for name in ("PGPWD", "DB_PASSWD", "MYSQL_PWD"):
        assert f.cleanse_setting(name, "v") != "v", name
