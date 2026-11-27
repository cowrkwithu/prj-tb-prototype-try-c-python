import re

import pytest
from django.conf import settings
from django.template import Context, Template
from django.test import RequestFactory

from apps.core.i18n import t

pytestmark = pytest.mark.django_db


def test_home_public(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "TBRS Platform" in body
    assert 'href="/login"' in body  # 로그인 버튼
    nav = body[body.index('class="gnb"'):body.index("</nav>", body.index('class="gnb"'))]
    assert "대상자 등록조회" not in nav  # 비로그인 시 상단 메뉴 숨김 (원본 allNavItems = [])
    assert "beta_clinical_identified.csv" in body  # [베타 임시] 샘플 다운로드


def test_nav_items_for_researcher(researcher_client):
    body = researcher_client.get("/").content.decode()
    labels = ["홈", "대상자 등록조회", "데이터 탐색", "유전자 분석", "영상 분석", "데이터신청", "데이터 정의", "데이터 업로드"]
    nav = body[body.index('class="gnb"'):body.index("</nav>", body.index('class="gnb"'))]
    positions = [nav.index(f"{l}") for l in labels]
    assert positions == sorted(positions)
    assert "사용자 매뉴얼" in nav
    assert 'href="/admin"' not in nav  # 연구원은 관리 메뉴 없음
    assert "김연구원" in body and "로그아웃" in body
    assert "데이터 구조 탐색" in body and "새 데이터 구조 만들기" in body and "표준 데이터사전" in body


def test_nav_admin_item_for_admin(admin_client_portal):
    body = admin_client_portal.get("/").content.decode()
    assert 'href="/admin"' in body and "관리자 페이지" in body


def test_active_menu(researcher_client):
    body = researcher_client.get("/dictionary").content.decode()
    # /dictionary 에서는 '데이터 정의' 드롭다운이 활성
    assert re.search(r'class="gnb-link active"[^>]*data-tip="연구 맞춤형', body)


def test_sidebar_slot(researcher_client):
    body = researcher_client.get("/gap/gwas").content.decode()
    assert 'class="sidebar"' in body
    for label in ("유전체 브라우저", "약제내성 예측", "GWAS 분석", "유사도 분석", "변이 검색"):
        assert label in body
    assert re.search(r'href="/gap/gwas" class="sidebar-link active"', body)
    assert 'class="sidebar"' not in researcher_client.get("/cases").content.decode()


def test_rap_and_admin_sidebar(researcher_client, admin_client_portal):
    assert "타임시리즈" in researcher_client.get("/rap").content.decode()
    assert "데이터사전 관리" in admin_client_portal.get("/admin").content.decode()


def test_language_toggle(client):
    r = client.post("/lang", {"next": "/login"})
    assert r.status_code == 302 and r["Location"] == "/login"
    assert client.session["lang"] == "en"
    body = client.get("/login").content.decode()
    assert "TB-Specialized Research Support Platform" in body and '<html lang="en">' in body
    client.post("/lang", {"lang": "ko"})
    assert client.session["lang"] == "ko"
    assert "결핵 특화 연구지원 플랫폼" in client.get("/login").content.decode()


def test_language_toggle_english_nav(researcher_client):
    researcher_client.post("/lang", {"lang": "en"})
    body = researcher_client.get("/").content.decode()
    for label in ("Cases", "Data Explorer", "Genomics", "Imaging", "Data Request", "Data Definition", "Upload", "Manual"):
        assert label in body
    assert "Log Out" in body


def test_language_toggle_requires_post_and_safe_next(client):
    assert client.get("/lang").status_code == 405
    r = client.post("/lang", {"lang": "en", "next": "//evil.example.com"})
    assert r["Location"] == "/"
    r = client.post("/lang", {"lang": "xx"}, HTTP_REFERER="http://testserver/login")
    assert r["Location"] == "/login"
    r = client.post("/lang", {"lang": "ko"}, HTTP_REFERER="http://evil.example.com/x")
    assert r["Location"] == "/"


def test_t_helper_and_tag(client):
    rf = RequestFactory()
    req = rf.get("/")
    req.session = {"lang": "en"}
    assert t(req, "저장", "Save") == "Save"
    req.session = {}
    assert t(req, "저장", "Save") == "저장"
    req.session = {"lang": "en"}
    out = Template('{% t "저장" "Save" %}|{% t "취소" "Cancel" as c %}{{ c }}').render(Context({"request": req}))
    assert out == "Save|Cancel"


def test_icon_tag():
    out = Template('{% icon "microscope" "w-5 h-5" %}').render(Context({}))
    assert out.startswith('<svg class="icon w-5 h-5"') and "<path" in out
    assert Template('{% icon "../secret" %}').render(Context({})) == ""
    # 내부 도형의 width/height 는 보존 (panel-left: <rect width="18" height="18" …>)
    panel = Template('{% icon "panel-left" %}').render(Context({}))
    assert 'width="18"' in panel and 'height="18"' in panel


def test_toast_from_messages(researcher_client):
    r = researcher_client.post("/profile", {"display_name": "김연구원", "organization": "x", "email": "kim@snubh.org"},
                               follow=True)
    body = r.content.decode()
    assert 'class="toast toast-success"' in body and "저장되었습니다" in body


def test_vendor_files_present_and_no_cdn():
    vendor = settings.BASE_DIR / "static" / "vendor"
    for name in ("htmx.min.js", "alpine.min.js", "chart.umd.min.js", "VERSIONS.md"):
        assert (vendor / name).stat().st_size > 0, name
    for tpl in settings.BASE_DIR.rglob("*.html"):
        if ".venv" in tpl.parts or "static" in tpl.parts:
            continue
        text = tpl.read_text(encoding="utf-8")
        assert not re.search(r"(unpkg\.com|jsdelivr|cdnjs|googleapis)", text), tpl


def test_static_assets_copied():
    s = settings.BASE_DIR / "static"
    assert (s / "samples" / "beta_clinical_identified.csv").exists()
    assert any((s / "imaging").rglob("*.jpg")) or any((s / "imaging").rglob("*.png"))
    assert any((s / "manual").glob("*.png"))


def test_sqlite_wal_configured():
    opts = settings.DATABASES["default"]["OPTIONS"]
    assert "journal_mode=WAL" in opts["init_command"]
    assert settings.VAR_DIR == settings.BASE_DIR / "var"
    assert (settings.VAR_DIR / "secret_key").exists() or settings.SECRET_KEY


def test_secret_key_file_permissions(tmp_path, monkeypatch):
    import os
    import stat

    from config import settings as cfg

    monkeypatch.delenv("TB_SECRET_KEY", raising=False)
    monkeypatch.setattr(cfg, "VAR_DIR", tmp_path)
    key = cfg._load_secret_key()
    path = tmp_path / "secret_key"
    assert path.read_text(encoding="utf-8") == key and len(key) >= 50
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert cfg._load_secret_key() == key  # 재사용


def test_rundev_defaults_to_loopback_and_warns(monkeypatch, settings):
    from django.core.management import call_command

    calls = []
    monkeypatch.setattr("apps.core.management.commands.rundev.call_command",
                        lambda *a, **k: calls.append(a))
    monkeypatch.delenv("TB_BIND", raising=False)
    monkeypatch.delenv("TB_PORT", raising=False)
    monkeypatch.delenv("RUN_MAIN", raising=False)
    import io

    err = io.StringIO()
    settings.DEBUG = True
    call_command("rundev", stderr=err)
    assert calls[-1] == ("runserver", "127.0.0.1:4363") and "경고" not in err.getvalue()
    monkeypatch.setenv("TB_BIND", "0.0.0.0")
    monkeypatch.setenv("TB_PORT", "4391")
    call_command("rundev", stderr=err)
    assert calls[-1] == ("runserver", "0.0.0.0:4391") and "DEBUG=True" in err.getvalue()
    call_command("rundev", "--port", "5000", stderr=io.StringIO())
    assert calls[-1] == ("runserver", "0.0.0.0:5000")


def test_serve_warns_without_allowed_hosts(monkeypatch, settings):
    import io

    from django.core.management import call_command

    served = []
    monkeypatch.setattr("waitress.serve", lambda app, **kw: served.append(kw))
    settings.DEBUG = False
    settings.ALLOWED_HOSTS_FROM_ENV = False
    err = io.StringIO()
    call_command("serve", "--port", "4399", stdout=io.StringIO(), stderr=err)
    assert served[-1]["port"] == 4399 and "TB_ALLOWED_HOSTS" in err.getvalue()
    settings.ALLOWED_HOSTS_FROM_ENV = True
    err = io.StringIO()
    call_command("serve", stdout=io.StringIO(), stderr=err)
    assert "TB_ALLOWED_HOSTS" not in err.getvalue()


def test_chartjs_sourcemap_stripped():
    text = (settings.BASE_DIR / "static" / "vendor" / "chart.umd.min.js").read_text(encoding="utf-8")
    assert "sourceMappingURL" not in text


def test_serve_rejects_bad_port_without_traceback(monkeypatch):
    import io

    from django.core.management import call_command
    from django.core.management.base import CommandError

    monkeypatch.setattr("waitress.serve", lambda app, **kw: None)
    monkeypatch.setenv("TB_PORT", "abc")
    with pytest.raises(CommandError, match="TB_PORT"):
        call_command("serve", stdout=io.StringIO(), stderr=io.StringIO())
    with pytest.raises(CommandError):
        call_command("rundev", stderr=io.StringIO())
    with pytest.raises(CommandError):
        call_command("serve", "70000", stdout=io.StringIO(), stderr=io.StringIO())


def test_serve_positional_port(monkeypatch):
    import io

    from django.core.management import call_command

    served = []
    monkeypatch.setattr("waitress.serve", lambda app, **kw: served.append(kw))
    monkeypatch.delenv("TB_PORT", raising=False)
    call_command("serve", "4398", stdout=io.StringIO(), stderr=io.StringIO())
    assert served[-1]["port"] == 4398


def test_head_allowed_on_safe_views(researcher_client, client):
    assert client.head("/").status_code == 200
    assert researcher_client.head("/schemas").status_code == 200


def test_rundev_positional_port(monkeypatch):
    import io

    from django.core.management import call_command

    calls = []
    monkeypatch.setattr("apps.core.management.commands.rundev.call_command", lambda *a, **k: calls.append(a))
    monkeypatch.delenv("TB_BIND", raising=False)
    call_command("rundev", "4397", stderr=io.StringIO())
    assert calls[-1] == ("runserver", "127.0.0.1:4397")
