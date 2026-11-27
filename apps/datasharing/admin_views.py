"""포털 관리자 — 데이터 접근 신청(DAR) 검토 (/admin/dar). 원본은 3초 뒤 자동 승인 시뮬레이션이었다."""
from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import admin_required
from apps.core.i18n import get_lang, t
from apps.core.views import render_page

from . import catalog, services
from .models import DataAccessRequest

STATUS_OPTIONS = ["all", "pending", "approved", "rejected"]
REQUEST_ID_RE = r"DAR-\d{4}-\d{3,6}"


def _pick(lang, pair):
    return pair[1] if lang == "en" else pair[0]


@admin_required
@require_safe
def dar_list(request):
    lang = get_lang(request)
    status = request.GET.get("status", "pending")
    status = status if status in STATUS_OPTIONS else "pending"
    qs = DataAccessRequest.objects.select_related("applicant", "reviewer")
    if status != "all":
        qs = qs.filter(status=status)
    # 검토중은 오래된 신청부터 처리한다
    qs = qs.order_by("created_at", "id") if status == "pending" else qs.order_by("-created_at", "-id")
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    base = {"status": status}
    return render_page(request, "datasharing/admin/dar_list.html", {
        "rows": [services.dar_row(d, lang) for d in page.object_list], "page": page, "filters": base,
        "counts": services.dar_counts(),
        "status_options": [("all", t(request, "전체", "All"))] + [(k, _pick(lang, catalog.DAR_STATUS[k]["label"]))
                                                                for k in STATUS_OPTIONS[1:]],
        "prev_url": f"?{urlencode({**base, 'page': page.previous_page_number()})}" if page.has_previous() else "",
        "next_url": f"?{urlencode({**base, 'page': page.next_page_number()})}" if page.has_next() else "",
    }, sidebar="admin")


@admin_required
@require_safe
def dar_detail(request, request_id):
    lang = get_lang(request)
    dar = get_object_or_404(DataAccessRequest.objects.select_related("applicant", "reviewer"), request_id=request_id)
    states = services.dar_states(dar.applicant)
    return render_page(request, "datasharing/admin/dar_detail.html", {
        "row": services.dar_row(dar, lang), "dar": dar,
        "type_options": [{"id": o["id"], "label": _pick(lang, o["label"]), "desc": o["desc"],
                          "requested": o["id"] in (dar.data_types or []), "state": states[o["id"]]}
                         for o in catalog.DATA_TYPE_OPTIONS],
        "own_request": dar.applicant_id == request.user.pk,
        "history": [services.dar_row(d, lang) for d in
                    DataAccessRequest.objects.filter(applicant=dar.applicant).exclude(pk=dar.pk)[:10]],
    }, sidebar="admin")


def _action(request, request_id, approve: bool):
    dar = get_object_or_404(DataAccessRequest, request_id=request_id)
    try:
        if approve:
            services.approve_request(dar, request.user, request.POST.getlist("types"), request.POST.get("comment", ""))
            messages.success(request, t(request, f"{dar.request_id} 신청을 승인했습니다.", f"Request {dar.request_id} approved."))
        else:
            services.reject_request(dar, request.user, request.POST.get("reason", ""))
            messages.success(request, t(request, f"{dar.request_id} 신청을 반려했습니다.", f"Request {dar.request_id} rejected."))
    except services.DarError as exc:
        messages.error(request, exc.text(get_lang(request)))
        return redirect(f"/admin/dar/{dar.request_id}")
    return redirect("/admin/dar")


@admin_required
@require_POST
def dar_approve(request, request_id):
    return _action(request, request_id, True)


@admin_required
@require_POST
def dar_reject(request, request_id):
    return _action(request, request_id, False)
