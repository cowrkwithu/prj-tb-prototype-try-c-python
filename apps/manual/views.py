from django.shortcuts import render
from django.views.decorators.http import require_safe

from apps.accounts.decorators import login_required
from apps.core.i18n import get_lang
from apps.core.views import render_page

from . import services


@login_required
@require_safe
def manual(request):
    """사용자 매뉴얼 (원본 pages/Manual/ManualPage.tsx) — 아코디언·검색(현재 언어)·목차. 내용은 DB.

    HTMX 실시간 검색(hx-target=manual-results)은 결과 부분만 돌려준다. JS 가 없어도 검색 폼 제출(GET)로 같은 결과를 본다."""
    lang = get_lang(request)
    q = services.clean_query(request.GET.get("q"))
    sections = services.manual_sections(lang, q)
    ctx = {"q": q, "sections": sections, "toc": services.manual_sections(lang) if not q else []}
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "manual-results":
        return render(request, "manual/_results.html", ctx)
    return render_page(request, "manual/manual.html", ctx)
