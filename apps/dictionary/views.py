import json
from urllib.parse import urlencode

from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import login_required
from apps.core.i18n import get_lang

from . import viewer
from .models import DictionaryField
from .services import active_version


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class State:
    """화면 상태(쿼리스트링) — 새로고침·공유 시 같은 화면이 열린다."""

    def __init__(self, request, categories):
        g = request.GET
        codes = [c.code for c in categories]
        self.cat = g.get("cat") if g.get("cat") in codes else (codes[0] if codes else "")
        self.q = (g.get("q") or "").strip()[:100]
        seen = []
        for t in g.getlist("type"):
            if t in viewer.DATA_TYPES and t not in seen:
                seen.append(t)
        self.types = seen
        self.view = "table" if g.get("view") == "table" else "card"
        self.page = max(_int(g.get("page"), 1), 1)
        self.tq = (g.get("tq") or "").strip()[:100]
        self.sort = g.get("sort") if g.get("sort") in viewer.TABLE_COLUMNS else None
        self.dir = g.get("dir") if g.get("dir") in ("asc", "desc") and self.sort else None
        self.ps = _int(g.get("ps"), 10)

    def url(self, **over) -> str:
        d = {"cat": self.cat, "q": self.q, "type": list(self.types), "view": self.view, "page": self.page,
             "tq": self.tq, "sort": self.sort, "dir": self.dir, "ps": self.ps}
        d.update(over)
        pairs = []
        for k in ("cat", "q", "view", "page", "tq", "sort", "dir", "ps"):
            v = d[k]
            if v in (None, "") or (k == "page" and v == 1) or (k == "ps" and v == 10) or (k == "view" and v == "card"):
                continue
            pairs.append((k, v))
        pairs += [("type", t) for t in d["type"]]
        return "/dictionary?" + urlencode(pairs) if pairs else "/dictionary"


def _sort_next(state, col):
    """원본 DataTable: 다른 열 → asc, asc → desc, desc → 해제."""
    if state.sort != col:
        return state.url(sort=col, dir="asc", page=1)
    if state.dir == "asc":
        return state.url(sort=col, dir="desc", page=1)
    return state.url(sort=None, dir=None, page=1)


@login_required
@require_safe
def dictionary_viewer(request):
    lang = get_lang(request)
    categories = viewer.current_categories()
    state = State(request, categories)
    entries = viewer.filter_entries(state.cat, state.q, state.types)
    cat_meta = next((c for c in categories if c.code == state.cat), None)

    def card(e):
        info = viewer.DATA_TYPES.get(e.data_type, {})
        return {"e": e, **viewer.entry_text(e, lang), "icon": info.get("icon"), "color": info.get("color", "#94A3B8")}

    ctx = {
        "state": state,
        "categories": [{"c": c, "name": viewer.category_text(c, lang)["name"], "count": c.n,
                        "icon": viewer.CATEGORY_ICON_FILES.get(c.icon, "folder"),
                        "url": state.url(cat=c.code, q="", page=1, tq="", sort=None, dir=None),
                        "active": c.code == state.cat and not state.q} for c in categories],
        "cat_meta": viewer.category_text(cat_meta, lang) if cat_meta else None,
        "total": len(entries),
        "type_options": [{"type": t, "label": viewer.type_label(t, lang)} for t in viewer.DATA_TYPES],
        "active_types": [{"type": t, "label": viewer.type_label(t, lang),
                          "remove_url": state.url(type=[x for x in state.types if x != t], page=1)} for t in state.types],
        "clear_types_url": state.url(type=[], page=1),
        "card_url": state.url(view="card", page=1), "table_url": state.url(view="table", page=1),
        "has_version": active_version() is not None,
    }
    if state.view == "card":
        pages = max(1, -(-len(entries) // viewer.CARD_PAGE_SIZE))
        page = min(state.page, pages)
        chunk = entries[(page - 1) * viewer.CARD_PAGE_SIZE: page * viewer.CARD_PAGE_SIZE]
        present = {e.data_type for e in entries}
        ctx.update({
            "cards": [card(e) for e in chunk], "page": page, "pages": pages,
            "prev_url": state.url(page=page - 1) if page > 1 else None,
            "next_url": state.url(page=page + 1) if page < pages else None,
            "legend": [{"label": viewer.type_label(t, lang), "color": viewer.DATA_TYPES[t]["color"]}
                       for t in viewer.DATA_TYPES if t in present],
        })
    else:
        res = viewer.table_view(entries, lang, state.tq, state.sort, state.dir, state.page - 1, state.ps)
        cat_names = {c.code: viewer.category_text(c, lang)["name"] for c in categories}
        en = lang == "en"
        labels = {"category": "Category" if en else "카테고리", "fieldName": "Field Name" if en else "필드명",
                  "displayName": "Display Name" if en else "표시명", "dataType": "Type" if en else "타입",
                  "description": "Description" if en else "설명", "cdiscMapping": "CDISC"}
        ctx.update({
            "table": res,
            "table_rows": [{**r, "category_name": cat_names.get(r["category"], r["category"]),
                            "type_label": viewer.type_label(r["dataType"], lang)} for r in res.rows],
            "columns": [{"key": k, "label": labels[k], "sort_url": _sort_next(state, k),
                         "sorted": state.sort == k, "dir": state.dir if state.sort == k else None}
                        for k in viewer.TABLE_COLUMNS],
            "page": res.page + 1, "pages": res.pages,
            "prev_url": state.url(page=res.page) if res.page > 0 else None,
            "next_url": state.url(page=res.page + 2) if res.page + 1 < res.pages else None,
            "page_sizes": viewer.TABLE_PAGE_SIZES,
        })
    target = request.headers.get("HX-Target") if request.headers.get("HX-Request") else None
    if target == "dict-main":
        return render(request, "dictionary/_main_with_nav.html", ctx)
    ctx["scenarios"] = [{"id": s["id"], "prompt": s["prompt"][1 if lang == "en" else 0]} for s in viewer.SCENARIOS]
    ctx["greeting"] = viewer.GREETING[1 if lang == "en" else 0]
    ctx["help_prompt"] = viewer.HELP_PROMPT[1 if lang == "en" else 0]
    return render(request, "dictionary/viewer.html", ctx)


@login_required
@require_safe
def field_detail(request, dictionary_id):
    lang = get_lang(request)
    ver = active_version()
    e = DictionaryField.objects.select_related("category").filter(
        version=ver, dictionary_id=dictionary_id, is_deprecated=False).first() if ver else None
    if e is None:
        raise Http404
    c = e.constraints or {}
    code_system = (c.get("codeSystemEn") or c.get("codeSystem")) if lang == "en" else c.get("codeSystem")
    return render(request, "dictionary/_field_detail.html", {
        "e": e, **viewer.entry_text(e, lang),
        "category_name": viewer.category_text(e.category, lang)["name"],
        "type_label": viewer.type_label(e.data_type, lang),
        "c": c, "code_system": code_system,
        "options": [{"value": o.get("value"), "label": (o.get("labelEn") or o.get("label")) if lang == "en" else o.get("label")}
                    for o in e.options],
        "no_constraints": not c and not e.options,
    })


@login_required
@require_POST
def assistant(request):
    """AI 검색 도우미 한 턴 — 규칙 기반(외부 LLM 호출 없음). 결과가 있으면 메인 목록에도 같은 검색을 적용."""
    lang = get_lang(request)
    res = viewer.assistant_reply(text=request.POST.get("text"), scenario_id=request.POST.get("scenario"),
                                 help_request=request.POST.get("help") == "1", lang=lang)
    res["results"] = [{"e": e, "display_name": viewer.entry_text(e, lang)["display_name"],
                       "type_label": viewer.type_label(e.data_type, lang)} for e in res["results"]]
    scenarios = [{"id": s["id"], "prompt": s["prompt"][1 if lang == "en" else 0]} for s in viewer.SCENARIOS]
    resp = render(request, "dictionary/_assistant_turn.html", {"r": res, "scenarios": scenarios})
    if res["query"]:
        resp["HX-Trigger"] = json.dumps({"dictApplySearch": {"q": res["query"]}})
    return resp
