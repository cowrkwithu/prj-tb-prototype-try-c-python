"""데이터신청 화면 (/data — 원본 pages/DataSharing/index.tsx 4탭, 탭은 URL 상태 ?tab=).

탭: datasets(데이터셋) · api(API) · dar(데이터 접근 신청) · uploads(내 업로드 관리 — 2단계 서비스·partial 재사용).
"""
from __future__ import annotations

from urllib.parse import quote, urlencode

from django.contrib import messages
from django.http import FileResponse, Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import login_required
from apps.core.i18n import get_lang, t
from apps.core.views import render_page

from . import catalog, schema_view, services
from .forms import ApiKeyForm, DarForm
from .models import DataAccessRequest

MY_DAR_LIMIT = 50


def _pick(lang, pair):
    return pair[1] if lang == "en" else pair[0]


def _tab(request) -> str:
    tab = request.GET.get("tab", "datasets")
    return tab if tab in catalog.TAB_KEYS else "datasets"


def _prefill_types(request) -> list[str]:
    raw = request.GET.getlist("types")
    vals = []
    for r in raw[:10]:
        vals += [x.strip() for x in str(r).split(",")[:10]]
    return [t_ for t_ in catalog.DATA_TYPES if t_ in vals]


def _page_context(request, tab: str, **extra) -> dict:
    lang = get_lang(request)
    states = services.dar_states(request.user)
    ctx = {
        "tab": tab,
        "tabs": [{**x, "label_text": _pick(lang, x["label"]), "help_text": _pick(lang, x["help"])} for x in catalog.TABS],
        "states": states,
    }
    if tab == "datasets":
        ctx["datasets"] = services.dataset_cards(request.user, lang, states)
        ctx["custom_datasets"] = services.custom_dataset_cards(request.user, lang, states)
    elif tab == "api":
        ctx["endpoints"] = [{**e, "description_text": _pick(lang, e["description"]),
                             "method_cls": catalog.METHOD_COLORS[e["method"]],
                             "params_list": [{"name": n, "type": ty, "required": req, "desc": _pick(lang, d)}
                                             for n, ty, req, d in e["params"]]}
                            for e in catalog.API_ENDPOINTS]
        ctx["base_url"] = request.build_absolute_uri("/api/v1")
        ctx["can_issue"] = any(s == "approved" for s in states.values())
        ctx["approved_labels"] = [_pick(lang, o["label"]) for o in catalog.DATA_TYPE_OPTIONS if states[o["id"]] == "approved"]
        ctx["active_key"] = services.active_key(request.user)
        if "key_form" not in extra:
            user = request.user
            ctx["key_form_values"] = {"email": user.email or "", "institution": user.organization or ""}
    elif tab == "dar":
        ctx["type_options"] = [{**o, "label_text": _pick(lang, o["label"]), "state": states[o["id"]]}
                               for o in catalog.DATA_TYPE_OPTIONS]
        ctx["my_requests"] = [services.dar_row(d, lang) for d in
                              DataAccessRequest.objects.filter(applicant=request.user)[:MY_DAR_LIMIT]]
        if "dar_values" not in extra:
            user = request.user
            ctx["dar_values"] = {"name": user.display_name or "", "institution": user.organization or "",
                                 "email": user.email or "", "data_types": _prefill_types(request)}
        sub_id = request.GET.get("submitted", "")
        if sub_id:
            ctx["submitted"] = DataAccessRequest.objects.filter(applicant=request.user, request_id=sub_id[:20]).first()
    elif tab == "uploads":
        from apps.uploads.views import my_uploads_context

        ctx.update(my_uploads_context(request, {"tab": "uploads"}, back="/data?tab=uploads"))
    ctx.update(extra)
    return ctx


@login_required
@require_safe
def data_sharing(request):
    tab = _tab(request)
    return render_page(request, "datasharing/index.html", _page_context(request, tab))


# ═══ 데이터 접근 신청 (DAR) ═══
@login_required
@require_POST
def dar_submit(request):
    lang = get_lang(request)
    form = DarForm(request.POST, lang=lang)
    values = {k: request.POST.get(k, "")[:6000] for k in ("name", "institution", "email", "orcid", "approver_name",
                                                         "approver_title", "approver_email", "purpose")}
    values["data_types"] = [x for x in catalog.DATA_TYPES if x in request.POST.getlist("data_types")]
    values["dua_agreed"] = bool(request.POST.get("dua_agreed"))
    if form.is_valid():
        try:
            dar = services.submit_request(request.user, form.cleaned_data)
        except services.DarError as exc:
            return render_page(request, "datasharing/index.html", _page_context(
                request, "dar", dar_values=values, dar_errors={}, dar_error=exc.text(lang)), status=400)
        return redirect(f"/data?{urlencode({'tab': 'dar', 'submitted': dar.request_id})}")
    return render_page(request, "datasharing/index.html", _page_context(
        request, "dar", dar_values=values, dar_errors=form.error_map(),
        dar_error=t(request, "입력 내용을 확인하세요", "Please check your input")), status=400)


# ═══ API 키 ═══
@login_required
@require_POST
def api_key_issue(request):
    lang = get_lang(request)
    form = ApiKeyForm(request.POST, lang=lang)
    values = {"email": request.POST.get("email", "")[:300], "institution": request.POST.get("institution", "")[:300]}
    if not form.is_valid():
        return render_page(request, "datasharing/index.html", _page_context(
            request, "api", key_form=True, key_form_values=values, key_errors={k: v[0] for k, v in form.errors.items()}),
            status=400)
    try:
        key, raw = services.issue_key(request.user, form.cleaned_data["email"], form.cleaned_data["institution"])
    except services.DarError as exc:
        return render_page(request, "datasharing/index.html", _page_context(
            request, "api", key_form=True, key_form_values=values, key_error=exc.text(lang)), status=403)
    # 원문은 저장하지 않으므로 이 응답에서 한 번만 보인다(리다이렉트 없이 렌더, 캐시 금지)
    resp = render_page(request, "datasharing/index.html", _page_context(
        request, "api", key_form=True, key_form_values=values, new_key=raw))
    resp["Cache-Control"] = "no-store"
    return resp


@login_required
@require_POST
def api_key_revoke(request):
    n = services.revoke_keys(request.user)
    if n:
        messages.success(request, t(request, "API 키를 폐기했습니다.", "The API key has been revoked."))
    return redirect("/data?tab=api")


# ═══ 데이터셋 다운로드 (DAR 승인 강제) ═══
def _denied(request, ds_label: str, dar_type: str, state: str):
    return render_page(request, "datasharing/denied.html", {
        "label": ds_label, "dar_type": dar_type, "state": state}, status=403)


@login_required
@require_safe
def dataset_download(request, slug, ext):
    ds = catalog.DATASET_BY_SLUG.get(slug)
    if ds is None or ext not in ("csv", "xlsx"):
        raise Http404
    lang = get_lang(request)
    state = services.dar_states(request.user)[ds["dar_type"]]
    label = f"{ds['title']} ({_pick(lang, ds['subtitle'])})"
    if state != "approved":
        return _denied(request, label, ds["dar_type"], state)
    filename = services.export_filename(slug, ext)
    if ext == "csv":
        resp = StreamingHttpResponse(services.stream_csv(slug), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    else:
        try:
            fh = services.build_xlsx(slug)
        except services.ExportTooLarge:
            messages.error(request, t(request, "엑셀 행 상한을 넘는 데이터셋입니다. CSV 로 내려받으세요.",
                                      "This dataset exceeds the Excel row limit. Download it as CSV."))
            return redirect("/data?tab=datasets")
        resp = FileResponse(fh, as_attachment=True, filename=filename,
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "no-store"
    return resp


def _custom_schema_or_404(request, schema_id):
    from apps.schemas import services as schema_services
    from apps.schemas.models import Schema

    schema = get_object_or_404(schema_services.visible_schemas(request.user).filter(
        status=Schema.Status.PUBLISHED).exclude(visibility=Schema.Visibility.PRIVATE), schema_id=schema_id)
    return schema


@login_required
@require_safe
def custom_template(request, schema_id):
    """원본: 커스텀 데이터셋은 DAR 승인 후 '필드 서식(CSV 헤더)'만 내려받는다."""
    from apps.schemas import services as schema_services
    from apps.schemas.catalog import pick
    from apps.schemas.views import _csv_response, _safe_filename

    schema = _custom_schema_or_404(request, schema_id)
    lang = get_lang(request)
    dtype = catalog.dar_type_of_schema_category(schema.category)
    state = services.dar_states(request.user)[dtype]
    name = pick(lang, schema.name, schema.name_en)
    if state != "approved":
        return _denied(request, name, dtype, state)
    return _csv_response(schema_services.template_csv(schema), f"{_safe_filename(name)}.csv")


# ═══ 데이터 구조 모달 (원본 DatasetSchemaModal) ═══
@login_required
@require_safe
def dataset_schema(request, slug):
    if slug not in catalog.DATASET_BY_SLUG:
        raise Http404
    return render_page(request, "datasharing/_schema_modal.html", schema_view.dictionary_context(slug, get_lang(request)))


@login_required
@require_safe
def custom_schema(request, schema_id):
    schema = _custom_schema_or_404(request, schema_id)
    return render_page(request, "datasharing/_schema_modal.html", schema_view.custom_context(schema, get_lang(request)))
