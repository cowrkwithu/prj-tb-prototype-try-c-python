"""유전자 분석(GAP) 화면 — 원본 src/pages/Gap/*.

/gap                  유전체 브라우저(유전자 트랙 + 변이 표, 변이 상세 = 보유 대상자)
/gap/variants/<id>    변이 상세(HTMX 모달 본문)
/gap/search           변이 검색(필터·페이지), /gap/search/export.csv(DAR 유전체 승인), /gap/search/save(POST)
/gap/resistance       약제내성 예측(POST 실행 → PredictionRun), 이력(본인 / 관리자 전체)
/gap/gwas             GWAS(변이 × DST 표현형, Fisher)
/gap/similarity       유사도(선택 대상자 거리 행렬)
"""
from __future__ import annotations

import csv
import io
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from apps.accounts.decorators import login_required
from apps.cases.models import Patient
from apps.cohorts import conditions as cond
from apps.cohorts import services as cohort_services
from apps.cohorts.models import Cohort
from apps.core.i18n import get_lang, t
from apps.core.views import render_page
from apps.datasharing import services as ds_services
from apps.uploads.services.csvio import safe_cell

from . import analysis, prediction, services
from .forms import PredictionForm
from .models import PredictionRun, Variant

SEARCH_PER_PAGE = 20
RUNS_PER_PAGE = 10
CARRIERS_SHOWN = 200
CSV_COLUMNS = ["유전자", "유전체 위치", "참조 염기", "대립 염기", "아미노산 변이 (Codon No.)", "아미노산 변이 (E. coli)",
               "연관 약제", "검출 빈도(%)", "샘플 수", "주석"]
CSV_COLUMNS_EN = ["Gene", "Genomic Position", "Ref Allele", "Alt Allele", "AA Change (Codon No.)",
                  "AA Change (E. coli)", "Associated Drug", "Frequency (%)", "Samples", "Annotation"]


def _pick(lang, pair):
    return pair[1] if lang == "en" else pair[0]


def _uint(v, default=None, hi=2_147_483_647):
    n = cond.parse_uint(v) if isinstance(v, str) else None
    return n if n is not None and n <= hi else default


# ═══ 유전체 브라우저 ═══
@login_required
@require_safe
def browser(request):
    lang = get_lang(request)
    cat = services.load_catalog()
    st = services.variant_stats(cat)
    q = (request.GET.get("q") or "")[:100]
    gene_name = (request.GET.get("gene") or "").strip()
    gene = next((g for g in cat.genes if g.gene.lower() == gene_name.lower()), None) if gene_name else None
    variants = services.browser_filter(cat, q)
    if gene is not None:
        variants = [v for v in variants if v.gene_id == gene.pk]
    ctx = {
        "q": q, "gene": gene, "genes": cat.genes,
        "rows": [services.variant_row(v, st) for v in variants],
        "variant_total": len(cat.variants), "gene_total": len(cat.genes),
        "denominator": st.denominator,
        "uncatalogued": sorted(st.uncatalogued.items(), key=lambda kv: (-kv[1], kv[0]))[:20],
        "uncatalogued_total": len(st.uncatalogued),
        "track": services.genome_track(cat, st),
        "zoom": services.gene_track(cat, st, gene) if gene is not None else None,
    }
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "gap-variant-table":
        return render_page(request, "genomics/_variant_table.html", ctx)
    return render_page(request, "genomics/browser.html", ctx, sidebar="gap")


@login_required
@require_safe
def variant_detail(request, pk):
    v = Variant.objects.select_related("gene").filter(pk=int(pk)).first() if len(pk) <= 10 else None
    if v is None:
        raise Http404
    cat = services.load_catalog()
    st = services.variant_stats(cat)
    v = next((x for x in cat.variants if x.pk == v.pk), v)
    ids, raw = services.carriers(cat, [v])
    pats = list(Patient.objects.filter(pk__in=ids[:CARRIERS_SHOWN]).order_by("id")) if ids else []
    labels = list(dict.fromkeys([v.label] + raw))
    return render_page(request, "genomics/_variant_detail.html", {
        "v": services.variant_row(v, st), "gene": v.gene, "carriers": pats, "carrier_total": len(ids),
        "depot_url": cohort_services.depot_url_for_mutations(labels) if ids else "",
        "denominator": st.denominator,
    })


# ═══ 변이 검색 ═══
def _search_context(request):
    cat = services.load_catalog()
    st = services.variant_stats(cat)
    p = services.parse_search(request.GET, cat)
    searched = request.GET.get("searched") == "1"
    results = services.search_variants(cat, st, p) if searched and not p.errors else []
    return cat, st, p, searched, results


@login_required
@require_safe
def search(request):
    lang = get_lang(request)
    cat, st, p, searched, results = _search_context(request)
    page = Paginator(results, SEARCH_PER_PAGE).get_page(request.GET.get("page"))
    qs = urlencode(p.pairs() + [("searched", "1")], quote_via=quote)
    return render_page(request, "genomics/search.html", {
        "p": p, "searched": searched, "errors": [_pick(lang, e) for e in p.errors],
        "genes": cat.genes, "drugs": services.drug_options(cat),
        "page": page, "rows": [services.variant_row(v, st) for v in page.object_list], "total": len(results),
        "query": qs, "genomics_approved": ds_services.is_approved(request.user, "genomics"),
        "denominator": st.denominator,
    }, sidebar="gap", status=400 if p.errors else 200)


@login_required
@require_safe
def search_export(request):
    """검색 결과 CSV — 3c DAR 게이트 재사용(유전체 승인 필요). BOM 포함, 수식 주입 방지(safe_cell)."""
    lang = get_lang(request)
    state = ds_services.dar_states(request.user)["genomics"]
    if state != "approved":
        return render_page(request, "datasharing/denied.html", {
            "label": t(request, "변이 검색 결과 (유전체)", "Variant search results (genomics)"),
            "dar_type": "genomics", "state": state}, status=403)
    cat, st, p, _, _ = _search_context(request)
    if p.errors:
        return HttpResponse("\n".join(_pick(lang, e) for e in p.errors), status=400,
                            content_type="text/plain; charset=utf-8")
    results = services.search_variants(cat, st, p)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS_EN if lang == "en" else CSV_COLUMNS)
    for v in results:
        r = services.variant_row(v, st)
        w.writerow([safe_cell(str(x)) for x in (r["gene"], r["position"], r["ref"], r["alt"], r["aa"] or "",
                                                 r["ecoli"] or "", ";".join(r["drugs"]), f"{r['frequency'] * 100:.1f}",
                                                 r["sample_count"], r["annotation"])])
    resp = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="variant_search_{timezone.localdate():%Y-%m-%d}.csv"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "no-store"
    return resp


def _describe(p: services.SearchParams, en: bool) -> str:
    parts = []
    if p.gene:
        parts.append(f"Gene {p.gene}" if en else f"유전자 {p.gene}")
    if p.drug:
        parts.append(f"Drug {p.drug}" if en else f"약제 {p.drug}")
    if p.aa:
        parts.append(f"AA change '{p.aa}'" if en else f"아미노산 변이 '{p.aa}'")
    if p.pos_min is not None or p.pos_max is not None:
        rng = f"{p.pos_min if p.pos_min is not None else ''}–{p.pos_max if p.pos_max is not None else ''}"
        parts.append(f"Position {rng}" if en else f"위치 {rng}")
    if p.min_freq:
        parts.append(f"Min. frequency {p.min_freq:g}%" if en else f"최소 빈도 {p.min_freq:g}%")
    return " · ".join(parts) if parts else ("All" if en else "전체")


@login_required
@require_POST
def search_save(request):
    """원본 handleSaveCohort — 검색된 변이(하나라도) 보유 대상자를 저장 데이터셋으로 만들고 데이터 탐색으로 이동."""
    cat = services.load_catalog()
    st = services.variant_stats(cat)
    p = services.parse_search(request.POST, cat)
    back = "/gap/search?" + urlencode(p.pairs() + [("searched", "1")], quote_via=quote)
    if p.errors:
        messages.error(request, _pick(get_lang(request), p.errors[0]))
        return redirect(back)
    results = services.search_variants(cat, st, p)
    ids, raw = services.carriers(cat, results)
    if not results or not ids:
        messages.info(request, t(request, "검색된 변이를 보유한 대상자가 없어 데이터셋을 만들 수 없습니다",
                                 "Cannot create a dataset: no subjects carry the found variants"))
        return redirect(back)
    labels = list(dict.fromkeys([v.label for v in results] + raw))
    if len(labels) > cond.MAX_LIST:
        messages.error(request, t(request, f"변이가 너무 많습니다(최대 {cond.MAX_LIST}개). 조건을 좁혀 주세요.",
                                  f"Too many variants (max {cond.MAX_LIST}). Narrow the conditions."))
        return redirect(back)
    try:
        with transaction.atomic():
            c = cohort_services.save_mutation_cohort(request.user, labels)
            c.description = f"변이 검색 결과({len(results)}건)로 생성된 데이터셋. 조건: {_describe(p, False)}"[:2000]
            c.description_en = (f"Dataset created from Variant Search results ({len(results)} variants). "
                                f"Conditions: {_describe(p, True)}")[:2000]
            c.save(update_fields=["description", "description_en", "updated_at"])
    except cond.ConditionError as e:
        messages.error(request, e.text(get_lang(request)))
        return redirect(back)
    name = cohort_services.localized(c, get_lang(request))["name"]
    messages.success(request, t(request, f'"{name}" 저장 완료 ({c.snapshot_count}명)',
                                f'"{name}" saved ({c.snapshot_count} subjects)'))
    return redirect(f"/depot?loaded={c.pk}&run=1&panel=saved")


# ═══ 약제내성 예측 ═══
FORM_ERRORS = {
    ("patient", "required"): ("대상자를 고르세요", "Select a subject"),
    ("file", "required"): ("변이 파일을 올리거나 내용을 붙여 넣으세요", "Upload a variant file or paste its content"),
    ("file", "ext"): ("지원 형식: .vcf, .csv, .tsv, .txt", "Supported formats: .vcf, .csv, .tsv, .txt"),
    ("file", "size"): (f"파일이 너무 큽니다(최대 {prediction.MAX_BYTES // 1024}KB)",
                       f"File too large (max {prediction.MAX_BYTES // 1024} KB)"),
    ("algorithms", "required"): ("알고리즘을 하나 이상 고르세요", "Select at least one algorithm"),
    ("text", "max_length"): (f"입력이 너무 큽니다(최대 {prediction.MAX_BYTES // 1024}KB)",
                             f"Input too large (max {prediction.MAX_BYTES // 1024} KB)"),
}


def _form_error(form, lang) -> str:
    for fname, errs in form.errors.as_data().items():
        for e in errs:
            pair = FORM_ERRORS.get((fname, e.code))
            if pair:
                return _pick(lang, pair)
    return t_lang(lang, "입력 값을 확인하세요", "Check the input values")


def t_lang(lang, ko, en):
    return en if lang == "en" else ko


def _run_view(run: PredictionRun, lang: str) -> dict:
    drugs = run.results.get("drugs", []) if isinstance(run.results, dict) else []
    algos = [prediction.ALGO_BY_ID[a] for a in run.algorithms if a in prediction.ALGO_BY_ID]
    rows = []
    for d in drugs:
        calls = d.get("calls", {})
        cells = [{"algo": a["id"], **calls.get(a["id"], {"call": "U"})} for a in algos]
        evidence = list(dict.fromkeys(e for c in cells for e in c.get("evidence", [])))
        rows.append({"drug": d.get("drug"), "cells": cells, "consensus": d.get("consensus", "U"),
                     "evidence": evidence, "genes": d.get("genes", [])})
    inputs = run.inputs if isinstance(run.inputs, dict) else {}
    observed = inputs.get("observed", [])
    return {"run": run, "algos": [{"id": a["id"], "name": a["name"]} for a in algos], "rows": rows,
            "observed": observed,
            "n_catalog": sum(1 for o in observed if o.get("status") == "catalog"),
            "n_unchar": sum(1 for o in observed if o.get("status") == "uncharacterized"),
            "n_outside": sum(1 for o in observed if o.get("status") == "outside"),
            "skipped": sum(v for v in (inputs.get("skipped") or {}).values() if isinstance(v, int)),
            "tested_genes": inputs.get("tested_genes", []), "rows_in": inputs.get("rows", 0)}


def _resistance_context(request, form=None, error=""):
    lang = get_lang(request)
    runs = prediction.visible_runs(request.user)
    run = None
    rid = _uint(request.GET.get("run"))
    if rid:
        run = runs.filter(pk=rid).first()
    page = Paginator(runs, RUNS_PER_PAGE).get_page(request.GET.get("page"))
    data = form.data if form is not None else {}
    return {
        "algorithms": [{**a, "desc": _pick(lang, a["desc"]), "rule": _pick(lang, a["rule"])}
                       for a in prediction.ALGORITHMS],
        "selected_algos": data.getlist("algorithms") if hasattr(data, "getlist") else ["tb-profiler"],
        "source": "upload" if data and data.get("source") == "upload" else "patient",
        "assume_wgs": (data.get("assume_wgs") == "on" or data.get("assume_wgs") == "1") if data else True,
        "text": (data.get("text") or "")[:20000] if data else "",
        "patient_sel": _uint(data.get("patient")) if data else None,
        "patients": list(prediction.eligible_patients()
                         .only("id", "condition_id", "diagnosis_type", "lineage", "has_sequence")[:2000]),
        "result": _run_view(run, lang) if run else None,
        "run_missing": bool(rid and not run),
        "history": page, "is_admin_view": getattr(request.user, "is_portal_admin", False),
        "error": error, "max_kb": prediction.MAX_BYTES // 1024, "max_rows": prediction.MAX_ROWS,
    }


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def resistance(request):
    lang = get_lang(request)
    if request.method != "POST":
        ctx = _resistance_context(request)
        return render_page(request, "genomics/resistance.html", ctx, sidebar="gap",
                           status=404 if ctx["run_missing"] else 200)
    if getattr(request, "upload_aborted", False) or _aborted(request):
        return render_page(request, "genomics/resistance.html", _resistance_context(
            request, None, t(request, f"파일이 너무 큽니다(최대 {prediction.MAX_BYTES // 1024}KB)",
                             f"File too large (max {prediction.MAX_BYTES // 1024} KB)")), sidebar="gap", status=400)
    form = PredictionForm(request.POST, request.FILES)
    if not form.is_valid():
        return render_page(request, "genomics/resistance.html",
                           _resistance_context(request, form, _form_error(form, lang)), sidebar="gap", status=400)
    d = form.cleaned_data
    try:
        if d["source"] == "patient":
            patient = prediction.eligible_patients().filter(pk=d["patient"]).first()
            if patient is None:
                raise prediction.InputError("유전체 데이터가 있는 대상자가 아닙니다",
                                            "This subject has no genomic data")
            run = prediction.run_prediction(request.user, algorithms=d["algorithms"], patient=patient)
        else:
            f = d.get("file")
            if f is not None:
                raw = f.read(prediction.MAX_BYTES + 1)
                text, name = prediction.decode_upload(raw), f.name
            else:
                text, name = d.get("text") or "", ""
            run = prediction.run_prediction(request.user, algorithms=d["algorithms"], text=text, filename=name,
                                            assume_wgs=bool(d.get("assume_wgs")))
    except prediction.InputError as e:
        return render_page(request, "genomics/resistance.html",
                           _resistance_context(request, form, e.text(lang)), sidebar="gap", status=400)
    messages.success(request, t(request, "약제내성 예측을 실행했습니다", "Drug resistance prediction completed"))
    return redirect(f"/gap/resistance?run={run.pk}#result")


def _aborted(request) -> bool:
    try:
        request.FILES  # noqa: B018 — 본문 파싱 유도(업로드 핸들러 중단 여부)
    except Exception:
        return True
    return bool(getattr(request, "upload_aborted", False))


# ═══ GWAS ═══
def _cohort_options(lang):
    return [{"id": c.pk, "name": cohort_services.localized(c, lang)["name"], "n": c.snapshot_count}
            for c in cohort_services.visible_cohorts()[:500]]


@login_required
@require_safe
def gwas(request):
    lang = get_lang(request)
    drugs = analysis.dst_drug_options()
    codes = [d["code"] for d in drugs]
    drug = request.GET.get("drug") or ""
    if drug not in codes:
        drug = "RIF" if "RIF" in codes else (codes[0] if codes else "")
    cohort_id = _uint(request.GET.get("cohort"))
    cohort = Cohort.objects.filter(pk=cohort_id).first() if cohort_id else None
    run = request.GET.get("run") == "1"
    result = None
    if run and drug:
        scope = {i for i in (cohort.snapshot_ids or []) if isinstance(i, int)} if cohort else None
        result = analysis.gwas(drug, scope)
    chart = None
    if result and result["points"]:
        pts = [r for r in result["points"] if r["position"] is not None]
        chart = {
            "points": [{"x": round(r["position"] / 1e6, 6), "y": round(r["neglog"], 4), "label": r["label"],
                        "gene": r["gene"], "pos": r["position"], "approx": r["approx"], "p": r["p"],
                        "sig": r["significant"]} for r in pts],
            "threshold": result["neglog_threshold"], "xmax": round(services.GENOME_LENGTH / 1e6, 3),
        }
    return render_page(request, "genomics/gwas.html", {
        "drugs": drugs, "drug": drug, "cohorts": _cohort_options(lang), "cohort": cohort,
        "cohort_name": cohort_services.localized(cohort, lang)["name"] if cohort else "",
        "cohort_missing": bool(cohort_id and not cohort), "run": run, "result": result,
        "table": result["points"][:50] if result else [], "chart": chart,
        "min_group": analysis.MIN_GROUP, "max_tests": analysis.MAX_TESTS,
    }, sidebar="gap")


# ═══ 유사도 ═══
DEFAULT_SIMILARITY = 12


@login_required
@require_safe
def similarity(request):
    lang = get_lang(request)
    method = request.GET.get("method") or "snp"
    if method not in analysis.METHOD_IDS:
        method = "snp"
    cands = list(analysis.similarity_candidates()
                 .only("id", "condition_id", "lineage", "diagnosis_type", "resistance_mutations", "has_sequence")[:5000])
    by_id = {p.pk: p for p in cands}
    notes, error = [], ""
    cohort_id = _uint(request.GET.get("cohort"))
    cohort = Cohort.objects.filter(pk=cohort_id).first() if cohort_id else None
    if cohort_id and cohort is None:
        error = t(request, "데이터셋을 찾을 수 없습니다", "Dataset not found")
    raw_ids = request.GET.getlist("p")[:1000]
    if cohort is not None:
        ids = [i for i in (cohort.snapshot_ids or []) if isinstance(i, int) and i in by_id]
        if len(ids) > analysis.MAX_SIMILARITY:
            notes.append(t(request, f"데이터셋의 유전체 보유 대상자 {len(ids)}명 중 앞 {analysis.MAX_SIMILARITY}명만 불러왔습니다",
                           f"Loaded the first {analysis.MAX_SIMILARITY} of {len(ids)} subjects with genomic data"))
            ids = ids[:analysis.MAX_SIMILARITY]
        skipped = len([i for i in (cohort.snapshot_ids or []) if i not in by_id])
        if skipped:
            notes.append(t(request, f"유전체 데이터가 없는 대상자 {skipped}명은 제외했습니다",
                           f"Excluded {skipped} subjects without genomic data"))
    elif raw_ids:
        ids = []
        for r in raw_ids:
            n = _uint(r)
            if n in by_id and n not in ids:
                ids.append(n)
    else:
        ids = [p.pk for p in cands if p.has_sequence][:DEFAULT_SIMILARITY]
    run = request.GET.get("run") == "1"
    result = None
    if run and not error:
        if len(ids) > analysis.MAX_SIMILARITY:
            error = t(request, f"한 번에 최대 {analysis.MAX_SIMILARITY}명까지 비교할 수 있습니다",
                      f"You can compare up to {analysis.MAX_SIMILARITY} subjects at once")
        elif len(ids) < 2:
            error = t(request, "비교할 대상자를 2명 이상 고르세요", "Select at least two subjects to compare")
        else:
            result = analysis.similarity([by_id[i] for i in ids], method)
    info = analysis.METHOD_BY_ID[method]
    return render_page(request, "genomics/similarity.html", {
        "methods": [{"id": m["id"], "name": _pick(lang, m["name"]), "desc": _pick(lang, m["desc"])}
                    for m in analysis.METHODS],
        "method": method, "method_name": _pick(lang, info["name"]), "title": _pick(lang, info["title"]),
        "unit": _pick(lang, info["unit"]), "candidates": cands, "selected": ids, "selected_set": set(ids),
        "cohorts": _cohort_options(lang), "cohort": cohort, "notes": notes, "error": error, "run": run,
        "result": result, "max": analysis.MAX_SIMILARITY, "legend": [i / 10 for i in range(10)],
    }, sidebar="gap", status=400 if error else 200)
