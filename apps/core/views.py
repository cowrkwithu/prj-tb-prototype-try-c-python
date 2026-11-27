from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe, require_POST

from apps.accounts.decorators import admin_required, login_required
from apps.accounts.utils import safe_next

from . import dashboard, home_content
from .i18n import LANGS, get_lang, set_lang
from .navigation import build_sidebar

# 이후 단계에서 구현될 화면(업로드·관리자 대시보드·데이터 검증은 apps.uploads, 데이터신청 /data 는 apps.datasharing,
# 유전자 분석 /gap 은 apps.genomics, 영상 분석 /rap 은 apps.imaging 이 구현): route → (제목 ko, en, 접근 수준, 사이드바)
PLACEHOLDER_PAGES = {
    "manual": ("사용자 매뉴얼", "User Manual", "login", None),
}


def render_page(request, template, context=None, sidebar=None, status=200):
    """사이드바 슬롯을 채워 렌더링하는 공통 헬퍼."""
    ctx = dict(context or {})
    ctx["sidebar"] = build_sidebar(sidebar, request.path, get_lang(request))
    return render(request, template, ctx, status=status)


@require_safe
def home(request):
    """홈 대시보드 (원본 pages/Home/index.tsx) — 비로그인 열람 가능. 수치는 모두 DB 집계."""
    lang = get_lang(request)
    en = lang == "en"
    trends = dashboard.monthly_trends()
    countries = dashboard.country_distribution()
    resistance = dashboard.resistance_distribution()
    outcomes = dashboard.outcome_distribution()
    contrib_range = dashboard.contribution_range()
    contributions = home_content.localize(dashboard.contribution_stats(), lang)
    for c in contributions:
        c["points"] = dashboard.sparkline_points(c["spark"])
    sections = home_content.home_sections(lang)
    activity = home_content.localize(dashboard.recent_upload_activity(), lang)
    chart_data = {
        "trend": {
            "labels": [r["month"][5:] for r in trends],
            "series": [
                {"label": "Cases" if en else "케이스", "data": [r["cases"] for r in trends], "color": "#1B3A5C"},
                {"label": "Sequences" if en else "시퀀스", "data": [r["sequences"] for r in trends], "color": "#2A7F8E"},
                {"label": "Images" if en else "영상", "data": [r["images"] for r in trends], "color": "#2563EB"},
            ],
        },
        "resistance": {"labels": [r["key"] for r in resistance], "data": [r["count"] for r in resistance]},
        "outcome": {"labels": [r["key"] for r in outcomes], "data": [r["count"] for r in outcomes]},
    }
    return render_page(request, "core/home.html", {
        "stats": dashboard.stat_cards(),
        "trends": trends,
        "countries": countries,
        "resistance": resistance,
        "outcomes": outcomes,
        "contributions": contributions,
        "schema_stats": dashboard.custom_schema_stats(),
        "tools": home_content.localize(home_content.TOOLS, lang),
        **sections,
        "contrib_start": contrib_range[0],
        "contrib_end": contrib_range[1],
        "activity": activity,
        "chart_data": chart_data,
    })


def _make_placeholder(key):
    ko, en, access, sidebar = PLACEHOLDER_PAGES[key]

    def view(request, **kwargs):
        return render_page(request, "core/coming_soon.html",
                           {"title_ko": ko, "title_en": en}, sidebar=sidebar)

    view.__name__ = f"placeholder_{key.replace('/', '_')}"
    view = require_safe(view)
    return admin_required(view) if access == "admin" else login_required(view)


PLACEHOLDER_VIEWS = {key: _make_placeholder(key) for key in PLACEHOLDER_PAGES}

@require_POST
def set_language(request):
    """한/영 토글 — 세션에 저장. lang 값이 없으면 현재 언어를 뒤집는다."""
    lang = request.POST.get("lang")
    if lang not in LANGS:
        lang = "en" if get_lang(request) == "ko" else "ko"
    set_lang(request, lang)
    target = request.POST.get("next") or request.META.get("HTTP_REFERER", "")
    # Referer 는 절대 URL 이므로 같은 호스트면 경로만 취한다
    host_prefix = f"{request.scheme}://{request.get_host()}"
    if target.startswith(host_prefix):
        target = target[len(host_prefix):] or "/"
    return redirect(safe_next(request, target))


def fallback_redirect(request, *args, **kwargs):
    """정의되지 않은 경로는 홈으로 (원본 Navigate to="/" replace)."""
    return redirect("/")
