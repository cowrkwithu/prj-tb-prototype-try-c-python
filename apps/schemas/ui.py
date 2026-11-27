"""화면 표시용 가공 (템플릿에 넘길 dict). 모든 문구는 템플릿이 자동 이스케이프한다 — 여기서 mark_safe 금지."""
from __future__ import annotations

from types import SimpleNamespace

from django.utils import timezone

from apps.dictionary.viewer import DATA_TYPES

from . import catalog, validation
from .catalog import pick


def schema_card(s, lang: str) -> dict:
    tags = s.tags_en if lang == "en" and s.tags_en and len(s.tags_en) == len(s.tags) else s.tags
    return {
        "s": s, "name": pick(lang, s.name, s.name_en), "description": pick(lang, s.description, s.description_en),
        "category": catalog.category_label(s.category, lang), "category_tone": catalog.CATEGORY.get(s.category, {}).get("tone", "gray"),
        "status": catalog.status_info(s.status, lang), "visibility": s.visibility,
        "visibility_label": catalog.visibility_label(s.visibility, lang, "card"),
        "owner": pick(lang, s.owner_name, s.owner_name_en) or "-", "institution": s.owner_institution or "-",
        "created": timezone.localtime(s.created_at).date().isoformat(),
        "updated": timezone.localtime(s.updated_at).date().isoformat(),
        "published": timezone.localtime(s.published_at).date().isoformat() if s.published_at else "",
        "tags": tags, "tags_head": tags[:3], "tags_more": max(len(tags) - 3, 0),
        "field_count": getattr(s, "field_count", 0), "usage_count": getattr(s, "usage_count", 0),
        "data_count": getattr(s, "data_count", 0), "dict_field_count": getattr(s, "dict_field_count", 0),
        "link_label": pick(lang, s.link_display_name or s.link_field_name, s.link_display_name_en),
    }


def constraint_text(f, lang: str) -> str:
    """원본 SchemaDetail.FieldRow 의 제약조건 요약."""
    c = f.constraints or {}
    en = lang == "en"
    if c.get("min") is not None:
        return f"{validation._fmt_num(c['min'])}~{validation._fmt_num(c['max']) if c.get('max') is not None else ''}"
    if c.get("maxLength") is not None:
        return f"Max {c['maxLength']}" if en else f"최대 {c['maxLength']}"
    if c.get("options"):
        n = len(c["options"])
        return f"{n} options" if en else f"{n}개 선택지"
    if c.get("formula"):
        return f"Formula: {c['formula']}" if en else f"수식: {c['formula']}"
    if c.get("codeSystem"):
        return (c.get("codeSystemEn") or c["codeSystem"]) if en else c["codeSystem"]
    return ""


def field_row(f, lang: str) -> dict:
    info = DATA_TYPES.get(f.data_type, {})
    return {"f": f, "display": pick(lang, f.display_name, f.display_name_en),
            "description": pick(lang, f.description, f.description_en), "type_label": catalog.type_label(f.data_type, lang),
            "icon": info.get("icon"), "color": info.get("color", "#94A3B8"), "constraint": constraint_text(f, lang)}


def grouped_field_rows(schema, fields, lang: str) -> list[dict]:
    """원본 FieldStructureTab — 그룹 정의 순서대로, 그룹에 속하지 않은 필드는 '기타'."""
    out, used = [], set()
    for g in sorted(schema.field_groups or [], key=lambda g: g.get("order") or 0):
        rows = [field_row(f, lang) for f in fields if f.group == g.get("groupName")]
        used |= {f.pk for f in fields if f.group == g.get("groupName")}
        out.append({"name": pick(lang, g.get("displayName") or g["groupName"], g.get("displayNameEn")),
                    "description": pick(lang, g.get("description") or "", g.get("descriptionEn")), "rows": rows})
    rest = [field_row(f, lang) for f in fields if f.pk not in used]
    if rest:
        out.append({"name": "Other" if lang == "en" else "기타", "description": "", "rows": rest})
    return out


def _as_ns(f):
    if isinstance(f, dict):
        return SimpleNamespace(**f, pk=f.get("field_key"))
    return f


def form_groups(fields, group_defs, values: dict, errors: dict, lang: str) -> list[dict]:
    """원본 DynamicForm/FieldPreview — 그룹별 입력란. values 는 원시값(폼 재표시) 또는 저장값."""
    labels = {g["groupName"]: pick(lang, g.get("displayName") or g["groupName"], g.get("displayNameEn"))
              for g in (group_defs or []) if g.get("groupName")}
    groups: dict[str, list] = {}
    for f in map(_as_ns, fields):
        c = f.constraints or {}
        name = pick(lang, f.display_name, f.display_name_en)
        unit = c.get("unit") if c.get("unit") and not catalog.name_has_unit(name, c["unit"]) else ""
        v = values.get(f.field_name)
        item = {
            "f": f, "key": f"f__{f.field_name}", "label": name, "description": pick(lang, f.description, f.description_en),
            "unit": unit, "error": errors.get(f.field_name), "type": f.data_type, "value": "", "c": c,
            "required": f.is_required,
        }
        if f.data_type == "MULTISELECT":
            chosen = v if isinstance(v, list) else ([] if v in (None, "") else validation.MULTI_SEP.split(str(v)))
            item["options"] = [{"value": o["value"], "label": catalog.option_label(o, lang), "checked": o["value"] in chosen}
                               for o in c.get("options", [])]
            item["filled"] = bool(chosen)
        elif f.data_type == "SELECT":
            item["options"] = [{"value": o["value"], "label": catalog.option_label(o, lang)} for o in c.get("options", [])]
            item["value"] = "" if v is None else str(v)
        elif f.data_type == "NUMERIC_RANGE":
            v = v if isinstance(v, dict) else {}
            item["lo"] = "" if v.get("min") in (None, "") else validation._fmt_num(v["min"]) if not isinstance(v.get("min"), str) else v["min"]
            item["hi"] = "" if v.get("max") in (None, "") else validation._fmt_num(v["max"]) if not isinstance(v.get("max"), str) else v["max"]
        elif f.data_type == "BOOLEAN":
            item["value"] = "true" if v in (True, "true") else "false" if v in (False, "false") else ""
        elif f.data_type == "FILE":
            item["file"] = v if isinstance(v, dict) else None
            item["max_mb"] = c.get("maxFileSizeMB")
            item["accept"] = ",".join(c.get("allowedFileTypes") or [])
        elif f.data_type in ("INTEGER", "DECIMAL"):
            item["value"] = "" if v in (None, "") else (validation._fmt_num(v) if not isinstance(v, str) else v)
            step = 1 if f.data_type == "INTEGER" else (10 ** -c["precision"] if c.get("precision") else "any")
            item["step"] = step
            lo, hi = c.get("min"), c.get("max")
            en = lang == "en"
            if lo is not None and hi is not None:
                item["placeholder"] = f"{validation._fmt_num(lo)} ~ {validation._fmt_num(hi)}"
            elif lo is not None:
                item["placeholder"] = f"≥ {validation._fmt_num(lo)}" if en else f"{validation._fmt_num(lo)} 이상"
            elif hi is not None:
                item["placeholder"] = f"≤ {validation._fmt_num(hi)}" if en else f"{validation._fmt_num(hi)} 이하"
            else:
                item["placeholder"] = "Enter a number" if en else "숫자 입력"
        elif f.data_type == "CALCULATED":
            item["value"] = validation.display_value(f, v, lang) if v not in (None, "") else ""
        else:
            item["value"] = "" if v is None else str(v)
            if f.data_type == "CODED":
                item["code_system"] = (c.get("codeSystemEn") or c.get("codeSystem")) if lang == "en" else c.get("codeSystem")
        if "filled" not in item:
            item["filled"] = item.get("value") not in ("", None) or bool(item.get("lo") or item.get("hi") or item.get("file"))
        groups.setdefault(f.group or "_default", []).append(item)
    return [{"key": k, "label": labels.get(k, k), "is_default": k == "_default", "items": items,
             "required": sum(1 for i in items if i["required"])} for k, items in groups.items()]


def record_cells(r, fields, lang: str) -> list[dict]:
    out = []
    for f in fields:
        v = r.data.get(f.field_name)
        c = f.constraints or {}
        name = pick(lang, f.display_name, f.display_name_en)
        unit = c.get("unit") if c.get("unit") and not catalog.name_has_unit(name, c["unit"]) else ""
        text = validation.display_value(f, v, lang)
        kind = "text"
        if v is None or v == "":
            kind = "empty"
        elif f.data_type == "BOOLEAN":
            kind = "bool"
        elif f.data_type == "SELECT":
            kind = "badge"
        elif f.data_type == "MULTISELECT":
            kind = "badges"
        elif f.data_type in ("INTEGER", "DECIMAL", "CALCULATED", "NUMERIC_RANGE"):
            kind = "num"
        elif f.data_type == "FILE":
            kind = "file"
        short = text if len(text) <= 50 else text[:50] + "..."
        out.append({"kind": kind, "text": text, "short": short, "unit": unit, "value": v,
                    "badges": text.split(", ") if kind == "badges" else [], "f": f})
    return out


def issue_text(i: dict, lang: str) -> dict:
    return {**i, "text": i["en"] if lang == "en" else i["ko"]}


def errors_by_field(issues: list[dict], lang: str) -> dict:
    out = {}
    for i in issues:
        if i.get("severity") == "error" and i["field"] not in out:
            out[i["field"]] = i["en"] if lang == "en" else i["ko"]
    return out
