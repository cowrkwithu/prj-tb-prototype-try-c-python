"""데이터 탐색(DEPOT) — 원본 pages/Depot/index.tsx.

상태는 모두 URL 쿼리에 둔다(새로고침·공유 시 같은 화면):
  빌더 조건(country, age_min, …, logic) 또는 고급 조건 트리(tree=JSON), run=1(데이터 생성), tab, panel=saved, loaded=<id>
조건이 바뀌면 HTMX 가 #depot-result 만 다시 그리고 주소를 갱신한다.
"""
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import login_required
from apps.accounts.utils import safe_next
from apps.cases.services import data_label
from apps.core.i18n import get_lang, t

from . import conditions as cond
from . import services
from .models import Cohort

PREVIEW_LIMIT = 10
CHART_KEYS = ("sex", "country", "diag", "outcome", "lineage", "findings")


MAX_URL_CHARS = 8000  # 이보다 긴 주소는 기록하지 않는다(조건이 길면 저장 데이터셋 번호로 공유)


def _qs(pairs) -> str:
    return urlencode(pairs, quote_via=quote)


def _context(request):
    lang = get_lang(request)
    g = request.GET
    st = services.resolve_conditions(g, lang)
    filters, tree, error, params = st["filters"], st["tree"], st["error"], st["params"]
    run = g.get("run") == "1" or ("mutations" in g and "run" not in g and filters and filters["mutations"])
    tab = g.get("tab") if g.get("tab") in services.TAB_KEYS else "summary"

    qs = cond.matching_patients(tree)
    rows = None
    if run:
        rows = services.fetch_rows(qs)
        count = len(rows)
    else:
        count = qs.count()
    preview = [] if run else list(qs.values("id", "condition_id")[:PREVIEW_LIMIT])

    loaded = None
    if st["cohort"] is not None:  # 조건이 실제로 저장 데이터셋에서 온 경우에만 안내
        c = st["cohort"]
        loaded = {"id": c.pk, "name": services.localized(c, lang)["name"], "snapshot": c.snapshot_count}

    state_q = _qs(params)
    analysis = services.analyze(rows, tab, lang) if run and rows else None
    active = (cond.active_conditions(filters, lang) if filters is not None
              else [x for x in [cond.describe_tree(tree, lang)] if x])

    mutation_opts = services.mutation_options()
    if filters:
        mutation_opts += [m for m in filters["mutations"] if m not in mutation_opts]
    # 주소로 받은 값이 화면 선택지에 없으면 칩을 덧붙여 보인다(다음 조건 변경 때 빠지지 않게)
    def _opts(base, key):
        extra = [v for v in (filters or {}).get(key, []) if v not in base]
        return [{"value": c, "label": data_label(c, lang)} for c in list(base) + extra]
    lineage_opts = list(cond.LINEAGE_OPTIONS)
    if filters and filters["lineage"] and filters["lineage"] not in lineage_opts:
        lineage_opts.append(filters["lineage"])

    return {
        "lang": lang,
        "filters": filters, "tree_mode": filters is None, "tree": tree, "source": st["source"],
        "tree_text": cond.describe_tree(tree, lang) if filters is None else "",
        "tree_raw": params[0][1] if st["source"] == "tree" else "",
        "cohort_source_id": st["cohort"].pk if st["cohort"] else None,
        "can_save": error is None,
        "error": error,
        "run": bool(run), "tab": tab, "count": count, "preview": preview, "preview_more": max(0, count - len(preview)),
        "loaded": loaded, "panel_saved": g.get("panel") == "saved",
        "state_pairs": params, "state_q": state_q, "state_q_amp": ("&" + state_q) if state_q else "",
        "active": active, "logic": tree["op"],
        "analysis": analysis,
        "chart_payload": {"tab": tab, **{k: v for k, v in (analysis or {}).items() if k in CHART_KEYS}},
        "tabs": [{"key": k, "label": en if lang == "en" else ko, "help": hen if lang == "en" else hko}
                 for k, ko, en, hko, hen in services.TABS],
        "today": timezone.localdate(),
        "countries": _opts(cond.COUNTRY_OPTIONS, "countries"),
        "diag_options": cond.DIAG_OPTIONS,
        "outcome_options": cond.OUTCOME_OPTIONS + [v for v in (filters or {}).get("outcomes", [])
                                                   if v not in cond.OUTCOME_OPTIONS],
        "comorb_options": _opts(cond.COMORB_OPTIONS, "comorbidities"),
        "finding_options": _opts(cond.FINDING_OPTIONS, "cxrFindings"),
        "lineage_options": lineage_opts, "mutation_options": mutation_opts,
        "tri_seq": _tri_opts(filters, "hasSequence", lang),
        "tri_mut": _tri_opts(filters, "hasMutation", lang),
        "tri_cxr": _tri_opts(filters, "hasCxr", lang),
        "tri_ct": _tri_opts(filters, "hasCt", lang),
        "tri_pet": _tri_opts(filters, "hasPetCt", lang),
    }


def _tri_opts(filters, key, lang):
    cur = None if filters is None else filters[key]
    return [{"value": "", "label": "All" if lang == "en" else "전체", "on": cur is None},
            {"value": "1", "label": "Yes", "on": cur is True},
            {"value": "0", "label": "No", "on": cur is False}]


def clean_url(ctx) -> str:
    """현재 화면 상태의 정규화된 주소 — 기본값·빈 값 파라미터는 생략."""
    pairs = [p for p in ctx["state_pairs"]]
    if ctx["run"]:
        pairs.append(("run", "1"))
    if ctx["tab"] != "summary":
        pairs.append(("tab", ctx["tab"]))
    if ctx["panel_saved"]:
        pairs.append(("panel", "saved"))
    return "/depot" + ("?" + _qs(pairs) if pairs else "")


@login_required
@require_safe
def depot(request):
    ctx = _context(request)
    ctx["url_too_long"] = len(clean_url(ctx)) > MAX_URL_CHARS
    status = 400 if ctx["error"] else 200
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "depot-result":
        # htmx 는 4xx 응답을 화면에 넣지 않으므로 부분 응답은 200 + 오류 문구로 보낸다(전체 페이지는 400)
        resp = render(request, "cohorts/_result.html", {**ctx, "oob": True})
        # 빈 파라미터(run=&seq=…)를 뺀 주소로 기록. 너무 길면 기록하지 않는다(화면에 안내).
        resp["HX-Push-Url"] = "false" if ctx["url_too_long"] else clean_url(ctx)
        return resp
    ctx["saved"] = services.saved_cards(request.user, ctx["lang"])
    return render(request, "cohorts/depot.html", ctx, status=status)


def _back(request, default="/depot?panel=saved"):
    return safe_next(request, request.POST.get("next"), default)


@login_required
@require_POST
def cohort_save(request):
    """현재 조건(빌더 상태 쿼리)을 저장 데이터셋으로 — 원본 handleSaveDataset.

    저장·응답 생성을 한 트랜잭션에서 처리한다. 리다이렉트는 조건 원문 없이 짧은 주소(loaded=<id>)로 —
    조건은 서버가 저장된 데이터셋에서 복원하므로 조건 길이와 무관하게 응답이 만들어진다.
    """
    lang = get_lang(request)
    raw = request.POST.get("query", "")
    if len(raw) > cond.MAX_JSON_CHARS * 12:  # 퍼센트 인코딩(한글 1자 = 9자)을 감안한 상한 — 잘라 쓰지 않고 거부
        return HttpResponseBadRequest(t(request, "조건이 너무 깁니다.", "Conditions are too long."))
    st = services.resolve_conditions(QueryDict(raw), lang)
    if st["error"]:
        # 저장하지 않는다 — 조건 없는 전체 데이터셋이 실수로 저장되지 않게
        return HttpResponseBadRequest(st["error"])
    tab = request.POST.get("tab") if request.POST.get("tab") in services.TAB_KEYS else "summary"
    with transaction.atomic():
        c = services.save_cohort(request.user, st["tree"], st["filters"], request.POST.get("name", ""))
        resp = redirect("/depot?" + _qs([("loaded", str(c.pk)), ("run", "1"), ("tab", tab), ("panel", "saved")]))
    name = services.localized(c, lang)["name"]
    messages.success(request, t(request, f'"{name}" 저장 완료 ({c.snapshot_count}명)',
                                f'"{name}" saved ({c.snapshot_count} subjects)'))
    return resp


def _owned(request, pk):
    c = get_object_or_404(Cohort, pk=pk)
    if not services.can_edit(request.user, c):
        return c, HttpResponseForbidden(t(request, "본인이 저장한 데이터셋만 변경할 수 있습니다.",
                                          "You can only change datasets you saved."))
    return c, None


@login_required
@require_POST
def cohort_rename(request, pk):
    c, denied = _owned(request, pk)
    if denied:
        return denied
    try:
        services.rename_cohort(c, request.POST.get("name", ""), request.POST.get("name_en", ""))
        messages.success(request, t(request, "데이터셋 이름을 변경했습니다.", "Dataset renamed."))
    except ValueError:
        messages.error(request, t(request, "이름을 입력하세요.", "Enter a name."))
    return redirect(_back(request))


@login_required
@require_POST
def cohort_delete(request, pk):
    c, denied = _owned(request, pk)
    if denied:
        return denied
    name = services.localized(c, get_lang(request))["name"]
    c.delete()
    messages.success(request, t(request, f'"{name}" 데이터셋을 삭제했습니다.', f'Deleted dataset "{name}".'))
    return redirect(_back(request))


@login_required
@require_safe
def cohort_export(request, pk):
    c, denied = _owned(request, pk)
    if denied:
        return denied
    tree = services.stored_tree(c)
    if tree is None:
        return HttpResponseBadRequest(t(request, "저장된 조건이 손상되어 내보낼 수 없습니다.",
                                        "The saved conditions are damaged and cannot be exported."))
    resp = HttpResponse(services.export_csv(c, tree), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="dataset_{c.pk}.csv"'
    return resp
