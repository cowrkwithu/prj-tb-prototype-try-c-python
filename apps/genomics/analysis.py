"""GWAS(변이 × DST 표현형 연관)와 대상자 간 유사도(변이 집합 거리) — 원본 generateMockSnps·strainPoint 대체.

GWAS
  표본: 선택 약제의 DST 결과가 R 또는 S 인 대상자(I 제외), 선택하면 저장 데이터셋(코호트)의 대상자로 한정.
  검정 단위: 정규화한 환자 변이(카탈로그에 있으면 카탈로그 라벨로 묶는다). 2×2 표
      [[보유·R, 보유·S], [비보유·R, 비보유·S]] 에 Fisher 정확검정(양측, apps.cohorts.services.fisher_exact — 순수 파이썬).
  다중검정: Bonferroni 기준 0.05 / 검정 수. 위치는 카탈로그 좌표, 카탈로그 밖 변이는 유전자 중앙(근사) — 유전자를
      모르면 플롯에서 뺀다(표에는 남김).
  상한: 대상자 MAX_SAMPLES, 검정 변이 MAX_TESTS(보유자 많은 순). 표본이 R·S 각각 MIN_GROUP 미만이면 검정하지 않는다.

유사도
  선택 대상자(2~50명)의 내성 변이 집합으로 거리 행렬을 만든다(전장 유전체 SNP 거리가 아니다).
      SNP 거리   = |A △ B| (서로 다른 변이 수)
      Jaccard   = 1 − |A ∩ B| / |A ∪ B| (둘 다 비면 0)
      계통 가중  = SNP 거리 + LINEAGE_PENALTY × (두 대상자의 Lineage 가 모두 있고 서로 다르면 1)
  세 거리 모두 대칭이고 대각선은 0 이다.
"""
from __future__ import annotations

import math
from collections import Counter

from apps.cases.models import DSTResult, Patient
from apps.cohorts.services import fisher_exact

from . import services

MAX_SAMPLES = 20_000
MAX_TESTS = 500
MIN_GROUP = 3
ALPHA = 0.05
MAX_SIMILARITY = 50
LINEAGE_PENALTY = 5

METHODS = [
    {"id": "snp", "name": ("SNP 거리", "SNP Distance"),
     "desc": ("두 대상자 사이 서로 다른 내성 변이 수", "Number of resistance variants that differ between two subjects"),
     "title": ("SNP 거리 행렬", "SNP Distance Matrix"), "unit": ("변이 차이 수", "variant differences"), "decimals": 0},
    {"id": "jaccard", "name": ("Jaccard 거리", "Jaccard Distance"),
     "desc": ("1 − 공유 변이 / 합집합 변이 (0~1)", "1 − shared / union of variants (0–1)"),
     "title": ("Jaccard 거리 행렬", "Jaccard Distance Matrix"), "unit": ("Jaccard 거리", "Jaccard distance"),
     "decimals": 2},
    {"id": "lineage", "name": ("계통 가중 거리", "Lineage-weighted Distance"),
     "desc": (f"SNP 거리 + Lineage 가 다르면 {LINEAGE_PENALTY} 가산",
              f"SNP distance + {LINEAGE_PENALTY} when lineages differ"),
     "title": ("계통 가중 거리 행렬", "Lineage-weighted Distance Matrix"),
     "unit": ("변이 차이 + 계통 가중", "variant differences + lineage weight"), "decimals": 0},
]
METHOD_IDS = [m["id"] for m in METHODS]
METHOD_BY_ID = {m["id"]: m for m in METHODS}


def _unit_of(m: str, cat: services.Catalog):
    """환자 변이 표기 → (검정 단위 키, 표시 라벨, Variant|None, 유전자)."""
    v = cat.match(m)
    if v is not None:
        return f"v:{v.pk}", v.label, v, v.gene.gene
    parsed = services.parse_mutation(m)
    if parsed:
        gene = cat.gene_names.get(parsed[0].lower(), parsed[0])
        return "k:" + services.mutation_key(*parsed), f"{gene} {parsed[1]}", None, gene
    s = " ".join(m.split())[:60]
    return "r:" + s.lower(), s, None, ""


def dst_drug_options() -> list[dict]:
    """DB 에 DST 결과가 있는 약제와 R/S 수."""
    counts = Counter(DSTResult.objects.filter(result__in=["R", "S"]).values_list("drug", "result"))
    out = []
    for code in sorted({d for d, _ in counts}):
        name = services.CODE_DRUGS.get(code, "")
        out.append({"code": code, "name": name, "r": counts.get((code, "R"), 0), "s": counts.get((code, "S"), 0)})
    return out


def gwas(drug_code: str, scope_ids: set[int] | None = None) -> dict:
    cat = services.load_catalog()
    gene_mid = {g.gene: (g.start + g.end) // 2 for g in cat.genes}
    qs = (Patient.objects.filter(dst_results__drug=drug_code, dst_results__result__in=["R", "S"])
          .order_by("id").values_list("id", "dst_results__result", "resistance_mutations"))
    samples = []
    truncated = False
    for pid, res, muts in qs.iterator(chunk_size=services.ITER_CHUNK):
        if scope_ids is not None and pid not in scope_ids:
            continue
        if len(samples) >= MAX_SAMPLES:
            truncated = True
            break
        samples.append((pid, res, muts))
    n_r = sum(1 for _, r, _ in samples if r == "R")
    n_s = len(samples) - n_r
    out = {"drug": drug_code, "drug_name": services.CODE_DRUGS.get(drug_code, ""), "n": len(samples),
           "n_r": n_r, "n_s": n_s, "truncated": truncated, "insufficient": False, "points": [], "tests": 0,
           "threshold": None, "neglog_threshold": None, "significant": 0, "skipped_tests": 0}
    if n_r < MIN_GROUP or n_s < MIN_GROUP:
        out["insufficient"] = True
        return out
    carriers: dict[str, list[int]] = {}   # 키 → [R 보유, S 보유]
    meta: dict[str, tuple] = {}
    for _, res, muts in samples:
        units = {}
        for m in services.clean_mutations(muts):
            key, label, v, gene = _unit_of(m, cat)
            units[key] = (label, v, gene)
        for key, info in units.items():
            c = carriers.setdefault(key, [0, 0])
            c[0 if res == "R" else 1] += 1
            meta.setdefault(key, info)
    keys = sorted(carriers, key=lambda k: (-(carriers[k][0] + carriers[k][1]), k))
    out["skipped_tests"] = max(0, len(keys) - MAX_TESTS)
    keys = keys[:MAX_TESTS]
    m = len(keys)
    out["tests"] = m
    if not m:
        return out
    threshold = ALPHA / m
    out["threshold"] = threshold
    out["neglog_threshold"] = -math.log10(threshold)
    points = []
    for key in keys:
        a, b = carriers[key]
        c, d = n_r - a, n_s - b
        odds, p = fisher_exact(a, b, c, d)
        label, v, gene = meta[key]
        if v is not None:
            position, approx = v.position, False
        elif gene in gene_mid:
            position, approx = gene_mid[gene], True
        else:
            position, approx = None, False
        aa = (v.amino_acid_change if v is not None else (label.split(" ", 1)[1] if " " in label else ""))
        neglog = -math.log10(max(p, 1e-300))
        points.append({
            "label": label, "gene": gene, "position": position, "approx": approx, "aa": aa,
            "ecoli": services.to_ecoli(gene, services.strip_change(aa)) if gene else None,
            "catalog": v is not None, "drugs": services.variant_drugs(v) if v is not None else [],
            # 카탈로그상 이 약제에 연관된 변이인가(약제 판정은 variant.drugs 전체 — 예측·검색과 같은 규칙)
            "linked": v is not None and out["drug_name"] in services.variant_drugs(v), "a": a, "b": b, "c": c, "d": d, "odds": odds, "p": p,
            "neglog": neglog, "significant": p < threshold,
        })
    points.sort(key=lambda r: (r["p"], r["label"]))
    out["points"] = points
    out["significant"] = sum(1 for r in points if r["significant"])
    return out


# ═══════════════════════════════════════════════════════════
#  유사도
# ═══════════════════════════════════════════════════════════
def similarity_candidates():
    return Patient.objects.filter(services.genomic_patients_q()).order_by("id")


def _unit_set(muts, cat) -> frozenset:
    return frozenset(_unit_of(m, cat)[0] for m in services.clean_mutations(muts))


def distance(method: str, a: frozenset, b: frozenset, la: str | None, lb: str | None) -> float:
    diff = len(a ^ b)
    if method == "jaccard":
        union = len(a | b)
        return 0.0 if union == 0 else 1 - len(a & b) / union
    if method == "lineage":
        return diff + (LINEAGE_PENALTY if la and lb and la != lb else 0)
    return float(diff)


def similarity(patients: list, method: str) -> dict:
    """patients: Patient 목록(2~MAX_SIMILARITY). 반환: 행렬·최댓값·표시 소수 자리."""
    cat = services.load_catalog()
    sets = [_unit_set(p.resistance_mutations, cat) for p in patients]
    lins = [(p.lineage or "").strip() or None for p in patients]
    n = len(patients)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = distance(method, sets[i], sets[j], lins[i], lins[j])
            matrix[i][j] = matrix[j][i] = d
    top = max([max(r) for r in matrix] + [0.0])
    info = METHOD_BY_ID[method]
    rows = []
    for i, p in enumerate(patients):
        cells = []
        for j in range(n):
            d = matrix[i][j]
            inten = (d / top) if top else 0.0
            cells.append({"value": d, "text": f"{d:.{info['decimals']}f}", "diag": i == j,
                          "alpha": round(inten, 3), "dark": i == j or inten > 0.5})
        rows.append({"patient": p, "cells": cells})
    return {"rows": rows, "max": top, "n": n, "matrix": matrix}
