import pytest
from django.core.management import call_command

from apps.accounts.models import User
from apps.core.views import PLACEHOLDER_PAGES

pytestmark = pytest.mark.django_db


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "결핵 특화 연구지원 플랫폼" in r.content.decode()


def test_login_success_redirects_home(client, demo_users):
    r = client.post("/login", {"username": "researcher", "password": "res1234"})
    assert r.status_code == 302 and r["Location"] == "/"
    assert int(client.session["_auth_user_id"]) == demo_users["researcher"].pk


def test_login_failure_shows_error(client, demo_users):
    r = client.post("/login", {"username": "researcher", "password": "wrong"})
    assert r.status_code == 200
    assert "아이디 또는 비밀번호가 올바르지 않습니다" in r.content.decode()
    assert "_auth_user_id" not in client.session


def test_logged_in_user_visiting_login_goes_home(researcher_client):
    r = researcher_client.get("/login")
    assert r.status_code == 302 and r["Location"] == "/"


def test_logout_requires_post(researcher_client):
    assert researcher_client.get("/logout").status_code == 405
    r = researcher_client.post("/logout")
    assert r.status_code == 302 and r["Location"] == "/"
    assert "_auth_user_id" not in researcher_client.session


def test_protected_redirects_to_login_with_next(client):
    r = client.get("/cases?q=1")
    assert r.status_code == 302
    assert r["Location"] == "/login?next=/cases%3Fq%3D1"


def test_login_returns_to_next(client, demo_users):
    r = client.post("/login", {"username": "researcher", "password": "res1234", "next": "/cases"})
    assert r["Location"] == "/cases"


@pytest.mark.parametrize("bad", [
    "//evil.example.com/x", "http://evil.example.com/", "https://evil.example.com",
    "/\\evil.example.com", "javascript:alert(1)", "evil", "/\r\nSet-Cookie:x=1",
])
def test_unsafe_next_is_blocked(client, demo_users, bad):
    r = client.post("/login", {"username": "researcher", "password": "res1234", "next": bad})
    assert r.status_code == 302 and r["Location"] == "/"


def test_unsafe_next_in_query_not_rendered(client):
    r = client.get("/login?next=//evil.example.com")
    # 로그인 폼의 hidden next 에는 들어가지 않는다 (언어 토글의 현재 URL 표기는 무관)
    assert 'name="next" value="//evil' not in r.content.decode()


def test_admin_area_anonymous_goes_to_admin_login(client):
    r = client.get("/admin/review")
    assert r.status_code == 302 and r["Location"] == "/admin/login?next=/admin/review"


def test_admin_area_blocks_researcher(researcher_client):
    for path in ("/admin", "/admin/review", "/admin/dictionary", "/admin/schemas"):
        r = researcher_client.get(path)
        assert r.status_code == 302, path
        assert r["Location"].startswith("/admin/login?next="), path


def test_admin_area_allows_admin(admin_client_portal):
    for path in ("/admin", "/admin/review", "/admin/dictionary", "/admin/schemas"):
        assert admin_client_portal.get(path).status_code == 200, path


def test_dmanager_is_admin(client, demo_users):
    client.force_login(demo_users["dmanager"])
    assert client.get("/admin").status_code == 200


def test_admin_login_rejects_researcher(client, demo_users):
    r = client.post("/admin/login", {"username": "researcher", "password": "res1234"})
    assert r.status_code == 200
    assert "관리자 계정으로만 로그인할 수 있습니다" in r.content.decode()
    assert "_auth_user_id" not in client.session


def test_admin_login_logs_out_researcher_first(researcher_client):
    r = researcher_client.post("/admin/login", {"username": "researcher", "password": "res1234"})
    assert r.status_code == 200
    assert "_auth_user_id" not in researcher_client.session


def test_admin_login_success(client, demo_users):
    r = client.post("/admin/login", {"username": "admin", "password": "admin1234"})
    assert r.status_code == 302 and r["Location"] == "/admin"
    r = client.post("/logout")
    r = client.post("/admin/login", {"username": "dmanager", "password": "dmng1234", "next": "/admin/review"})
    assert r["Location"] == "/admin/review"


def test_admin_login_wrong_password(client, demo_users):
    r = client.post("/admin/login", {"username": "admin", "password": "x"})
    assert "아이디 또는 비밀번호가 올바르지 않습니다" in r.content.decode()


def test_all_placeholder_pages_require_login(client):
    for key, (_, _, access, _) in PLACEHOLDER_PAGES.items():
        r = client.get(f"/{key}")
        assert r.status_code == 302, key
        expected = "/admin/login?next=" if access == "admin" else "/login?next="
        assert r["Location"].startswith(expected), key


def test_placeholder_pages_render_for_researcher(researcher_client):
    for key, (ko, _, access, _) in PLACEHOLDER_PAGES.items():
        if access == "admin":
            continue
        r = researcher_client.get(f"/{key}")
        assert r.status_code == 200, key
        assert ko in r.content.decode()
    # /schemas/<id>(상세·편집·데이터 입력)는 3a 에서 실제 화면이 되었다 — apps/schemas/tests 에서 권한까지 검사


def test_trailing_slash_variants(researcher_client):
    assert researcher_client.get("/cases/").status_code == 200
    assert researcher_client.get("/profile/").status_code == 200


def test_unknown_path_redirects_home(client):
    r = client.get("/no/such/page")
    assert r.status_code == 302 and r["Location"] == "/"


def test_profile_requires_login(client):
    r = client.get("/profile")
    assert r["Location"] == "/login?next=/profile"


def test_profile_update_persists(researcher_client, demo_users):
    r = researcher_client.get("/profile")
    assert r.status_code == 200 and "김연구원" in r.content.decode()
    r = researcher_client.post("/profile", {"display_name": "박연구원", "organization": "서울대병원",
                                            "email": "park@example.org"}, follow=True)
    assert "저장되었습니다" in r.content.decode()
    u = User.objects.get(username="researcher")
    # 소속 기관은 본인이 바꿀 수 없다(3a: 기관 공유 데이터 구조 열람 범위 보호) — 보낸 값은 무시
    assert (u.display_name, u.organization, u.email) == ("박연구원", "분당서울대병원", "park@example.org")


def test_profile_rejects_invalid(researcher_client):
    r = researcher_client.post("/profile", {"display_name": "  ", "organization": "x", "email": "not-an-email"})
    assert r.status_code == 200
    u = User.objects.get(username="researcher")
    assert u.display_name == "김연구원" and u.email == "kim@snubh.org"
    body = r.content.decode()
    assert "이름을 입력하세요" in body
    assert "김연구원" in body  # 헤더 표시명은 저장값 유지


def test_csrf_enforced_on_login():
    from django.test import Client

    c = Client(enforce_csrf_checks=True)
    assert c.post("/login", {"username": "a", "password": "b"}).status_code == 403


def test_seed_demo_idempotent(db):
    call_command("seed_demo", "--accounts-only")
    call_command("seed_demo", "--accounts-only")
    assert User.objects.count() == 3
    roles = dict(User.objects.values_list("username", "role"))
    assert roles == {"admin": "admin", "dmanager": "admin", "researcher": "researcher"}
    assert User.objects.get(username="admin").check_password("admin1234")
    assert User.objects.get(username="researcher").check_password("res1234")
    assert not User.objects.get(username="researcher").is_staff
    # Django 관리 사이트 권한은 admin 만; dmanager 는 포털 관리자(role)지만 staff/superuser 아님
    admin, dmanager = User.objects.get(username="admin"), User.objects.get(username="dmanager")
    assert admin.is_staff and admin.is_superuser
    assert not dmanager.is_staff and not dmanager.is_superuser and dmanager.is_portal_admin


def test_seed_demo_does_not_resurrect_deleted_account(db):
    call_command("seed_demo", "--accounts-only")
    User.objects.filter(username="dmanager").delete()
    call_command("seed_demo", "--accounts-only")
    call_command("bootstrap")
    assert not User.objects.filter(username="dmanager").exists()
    call_command("seed_demo", "--accounts-only", "--force")
    assert User.objects.filter(username="dmanager").exists()


def test_dmanager_cannot_use_django_admin(client, demo_users):
    client.force_login(demo_users["dmanager"])
    assert client.get("/django-admin/").status_code == 302
    assert client.get("/admin/review").status_code == 200


def test_sensitive_post_parameters_on_login_views():
    from apps.accounts import views

    for view in (views.login_view, views.admin_login_view):
        # sensitive_post_parameters 는 요청에 sensitive_post_parameters 속성을 붙인다
        from django.test import RequestFactory

        req = RequestFactory().post("/login", {"username": "x", "password": "secret"})
        req.session = {}
        from django.contrib.auth.models import AnonymousUser

        req.user = AnonymousUser()
        try:
            view(req)
        except Exception:
            pass
        assert req.sensitive_post_parameters == ("password",), view


def test_placeholder_pages_get_only(researcher_client, admin_client_portal):
    assert researcher_client.post("/cases").status_code == 405
    assert researcher_client.post("/schemas/schema-001").status_code == 405
    assert admin_client_portal.post("/admin/review").status_code == 405


def test_admin_denied_message_bilingual(researcher_client):
    researcher_client.post("/lang", {"lang": "en"})
    r = researcher_client.get("/admin", follow=True)
    body = r.content.decode()
    assert "Administrator access required" in body and "관리자 권한이 필요합니다" not in body


def test_seed_demo_preserves_edits(db):
    call_command("seed_demo", "--accounts-only")
    User.objects.filter(username="researcher").update(display_name="변경됨")
    call_command("seed_demo", "--accounts-only")
    assert User.objects.get(username="researcher").display_name == "변경됨"


def test_django_admin_moved(admin_client_portal):
    assert admin_client_portal.get("/django-admin/").status_code == 200
