"""포털 관리자 — 업로드 삭제 요청 처리 (/admin/deletions). 원본은 '관리자 승인 후 처리됩니다' 안내만 있었다."""
from __future__ import annotations

import logging

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import admin_required
from apps.cases.services import data_label
from apps.core.i18n import get_lang, t
from apps.core.views import render_page

from .models import DeletedSubmission, Submission
from .services import deletion, workflow
from .views import domain_label, status_info

logger = logging.getLogger(__name__)


def _row(sub, lang):
    return {"sub": sub, "domain_label": domain_label(sub.domain, lang), "status": status_info(sub, lang),
            "submitter": sub.submitter.shown_name, "org": data_label(sub.submitter.organization or "-", lang)}


@admin_required
@require_safe
def deletion_list(request):
    lang = get_lang(request)
    qs = deletion.pending_deletions().select_related("submitter").order_by("deletion_requested_at", "id")
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    done = DeletedSubmission.objects.select_related("reviewer")[:10]
    return render_page(request, "uploads/admin/deletions.html", {
        "rows": [_row(s, lang) for s in page.object_list], "page": page, "pending": qs.count(),
        "done": [{"log": d, "domain_label": domain_label(d.domain, lang) if d.domain in ("clinical", "regimen", "lab", "genomic", "imaging") else d.domain}
                 for d in done],
    }, sidebar="admin")


def _localize(items, lang):
    return [x["en"] if lang == "en" else x["ko"] for x in items]


@admin_required
@require_safe
def deletion_detail(request, uid):
    lang = get_lang(request)
    sub = get_object_or_404(deletion.pending_deletions().select_related("submitter"), upload_id=uid)
    p = deletion.plan(sub)
    return render_page(request, "uploads/admin/deletion_detail.html", {
        "row": _row(sub, lang), "sub": sub, "plan": p, "irreversible": _localize(p["irreversible"], lang),
        "pairs": [{"label": x["en"] if lang == "en" else x["ko"], "mode": x["mode"], "others": x["others"]}
                  for x in p["pairs"]],
        "notes": _localize(p["notes"], lang),
        "own_upload": workflow.is_own_upload(sub, request.user),
    }, sidebar="admin")


def _action(request, uid, approve: bool):
    try:
        sub = get_object_or_404(Submission, upload_id=uid)
    except DatabaseError:  # 다른 처리의 쓰기 잠금과 겹친 첫 조회 — 아래와 같은 안내
        logger.exception("deletion lookup failed for %s", uid)
        messages.error(request, t(request, "다른 처리와 겹쳐 삭제 요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.",
                                  "The deletion request could not be processed because it overlapped with another "
                                  "operation. Please try again shortly."))
        return redirect("/admin/deletions")
    try:
        if approve:
            deletion.approve_deletion(sub, request.user)
            messages.success(request, t(request, f"{uid} 업로드를 삭제했습니다.", f"Upload {uid} has been deleted."))
        else:
            deletion.reject_deletion(sub, request.user, request.POST.get("reason", ""))
            messages.success(request, t(request, "삭제 요청을 반려했습니다.", "The deletion request has been rejected."))
    except workflow.WorkflowError as exc:
        messages.error(request, exc.en if get_lang(request) == "en" else exc.ko)
        if deletion.pending_deletions().filter(pk=sub.pk).exists():
            return redirect(f"/admin/deletions/{uid}")
    except DatabaseError:
        # 동시 처리(잠금 대기 초과·직렬화 실패·교착 해소·unique 경합) — 트랜잭션은 롤백됐다. 500 대신 다시 시도 안내.
        # 그 밖의 예외(LookupError 등)는 코드 결함이므로 잡지 않는다(드러나야 고친다).
        logger.exception("deletion %s failed for %s", "approve" if approve else "reject", uid)
        messages.error(request, t(request, "다른 처리와 겹쳐 삭제 요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.",
                                  "The deletion request could not be processed because it overlapped with another "
                                  "operation. Please try again shortly."))
    return redirect("/admin/deletions")


@admin_required
@require_POST
def deletion_approve(request, uid):
    return _action(request, uid, True)


@admin_required
@require_POST
def deletion_reject(request, uid):
    return _action(request, uid, False)
