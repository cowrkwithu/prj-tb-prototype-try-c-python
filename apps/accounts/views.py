from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from apps.core.i18n import t

from .decorators import login_required
from .forms import LoginForm, ProfileForm
from .models import User
from .utils import safe_next


@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def login_view(request):
    """원본 pages/Auth/Login.tsx — 성공 시 next(안전한 경우) 또는 홈으로."""
    next_url = safe_next(request, request.POST.get("next") or request.GET.get("next"))
    if request.user.is_authenticated:
        return redirect(next_url)

    error = None
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = authenticate(request, username=form.cleaned_data["username"],
                            password=form.cleaned_data["password"])
        if user is not None:
            login(request, user)
            return redirect(next_url)
        error = t(request, "아이디 또는 비밀번호가 올바르지 않습니다", "Incorrect username or password")

    return render(request, "accounts/login.html", {
        "form": form, "error": error, "next": next_url if next_url != "/" else "",
        "admin_mode": False,
    }, status=200)


@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def admin_login_view(request):
    """원본 pages/Admin/Login.tsx — 관리자 계정만 로그인 허용."""
    next_url = safe_next(request, request.POST.get("next") or request.GET.get("next"), default="/admin")
    if request.user.is_authenticated and request.user.is_portal_admin:
        return redirect(next_url)

    error = None
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        # 원본: 비관리자로 로그인된 상태면 먼저 로그아웃한다
        if request.user.is_authenticated:
            logout(request)
        user = authenticate(request, username=form.cleaned_data["username"],
                            password=form.cleaned_data["password"])
        if user is None:
            error = t(request, "아이디 또는 비밀번호가 올바르지 않습니다", "Incorrect username or password")
        elif not user.is_portal_admin:
            error = t(request, "관리자 계정으로만 로그인할 수 있습니다", "Only administrator accounts can log in here")
        else:
            login(request, user)
            return redirect(next_url)

    return render(request, "accounts/login.html", {
        "form": form, "error": error, "next": next_url if next_url != "/admin" else "",
        "admin_mode": True,
    })


@require_POST
def logout_view(request):
    logout(request)
    return redirect("/")


@login_required
@require_http_methods(["GET", "POST"])
def profile_view(request):
    """원본 pages/Auth/Profile.tsx — 이름·이메일 수정(DB 저장). 소속 기관은 읽기 전용(관리자만 변경 — 기관 공유 열람 범위 보호)."""
    user = request.user
    saved = {"display_name": user.display_name, "organization": user.organization, "email": user.email}
    if request.method == "POST":
        # 별도 인스턴스로 검증 — 실패 시 request.user(헤더 표시)가 오염되지 않게
        form = ProfileForm(request.POST, instance=User.objects.get(pk=user.pk))
        if form.is_valid():
            form.save()
            messages.success(request, t(request, "저장되었습니다", "Saved"))
            return redirect("/profile")
    else:
        form = ProfileForm(instance=user)
    return render(request, "accounts/profile.html", {"form": form, "saved": saved})
