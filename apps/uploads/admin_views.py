"""포털 관리자 화면 — 원본 pages/Admin/Dashboard.tsx, ReviewDashboard.tsx. 수치는 모두 DB 집계."""
from __future__ import annotations

import logging
import shutil
import time
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import admin_required
from apps.accounts.models import User
from apps.cases.services import data_label
from apps.core import dashboard
from apps.core.i18n import get_lang, t, tr_pair
from apps.core.views import render_page

from .models import Submission, SubmissionEvent
from .services import apply, schemas, workflow
from .services.files import format_size
from .views import domain_label, issues_of, localize_messages, pii_context, status_info

logger = logging.getLogger(__name__)

PROCESS_STARTED = time.time()
REVIEW_STATUS_OPTIONS = ["all", "pending", "revision", "approved", "rejected"]
EVENT_STYLE = {
    "submitted": ("upload", "text-info", ("데이터 제출", "Data Submitted")),
    "approved": ("circle-check", "text-success", ("데이터 승인", "Data Approved")),
    "rejected": ("circle-x", "text-danger", ("데이터 반려", "Data Rejected")),
    "revision": ("triangle-alert", "text-warning", ("수정 요청", "Revision Requested")),
    "deletion_requested": ("trash-2", "text-muted", ("삭제 요청", "Deletion Requested")),
    "deletion_rejected": ("circle-x", "text-muted", ("삭제 요청 반려", "Deletion Request Rejected")),
    "deletion_cleared": ("refresh-cw", "text-muted", ("삭제 요청 해제", "Deletion Request Cleared")),
}


def _today_range():
    today = timezone.localdate()
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()), tz)
    return start, start + timedelta(days=1)


def review_counts() -> dict:
    start, end = _today_range()
    agg = Submission.objects.aggregate(
        pending=Count("id", filter=Q(status="pending")),
        pending_warn=Count("id", filter=Q(status="pending") & (Q(warning_records__gt=0) | Q(error_records__gt=0))),
        revision=Count("id", filter=Q(status="draft", revision_requested=True)),
        rejected=Count("id", filter=Q(status="rejected")),
        approved=Count("id", filter=Q(status="approved")),
        approved_today=Count("id", filter=Q(status="approved", reviewed_at__gte=start, reviewed_at__lt=end)),
        rejected_today=Count("id", filter=Q(status="rejected", reviewed_at__gte=start, reviewed_at__lt=end)),
        submitted_today=Count("id", filter=Q(submitted_at__gte=start, submitted_at__lt=end)),
    )
    return agg


def _event_rows(qs, lang):
    out = []
    for e in qs:
        icon, color, action = EVENT_STYLE[e.action]
        s = e.submission
        out.append({"date": timezone.localtime(e.at).date(), "icon": icon, "color": color, "action": tr_pair(lang, action),
                    "detail": f"{s.upload_id} ({domain_label(s.domain, lang)})", "upload_id": s.upload_id})
    return out


@admin_required
@require_safe
def admin_dashboard(request):
    lang = get_lang(request)
    counts = review_counts()
    by_domain = {r["domain"]: r for r in Submission.objects.exclude(status="draft").values("domain").annotate(
        pending=Count("id", filter=Q(status="pending")), approved=Count("id", filter=Q(status="approved")),
        rejected=Count("id", filter=Q(status="rejected")), total=Count("id"))}
    domains = [{"label": domain_label(d, lang), "key": d, **{k: by_domain.get(d, {}).get(k, 0)
                                                           for k in ("pending", "approved", "rejected", "total")}}
               for d in schemas.DOMAINS]
    events = SubmissionEvent.objects.select_related("submission").order_by("-at", "-id")[:6]
    try:
        usage = shutil.disk_usage(settings.MEDIA_ROOT if settings.MEDIA_ROOT.exists() else settings.VAR_DIR)
        storage = f"{usage.used / usage.total * 100:.0f}%"
        storage_color = "text-warning" if usage.used / usage.total > 0.8 else "text-success"
    except OSError:
        storage, storage_color = "-", "text-muted"
    try:
        connection.ensure_connection()
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    up = int(time.time() - PROCESS_STARTED)
    uptime = (f"{up // 86400}d {up % 86400 // 3600}h" if lang == "en" else f"{up // 86400}일 {up % 86400 // 3600}시간")
    cards = [
        {"label": t(request, "전체 사용자", "Total Users"), "value": User.objects.filter(is_active=True).count(),
         "icon": "users", "cls": "sum-blue"},
        {"label": t(request, "금일 업로드", "Today's Uploads"), "value": counts["submitted_today"], "icon": "upload", "cls": "sum-teal"},
        {"label": t(request, "등록 데이터 구조", "Registered Data Schemas"), "value": dashboard.custom_schema_stats()["active"],
         "icon": "layers", "cls": "sum-purple"},
        {"label": t(request, "누적 승인 업로드", "Approved Uploads"), "value": counts["approved"], "icon": "circle-check",
         "cls": "sum-amber"},
    ]
    from apps.datasharing.services import dar_counts  # 데이터 접근 신청 대기 건수(3c)

    from .services.deletion import deletion_counts

    return render_page(request, "uploads/admin/dashboard.html", {
        "cards": cards, "counts": counts, "domains": domains, "activities": _event_rows(events, lang),
        "dar": dar_counts(), "deletions": deletion_counts(),
        "system": [
            {"label": t(request, "서버", "Server"), "icon": "server", "status": t(request, "정상", "Normal"), "color": "text-success"},
            {"label": t(request, "데이터베이스", "Database"), "icon": "database",
             "status": t(request, "정상", "Normal") if db_ok else t(request, "오류", "Error"),
             "color": "text-success" if db_ok else "text-danger"},
            {"label": t(request, "스토리지", "Storage"), "icon": "hard-drive", "status": storage, "color": storage_color},
            {"label": t(request, "업타임", "Uptime"), "icon": "activity", "status": uptime, "color": "text-success"},
        ],
        "today": timezone.localdate(),
    }, sidebar="admin")


def _queue_qs(ftype, fstatus):
    qs = Submission.objects.select_related("submitter").exclude(status="draft", revision_requested=False)
    if ftype in schemas.DOMAINS:
        qs = qs.filter(domain=ftype)
    if fstatus == "revision":
        qs = qs.filter(status="draft", revision_requested=True)
    elif fstatus in ("pending", "approved", "rejected"):
        qs = qs.filter(status=fstatus)
    # 검증 대기 먼저, 그 안에서는 제출 순(오래된 것 먼저)
    return qs.order_by(
        timezone_case(), "submitted_at", "-id")


def timezone_case():
    from django.db.models import Case, IntegerField, Value, When

    return Case(When(status="pending", then=Value(0)), default=Value(1), output_field=IntegerField())


def queue_row(sub, lang):
    auto = sub.auto_result
    prio_key, prio_label = schemas.PRIORITY[auto]
    color, icon = schemas.AUTO_STYLE[auto]
    org = sub.submitter.organization or "-"
    return {"sub": sub, "status": status_info(sub, lang), "domain_label": domain_label(sub.domain, lang),
            "org": data_label(org, lang), "submitter": sub.submitter.shown_name,
            "priority": prio_key, "priority_label": tr_pair(lang, prio_label),
            "auto": auto, "auto_label": tr_pair(lang, schemas.AUTO_LABEL[auto]), "auto_color": color, "auto_icon": icon}


@admin_required
@require_safe
def review_queue(request):
    lang = get_lang(request)
    ftype = request.GET.get("type", "all")
    fstatus = request.GET.get("status", "all")
    ftype = ftype if ftype in ["all"] + schemas.DOMAINS else "all"
    fstatus = fstatus if fstatus in REVIEW_STATUS_OPTIONS else "all"
    page = Paginator(_queue_qs(ftype, fstatus), 20).get_page(request.GET.get("page"))
    base = {"type": ftype, "status": fstatus}
    all_label = t(request, "전체", "All")
    return render_page(request, "uploads/admin/review_queue.html", {
        "counts": review_counts(), "rows": [queue_row(s, lang) for s in page.object_list], "page": page,
        "filters": base,
        "type_options": [("all", all_label)] + [(d, domain_label(d, lang)) for d in schemas.DOMAINS],
        "status_options": [("all", all_label)] + [(k, tr_pair(lang, schemas.STATUS_LABEL[k])) for k in REVIEW_STATUS_OPTIONS[1:]],
        "prev_url": f"?{urlencode({**base, 'page': page.previous_page_number()})}" if page.has_previous() else "",
        "next_url": f"?{urlencode({**base, 'page': page.next_page_number()})}" if page.has_next() else "",
    }, sidebar="admin")


PREVIEW_ROWS = 50


@admin_required
@require_safe
def review_detail(request, uid):
    lang = get_lang(request)
    sub = get_object_or_404(Submission.objects.select_related("submitter", "reviewer"), upload_id=uid)
    if sub.status == Submission.Status.DRAFT and not sub.revision_requested:
        # 제출 전 임시저장은 검토 대상이 아니다
        from django.http import Http404

        raise Http404
    recs = list(sub.records.order_by("row_no")[:PREVIEW_ROWS])
    cols = _preview_columns(sub, recs)
    iss = issues_of(sub, limit=200)
    listed = (iss["errors"] + iss["warnings"])[:5]
    org = sub.submitter.organization
    history = []
    if org:
        hist_qs = (SubmissionEvent.objects.filter(submission__submitter__organization=org,
                                                  action__in=["approved", "rejected", "revision"])
                   .exclude(submission=sub).select_related("submission").order_by("-at")[:5])
        history = [{"date": timezone.localtime(e.at).date(), "upload_id": e.submission.upload_id,
                    "label": tr_pair(lang, schemas.STATUS_LABEL["revision" if e.action == "revision" else e.action]),
                    "variant": "neutral" if e.action == "revision" else ("success" if e.action == "approved" else "danger")}
                   for e in hist_qs]
    summary = sub.apply_summary or {}
    is_pending = sub.status == Submission.Status.PENDING

    def loc(items):
        return [{**x, "text": x["en"] if lang == "en" else x["ko"]} for x in items]

    return render_page(request, "uploads/admin/review_detail.html", {
        "dup_preview": loc(apply.preview_duplicates(sub)) if is_pending else [],
        "duplicates": loc(summary.get("duplicates", [])),
        "dropped": summary.get("dropped", []),
        "own_upload": workflow.is_own_upload(sub, request.user),
        "sub": sub, "row": queue_row(sub, lang), "domain_name": domain_label(sub.domain, lang, short=False),
        "org": data_label(org, lang) if org else "-",
        "listed_issues": localize_messages(listed, lang), "issue_total": iss["error_count"] + iss["warning_count"],
        "errors": localize_messages(iss["errors"], lang), "warnings": localize_messages(iss["warnings"], lang),
        "error_count": iss["error_count"], "warning_count": iss["warning_count"],
        "preview_cols": cols, "preview": [{"rec": r, "cells": [_cell(r.payload.get(c)) for c in cols]} for r in recs],
        "more_rows": max(0, sub.total_records - len(recs)),
        "files": [{"f": f, "size": format_size(f.size)} for f in sub.files.all()],
        "pii": pii_context(sub, lang),
        "checklist": [tr_pair(lang, c) for c in schemas.REVIEW_CHECK_ITEMS],
        "history": history, "is_pending": is_pending,
        "skipped": [{**s, "text": s["en"] if lang == "en" else s["ko"]} for s in summary.get("skipped", [])],
        "dst_profiles": [{**p, "text": p["en"] if lang == "en" else p["ko"]} for p in summary.get("dst_profiles", [])],
        "deletion_requested": bool(sub.deletion_requested_at),
    }, sidebar="admin")


def _preview_columns(sub, recs):
    if sub.csv_headers:
        return [c for c in sub.csv_headers][:12]
    cols = []
    for r in recs:
        for k, v in r.payload.items():
            if k not in cols and not isinstance(v, (list, dict)):
                cols.append(k)
    return cols[:12]


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return f"[{len(v)}]"
    return str(v)


def _review_action(request, uid, action):
    try:
        sub = get_object_or_404(Submission, upload_id=uid)
    except DatabaseError:  # 다른 처리의 쓰기 잠금과 겹친 첫 조회(테스트용 공유 캐시 SQLite 등) — 아래와 같은 안내
        logger.exception("review %s lookup failed for %s", action, uid)
        messages.error(request, t(request, "다른 처리와 겹쳐 처리하지 못했습니다. 잠시 후 다시 시도해주세요.",
                                  "This could not be processed because it overlapped with another operation. "
                                  "Please try again shortly."))
        return redirect("/admin/review")
    try:
        if action == "approve":
            workflow.approve(sub, request.user, request.POST.getlist("check"), request.POST.get("comment", ""))
            messages.success(request, t(request, "승인 처리되었습니다.", "The data has been approved."))
        elif action == "reject":
            workflow.reject(sub, request.user, request.POST.get("reason", ""))
            messages.success(request, t(request, "반려 처리되었습니다.", "The data has been rejected."))
        else:
            workflow.request_revision(sub, request.user, request.POST.get("reason", ""))
            messages.success(request, t(request, "수정 요청이 전달되었습니다.", "The revision request has been sent."))
    except workflow.WorkflowError as exc:
        messages.error(request, exc.en if get_lang(request) == "en" else exc.ko)
        return redirect(f"/admin/review/{sub.upload_id}")
    except DatabaseError:
        # 동시 처리(잠금 대기 초과·직렬화 실패·unique 경합) — 트랜잭션은 롤백됐다. 다른 예외는 결함이므로 잡지 않는다.
        logger.exception("review %s failed for %s", action, uid)
        messages.error(request, t(request, "다른 처리와 겹쳐 처리하지 못했습니다. 잠시 후 다시 시도해주세요.",
                                  "This could not be processed because it overlapped with another operation. "
                                  "Please try again shortly."))
        return redirect(f"/admin/review/{sub.upload_id}")
    return redirect("/admin/review")


@admin_required
@require_POST
def review_approve(request, uid):
    return _review_action(request, uid, "approve")


@admin_required
@require_POST
def review_reject(request, uid):
    return _review_action(request, uid, "reject")


@admin_required
@require_POST
def review_revise(request, uid):
    return _review_action(request, uid, "revise")
