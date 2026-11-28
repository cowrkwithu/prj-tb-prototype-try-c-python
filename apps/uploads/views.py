"""데이터 업로드 화면 (원본 pages/Upload/*, DataSharing/MyUploadsTab).

화면 흐름은 서버 렌더링 + POST-Redirect-GET. 업로드 1건의 진행 상태(단계·입력값·행·파일)는 Submission(draft)에
저장되므로 새로고침·재로그인 후에도 ?id=<업로드 ID> 로 이어서 작성할 수 있다(원본은 브라우저 메모리/localStorage).
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Max
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import login_required
from apps.accounts.utils import safe_next
from apps.core.filetx import atomic_write
from apps.core.i18n import get_lang, t, tr_pair
from apps.core.safejson import has_bad_chars
from apps.core.views import render_page

from .models import Submission, SubmissionFile
from .services import csvio, deidentify, schemas, workflow
from .services.files import FileRejected, format_size

UPLOAD_ID_RE = r"UPL-\d{8}-\d{3,6}"


# ═══ 공통 표시 도우미 ═══
def status_info(sub: Submission, lang: str) -> dict:
    key = sub.display_status
    return {"key": key, "label": tr_pair(lang, schemas.STATUS_LABEL[key]), "variant": schemas.STATUS_VARIANT[key],
            "icon": schemas.STATUS_ICON[key]}


def domain_label(domain: str, lang: str, short: bool = True) -> str:
    meta = schemas.DOMAIN_META[domain]
    return tr_pair(lang, meta["short"] if short else meta["name"])


def submission_row(sub: Submission, lang: str) -> dict:
    return {"sub": sub, "status": status_info(sub, lang), "domain_label": domain_label(sub.domain, lang),
            "file_name": sub.source_filename or (_first_file_name(sub) if sub.domain in ("genomic", "imaging") else "")
            or ("web_form_entry" if sub.method == "form" else "-"),
            "path": schemas.upload_path(sub.domain)}


def _first_file_name(sub):
    f = next(iter(sub.files.all()), None)
    return f.original_name if f else ""


def issues_of(sub: Submission, limit: int | None = None) -> dict:
    """검증 메시지를 오류/경고로 나눈다(화면은 limit 건까지, 리포트 CSV 는 전체)."""
    errors, warnings = [], []
    total_err = total_warn = 0
    for rec in sub.records.order_by("row_no"):
        for m in rec.messages:
            if m["severity"] == "error":
                total_err += 1
                if limit is None or len(errors) < limit:
                    errors.append(m)
            else:
                total_warn += 1
                if limit is None or len(warnings) < limit:
                    warnings.append(m)
    return {"errors": errors, "warnings": warnings, "error_count": total_err, "warning_count": total_warn}


def localize_messages(items, lang):
    return [{**m, "text": m["en"] if lang == "en" else m["ko"]} for m in items]


def pii_context(sub: Submission, lang: str) -> dict:
    rep = sub.pii_report or {}
    det = [{**d, "text": d["en"] if lang == "en" else d["ko"],
            "type_label": tr_pair(lang, deidentify.PII_TYPE_LABEL[d["type"]])} for d in rep.get("detections", [])]
    counts = [{"label": tr_pair(lang, deidentify.PII_TYPE_LABEL[k]), "count": n}
              for k, n in (rep.get("count_by_type") or {}).items() if n]
    tone = "block" if rep.get("blocking") else ("clean" if rep.get("clean", True) else "review")
    return {"report": rep, "detections": det, "counts": counts, "tone": tone,
            "summary": deidentify.summary_text(rep, lang) if rep else "", "has_rows": bool(rep.get("scanned_rows"))}


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _domain_or_404(domain):
    if domain not in schemas.DOMAINS:
        raise Http404
    return domain


def _own_submission(request, uid, domain=None) -> Submission:
    if not isinstance(uid, str) or has_bad_chars(uid):  # NUL 이 든 ID 는 없다(PostgreSQL 은 비교 시 DataError)
        raise Http404
    qs = Submission.objects.filter(submitter=request.user)
    if domain:
        qs = qs.filter(domain=domain)
    return get_object_or_404(qs, upload_id=uid)


def _wizard_url(domain, sub=None, **params) -> str:
    q = {}
    if sub is not None:
        q["id"] = sub.upload_id
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    return f"/upload/{domain}" + (f"?{urlencode(q)}" if q else "")


def _upload_aborted(request) -> bool:
    """본문(multipart)을 먼저 파싱해야 업로드 핸들러의 중단 여부를 알 수 있다."""
    request.FILES  # noqa: B018 — 파싱 유도
    return bool(getattr(request, "upload_aborted", False))


def _err(request, ko, en):
    messages.error(request, t(request, ko, en))


# ═══ 업로드 허브 ═══
@login_required
@require_safe
def hub(request):
    """원본 pages/Upload/index.tsx — 6개 카드(커스텀은 3단계 데이터 구조로 연결) + 최근 업로드 이력(DB)."""
    lang = get_lang(request)
    latest = dict(Submission.objects.filter(submitter=request.user).values_list("domain")
                  .annotate(m=Max("created_at")).values_list("domain", "m"))
    cards = []
    for d in schemas.DOMAINS:
        meta = schemas.DOMAIN_META[d]
        cards.append({"key": d, "path": schemas.upload_path(d), "icon": meta["icon"], "color": meta["color"],
                      "name": tr_pair(lang, meta["name"]), "desc": tr_pair(lang, meta["desc"]),
                      "formats": [t(request, "웹 폼", "Web Form") if f == "web" else f for f in meta["formats"]],
                      "latest": timezone.localtime(latest[d]).date() if latest.get(d) else None})
    recent = Submission.objects.filter(submitter=request.user).prefetch_related("files").order_by("-created_at", "-id")[:10]
    return render_page(request, "uploads/hub.html", {
        "cards": cards, "recent": [submission_row(s, lang) for s in recent]})


# ═══ 업로드 마법사 ═══
@login_required
@require_safe
def wizard(request, domain):
    domain = _domain_or_404(domain)
    lang = get_lang(request)
    steps = schemas.STEPS[domain]
    sub = None
    uid = request.GET.get("id")
    if uid:
        sub = _own_submission(request, uid, domain)
    prefill_cid = (request.GET.get("conditionId") or "").strip()[:40]
    if sub is not None:
        mode = sub.mode
        if sub.status != Submission.Status.DRAFT:
            idx = schemas.step_index(domain, "done")
        else:
            idx = min(sub.step, len(steps) - 2)
            req = _int(request.GET.get("step"))
            if req is not None and 0 <= req <= sub.step and req < len(steps) - 1:
                idx = req
    else:
        mode = request.GET.get("mode")
        if mode not in schemas.MODES[domain]:
            mode = None
        if mode is None and prefill_cid and domain in ("clinical", "regimen", "lab"):
            mode = "form"  # 원본: 케이스 상세에서 conditionId 로 들어오면 웹 폼 입력 단계부터
        idx = 1 if mode else 0
    key = steps[idx][0]
    ctx = {
        "domain": domain, "meta": schemas.DOMAIN_META[domain], "title": tr_pair(lang, schemas.DOMAIN_META[domain]["title"]),
        "subtitle": tr_pair(lang, schemas.DOMAIN_META[domain]["subtitle"]),
        "steps": [{"label": tr_pair(lang, lbl), "state": "done" if i < idx else ("current" if i == idx else "todo")}
                  for i, (_, lbl) in enumerate(steps)],
        "step_idx": idx, "step_key": key, "sub": sub, "mode": mode, "prefill_cid": prefill_cid,
        "status": status_info(sub, lang) if sub else None,
        "uses_csv": bool(mode) and schemas.uses_csv(domain, mode),
        "prev_url": _wizard_url(domain, sub, step=idx - 1) if sub and idx > 1 else (
            _wizard_url(domain, sub, step=0) if sub else _wizard_url(domain, conditionId=prefill_cid)),
    }
    ctx.update(_step_context(request, domain, key, sub, mode, lang, prefill_cid))
    ctx["step_template"] = _step_template(domain, key, mode)
    return render_page(request, "uploads/wizard.html", ctx)


def _step_template(domain, key, mode):
    if key == "method":
        return {"genomic": "uploads/steps/method_genomic.html", "imaging": "uploads/steps/method_imaging.html"}.get(
            domain, "uploads/steps/method.html")
    if key == "data":
        if mode and schemas.uses_csv(domain, mode):
            return "uploads/steps/csv.html"
        return f"uploads/steps/form_{domain}.html"
    return f"uploads/steps/{key}.html"


def _step_context(request, domain, key, sub, mode, lang, prefill_cid):
    ctx = {}
    if key == "method":
        ctx["modes"] = schemas.MODES[domain]
    if key == "data":
        if mode and schemas.uses_csv(domain, mode):
            tpl = schemas.template(domain)
            cols = ", ".join(tpl["required"])
            ctx["required_cols"] = tpl["required"]
            ctx["csv_label"] = t(request, "CSV / TSV 파일을 드래그하거나 클릭하여 선택", "Drag a CSV / TSV file here or click to select")
            ctx["csv_hint"] = t(request, f"필수 컬럼: {cols}", f"Required columns: {cols}")
            if sub is not None and (sub.total_records or sub.file_errors or sub.source_filename):
                recs = list(sub.records.order_by("row_no")[:10])
                headers = sub.csv_headers or []
                iss = issues_of(sub, limit=200)
                ctx.update({
                    "parsed": True, "preview_rows": [{"rec": r, "cells": [r.payload.get(h, "") for h in headers[:8]]} for r in recs],
                    "display_cols": headers[:8], "more_cols": max(0, len(headers) - 8),
                    "errors": localize_messages(iss["errors"], lang), "warnings": localize_messages(iss["warnings"], lang),
                    "error_count": iss["error_count"], "warning_count": iss["warning_count"],
                    "file_errors": [e["en"] if lang == "en" else e["ko"] for e in sub.file_errors],
                    "encoding": (sub.form_data or {}).get("encoding", ""),
                })
        else:
            ctx.update(_form_context(domain, sub, lang, prefill_cid))
    elif key == "files":
        kind = SubmissionFile.Kind.SEQUENCE if domain == "genomic" else SubmissionFile.Kind.IMAGE
        stored = list(sub.files.filter(kind=kind)) if sub else []
        if domain == "genomic":
            lim = workflow.files.settings.UPLOAD_LIMITS["sequence"]
            max_files = lim["max_files"][mode or "individual"]
        else:
            lim = workflow.files.settings.UPLOAD_LIMITS[schemas.image_limit_key(mode or "CXR")]
            max_files = lim["max_files"]
        fd = sub.form_data if sub else {}
        rec = sub.records.first() if sub else None
        ctx["files_label"] = t(request, f"{mode} 파일을 드래그하거나 클릭하여 선택", f"Drag {mode} files here or click to select")
        ctx.update({"stored_files": [{"f": f, "size": format_size(f.size)} for f in stored], "max_files": max_files,
                    "max_size": format_size(lim["max_bytes"]), "max_bytes": lim["max_bytes"], "accept": ",".join(lim["exts"]),
                    "total_size": format_size(sum(f.size for f in stored)) if stored else "",
                    "cid": fd.get("condition_id", prefill_cid), "study_date": fd.get("study_date", ""),
                    "field_errors": _field_errors(rec, lang) if rec else {}})
    elif key == "dicom":
        imgs = list(sub.files.filter(kind=SubmissionFile.Kind.IMAGE))
        ctx["dicom_files"] = [{"f": f, "info": f.meta.get("dicom")} for f in imgs]
        ctx["dicom_risky"] = any(t_["risky"] for f in imgs for t_ in (f.meta.get("dicom") or {}).get("tags", []))
        ctx["dicom_unreadable"] = any((f.meta.get("dicom") or {}).get("parsed") is False for f in imgs)
    elif key == "deid":
        ctx["pii"] = pii_context(sub, lang)
        ctx["deid_items"] = [tr_pair(lang, p) for p in schemas.DEID_CHECK_ITEMS]
        ctx["today"] = timezone.localdate().isoformat()
        ready, problems = workflow.data_ready(sub)
        ctx["data_problems"] = [en if lang == "en" else ko for ko, en in problems]
    elif key == "done":
        ctx["records_label"] = sub.total_records if sub else 0
    return ctx


def _field_errors(rec, lang) -> dict:
    out = {}
    for m in (rec.messages if rec else []):
        if m["severity"] == "error" and m["field"] not in out:
            out[m["field"]] = m["en"] if lang == "en" else m["ko"]
    return out


def _form_context(domain, sub, lang, prefill_cid):
    fd = dict(sub.form_data) if sub else {}
    if not fd.get("condition_id") and prefill_cid:
        fd["condition_id"] = prefill_cid
    recs = list(sub.records.order_by("row_no")) if sub else []
    vl = lambda v: tr_pair(lang, schemas.VALUE_LABEL.get(v, (v, v)))  # noqa: E731
    ctx = {"fd": fd, "touched": bool(sub and sub.total_records), "field_errors": _field_errors(recs[0], lang) if recs else {}}
    all_errors = [m for r in recs for m in r.messages if m["severity"] == "error"]
    ctx["error_list"] = [{"label": _field_label(domain, m["field"], lang), "text": m["en"] if lang == "en" else m["ko"],
                          "row": m.get("row")} for m in all_errors]
    ctx["warning_list"] = [{"label": _field_label(domain, m["field"], lang), "text": m["en"] if lang == "en" else m["ko"]}
                           for r in recs for m in r.messages if m["severity"] == "warning"]
    if domain == "clinical":
        ctx.update({
            "sexes": [(v, vl(v)) for v in schemas.SEXES], "countries": [(v, vl(v)) for v in schemas.FORM_COUNTRIES],
            "diag_types": schemas.DIAGNOSIS_TYPES, "sites": [(v, vl(v)) for v in schemas.SITES_OF_DISEASE],
            "comorbidities": [(v, vl(v)) for v in schemas.COMORBIDITIES],
            "outcomes": [(v, vl(v)) for v in schemas.OUTCOMES],
            "selected_comorbidities": [c for c in (fd.get("comorbidities") or "").split(",") if c],
            "value_labels": {k: tr_pair(lang, v) for k, v in schemas.VALUE_LABEL.items()},
        })
    elif domain == "regimen":
        ctx.update({
            "drug_groups": [{"label": tr_pair(lang, g), "drugs": d} for g, d in schemas.DRUG_GROUPS],
            "drug_codes": schemas.DRUG_CODE_BY_NAME, "routes": schemas.ROUTES, "drug_colors": schemas.DRUG_COLORS,
            "templates": [{"label": tr_pair(lang, lbl), "drugs": [{"name": n, "code": c, "startDay": s, "endDay": e}
                                                                  for n, c, s, e in drugs]}
                          for lbl, drugs in schemas.REGIMEN_TEMPLATES],
            "initial_drugs": fd.get("drugs", []),
        })
    elif domain == "lab":
        flags = fd.get("flags") or {}
        dst = {d: fd.get(d.lower(), "") for d in schemas.DST_FORM_DRUGS}
        ctx.update({
            "flags": flags, "specimen_types": schemas.SPECIMEN_TYPES, "microscopy_opts": schemas.MICROSCOPY,
            "culture_methods": schemas.CULTURE_METHODS, "culture_results": schemas.CULTURE_RESULTS,
            "mtb_opts": schemas.GENEXPERT_MTB, "rif_opts": schemas.GENEXPERT_RIF, "dst_methods": schemas.DST_METHODS,
            "dst_groups": [{"label": tr_pair(lang, g), "drugs": d} for g, d in schemas.DST_DRUG_GROUPS],
            "dst_initial": dst, "biochem_tests": schemas.BIOCHEM_TESTS, "biochem_units": schemas.BIOCHEM_UNITS,
            "biochem_initial": fd.get("biochemistry", []),
        })
    elif domain == "genomic":
        ctx.update({"platforms": schemas.SEQ_PLATFORMS_FORM, "seq_methods": schemas.SEQ_METHODS, "lineages": schemas.LINEAGES})
    elif domain == "imaging":
        ctx.update({"finding_groups": [{"label": tr_pair(lang, g), "items": i} for g, i in schemas.FINDING_GROUPS],
                    "locations": schemas.LOCATIONS, "severities": schemas.SEVERITIES, "tb_activity": schemas.TB_ACTIVITY,
                    "initial_findings": fd.get("findings", []),
                    "preview_file": sub.files.filter(kind=SubmissionFile.Kind.IMAGE).first() if sub else None})
    return ctx


def _field_label(domain, field, lang):
    if domain == "clinical" and field in schemas.CLINICAL_FIELD_LABEL:
        return tr_pair(lang, schemas.CLINICAL_FIELD_LABEL[field])
    return field


def _load_or_create(request, domain, require_mode_csv=None) -> Submission | None:
    """POST 의 id 로 본인 draft 를 찾거나, 없으면 mode 로 새 draft 를 만든다."""
    uid = request.POST.get("id")
    if uid:
        sub = _own_submission(request, uid, domain)
        if sub.status != Submission.Status.DRAFT:
            _err(request, "이미 제출된 업로드는 수정할 수 없습니다", "Submitted uploads cannot be edited")
            return None
        return sub
    mode = request.POST.get("mode")
    if mode not in schemas.MODES[domain]:
        _err(request, "업로드 방식을 먼저 선택하세요", "Select an upload method first")
        return None
    if require_mode_csv is not None and schemas.uses_csv(domain, mode) != require_mode_csv:
        _err(request, "업로드 방식이 맞지 않습니다", "Upload method mismatch")
        return None
    return workflow.create_draft(request.user, domain, mode, workflow.form_text(request.POST.get("condition_id"))[:40])


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def choose_mode(request, domain):
    """방식 선택(0단계) — 기존 draft 의 방식을 바꾸면 입력한 행·CSV 는 비운다."""
    domain = _domain_or_404(domain)
    mode = request.POST.get("mode")
    cid = (request.POST.get("conditionId") or "").strip()[:40]
    if mode not in schemas.MODES[domain]:
        _err(request, "업로드 방식을 먼저 선택해야 다음 단계로 넘어갈 수 있습니다.", "Select an upload method first to continue.")
        return redirect(_wizard_url(domain, conditionId=cid))
    uid = request.POST.get("id")
    if not uid:
        return redirect(_wizard_url(domain, mode=mode, conditionId=cid))
    sub = _own_submission(request, uid, domain)
    try:
        workflow.change_mode(sub, mode)
    except workflow.WorkflowError as exc:
        _err(request, exc.ko, exc.en)
    return redirect(_wizard_url(domain, sub))


def _edit_failed(request, domain, sub, exc):
    """편집 중 다른 요청이 먼저 제출·검토해 draft 가 아니게 된 경우."""
    _err(request, exc.ko, exc.en)
    return redirect(_wizard_url(domain, sub))


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def save_data(request, domain):
    """웹 폼 입력(임상·레지멘·검사실·유전체 메타데이터·영상 어노테이션) — 임시저장 또는 다음 단계."""
    domain = _domain_or_404(domain)
    sub = _load_or_create(request, domain, require_mode_csv=False)
    if sub is None:
        return redirect(_wizard_url(domain, conditionId=request.POST.get("condition_id", "")))
    data_idx = schemas.step_index(domain, "data")
    try:
        workflow.save_form(sub, request.POST)
        if request.POST.get("action") == "draft":
            workflow.set_step(sub, max(sub.step, data_idx))
            messages.success(request, t(request, f"임시저장되었습니다 ({sub.upload_id})", f"Draft saved ({sub.upload_id})"))
            return redirect(_wizard_url(domain, sub, step=data_idx))
        ok, problems = workflow.data_ready(sub)
        if not ok:
            workflow.set_step(sub, max(sub.step, data_idx))
            _err(request, *problems[0])
            return redirect(_wizard_url(domain, sub, step=data_idx))
        workflow.set_step(sub, data_idx + 1)
    except workflow.WorkflowError as exc:
        return _edit_failed(request, domain, sub, exc)
    return redirect(_wizard_url(domain, sub))


def _aborted_redirect(request, domain):
    _err(request, "파일이 너무 큽니다(업로드 크기 상한 초과) — 수신을 중단했습니다",
         "File too large (upload size limit exceeded) — the upload was stopped")
    back = safe_next(request, request.POST.get("back"), default="")
    if back:
        return redirect(back)
    uid = request.POST.get("id") or request.GET.get("id")
    if uid and re.fullmatch(UPLOAD_ID_RE, uid):
        return redirect(_wizard_url(domain, None, id=uid))
    return redirect(_wizard_url(domain))


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def upload_csv(request, domain):
    domain = _domain_or_404(domain)
    if _upload_aborted(request):
        return _aborted_redirect(request, domain)
    up = request.FILES.get("file")
    if up is None:
        _err(request, "CSV 파일을 선택하세요", "Choose a CSV file")
        uid = request.POST.get("id")
        return redirect(_wizard_url(domain, None, id=uid, mode=request.POST.get("mode")))
    sub = _load_or_create(request, domain, require_mode_csv=True)
    if sub is None:
        return redirect(_wizard_url(domain))
    data_idx = schemas.step_index(domain, "data")
    try:
        try:
            workflow.handle_csv(sub, up, get_lang(request))
        except FileRejected as exc:
            workflow.record_file_error(sub, exc.ko, exc.en)
            _err(request, exc.ko, exc.en)
        workflow.set_step(sub, data_idx, ignore_errors=False)
    except workflow.WorkflowError as exc:
        return _edit_failed(request, domain, sub, exc)
    return redirect(_wizard_url(domain, sub, step=data_idx))


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def proceed(request, domain):
    """CSV 데이터·파일·DICOM 단계에서 다음 단계로."""
    domain = _domain_or_404(domain)
    sub = _own_submission(request, request.POST.get("id", ""), domain)
    if sub.status != Submission.Status.DRAFT:
        return redirect(_wizard_url(domain, sub))
    key = request.POST.get("from")
    idx = schemas.step_index(domain, key) if key in [k for k, _ in schemas.STEPS[domain]] else None
    if idx is None or idx == 0 or idx > sub.step:
        return redirect(_wizard_url(domain, sub))
    try:
        if key == "data":
            workflow.set_step(sub, sub.step, ignore_errors=bool(request.POST.get("ignore_errors")))
            ok, problems = workflow.data_ready(sub)
        elif key == "files":
            ok, problems = workflow.files_ready(sub)
        else:  # dicom: 위험·판독 불가 태그는 다음 단계(비식별화 확인)의 자동 탐지가 제출을 막는다
            ok, problems = True, []
        if not ok:
            _err(request, *problems[0])
            return redirect(_wizard_url(domain, sub, step=idx))
        workflow.set_step(sub, idx + 1)
    except workflow.WorkflowError as exc:
        return _edit_failed(request, domain, sub, exc)
    return redirect(_wizard_url(domain, sub))


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def upload_files(request, domain):
    """시퀀스(유전체)·영상 파일 추가 + (영상) Condition ID·촬영일.

    요청 1건에 파일은 1개만 받는다(대용량 요청 버퍼링을 줄이기 위해 화면이 파일마다 순차 전송 — 리뷰 결정).
    화면의 XHR 요청(X-Requested-With)에는 JSON 으로 응답해 파일별 진행·실패·재시도를 표시한다.
    """
    domain = _domain_or_404(domain)
    if domain not in ("genomic", "imaging"):
        raise Http404
    ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    lang = get_lang(request)

    def fail(ko, en, status=400, sub=None):
        if ajax:
            return JsonResponse({"ok": False, "message": en if lang == "en" else ko,
                                 "upload_id": sub.upload_id if sub else ""}, status=status)
        _err(request, ko, en)
        return redirect(_wizard_url(domain, sub, step=schemas.step_index(domain, "files")) if sub else _wizard_url(domain))

    if _upload_aborted(request):
        if ajax:
            return fail("파일이 너무 큽니다(업로드 크기 상한 초과) — 수신을 중단했습니다",
                        "File too large (upload size limit exceeded) — the upload was stopped", status=413)
        return _aborted_redirect(request, domain)
    uploads = request.FILES.getlist("files")
    if len(uploads) > 1:
        return fail("파일은 요청마다 1개씩 보내야 합니다", "Send one file per request")
    sub = _load_or_create(request, domain)
    if sub is None:
        return fail("업로드를 시작할 수 없습니다", "Cannot start the upload")
    files_idx = schemas.step_index(domain, "files")
    try:
        rejected = workflow.add_files(sub, uploads)
        if domain == "imaging":
            workflow.save_imaging_meta(sub, request.POST)
        if ajax:
            workflow.set_step(sub, max(sub.step, files_idx))
            if rejected:
                r = rejected[0]
                return fail(f"{r['name']}: {r['ko']}", f"{r['name']}: {r['en']}", sub=sub)
            return JsonResponse({"ok": True, "upload_id": sub.upload_id,
                                 "redirect": _wizard_url(domain, sub, step=files_idx)})
        for r in rejected:
            _err(request, f"{r['name']}: {r['ko']}", f"{r['name']}: {r['en']}")
        if request.POST.get("action") == "next" and not rejected:
            ok, problems = workflow.files_ready(sub)
            if ok:
                workflow.set_step(sub, files_idx + 1)
                return redirect(_wizard_url(domain, sub))
            _err(request, *problems[0])
        workflow.set_step(sub, max(sub.step, files_idx))
    except workflow.WorkflowError as exc:
        if ajax:
            return fail(exc.ko, exc.en, status=409, sub=sub)
        return _edit_failed(request, domain, sub, exc)
    return redirect(_wizard_url(domain, sub, step=files_idx))


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def remove_file(request, domain, file_id):
    domain = _domain_or_404(domain)
    if "files" not in [k for k, _ in schemas.STEPS[domain]]:  # 파일 단계가 없는 도메인(임상 등) — 500 대신 404
        raise Http404
    sub = _own_submission(request, request.POST.get("id", ""), domain)
    if sub.status != Submission.Status.DRAFT:
        raise Http404
    try:
        workflow.remove_file(sub, file_id)
    except workflow.WorkflowError as exc:
        _err(request, exc.ko, exc.en)
    return redirect(_wizard_url(domain, sub, step=schemas.step_index(domain, "files")))


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def submit(request, domain):
    domain = _domain_or_404(domain)
    sub = _own_submission(request, request.POST.get("id", ""), domain)
    deid_idx = schemas.step_index(domain, "deid")
    if sub.step < deid_idx and sub.status == Submission.Status.DRAFT:
        return redirect(_wizard_url(domain, sub))
    try:
        workflow.submit(sub, request.user, request.POST.getlist("deid"), request.POST.get("signer", ""),
                        request.POST.get("confirm_date", ""))
    except workflow.WorkflowError as exc:
        _err(request, exc.ko, exc.en)
        return redirect(_wizard_url(domain, sub, step=deid_idx))
    messages.success(request, t(request, "제출되었습니다. 관리자 검증 후 반영됩니다.",
                                "Submitted. It will be published after administrator review."))
    return redirect(_wizard_url(domain, sub))


@login_required
@require_safe
def template_csv(request, domain):
    domain = _domain_or_404(domain)
    resp = HttpResponse(csvio.template_csv(domain), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{schemas.template(domain)["type"]}_template.csv"'
    return resp


def _can_view(request, sub) -> bool:
    return sub.submitter_id == request.user.pk or request.user.is_portal_admin


@login_required
@require_safe
def report_csv(request, uid):
    sub = get_object_or_404(Submission, upload_id=uid)
    if not _can_view(request, sub):
        raise Http404
    iss = issues_of(sub)
    resp = HttpResponse(csvio.report_csv(iss["errors"] + iss["warnings"], get_lang(request)),
                        content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="validation_report_{sub.upload_id}.csv"'
    return resp


@login_required
@require_safe
def download_file(request, file_id):
    """업로드 파일 내려받기 — 제출자 본인 또는 관리자만. 공개 media URL 은 없다."""
    sf = get_object_or_404(SubmissionFile.objects.select_related("submission"), pk=file_id)
    if not _can_view(request, sf.submission):
        raise Http404
    try:
        fh = sf.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404 from None
    return FileResponse(fh, as_attachment=True, filename=sf.original_name)


# ═══ 내 업로드 (원본 DataSharing/MyUploadsTab — 3단계에서 /data?tab=uploads 탭으로 포함) ═══
MY_TYPE_OPTIONS = ["all"] + schemas.DOMAINS
MY_STATUS_OPTIONS = ["all", "draft", "revision", "pending", "approved", "rejected"]


def my_uploads_context(request, extra_params: dict | None = None, back: str = "/upload/mine") -> dict:
    """내 업로드 목록 문맥. /data?tab=uploads 탭은 extra_params={"tab": "uploads"} 로 필터·페이지 주소에 탭을 싣고,
    삭제·재제출 후 돌아올 주소(back)를 넘긴다 — 같은 partial(uploads/_my_uploads.html)을 그대로 쓴다."""
    lang = get_lang(request)
    extra_params = dict(extra_params or {})
    ftype = request.GET.get("type", "all")
    fstatus = request.GET.get("status", "all")
    ftype = ftype if ftype in MY_TYPE_OPTIONS else "all"
    fstatus = fstatus if fstatus in MY_STATUS_OPTIONS else "all"
    qs = Submission.objects.filter(submitter=request.user).prefetch_related("files").order_by("-created_at", "-id")
    if ftype != "all":
        qs = qs.filter(domain=ftype)
    if fstatus == "draft":
        qs = qs.filter(status="draft", revision_requested=False)
    elif fstatus == "revision":
        qs = qs.filter(status="draft", revision_requested=True)
    elif fstatus != "all":
        qs = qs.filter(status=fstatus)
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    all_label = t(request, "전체", "All")
    base = {**extra_params, "type": ftype, "status": fstatus}
    return {
        "my_rows": [submission_row(s, lang) for s in page.object_list], "my_page": page,
        "my_filters": base, "my_open": request.GET.get("open", ""),
        "my_hidden": list(extra_params.items()), "my_back": back,
        "my_type_options": [("all", all_label)] + [(d, domain_label(d, lang)) for d in schemas.DOMAINS],
        "my_status_options": [("all", all_label)] + [(k, tr_pair(lang, schemas.STATUS_LABEL[k])) for k in MY_STATUS_OPTIONS[1:]],
        "my_prev_url": f"?{urlencode({**base, 'page': page.previous_page_number()})}" if page.has_previous() else "",
        "my_next_url": f"?{urlencode({**base, 'page': page.next_page_number()})}" if page.has_next() else "",
    }


@login_required
@require_safe
def my_uploads(request):
    return render_page(request, "uploads/mine.html", my_uploads_context(request))


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def my_delete(request, uid):
    sub = _own_submission(request, uid)
    try:
        if sub.status == Submission.Status.DRAFT:
            workflow.delete_draft(sub, request.user)
            messages.success(request, t(request, "임시저장 업로드를 삭제했습니다.", "Draft deleted."))
        else:
            if workflow.request_deletion(sub, request.user):
                messages.success(request, t(request, "삭제 요청이 접수되었습니다.", "Your deletion request has been submitted."))
            else:
                messages.info(request, t(request, "이미 삭제 요청되어 관리자 처리를 기다리고 있습니다.",
                                         "A deletion request is already pending administrator review."))
    except workflow.WorkflowError as exc:
        _err(request, exc.ko, exc.en)
    return redirect(safe_next(request, request.POST.get("next"), "/upload/mine"))


@login_required
@require_POST
@atomic_write  # 요청 1건 = 커밋 1번(503 '저장되지 않았습니다' 보장, 실패 시 새 파일 정리)
def my_resubmit(request, uid):
    sub = _own_submission(request, uid)
    try:
        new = workflow.clone_for_resubmit(sub, request.user)
    except workflow.WorkflowError as exc:
        _err(request, exc.ko, exc.en)
        return redirect(safe_next(request, request.POST.get("next"), "/upload/mine"))
    messages.success(request, t(request, f"재제출용 임시저장을 만들었습니다 ({new.upload_id})",
                                f"Created a draft for resubmission ({new.upload_id})"))
    return redirect(_wizard_url(new.domain, new))

