"""데이터셋 '데이터 구조' 모달의 표 데이터 (원본 DatasetSchemaModal.tsx — SchemaContent·formatConstraints).

정식 데이터셋은 현행 표준 데이터사전(DictionaryField)에서, 커스텀 데이터셋은 데이터 구조(SchemaField)에서 읽는다.
"""
from __future__ import annotations

from apps.dictionary import viewer
from apps.dictionary.models import DictionaryCategory
from apps.dictionary.services import active_version
from apps.schemas.catalog import option_label, pick

from . import catalog

ALL = "__all__"
OPTION_PREVIEW = 5


def _num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def format_constraints(c: dict, lang: str, full: bool) -> str:
    """원본 formatConstraints — 단위·범위·선택지·코드체계 등을 한 줄로. full 이면 선택지를 전부."""
    c = c if isinstance(c, dict) else {}
    en = lang == "en"
    parts: list[str] = []
    if c.get("unit"):
        parts.append(f"Unit: {c['unit']}" if en else f"단위: {c['unit']}")
    if c.get("min") is not None or c.get("max") is not None:
        lo = _num(c["min"]) if c.get("min") is not None else ""
        hi = _num(c["max"]) if c.get("max") is not None else ""
        parts.append(f"{lo}–{hi}")
    if c.get("minLength") is not None:
        parts.append(f"min {c['minLength']} chars" if en else f"최소 {c['minLength']}자")
    if c.get("maxLength") is not None:
        parts.append(f"max {c['maxLength']} chars" if en else f"최대 {c['maxLength']}자")
    if c.get("precision") is not None:
        parts.append(f"{c['precision']} decimal place(s)" if en else f"소수 {c['precision']}자리")
    opts = c.get("options") if isinstance(c.get("options"), list) else []
    if opts:
        shown = [option_label(o, lang) for o in (opts if full else opts[:OPTION_PREVIEW]) if isinstance(o, dict)]
        rest = len(opts) - len(shown)
        parts.append(" / ".join(shown) + ((f" (+{rest} more)" if en else f" 외 {rest}개") if rest > 0 else ""))
    if c.get("maxSelections") is not None:
        parts.append(f"up to {c['maxSelections']} selections" if en else f"최대 {c['maxSelections']}개 선택")
    code = (c.get("codeSystemEn") or c.get("codeSystem")) if en else c.get("codeSystem")
    if code:
        parts.append(str(code))
    if c.get("pattern"):
        parts.append(f"Pattern: {c['pattern']}" if en else f"패턴: {c['pattern']}")
    if c.get("minDate") or c.get("maxDate"):
        parts.append(f"{c.get('minDate') or ''} ~ {c.get('maxDate') or ''}")
    if c.get("refDictionary"):
        ref = c["refDictionary"] + (f".{c['refField']}" if c.get("refField") else "")
        parts.append(f"Ref: {ref}" if en else f"참조: {ref}")
    if c.get("formula"):
        parts.append(f"Formula: {c['formula']}" if en else f"계산식: {c['formula']}")
    if isinstance(c.get("allowedFileTypes"), list) and c["allowedFileTypes"]:
        parts.append(", ".join(str(x) for x in c["allowedFileTypes"]))
    if c.get("maxFileSizeMB") is not None:
        parts.append(f"max {c['maxFileSizeMB']} MB" if en else f"최대 {c['maxFileSizeMB']}MB")
    return " · ".join(str(p) for p in parts)


def _rows_meta(rows: list[dict], groups: list[dict]) -> dict:
    non_empty = [dict(g, count=sum(1 for r in rows if r["group"] == g["id"])) for g in groups]
    non_empty = [g for g in non_empty if g["count"]]
    sections = []
    for g in non_empty:
        sections.append({"group": g, "rows": [r for r in rows if r["group"] == g["id"]]})
    return {"rows": rows, "groups": non_empty, "show_groups": len(non_empty) > 1, "sections": sections,
            "total": len(rows)}


def dictionary_context(slug: str, lang: str) -> dict:
    ds = catalog.DATASET_BY_SLUG[slug]
    entries = list(viewer.current_fields())
    picked = catalog.dictionary_entries_for_dataset(slug, entries)
    ver = active_version()
    cats = list(DictionaryCategory.objects.filter(version=ver).order_by("order")) if ver else []
    used = {e.category.code for e in picked}
    groups = [{"id": c.code, "label": viewer.category_text(c, lang)["name"]} for c in cats if c.code in used]
    rows = []
    for e in picked:
        tx = viewer.entry_text(e, lang)
        rows.append({"id": e.dictionary_id, "code": e.dictionary_id, "name": tx["display_name"],
                     "type_label": viewer.type_label(e.data_type, lang), "link_key": False, "required": None,
                     "constraint": format_constraints(e.full_constraints, lang, False),
                     "constraint_full": format_constraints(e.full_constraints, lang, True),
                     "description": tx["description"], "group": e.category.code})
    mapped = catalog.DATASET_DICTIONARY_MAP[slug]["categories"]
    by_code = {c.code: c for c in cats}
    included = [viewer.category_text(by_code[c], lang)["name"] if c in by_code else c for c in mapped]
    return {"kind": "dictionary", "title": ds["title"], "subtitle": pick(lang, *ds["subtitle"]),
            "description": pick(lang, *ds["description"]), "version": ver.version if ver else "",
            "included": included, "field_count": len(rows), **_rows_meta(rows, groups)}


def custom_context(schema, lang: str) -> dict:
    fields = list(schema.fields.order_by("order", "id"))
    gdefs = sorted([g for g in (schema.field_groups or []) if isinstance(g, dict) and g.get("groupName")],
                   key=lambda g: g.get("order") or 0)
    groups = [{"id": "", "label": "Link key / Ungrouped" if lang == "en" else "연결 키·미분류"}]
    groups += [{"id": g["groupName"], "label": pick(lang, g.get("displayName") or g["groupName"], g.get("displayNameEn"))}
               for g in gdefs]
    known = {g["id"] for g in groups}
    rows = [{"id": f"link:{schema.link_field_name}", "code": schema.link_field_name,
             "name": pick(lang, schema.link_display_name or schema.link_field_name, schema.link_display_name_en),
             "type_label": "", "link_key": True, "required": True, "constraint": "", "constraint_full": "",
             "description": pick(lang, schema.link_description, schema.link_description_en), "group": ""}]
    for f in fields:
        rows.append({"id": f.field_key, "code": f.field_name, "name": pick(lang, f.display_name, f.display_name_en),
                     "type_label": viewer.type_label(f.data_type, lang), "link_key": False, "required": f.is_required,
                     "constraint": format_constraints(f.constraints, lang, False),
                     "constraint_full": format_constraints(f.constraints, lang, True),
                     "description": pick(lang, f.description, f.description_en),
                     "group": f.group if f.group in known else ""})
    return {"kind": "custom", "title": pick(lang, schema.name, schema.name_en), "subtitle": "",
            "description": pick(lang, schema.description, schema.description_en), "version": schema.version,
            "institution": schema.owner_institution or "-", "field_count": len(fields), **_rows_meta(rows, groups)}
