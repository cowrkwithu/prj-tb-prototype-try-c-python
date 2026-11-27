"""약제내성 예측 — 카탈로그 기반 규칙 엔진(원본 ResearchPrediction 의 mockResults 대체).

실제 TB-Profiler·Mykrobe·ResFinder 를 실행하지 않는다. 각 도구의 판정 성격을 본뜬 **규칙**을 DB 카탈로그로 실행한다.
  TB-Profiler 규칙: 카탈로그 전체. 해당 약제의 카탈로그 변이 검출 → R, 연관 유전자에 그 밖의 변이(미분류·다른 약제의
                    카탈로그 변이) → U, 연관 유전자 검사됨·무변이 → S, 검사 안 됨 → U.
  Mykrobe 규칙    : 근거 등급 '높음' 변이 패널만 R. 그 밖 해당 약제 카탈로그 변이 → U, 연관 유전자의 그 밖 변이 → U.
불변식(모든 알고리즘): 약제의 연관 유전자에서 변이가 하나라도 검출되면 그 약제는 S 가 아니다(R 또는 U).
카탈로그 변이의 약제는 variant.drugs(대표 + 추가 연관 — services.EXTRA_VARIANT_DRUGS) 전체로 판정한다.
  ResFinder 규칙  : 획득 내성 유전자 검출. 결핵균 내성은 염색체 변이이고 획득 유전자 카탈로그가 비어 있어 모든 약제 U.
합의: 원본과 같이 R 수 > S 수 → R, S 수 > R 수 → S, 같으면 U.

입력: (1) 시퀀스 보유 대상자 선택 — 대상자의 내성 변이 목록, (2) 변이 목록 업로드(VCF / CSV / TSV 텍스트).
업로드는 크기(1MB)·행 수(5,000)·줄 길이·UTF-8 을 검증하고, 어떤 입력에도 예외 대신 InputError 를 낸다.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction

from apps.cases.models import Patient
from apps.core.safejson import has_bad_chars

from . import services
from .models import PredictionRun, Variant

MAX_BYTES = 1024 * 1024
MAX_ROWS = 5000
MAX_LINE = 10_000
MAX_OBSERVED = 2000      # 결과·DB 에 남기는 관측 변이 수 상한
RUNS_KEPT_PER_USER = 200  # 사용자별 실행 기록 보관 개수(초과분은 오래된 것부터 삭제)
EXTS = (".vcf", ".csv", ".tsv", ".txt")

ALGORITHMS = [
    {"id": "tb-profiler", "name": "TB-Profiler",
     "desc": ("광범위 약제내성 프로파일링", "Broad drug resistance profiling"),
     "rule": ("규칙: 카탈로그 전체 — 카탈로그 변이 → 내성, 연관 유전자의 그 밖 변이 → 불명, 검사됨·무변이 → 감수성",
              "Rule: full catalogue — catalogue variant → R, any other variant in a linked gene → U, tested & wild type → S")},
    {"id": "mykrobe", "name": "Mykrobe",
     "desc": ("균종 동정 + 항생제 내성(AMR)", "Species identification + AMR"),
     "rule": ("규칙: 근거 등급 '높음' 패널만 내성 판정, 연관 유전자의 그 밖 변이 → 불명, 검사됨·무변이 → 감수성",
              "Rule: high-confidence panel only → R, any other variant in a linked gene → U, tested & wild type → S")},
    {"id": "resfinder", "name": "ResFinder",
     "desc": ("획득 내성 유전자 검출", "Acquired resistance gene detection"),
     "rule": ("규칙: 획득 내성 유전자 카탈로그(결핵균 해당 없음) — 모든 약제 불명",
              "Rule: acquired resistance gene catalogue (none for M. tuberculosis) — every drug U")},
]
ALGO_IDS = [a["id"] for a in ALGORITHMS]
ALGO_BY_ID = {a["id"]: a for a in ALGORITHMS}
DRUGS = ["Isoniazid", "Rifampicin", "Ethambutol", "Pyrazinamide", "Fluoroquinolones", "Amikacin", "Kanamycin",
         "Ethionamide"]


class InputError(ValueError):
    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en

    def text(self, lang: str) -> str:
        return self.en if lang == "en" else self.ko


# ═══════════════════════════════════════════════════════════
#  입력 해석
# ═══════════════════════════════════════════════════════════
def decode_upload(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise InputError(f"파일이 너무 큽니다(최대 {MAX_BYTES // 1024}KB)", f"File too large (max {MAX_BYTES // 1024} KB)")
    if b"\x00" in data:
        raise InputError("텍스트 파일이 아닙니다(이진 데이터)", "Not a text file (binary data)")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InputError("UTF-8 텍스트만 지원합니다", "Only UTF-8 text is supported") from None


def check_text(text: str) -> str:
    if not isinstance(text, str):
        raise InputError("입력이 올바르지 않습니다", "Invalid input")
    if has_bad_chars(text):
        raise InputError("허용되지 않는 문자(NUL·서로게이트)가 있습니다", "Input contains disallowed characters")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_BYTES:
        raise InputError(f"입력이 너무 큽니다(최대 {MAX_BYTES // 1024}KB)", f"Input too large (max {MAX_BYTES // 1024} KB)")
    return text.lstrip("﻿")


_ALLELE = re.compile(r"^[ACGTNacgtn]{1,50}$")
_POS = re.compile(r"^[0-9]{1,9}$")
COLUMN_ALIASES = {
    "mutation": ("mutation", "variant", "mutations", "변이"),
    "gene": ("gene", "유전자"),
    "change": ("change", "aa_change", "amino_acid_change", "aminoacidchange", "aa", "protein_change", "아미노산 변이"),
    "position": ("position", "pos", "위치"),
    "ref": ("ref", "ref_allele", "reference"),
    "alt": ("alt", "alt_allele", "alternate"),
}


@dataclass
class Parsed:
    fmt: str = ""
    observed: list = field(default_factory=list)   # {"kind": "mutation"|"position", ...}
    skipped: Counter = field(default_factory=Counter)
    rows: int = 0


def _lines(text: str) -> list[str]:
    lines = text.splitlines()
    data_rows = sum(1 for ln in lines if ln.strip() and not ln.startswith("#"))
    if data_rows > MAX_ROWS:
        raise InputError(f"행이 너무 많습니다(최대 {MAX_ROWS:,}행)", f"Too many rows (max {MAX_ROWS:,})")
    if any(len(ln) > MAX_LINE for ln in lines):
        raise InputError(f"한 줄이 너무 깁니다(최대 {MAX_LINE:,}자)", f"A line is too long (max {MAX_LINE:,} characters)")
    return lines


def _is_vcf(lines: list[str], filename: str) -> bool:
    if filename.lower().endswith(".vcf"):
        return True
    head = [ln for ln in lines[:200] if ln.strip()]
    return any(ln.startswith("##fileformat=VCF") or ln.upper().startswith("#CHROM") for ln in head)


def parse_vcf(lines: list[str]) -> Parsed:
    out = Parsed(fmt="vcf")
    for ln in lines:
        if not ln.strip() or ln.startswith("#"):
            continue
        out.rows += 1
        cols = ln.split("\t")
        if len(cols) < 5:
            out.skipped["malformed"] += 1
            continue
        pos, ref, alts = cols[1].strip(), cols[3].strip(), cols[4].strip()
        if not _POS.match(pos) or not 1 <= int(pos) <= services.GENOME_LENGTH or not _ALLELE.match(ref):
            out.skipped["malformed"] += 1
            continue
        if len(cols) > 6 and cols[6].strip() not in ("PASS", ".", ""):
            out.skipped["filtered"] += 1
            continue
        for alt in alts.split(",")[:10]:
            alt = alt.strip()
            if not _ALLELE.match(alt):
                out.skipped["malformed"] += 1
                continue
            out.observed.append({"kind": "position", "position": int(pos), "ref": ref.upper(), "alt": alt.upper()})
    return out


def _norm_header(h: str) -> str:
    return " ".join(h.strip().lower().split())


def parse_table(lines: list[str], filename: str) -> Parsed:
    body = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    tab = filename.lower().endswith(".tsv") or (bool(body) and "\t" in body[0])
    out = Parsed(fmt="tsv" if tab else "csv")
    if not body:
        return out
    try:
        rows = list(csv.reader(io.StringIO("\n".join(body)), delimiter="\t" if tab else ","))
    except csv.Error:
        raise InputError("CSV/TSV 형식을 읽을 수 없습니다", "Could not read the CSV/TSV content") from None
    header = [_norm_header(h) for h in rows[0]]
    cols = {}
    for key, aliases in COLUMN_ALIASES.items():
        for i, h in enumerate(header):
            if h in aliases:
                cols.setdefault(key, i)
    if not cols:
        # 머리글 없는 한 열 목록('rpoB S450L' 한 줄에 하나)
        cols = {"mutation": 0}
        data = rows
    else:
        data = rows[1:]

    def cell(r, key):
        i = cols.get(key)
        return r[i].strip() if i is not None and i < len(r) else ""

    for r in data:
        if not any(c.strip() for c in r):
            continue
        out.rows += 1
        m = cell(r, "mutation")
        gene, change = cell(r, "gene"), cell(r, "change")
        pos, ref, alt = cell(r, "position"), cell(r, "ref"), cell(r, "alt")
        if m:
            parsed = services.parse_mutation(m)
        elif gene and change:
            parsed = services.parse_mutation(f"{gene} {change}")
        else:
            parsed = None
        if parsed:
            out.observed.append({"kind": "mutation", "gene": parsed[0], "change": parsed[1]})
        elif pos and ref and alt and _POS.match(pos) and 1 <= int(pos) <= services.GENOME_LENGTH \
                and _ALLELE.match(ref) and _ALLELE.match(alt):
            out.observed.append({"kind": "position", "position": int(pos), "ref": ref.upper(), "alt": alt.upper()})
        else:
            out.skipped["malformed"] += 1
    return out


def parse_input(text: str, filename: str = "") -> Parsed:
    text = check_text(text)
    lines = _lines(text)
    parsed = parse_vcf(lines) if _is_vcf(lines, filename) else parse_table(lines, filename)
    if parsed.rows == 0:
        raise InputError("변이 행이 없습니다", "No variant rows found")
    return parsed


# ═══════════════════════════════════════════════════════════
#  카탈로그 대조
# ═══════════════════════════════════════════════════════════
def resolve(observed: list[dict], cat: services.Catalog) -> list[dict]:
    """관측 변이 → {label, gene, status(catalog|uncharacterized|outside), variant_id, drug, confidence}. 중복 제거."""
    spans = cat.spans()
    out, seen = [], set()
    for o in observed:
        v = None
        if o["kind"] == "mutation":
            v = cat.by_key.get(services.mutation_key(o["gene"], o["change"]))
            gene = cat.gene_names.get(o["gene"].lower(), o["gene"])
            label = f"{gene} {o['change']}"
        else:
            v = cat.by_pos.get((o["position"], o["ref"], o["alt"]))
            gene = v.gene.gene if v else services.gene_at(o["position"], spans)
            label = f"{o['position']} {o['ref']}>{o['alt']}"
        if v is not None:
            key = ("v", v.pk)
            row = {"label": v.label, "gene": v.gene.gene, "status": "catalog", "variant_id": v.pk,
                   "drug": v.drug_resistance, "drugs": services.variant_drugs(v), "confidence": v.confidence,
                   "input": label if label.lower() != v.label.lower() else ""}
        else:
            key = ("o", label.lower())
            in_gene = gene.lower() in cat.gene_names
            row = {"label": label, "gene": gene, "status": "uncharacterized" if in_gene else "outside",
                   "variant_id": None, "drug": "", "drugs": [], "confidence": "", "input": ""}
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= MAX_OBSERVED:
            break
    return out


def predict(resolved: list[dict], tested_genes: set[str], algorithms: list[str], cat: services.Catalog) -> list[dict]:
    drug_genes = cat.drug_genes()
    rows = []
    for drug in DRUGS:
        genes = drug_genes.get(drug, [])
        tested = bool(genes) and any(g in tested_genes for g in genes)
        cat_hits = [r for r in resolved if r["status"] == "catalog" and drug in (r.get("drugs") or [r["drug"]])]
        hit_labels = {r["label"] for r in cat_hits}
        # 연관 유전자의 그 밖 변이: 미분류 변이 + 다른 약제의 카탈로그 변이 — 있으면 S 로 판정하지 않는다(불변식)
        other = [r for r in resolved if r["status"] in ("catalog", "uncharacterized") and r["gene"] in genes
                 and r["label"] not in hit_labels]
        calls = {}
        for algo in algorithms:
            if algo == "tb-profiler":
                if cat_hits:
                    calls[algo] = {"call": "R", "evidence": [r["label"] for r in cat_hits]}
                elif other:
                    calls[algo] = {"call": "U", "evidence": [r["label"] for r in other], "reason": "uncharacterized"}
                elif tested:
                    calls[algo] = {"call": "S", "evidence": []}
                else:
                    calls[algo] = {"call": "U", "evidence": [], "reason": "not_tested"}
            elif algo == "mykrobe":
                high = [r for r in cat_hits if r["confidence"] == Variant.Confidence.HIGH]
                low = [r for r in cat_hits if r["confidence"] != Variant.Confidence.HIGH]
                if high:
                    calls[algo] = {"call": "R", "evidence": [r["label"] for r in high]}
                elif low:
                    calls[algo] = {"call": "U", "evidence": [r["label"] for r in low], "reason": "low_confidence"}
                elif other:
                    calls[algo] = {"call": "U", "evidence": [r["label"] for r in other], "reason": "uncharacterized"}
                elif tested:
                    calls[algo] = {"call": "S", "evidence": []}
                else:
                    calls[algo] = {"call": "U", "evidence": [], "reason": "not_tested"}
            else:  # resfinder
                calls[algo] = {"call": "U", "evidence": [], "reason": "no_acquired_genes"}
        votes = Counter(c["call"] for c in calls.values())
        consensus = "R" if votes["R"] > votes["S"] else ("S" if votes["S"] > votes["R"] else "U")
        rows.append({"drug": drug, "genes": genes, "calls": calls, "consensus": consensus})
    return rows


def clean_algorithms(values) -> list[str]:
    vals = {v for v in (values or []) if isinstance(v, str)}
    return [a for a in ALGO_IDS if a in vals]


def eligible_patients():
    """예측에 쓸 수 있는 대상자 — 시퀀스 보유 또는 내성 변이 기록."""
    return Patient.objects.filter(services.genomic_patients_q()).order_by("id")


@transaction.atomic
def run_prediction(user, *, algorithms: list[str], patient: Patient | None = None, text: str | None = None,
                   filename: str = "", assume_wgs: bool = True) -> PredictionRun:
    """입력을 해석·대조·판정해 PredictionRun 으로 저장한다. 입력 오류는 InputError."""
    algorithms = clean_algorithms(algorithms)
    if not algorithms:
        raise InputError("알고리즘을 하나 이상 고르세요", "Select at least one algorithm")
    cat = services.load_catalog()
    all_genes = {g.gene for g in cat.genes}
    if patient is not None:
        observed = []
        skipped: Counter = Counter()
        for m in services.clean_mutations(patient.resistance_mutations):
            parsed = services.parse_mutation(m)
            if parsed:
                observed.append({"kind": "mutation", "gene": parsed[0], "change": parsed[1]})
            else:
                skipped["malformed"] += 1
        resolved = resolve(observed, cat)
        # 시퀀스(WGS) 보유 대상자는 모든 내성 유전자를 검사한 것으로, 그 밖에는 기록된 변이의 유전자만 검사한 것으로 본다
        tested = all_genes if patient.has_sequence else {r["gene"] for r in resolved if r["gene"] in all_genes}
        source, fmt, sample, lineage, wgs = PredictionRun.Source.PATIENT, "patient", patient.condition_id, \
            patient.lineage or "", bool(patient.has_sequence)
        rows = len(observed)
    else:
        parsed = parse_input(text or "", filename)
        resolved = resolve(parsed.observed, cat)
        skipped = parsed.skipped
        tested = all_genes if assume_wgs else {r["gene"] for r in resolved if r["gene"] in all_genes}
        source, fmt, lineage, wgs, rows = PredictionRun.Source.UPLOAD, parsed.fmt, "", assume_wgs, parsed.rows
        sample = re.sub(r"[\x00-\x1f\x7f]", "", filename or "").strip()[:200] or "pasted_variants"
    results = predict(resolved, tested, algorithms, cat)
    run = PredictionRun.objects.create(
        user=user, source=source, patient=patient, sample_name=sample, input_format=fmt, algorithms=algorithms,
        assume_wgs=wgs, lineage=lineage[:40],
        inputs={"observed": resolved, "tested_genes": sorted(tested), "skipped": dict(skipped), "rows": rows},
        results={"drugs": results})
    old = list(PredictionRun.objects.filter(user=user).order_by("-created_at", "-id")
               .values_list("pk", flat=True)[RUNS_KEPT_PER_USER:RUNS_KEPT_PER_USER + 500])
    if old:
        PredictionRun.objects.filter(pk__in=old).delete()
    return run


def visible_runs(user):
    qs = PredictionRun.objects.select_related("user", "patient")
    return qs if getattr(user, "is_portal_admin", False) else qs.filter(user=user)
