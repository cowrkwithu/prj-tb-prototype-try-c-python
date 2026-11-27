"""서버측 접근 제어 (원본 components/auth/ProtectedRoute.tsx).

- 비로그인 → /login?next=<원래 경로>   (admin 화면은 /admin/login?next=…)
- 로그인했지만 관리자 아님 → /admin/login?next=…  (원본과 동일하게 관리자 로그인으로 보낸다)
HTMX 요청이면 302 대신 HX-Redirect 헤더로 전체 페이지 이동시킨다.
"""
from functools import wraps

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect

from apps.core.i18n import t

from .utils import login_url_with_next


def _redirect(request, url):
    if request.headers.get("HX-Request"):
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = url
        return resp
    return redirect(url)


def login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _redirect(request, login_url_with_next("/login", request.get_full_path()))
        return view(request, *args, **kwargs)

    wrapper.portal_access = "login"
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _redirect(request, login_url_with_next("/admin/login", request.get_full_path()))
        if not request.user.is_portal_admin:
            messages.error(request, t(request, "관리자 권한이 필요합니다", "Administrator access required"))
            return _redirect(request, login_url_with_next("/admin/login", request.get_full_path()))
        return view(request, *args, **kwargs)

    wrapper.portal_access = "admin"
    return wrapper
