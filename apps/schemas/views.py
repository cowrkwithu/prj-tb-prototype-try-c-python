"""데이터 정의 화면 — 원본 pages/Schemas/{SchemaExplorer,SchemaCreate,SchemaDetail,SchemaData}.tsx."""
from __future__ import annotations

import json
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from apps.accounts.decorators import login_required
from apps.core.i18n import get_lang, t
from apps.core.safejson import loads_user_json

from . import catalog, services, ui, validation
from .catalog import pick
from .models import PendingEntry, Schema, SchemaFile, SchemaRecord
from .services import SchemaError

SORTS = ("newest", "usage", "data", "name")
RECORD_PAGE = 10


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _schema_or_404(request, schema_id) -> Schema:
    """볼 수 없는 데이터 구조는 존재 여부도 드러내지 않는다(404)."""
    s = services.visible_schemas(request.user).filter(schema_id=schema_id).select_related("owner").first()
    if s is None:
        raise Http404
    return s


def _err_text(request, e: SchemaError) -> str:
    return t(request, e.ko, e.en)


def _hx_target(request):
    return request.headers.get("HX-Target") if request.headers.get("HX-Request") else None


# ═══════════════════════════════════════════════════════════
#  탐색 (SchemaExplorer)
# ═══════════════════════════════════════════════════════════
class ExploreState:
    def __init__(self, g):
        self.q = (g.get("q") or "").strip()[:100]
        self.category = g.get("category") if g.get("category") in catalog.CATEGORY else ""
        self.status = g.get("status") if g.get("status") in catalog.STATUS else ""
        self.sort = g.get("sort") if g.get("sort") in SORTS else "newest"

    def url(self):
        pairs = [(k, v) for k, v in (("q", self.q), ("category", self.category), ("status", self.status)) if v]
        if self.sort != "newest":
            pairs.append(("sort", self.sort))
        return "/schemas" + ("?" + urlencode(pairs) if pairs else "")


@login_required
@require_safe
def explorer(request):
    lang = get_lang(request)
    st = ExploreState(request.GET)
    qs = services.with_counts(services.visible_schemas(request.user))
    if st.category:
        qs = qs.filter(category=st.category)
    if st.status:
        qs = qs.filter(status=st.status)
    items = services.search_schemas(qs, st.q)
    if st.sort == "newest":
        items.sort(key=lambda s: s.created_at, reverse=True)
    elif st.sort == "usage":
        items.sort(key=lambda s: s.usage_count, reverse=True)
    elif st.sort == "data":
        items.sort(key=lambda s: s.data_count, reverse=True)
    else:
        items.sort(key=lambda s: pick(lang, s.name, s.name_en).lower())
    ctx = {"state": st, "cards": [ui.schema_card(s, lang) for s in items],
           "filtered": bool(st.q or st.category or st.status),
           "categories": [(c[0], c[2] if lang == "en" else c[1]) for c in catalog.CATEGORIES]}
    if _hx_target(request) == "schema-grid":
        return render(request, "schemas/_grid.html", ctx)
    return render(request, "schemas/explorer.html", ctx)


@login_required
@require_safe
def select_modal(request):
    """업로드 허브 '커스텀 데이터' — 원본 SchemaSelectModal(발행된 구조만, 검색·카테고리)."""
    lang = get_lang(request)
    q = (request.GET.get("q") or "").strip()[:100]
    cat = request.GET.get("category") if request.GET.get("category") in catalog.CATEGORY else ""
    qs = services.with_counts(services.visible_schemas(request.user).filter(status=Schema.Status.PUBLISHED))
    if cat:
        qs = qs.filter(category=cat)
    items = [ui.schema_card(s, lang) for s in services.search_schemas(qs.order_by("name"), q)]
    tpl = "schemas/_select_list.html" if _hx_target(request) == "schema-select-list" else "schemas/_select_modal.html"
    return render(request, tpl, {"cards": items, "q": q, "category": cat,
                                 "categories": [(c[0], c[2] if lang == "en" else c[1]) for c in catalog.CATEGORIES]})


# ═══════════════════════════════════════════════════════════
#  빌더 (SchemaBuilder)
# ═══════════════════════════════════════════════════════════
def _builder_state(schema: Schema | None, lang: str) -> dict:
    if schema is None:
        return {"meta": {"name": "New Data Schema" if lang == "en" else "새 데이터 구조", "nameEn": "", "description": "",
                         "descriptionEn": "", "category": "clinical_ext", "visibility": "private", "tags": [],
                         "linkFieldName": "condition_id", "fieldGroups": [], "validationRules": []},
                "fields": [], "rev": 0, "schemaId": "", "status": "draft"}
    data = services.schema_to_json(schema)
    return {"meta": {"name": schema.name, "nameEn": schema.name_en, "description": schema.description,
                     "descriptionEn": schema.description_en, "category": schema.category, "visibility": schema.visibility,
                     "tags": schema.tags, "tagsEn": schema.tags_en, "linkFieldName": schema.link_field_name,
                     "fieldGroups": schema.field_groups, "validationRules": schema.validation_rules},
            "fields": data["fields"], "rev": schema.rev, "schemaId": schema.schema_id, "status": schema.status}


def _builder_ctx(request, schema, state, errors=None):
    lang = get_lang(request)
    return {
        "schema": schema, "state": state, "errors": [ui.issue_text(e, lang) for e in (errors or [])],
        "palette": catalog.type_palette(lang),
        "type_options": [(tp, catalog.type_label(tp, lang)) for tp in catalog.TYPE_ORDER],
        "categories": [(c[0], c[2] if lang == "en" else c[1]) for c in catalog.CATEGORIES],
        "link_fields": [(x[0], x[2] if lang == "en" else x[1]) for x in catalog.LINK_FIELDS],
        "code_systems": catalog.CODE_SYSTEMS,
        "type_meta": {tp: {"label": catalog.type_label(tp, lang), "icon": info["icon"], "color": info["color"]}
                      for tp, info in catalog.DATA_TYPES.items()},
        "with_data": sorted(services.fields_with_data(schema)) if schema else [],
        "dict_categories": _dict_categories(lang),
    }


def _dict_categories(lang):
    from apps.dictionary import viewer

    return [{"code": c.code, "name": viewer.category_text(c, lang)["name"], "count": c.n} for c in viewer.current_categories()]


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def builder(request, schema_id=None):
    lang = get_lang(request)
    schema = None
    if schema_id:
        schema = _schema_or_404(request, schema_id)
        if not services.can_manage(request.user, schema):
            messages.error(request, t(request, "작성자나 관리자만 데이터 구조를 수정할 수 있습니다",
                                      "Only the owner or an administrator can edit this data schema"))
            return redirect(f"/schemas/{schema.schema_id}")
        if schema.status == Schema.Status.ARCHIVED:
            messages.error(request, t(request, "보관된 데이터 구조는 수정할 수 없습니다. 먼저 복원하세요.",
                                      "Archived data schemas cannot be edited. Restore it first."))
            return redirect(f"/schemas/{schema.schema_id}")
    if request.method == "POST":
        action = request.POST.get("action") if request.POST.get("action") in ("draft", "save", "publish") else "save"
        try:
            payload = loads_user_json(request.POST.get("payload") or "{}")
        except (ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            messages.error(request, t(request, "요청 형식이 올바르지 않습니다(허용되지 않는 문자 포함 등)",
                                      "Malformed request (e.g. disallowed characters)"))
            return redirect(f"/schemas/{schema.schema_id}/edit" if schema else "/schemas/create")
        try:
            saved = services.save_definition(request.user, payload, schema=schema, action=action,
                                             expected_rev=_int(request.POST.get("rev"), 0) if schema else None)
        except SchemaError as e:
            state = {"meta": payload.get("meta") or {}, "fields": payload.get("fields") or [],
                     "rev": _int(request.POST.get("rev"), 0), "schemaId": schema.schema_id if schema else "",
                     "status": schema.status if schema else "draft"}
            errs = e.errors or [validation._msg(e.ko, e.en)]
            return render(request, "schemas/builder.html", _builder_ctx(request, schema, state, errs), status=400)
        msg = {"draft": ("임시저장되었습니다", "Draft saved"), "save": ("데이터 구조가 저장되었습니다", "Data schema saved"),
               "publish": ("데이터 구조가 발행되었습니다", "Data schema published")}[action]
        if (action == "publish" and saved.status != Schema.Status.PUBLISHED) or \
                (action == "draft" and saved.status != Schema.Status.DRAFT):  # 발행된 구조는 임시저장 없이 일반 저장
            msg = ("데이터 구조가 저장되었습니다", "Data schema saved")
        messages.success(request, t(request, *msg))
        return redirect(f"/schemas/{saved.schema_id}" if action == "publish" else f"/schemas/{saved.schema_id}/edit")
    return render(request, "schemas/builder.html", _builder_ctx(request, schema, _builder_state(schema, lang)))


@login_required
@require_safe
def builder_dictionary(request):
    """빌더 좌측 '데이터사전' 탭 — 카테고리를 펼치면(또는 검색하면) 항목을 불러온다(전량 1,257건을 한 번에 그리지 않음)."""
    from apps.dictionary import viewer

    lang = get_lang(request)
    q = (request.GET.get("q") or "").strip()[:100]
    cat = (request.GET.get("cat") or "").strip()[:20]
    entries = list(viewer.current_fields())
    if q:
        ql = q.lower()
        entries = [e for e in entries if ql in e.field_name.lower() or ql in e.display_name.lower()
                   or ql in (e.display_name_en or "").lower()]  # 원본 DictionaryBrowser 검색 규칙
    elif cat:
        entries = [e for e in entries if e.category.code == cat]
    else:
        entries = []
    items = []
    for e in entries[:300]:
        text = viewer.entry_text(e, lang)
        cons = dict(e.constraints or {})
        if e.options:
            cons["options"] = e.options
        payload = {"dictionaryId": e.dictionary_id, "fieldName": e.field_name, "displayName": e.display_name,
                   "displayNameEn": e.display_name_en, "description": e.description, "descriptionEn": e.description_en,
                   "dataType": e.data_type, "constraints": cons}
        items.append({"e": e, "name": text["display_name"], "description": text["description"],
                      "type_label": catalog.type_label(e.data_type, lang), "category": e.category.code,
                      "json": json.dumps(payload, ensure_ascii=False)})
    groups = {}
    for it in items:
        groups.setdefault(it["category"], []).append(it)
    return render(request, "schemas/_dict_entries.html", {"items": items, "q": q, "cat": cat,
                                                          "truncated": len(entries) > 300, "total": len(entries),
                                                          "groups": groups})


@login_required
@require_POST
def builder_preview(request):
    """폼 미리보기 — 서버가 데이터 입력 화면과 같은 템플릿으로 그린다(원본 PreviewModal)."""
    lang = get_lang(request)
    try:
        payload = loads_user_json(request.POST.get("payload") or "{}")
    except (ValueError, RecursionError):
        payload = {}
    meta, fields, _ = validation.clean_definition(payload if isinstance(payload, dict) else {}, strict=False)
    values = {}
    if request.POST.get("fill") == "1":  # 원본 fillTestData
        for f in fields:
            dt, opts = f["data_type"], f["constraints"].get("options") or []
            name = pick(lang, f["display_name"], f["display_name_en"])
            values[f["field_name"]] = {
                "TEXT": t(request, f"테스트_{f['field_name']}", f"test_{f['field_name']}"),
                "TEXTAREA": t(request, f"{f['display_name']} 샘플 텍스트입니다.", f"Sample text for {name}."),
                "INTEGER": f["constraints"].get("min", 0) if f["constraints"].get("min") is not None else 42,
                "DECIMAL": f["constraints"].get("min", 0) if f["constraints"].get("min") is not None else 12.5,
                "BOOLEAN": True, "DATE": "2026-03-01", "DATETIME": "2026-03-01T09:00",
                "SELECT": opts[0]["value"] if opts else "", "MULTISELECT": [opts[0]["value"]] if opts else [],
            }.get(dt, "")
    groups = ui.form_groups(fields, meta.get("field_groups") or [], values, {}, lang)
    return render(request, "schemas/_preview.html", {"groups": groups, "field_count": len(fields),
                                                     "schema_name": meta.get("name") or "", "values_json": values})


# ═══════════════════════════════════════════════════════════
#  가져오기 (SchemaImportModal)
# ═══════════════════════════════════════════════════════════
@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def import_schema(request):
    lang = get_lang(request)
    if request.method != "POST":
        return render(request, "schemas/_import_modal.html", {"step": "upload"})
    if request.POST.get("confirm") == "1":
        try:
            # 파싱 검사와 저장 검사가 같은 CheckContext 를 쓴다 — 정의 검사(정규식 컴파일·탐침)는 한 번만
            ctx = validation.CheckContext()
            obj = services.parse_import_text(request.POST.get("json") or "", ctx=ctx)
            schema = services.import_definition(request.user, obj, ctx=ctx)
        except SchemaError as e:
            return render(request, "schemas/_import_modal.html", {"step": "error", "error": _err_text(request, e),
                                                                  "details": [ui.issue_text(x, lang) for x in e.errors]})
        messages.success(request, t(request, "데이터 구조를 성공적으로 가져왔습니다", "Data schema imported successfully"))
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = f"/schemas/{schema.schema_id}"
        return resp
    up = request.FILES.get("file")
    if not up:
        return render(request, "schemas/_import_modal.html", {"step": "error", "error": t(request, "파일을 선택하세요", "Choose a file")})
    try:
        obj = services.parse_import(up)
    except SchemaError as e:
        return render(request, "schemas/_import_modal.html", {"step": "error", "error": _err_text(request, e),
                                                              "details": [ui.issue_text(x, lang) for x in e.errors][:20]})
    fields = [{"name": f.get("fieldName"), "display": pick(lang, f.get("displayName") or "", f.get("displayNameEn")),
               "type": catalog.type_label(f.get("dataType"), lang), "required": bool(f.get("isRequired"))}
              for f in obj["fields"]]
    return render(request, "schemas/_import_modal.html", {
        "step": "preview", "name": pick(lang, obj["name"], obj.get("nameEn")),
        "description": pick(lang, obj.get("description") or "", obj.get("descriptionEn")),
        "category": catalog.category_label(obj.get("category"), lang) if obj.get("category") else "",
        "version": obj.get("version") or "", "fields": fields, "json": json.dumps(obj, ensure_ascii=False)})


# ═══════════════════════════════════════════════════════════
#  상세 (SchemaDetail)
# ═══════════════════════════════════════════════════════════
class RecordState:
    def __init__(self, g, fields):
        names = {f.field_name for f in fields} | {"condition_id"}
        self.q = (g.get("q") or "").strip()[:100]
        self.sort = g.get("sort") if g.get("sort") in names else None
        self.dir = g.get("dir") if g.get("dir") in ("asc", "desc") and self.sort else None
        self.page = max(_int(g.get("page"), 1), 1)

    def url(self, base, **over):
        d = {"tab": "data", "q": self.q, "sort": self.sort, "dir": self.dir, "page": self.page}
        d.update(over)
        pairs = [(k, v) for k, v in d.items() if v not in (None, "") and not (k == "page" and v == 1)]
        return base + "?" + urlencode(pairs)


def _records_ctx(request, schema, fields):
    lang = get_lang(request)
    st = RecordState(request.GET, fields)
    base = f"/schemas/{schema.schema_id}"
    recs = list(schema.records.all().order_by("created_at", "id"))
    recs = services.search_records(recs, fields, st.q, lang)
    recs = services.sort_records(recs, fields, st.sort, st.dir)
    pager = Paginator(recs, RECORD_PAGE)
    page = pager.get_page(st.page)

    def next_sort(col):
        if st.sort != col:
            return st.url(base, sort=col, dir="asc", page=1)
        if st.dir == "asc":
            return st.url(base, sort=col, dir="desc", page=1)
        return st.url(base, sort=None, dir=None, page=1)

    cols = [{"key": "condition_id", "label": pick(lang, schema.link_display_name or schema.link_field_name,
                                                    schema.link_display_name_en), "sort_url": next_sort("condition_id"),
             "dir": st.dir if st.sort == "condition_id" else None}]
    cols += [{"key": f.field_name, "label": pick(lang, f.display_name, f.display_name_en), "sort_url": next_sort(f.field_name),
              "dir": st.dir if st.sort == f.field_name else None} for f in fields]
    return {"rstate": st, "columns": cols, "total": len(recs),
            "rows": [{"r": r, "cells": ui.record_cells(r, fields, lang)} for r in page.object_list],
            "page": page, "prev_url": st.url(base, page=page.number - 1) if page.has_previous() else None,
            "next_url": st.url(base, page=page.number + 1) if page.has_next() else None,
            "search_url": base}


@login_required
@require_safe
def detail(request, schema_id):
    lang = get_lang(request)
    schema = _schema_or_404(request, schema_id)
    schema = services.with_counts(Schema.objects.filter(pk=schema.pk)).select_related("owner").get()
    fields = list(schema.fields.all())
    ctx = _records_ctx(request, schema, fields)
    if _hx_target(request) == "schema-records":
        return render(request, "schemas/_records.html", {"schema": schema, **ctx})
    tab = request.GET.get("tab") if request.GET.get("tab") in ("fields", "data", "history") else "fields"
    logs = list(schema.logs.all()[:50])
    ctx.update({
        "schema": schema, "card": ui.schema_card(schema, lang), "tab": tab,
        "groups": ui.grouped_field_rows(schema, fields, lang),
        "history": [{"l": lg, "detail": pick(lang, lg.detail, lg.detail_en), "version": lg.version,
                     "date": timezone.localtime(lg.at).date().isoformat(),
                     "action": pick(lang, *services.ACTION_LABEL.get(lg.action, (lg.get_action_display(), lg.get_action_display())))}
                    for lg in logs],
        "can_manage": services.can_manage(request.user, schema),
        "can_edit": services.can_edit_structure(request.user, schema),
        "can_enter": services.can_enter_data(request.user, schema),
        "is_published": schema.status == Schema.Status.PUBLISHED,
    })
    return render(request, "schemas/detail.html", ctx)


@login_required
@require_safe
def record_detail(request, schema_id, record_id):
    lang = get_lang(request)
    schema = _schema_or_404(request, schema_id)
    rec = get_object_or_404(SchemaRecord, schema=schema, record_id=record_id)
    fields = list(schema.fields.all())
    rows = [{"label": pick(lang, f.display_name, f.display_name_en), "text": c["text"] or "-", "cell": c}
            for f, c in zip(fields, ui.record_cells(rec, fields, lang))]
    return render(request, "schemas/_record_modal.html", {
        "schema": schema, "rec": rec, "rows": rows,
        "link_label": pick(lang, schema.link_display_name or schema.link_field_name, schema.link_display_name_en),
        "can_modify": services.can_modify_record(request.user, rec)})


@login_required
@require_safe
def download_file(request, schema_id, file_id):
    schema = _schema_or_404(request, schema_id)
    sf = get_object_or_404(SchemaFile, pk=file_id, schema=schema)
    if sf.record_id is None and sf.uploaded_by_id != request.user.pk:
        raise Http404
    try:
        fh = sf.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404 from None
    resp = FileResponse(fh, as_attachment=True, filename=sf.original_name)
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


def _csv_response(text: str, filename: str) -> HttpResponse:
    resp = HttpResponse(text.encode("utf-8"), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


def _safe_filename(name: str) -> str:
    import re

    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")[:80] or "schema"


@login_required
@require_safe
def template_csv(request, schema_id):
    schema = _schema_or_404(request, schema_id)
    lang = get_lang(request)
    name = pick(lang, schema.name, schema.name_en)
    return _csv_response(services.template_csv(schema), f"template_{_safe_filename(name)}_v{schema.version}.csv")


@login_required
@require_safe
def export_records(request, schema_id):
    schema = _schema_or_404(request, schema_id)
    lang = get_lang(request)
    recs = schema.records.all().order_by("created_at", "id")
    name = pick(lang, schema.name, schema.name_en)
    return _csv_response(services.export_csv(schema, recs), f"data_{_safe_filename(name)}_v{schema.version}.csv")


@login_required
@require_safe
def export_json(request, schema_id):
    schema = _schema_or_404(request, schema_id)
    body = json.dumps(services.schema_to_json(schema), ensure_ascii=False, indent=2)
    resp = HttpResponse(body.encode("utf-8"), content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(f'schema_{_safe_filename(schema.name)}_v{schema.version}.json')}"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required
@require_POST
def duplicate(request, schema_id):
    schema = _schema_or_404(request, schema_id)
    copy = services.duplicate(schema, request.user)
    lang = get_lang(request)
    messages.success(request, t(request, f"'{schema.name}' 데이터 구조가 초안으로 복제되었습니다",
                                f"Data schema '{pick(lang, schema.name, schema.name_en)}' was duplicated as a draft"))
    return redirect(f"/schemas/{copy.schema_id}")


@login_required
@require_POST
def owner_status(request, schema_id):
    """상세 화면 '더보기' — 작성자·관리자의 보관/복원/발행."""
    schema = _schema_or_404(request, schema_id)
    if not services.can_manage(request.user, schema):
        return HttpResponseForbidden()
    action = request.POST.get("action")
    if action not in services.TRANSITIONS:
        return HttpResponse(status=400)
    try:
        services.transition(schema, action, request.user, note=(request.POST.get("note") or "")[:500],
                            by_admin=services.is_admin(request.user) and not services.is_owner(request.user, schema))
        ko, en = services.ACTION_LABEL[action]
        messages.success(request, t(request, f"{ko} 처리되었습니다", f"{en}"))
    except SchemaError as e:
        messages.error(request, _err_text(request, e))
    return redirect(f"/schemas/{schema.schema_id}")


@login_required
@require_POST
def owner_delete(request, schema_id):
    schema = _schema_or_404(request, schema_id)
    if not services.can_manage(request.user, schema):
        return HttpResponseForbidden()
    try:
        services.delete_schema(schema)
    except SchemaError as e:
        messages.error(request, _err_text(request, e))
        return redirect(f"/schemas/{schema.schema_id}")
    messages.success(request, t(request, "데이터 구조가 삭제되었습니다", "Data schema deleted"))
    return redirect("/schemas")


# ═══════════════════════════════════════════════════════════
#  데이터 입력 (SchemaData 4단계)
# ═══════════════════════════════════════════════════════════
STEPS = [("입력 방식", "Input Method"), ("데이터 입력", "Enter Data"), ("유효성 검사", "Validation"), ("제출 완료", "Submitted")]
METHOD_LABEL = {"form": ("웹 폼 입력", "Web Form Entry"), "csv": ("CSV 업로드", "CSV Upload"),
                "edit": ("기존 데이터 수정", "Edit Existing Data")}


def _steps(lang, current):
    return [{"label": en if lang == "en" else ko,
             "state": "done" if i < current else "current" if i == current else "idle"} for i, (ko, en) in enumerate(STEPS)]


def _data_base_ctx(request, schema, step):
    lang = get_lang(request)
    return {"schema": schema, "name": pick(lang, schema.name, schema.name_en), "steps": _steps(lang, step),
            "field_count": schema.fields.count(),
            "link_label": pick(lang, schema.link_display_name or schema.link_field_name, schema.link_display_name_en)}


def _form_values_for(schema, fields, *, pending=None, record=None):
    if pending is not None:
        return pending.raw.get("values", {}), pending.raw.get("link_value", "")
    if record is not None:
        return dict(record.data), record.condition_id
    return {f.field_name: f.default_value for f in fields if f.default_value is not None}, ""


def _patient_info(schema, link_value, lang):
    if schema.link_field_name != "condition_id" or not link_value:
        return None
    from apps.cases.models import Patient
    from apps.cases.services import data_label

    p = Patient.objects.filter(condition_id=link_value).first()
    if p is None:
        return {"found": False}
    return {"found": True, "p": p, "country": data_label(p.country, lang)}


@login_required
@require_safe
def data_entry(request, schema_id):
    lang = get_lang(request)
    schema = _schema_or_404(request, schema_id)
    method = request.GET.get("method") if request.GET.get("method") in METHOD_LABEL else None
    ctx = _data_base_ctx(request, schema, 1 if method else 0)
    ctx["published"] = schema.status == Schema.Status.PUBLISHED
    if not ctx["published"]:
        return render(request, "schemas/data_entry.html", {**ctx, "method": None})
    fields = list(schema.fields.all())
    editable = services.modifiable_records(request.user, schema).order_by("-updated_at")
    ctx.update({"method": method, "editable_count": editable.count()})
    pending = None
    if request.GET.get("pending"):
        pending = PendingEntry.objects.filter(pk=_int(request.GET.get("pending"), 0), user=request.user,
                                              schema=schema, consumed=False).first()
    if method == "form":
        values, link_value = _form_values_for(schema, fields, pending=pending if pending and pending.mode == "form" else None)
        errors = ui.errors_by_field(pending.errors, lang) if pending else {}
        ctx.update({"groups": ui.form_groups(fields, schema.field_groups, values, errors, lang), "link_value": link_value,
                    "link_error": errors.get("condition_id"), "patient": _patient_info(schema, link_value, lang)})
    elif method == "edit":
        rows = list(editable[:200])
        sel = None
        if request.GET.get("row"):
            sel = next((r for r in rows if r.record_id == request.GET.get("row")), None) or \
                editable.filter(record_id=request.GET.get("row")).first()
        if sel is None and rows:
            sel = rows[0]  # 원본: 첫 행을 골라 둔다
        ctx["rows"] = rows
        ctx["selected"] = sel
        if sel is not None:
            use_pending = pending if pending and pending.mode == "edit" and pending.record_id == sel.pk else None
            values, link_value = _form_values_for(schema, fields, pending=use_pending, record=None if use_pending else sel)
            errors = ui.errors_by_field(use_pending.errors, lang) if use_pending else {}
            ctx.update({"groups": ui.form_groups(fields, schema.field_groups, values, errors, lang), "link_value": link_value,
                        "link_error": errors.get("condition_id")})
    elif method == "csv":
        ctx["template_url"] = f"/schemas/{schema.schema_id}/template.csv"
    return render(request, "schemas/data_entry.html", ctx)


@login_required
@require_POST
def data_check(request, schema_id):
    schema = _schema_or_404(request, schema_id)
    if not services.can_enter_data(request.user, schema):
        messages.error(request, t(request, "발행된 데이터 구조에서만 데이터를 입력할 수 있습니다.",
                                  "Data entry is available only for published schemas."))
        return redirect(f"/schemas/{schema.schema_id}")
    mode = request.POST.get("mode")
    try:
        if mode == "csv":
            up = request.FILES.get("file")
            if not up:
                raise SchemaError("CSV 파일을 선택하세요", "Select a CSV file")
            pending = services.check_csv(schema, request.user, up)
        elif mode == "edit":
            rec = get_object_or_404(SchemaRecord, schema=schema, record_id=request.POST.get("record_id"))
            if not services.can_modify_record(request.user, rec):
                return HttpResponseForbidden()
            pending = services.check_form(schema, request.user, request.POST, request.FILES, record=rec)
        else:
            pending = services.check_form(schema, request.user, request.POST, request.FILES)
    except SchemaError as e:
        messages.error(request, _err_text(request, e))
        return redirect(f"/schemas/{schema.schema_id}/data?method={'csv' if mode == 'csv' else 'form'}")
    return redirect(f"/schemas/{schema.schema_id}/data/review/{pending.pk}")


def _pending_or_404(request, schema, pk):
    p = PendingEntry.objects.filter(pk=pk, schema=schema, user=request.user).select_related("record").first()
    if p is None:
        raise Http404
    return p


@login_required
@require_safe
def data_review(request, schema_id, pk):
    lang = get_lang(request)
    schema = _schema_or_404(request, schema_id)
    p = _pending_or_404(request, schema, pk)
    if p.consumed and p.result:
        return redirect(f"/schemas/{schema.schema_id}/data/done/{p.pk}")
    ctx = _data_base_ctx(request, schema, 2)
    back = {"form": f"?method=form&pending={p.pk}", "csv": "?method=csv",
            "edit": f"?method=edit&row={p.record.record_id if p.record else ''}&pending={p.pk}"}[p.mode]
    count = p.raw.get("count", len(p.rows)) if p.mode == "csv" else 1
    fields = list(schema.fields.all())
    ctx.update({"p": p, "count": count, "errors": [ui.issue_text(i, lang) for i in p.errors[:200]],
                "warnings": [ui.issue_text(i, lang) for i in p.warnings[:200]], "error_total": len(p.errors),
                "warning_total": len(p.warnings), "back_url": f"/schemas/{schema.schema_id}/data{back}",
                "preview_headers": p.raw.get("headers", [])[:7] if p.mode == "csv" else [],
                "preview_rows": [[row.get(h, "") for h in p.raw.get("headers", [])[:7]] for row in p.raw.get("preview", [])],
                "preview_fields": fields[:6]})
    return render(request, "schemas/data_review.html", ctx)


@login_required
@require_POST
def data_submit(request, schema_id, pk):
    schema = _schema_or_404(request, schema_id)
    p = _pending_or_404(request, schema, pk)
    try:
        services.submit_pending(p.pk, request.user)
    except SchemaError as e:
        if e.errors:
            PendingEntry.objects.filter(pk=p.pk).update(errors=e.errors)
        messages.error(request, _err_text(request, e))
        if PendingEntry.objects.filter(pk=p.pk, consumed=True).exclude(result={}).exists():
            return redirect(f"/schemas/{schema.schema_id}/data/done/{p.pk}")
        return redirect(f"/schemas/{schema.schema_id}/data/review/{p.pk}")
    messages.success(request, t(request, "데이터가 성공적으로 제출되었습니다", "Data submitted successfully"))
    return redirect(f"/schemas/{schema.schema_id}/data/done/{p.pk}")


@login_required
@require_safe
def data_done(request, schema_id, pk):
    lang = get_lang(request)
    schema = _schema_or_404(request, schema_id)
    p = _pending_or_404(request, schema, pk)
    if not (p.consumed and p.result):
        return redirect(f"/schemas/{schema.schema_id}/data/review/{p.pk}")
    ctx = _data_base_ctx(request, schema, 3)
    ctx["steps"] = [dict(s, state="done") for s in ctx["steps"]]
    ko, en = METHOD_LABEL[p.mode]
    ctx.update({"p": p, "count": p.result.get("count", 0), "method_label": en if lang == "en" else ko, "method_ko": ko,
                "method_en": en, "at": timezone.localtime(timezone.datetime.fromisoformat(p.result["at"]))
                if p.result.get("at") else None})
    return render(request, "schemas/data_done.html", ctx)


@login_required
@require_POST
def record_delete(request, schema_id, record_id):
    schema = _schema_or_404(request, schema_id)
    rec = get_object_or_404(SchemaRecord.objects.select_related("schema"), schema=schema, record_id=record_id)
    try:
        services.delete_record(rec, request.user)
        messages.success(request, t(request, "데이터 행을 삭제했습니다", "Data row deleted"))
    except SchemaError as e:
        messages.error(request, _err_text(request, e))
    return redirect(f"/schemas/{schema.schema_id}/data?method=edit")


# ═══════════════════════════════════════════════════════════
#  케이스 상세 '커스텀' 탭 (HTMX 지연 로드)
# ═══════════════════════════════════════════════════════════
@login_required
@require_safe
def case_tab(request, condition_id):
    lang = get_lang(request)
    items = services.case_custom_data(request.user, condition_id)
    out = []
    for it in items:
        s = it["schema"]
        cols = it["fields"][:5]
        out.append({"card": ui.schema_card(s, lang), "count": len(it["records"]),
                    "columns": [pick(lang, f.display_name, f.display_name_en) for f in cols],
                    "rows": [{"r": r, "cells": ui.record_cells(r, cols, lang)} for r in it["records"][:20]]})
    return render(request, "schemas/_case_tab.html", {"items": out, "condition_id": condition_id})
