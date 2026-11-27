"""유전자 분석(GAP) 공통 서비스 — 카탈로그·환자 변이 대조 규칙·DB 집계·시드.

환자 변이 문자열 ↔ 카탈로그 매칭 규칙(이 모듈 한 곳에서만 정의)
  1. 표기 정규화: 앞뒤 공백 제거, 연속 공백 1칸, 끝의 괄호 설명 제거('c-15t (promoter)' → 'c-15t').
     '유전자 변화' 두 토큰으로 나눈다. 비교 키는 소문자(`mutation_key`) — 'rpoB S450L' == 'rpob s450l'.
  2. 카탈로그 변이는 자기 라벨의 키와 **E. coli 번호 표기 키**(rpoB S531L 등, utils/ecoliNumbering 규칙) 둘 다로 찾는다.
  3. VCF 처럼 좌표로 들어온 변이는 (위치, 참조, 대립) 이 카탈로그와 정확히 같을 때만 매칭한다.
원본 utils/variantCohort.ts 의 toMutationLabel 과 같은 라벨을 쓰고(apps.cohorts.services.mutation_label),
데이터 탐색으로 넘길 때는 실제 환자 표기(raw)를 함께 넘겨 별칭으로 매칭된 환자도 빠지지 않게 한다.

원본의 frequency·sampleCount 는 시드 숫자 대신 환자 resistance_mutations 에서 집계한다
  sampleCount = 그 변이를 보유한 대상자 수,
  frequency   = sampleCount / 유전체 데이터 보유 대상자 수(시퀀스 보유 또는 내성 변이 기록이 있는 대상자).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from apps.cases.models import Patient
from apps.cohorts.services import mutation_label
from apps.core.seeding import is_seeded, mark_seeded, reset_sequences

from .models import ResistanceGene, Variant

DATASET = "genomics_catalog"
GENOME_LENGTH = 4_411_532  # M. tuberculosis H37Rv (NC_000962.3)
PROMOTER = 800  # 유전자 시작점 상류 이 거리까지는 그 유전자의 프로모터 변이로 본다(원본 GwasAnalysis PROMOTER)
ITER_CHUNK = 2000

# 원본 GwasAnalysis GENE_SPANS(H37Rv [시작, 끝]) + MINUS_STRAND — 시드 좌표
GENE_COORDS = {
    "gyrB": (5240, 7267, "+"), "gyrA": (7302, 9818, "+"), "rpoB": (759807, 763325, "+"),
    "rrs": (1471846, 1473382, "+"), "inhA": (1674202, 1675011, "+"), "katG": (2153889, 2156111, "-"),
    "pncA": (2288681, 2289241, "-"), "eis": (2714124, 2715332, "-"), "embB": (4246514, 4249810, "+"),
    "ethA": (4326004, 4327473, "-"),
}
# 상류 800bp 안의 이웃 유전자 몸체 — 그 안의 변이는 내성 유전자의 프로모터가 아니라 이웃 유전자에 붙인다(원본 그대로)
NEIGHBOR_SPANS = {
    "rpoC": (763370, 767320), "embA": (4243233, 4246517), "fabG1": (1673440, 1674183),
    "furA": (2156149, 2156601), "ethR": (4327549, 4328199),
}
# 원본 utils/ecoliNumbering.ts — 유전자별 E. coli 번호 오프셋
ECOLI_OFFSETS = {"rpoB": 81, "gyrA": -7, "gyrB": -74, "rrs": 7}
_SUBSTITUTION = re.compile(r"^([A-Za-z])(\d{1,6})([A-Za-z*]{1,10})$")
# 카탈로그 근거 등급(시드 지정) — 원본 주석('Most common'·'Predominant'·'Main' → 높음, 'Disputed' → 불확실, 그 밖 → 중간)
SEED_CONFIDENCE = {
    "rpoB S450L": "high", "rpoB H445Y": "high", "rpoB D435V": "high", "katG S315T": "high", "inhA c-15t": "high",
    "gyrA D94G": "high", "gyrA A90V": "high", "embB M306V": "high", "rrs a1401g": "high", "rpoB L430P": "uncertain",
}
# 대표 약제(원본 drugResistance) 외에 이 변이가 내성을 주는 약제 — 근거:
#  · 원본 resistanceGenes.associatedDrugs: rrs → Amikacin·Kanamycin·Capreomycin, inhA → Isoniazid·Ethionamide,
#    eis → Kanamycin.
#  · WHO 결핵균 변이 카탈로그(2021/2023) 상식 범위: rrs a1401g 는 아미카신·카나마이신·카프레오마이신 교차내성,
#    inhA 프로모터 c-15t 는 이소니아지드(저수준)·에티온아미드 교차내성, eis 프로모터 c-14t 는 카나마이신 내성.
# 0002 마이그레이션(fill_drugs)에 같은 값이 복사돼 있다 — 바꾸면 새 마이그레이션으로 보정할 것.
EXTRA_VARIANT_DRUGS = {
    "rrs a1401g": ["Amikacin", "Kanamycin", "Capreomycin"],
    "inhA c-15t": ["Isoniazid", "Ethionamide"],
    "eis c-14t": ["Kanamycin"],
}


def variant_drugs(v) -> list[str]:
    """변이의 연관 약제 전체(대표 먼저). drugs 가 비어 있으면 대표 약제 하나."""
    lst = [d for d in (v.drugs or []) if isinstance(d, str)] if isinstance(v.drugs, list) else []
    return list(dict.fromkeys([v.drug_resistance] + lst))


# 약제 이름 ↔ DST 약제 코드(apps.cases FIRST/SECOND_LINE_DRUGS)
DRUG_CODES = {"Isoniazid": "INH", "Rifampicin": "RIF", "Ethambutol": "EMB", "Pyrazinamide": "PZA",
              "Fluoroquinolones": "FQ", "Amikacin": "AMK", "Kanamycin": "KAN", "Capreomycin": "CAP",
              "Ethionamide": "ETH"}
CODE_DRUGS = {v: k for k, v in DRUG_CODES.items()}
DRUG_KO = {"Isoniazid": "이소니아지드", "Rifampicin": "리팜피신", "Ethambutol": "에탐부톨", "Pyrazinamide": "피라진아미드",
           "Fluoroquinolones": "플루오로퀴놀론", "Amikacin": "아미카신", "Kanamycin": "카나마이신",
           "Capreomycin": "카프레오마이신", "Ethionamide": "에티온아미드"}

_GENE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,19}$")
_CHANGE_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,40}$")
_PAREN = re.compile(r"\s*\(.*\)\s*$")


# ═══════════════════════════════════════════════════════════
#  표기 정규화 · E. coli 번호
# ═══════════════════════════════════════════════════════════
def parse_mutation(text) -> tuple[str, str] | None:
    """'rpoB S450L' · 'inhA c-15t (promoter)' → (유전자, 변화). 형식이 아니면 None."""
    if not isinstance(text, str) or len(text) > 200:
        return None
    s = _PAREN.sub("", " ".join(text.split()))
    parts = s.split(" ")
    if len(parts) != 2:
        return None
    gene, change = parts
    if not _GENE_RE.match(gene) or not _CHANGE_RE.match(change):
        return None
    return gene, change


def mutation_key(gene: str, change: str) -> str:
    return f"{gene.lower()} {change.lower()}"


def key_of(text) -> str | None:
    parsed = parse_mutation(text)
    return mutation_key(*parsed) if parsed else None


def to_ecoli(gene: str, change: str | None) -> str | None:
    """원본 toEcoliNumbering — 변환할 수 없으면 None."""
    offset = ECOLI_OFFSETS.get(gene)
    m = _SUBSTITUTION.match((change or "").strip())
    if offset is None or not m:
        return None
    ref, codon, alt = m.groups()
    return f"{ref}{int(codon) + offset}{alt}"


def strip_change(aa: str) -> str:
    return _PAREN.sub("", aa or "").strip()


def gene_at(position: int, genes: list[tuple] | None = None) -> str:
    """위치가 속한 유전자(원본 geneAt): 유전자 몸체(내성·이웃) → 내성 유전자 상류 프로모터 → '' (유전자 간 영역)."""
    spans = genes if genes is not None else [(g, s, e, st) for g, (s, e, st) in GENE_COORDS.items()]
    for g, s, e, _ in spans:
        if s <= position <= e:
            return g
    for g, (s, e) in NEIGHBOR_SPANS.items():
        if s <= position <= e:
            return g
    for g, s, e, st in spans:
        lo, hi = (e + 1, e + PROMOTER) if st == "-" else (s - PROMOTER, s - 1)
        if lo <= position <= hi:
            return g
    return ""


# ═══════════════════════════════════════════════════════════
#  카탈로그
# ═══════════════════════════════════════════════════════════
@dataclass
class Catalog:
    genes: list = field(default_factory=list)          # ResistanceGene (순서대로)
    variants: list = field(default_factory=list)       # Variant (순서대로)
    by_key: dict = field(default_factory=dict)         # 정규화 키(라벨·E. coli 별칭) → Variant
    by_pos: dict = field(default_factory=dict)         # (위치, REF, ALT) → Variant
    gene_names: dict = field(default_factory=dict)     # 소문자 → 정식 유전자명

    def spans(self) -> list[tuple]:
        return [(g.gene, g.start, g.end, g.strand) for g in self.genes]

    def match(self, text) -> Variant | None:
        k = key_of(text)
        return self.by_key.get(k) if k else None

    def drug_genes(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for g in self.genes:
            for d in g.associated_drugs or []:
                if isinstance(d, str):
                    out.setdefault(d, []).append(g.gene)
        return out


def load_catalog() -> Catalog:
    cat = Catalog()
    cat.genes = list(ResistanceGene.objects.all())
    cat.gene_names = {g.gene.lower(): g.gene for g in cat.genes}
    cat.variants = list(Variant.objects.select_related("gene").all())
    for v in cat.variants:
        change = strip_change(v.amino_acid_change)
        if change:
            cat.by_key.setdefault(mutation_key(v.gene.gene, change), v)
            ecoli = to_ecoli(v.gene.gene, change)
            if ecoli:
                cat.by_key.setdefault(mutation_key(v.gene.gene, ecoli), v)
        cat.by_pos.setdefault((v.position, v.ref_allele.upper(), v.alt_allele.upper()), v)
    return cat


# ═══════════════════════════════════════════════════════════
#  환자 집계
# ═══════════════════════════════════════════════════════════
def genomic_patients_q() -> Q:
    """유전체 데이터 보유 대상자 — 시퀀스 보유 또는 내성 변이 기록이 있다."""
    return Q(has_sequence=True) | ~Q(resistance_mutations=[])


def _iter_mutations(qs=None):
    qs = qs if qs is not None else Patient.objects.all()
    yield from qs.values_list("id", "resistance_mutations").iterator(chunk_size=ITER_CHUNK)


def clean_mutations(lst) -> list[str]:
    return [m for m in (lst or []) if isinstance(m, str) and m.strip()] if isinstance(lst, list) else []


@dataclass
class Stats:
    denominator: int = 0
    counts: Counter = field(default_factory=Counter)        # variant_id → 보유 대상자 수
    uncatalogued: Counter = field(default_factory=Counter)  # 카탈로그에 없는 환자 변이 표기 → 대상자 수

    def count(self, v) -> int:
        return self.counts.get(v.pk, 0)

    def freq(self, v) -> float:
        return self.counts.get(v.pk, 0) / self.denominator if self.denominator else 0.0


def variant_stats(cat: Catalog) -> Stats:
    st = Stats(denominator=Patient.objects.filter(genomic_patients_q()).count())
    for _, muts in _iter_mutations(Patient.objects.exclude(resistance_mutations=[])):
        seen, other = set(), set()
        for m in clean_mutations(muts):
            v = cat.match(m)
            if v is not None:
                seen.add(v.pk)
            else:
                other.add(" ".join(m.split())[:100])
        st.counts.update(seen)
        st.uncatalogued.update(other)
    return st


def carriers(cat: Catalog, variants) -> tuple[list[int], list[str]]:
    """변이(하나라도) 보유 대상자 id 와, 매칭에 쓰인 실제 환자 표기(raw, 처음 나온 순)."""
    want = {v.pk for v in variants}
    ids, raw = [], {}
    for pid, muts in _iter_mutations(Patient.objects.exclude(resistance_mutations=[]).order_by("id")):
        hit = False
        for m in clean_mutations(muts):
            v = cat.match(m)
            if v is not None and v.pk in want:
                hit = True
                raw.setdefault(m, None)
        if hit:
            ids.append(pid)
    return ids, list(raw)


def variant_row(v: Variant, st: Stats) -> dict:
    change = strip_change(v.amino_acid_change)
    freq = st.freq(v)
    return {
        "id": v.pk, "gene": v.gene.gene, "position": v.position, "ref": v.ref_allele, "alt": v.alt_allele,
        "aa": v.amino_acid_change, "ecoli": to_ecoli(v.gene.gene, change), "drug": v.drug_resistance,
        "drugs": variant_drugs(v),
        "annotation": v.annotation, "label": v.label, "confidence": v.confidence,
        "sample_count": st.count(v), "frequency": freq, "freq_pct": round(freq * 100, 1),
        "bar": min(100.0, round(freq * 100, 1)),
    }


def browser_filter(cat: Catalog, query: str) -> list[Variant]:
    """원본 GenomeBrowser 검색 — 유전자·아미노산(Codon No.)·E. coli 번호·약제 부분 일치."""
    q = (query or "").strip().lower()[:100]
    if not q:
        return list(cat.variants)
    out = []
    for v in cat.variants:
        ecoli = to_ecoli(v.gene.gene, strip_change(v.amino_acid_change)) or ""
        if (q in v.gene.gene.lower() or q in (v.amino_acid_change or "").lower() or q in ecoli.lower()
                or any(q in d.lower() for d in variant_drugs(v))):
            out.append(v)
    return out


def drug_options(cat: Catalog) -> list[str]:
    return list(dict.fromkeys(d for v in cat.variants for d in variant_drugs(v)))


@dataclass
class SearchParams:
    gene: str = ""
    drug: str = ""
    aa: str = ""
    pos_min: int | None = None
    pos_max: int | None = None
    min_freq: float = 0.0  # %
    errors: list = field(default_factory=list)

    def pairs(self) -> list[tuple[str, str]]:
        out = []
        for k in ("gene", "drug", "aa"):
            if getattr(self, k):
                out.append((k, getattr(self, k)))
        if self.pos_min is not None:
            out.append(("pos_min", str(self.pos_min)))
        if self.pos_max is not None:
            out.append(("pos_max", str(self.pos_max)))
        if self.min_freq:
            out.append(("min_freq", f"{self.min_freq:g}"))
        return out


_UINT = re.compile(r"^[0-9]{1,9}$")
_NUM = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,4})?$")


def parse_search(qd, cat: Catalog) -> SearchParams:
    """변이 검색 조건 — 허용 목록·형식 밖 값은 버리고 errors 에 (ko, en) 을 남긴다(예외 없음)."""
    p = SearchParams()
    gene = (qd.get("gene") or "").strip()
    if gene:
        if gene.lower() in cat.gene_names:
            p.gene = cat.gene_names[gene.lower()]
        else:
            p.errors.append(("알 수 없는 유전자입니다", "Unknown gene"))
    drug = (qd.get("drug") or "").strip()
    if drug:
        opts = {d.lower(): d for d in drug_options(cat)}
        if drug.lower() in opts:
            p.drug = opts[drug.lower()]
        else:
            p.errors.append(("알 수 없는 약제입니다", "Unknown drug"))
    aa = qd.get("aa") or ""
    aa = re.sub(r"[\x00-\x1f\x7f]", "", aa).strip()
    if len(aa) > 40:
        p.errors.append(("아미노산 변이 검색어는 40자 이하입니다", "Amino acid search must be 40 characters or fewer"))
        aa = ""
    p.aa = aa
    for name in ("pos_min", "pos_max"):
        raw = (qd.get(name) or "").strip()
        if not raw:
            continue
        if _UINT.match(raw) and int(raw) <= GENOME_LENGTH:
            setattr(p, name, int(raw))
        else:
            p.errors.append((f"위치는 0~{GENOME_LENGTH:,} 사이 정수입니다", f"Position must be an integer 0–{GENOME_LENGTH:,}"))
    if p.pos_min is not None and p.pos_max is not None and p.pos_min > p.pos_max:
        p.errors.append(("위치 범위의 시작이 끝보다 큽니다", "Position range start is greater than its end"))
        p.pos_min = p.pos_max = None
    raw = (qd.get("min_freq") or "").strip()
    if raw:
        if _NUM.match(raw) and float(raw) <= 100:
            p.min_freq = float(raw)
        else:
            p.errors.append(("최소 빈도는 0~100 사이 숫자입니다", "Minimum frequency must be a number from 0 to 100"))
    return p


def search_variants(cat: Catalog, st: Stats, p: SearchParams) -> list[Variant]:
    aa = p.aa.lower()
    out = []
    for v in cat.variants:
        if p.gene and v.gene.gene != p.gene:
            continue
        if p.drug and p.drug not in variant_drugs(v):
            continue
        if aa:
            ecoli = (to_ecoli(v.gene.gene, strip_change(v.amino_acid_change)) or "").lower()
            if aa not in (v.amino_acid_change or "").lower() and aa not in ecoli:
                continue
        if p.pos_min is not None and v.position < p.pos_min:
            continue
        if p.pos_max is not None and v.position > p.pos_max:
            continue
        if p.min_freq and st.freq(v) * 100 < p.min_freq:
            continue
        out.append(v)
    return out


# ═══════════════════════════════════════════════════════════
#  유전체 트랙(브라우저) — SVG 좌표를 서버에서 계산
# ═══════════════════════════════════════════════════════════
TRACK_W = 1000


def genome_track(cat: Catalog, st: Stats) -> dict:
    genes = []
    for g in cat.genes:
        x = g.start / GENOME_LENGTH * TRACK_W
        w = max(3.0, (g.end - g.start) / GENOME_LENGTH * TRACK_W)
        n = sum(1 for v in cat.variants if v.gene_id == g.pk)
        genes.append({"gene": g.gene, "x": round(x, 2), "w": round(w, 2), "cx": round(x + w / 2, 2), "n": n})
    # 이름이 겹치지 않도록 행을 번갈아 둔다(가까운 유전자: gyrB·gyrA, embB·ethA 등)
    genes.sort(key=lambda r: r["x"])
    last_x = {0: -999.0, 1: -999.0}
    for r in genes:
        row = 0 if r["cx"] - last_x[0] >= 45 else 1
        r["row"] = row
        r["ly"] = 70 if row == 0 else 84
        last_x[row] = r["cx"]
    ticks = [{"x": round(m * 1_000_000 / GENOME_LENGTH * TRACK_W, 2), "label": f"{m:.1f}"} for m in range(0, 5)]
    return {"genes": genes, "ticks": ticks, "width": TRACK_W}


def gene_track(cat: Catalog, st: Stats, gene: ResistanceGene) -> dict:
    """선택 유전자 확대: 몸체 + 상류 프로모터 구간, 변이 막대(높이 ∝ 보유 대상자 수)."""
    if gene.strand == "-":
        lo, hi = gene.start - 100, gene.end + PROMOTER
        prom = (gene.end + 1, gene.end + PROMOTER)
    else:
        lo, hi = gene.start - PROMOTER, gene.end + 100
        prom = (gene.start - PROMOTER, gene.start - 1)
    span = max(1, hi - lo)

    def sx(pos):
        return round((pos - lo) / span * TRACK_W, 2)

    vs = [v for v in cat.variants if v.gene_id == gene.pk or lo <= v.position <= hi]
    top = max([st.count(v) for v in vs] + [1])
    marks = []
    for v in sorted(vs, key=lambda v: v.position):
        c = st.count(v)
        h = 14 + (c / top) * 46
        marks.append({"id": v.pk, "x": sx(v.position), "y": round(70 - h, 2), "h": round(h, 2), "count": c,
                      "label": f"{v.gene.gene} {v.amino_acid_change or v.position}", "position": v.position})
    ticks = []
    step = 500 if span <= 4000 else 1000
    t0 = (lo // step + 1) * step
    for p in range(t0, hi, step):
        ticks.append({"x": sx(p), "label": f"{p:,}"})
    return {"gene": gene.gene, "start": gene.start, "end": gene.end, "strand": gene.strand, "lo": lo, "hi": hi,
            "body_x": sx(gene.start), "body_w": round(sx(gene.end) - sx(gene.start), 2),
            "prom_x": sx(prom[0]), "prom_w": round(sx(prom[1]) - sx(prom[0]), 2),
            "marks": marks, "ticks": ticks, "width": TRACK_W}


# ═══════════════════════════════════════════════════════════
#  시드 (원본 data/genomics.ts → data/seed/genomics.json)
# ═══════════════════════════════════════════════════════════
def load_genomics(path: Path | None = None, force: bool = False) -> dict:
    """유전자 10종·변이 카탈로그 20건을 1회 적재한다(SeedLog). force=True 면 없는 항목만 다시 만든다.

    원본의 frequency·sampleCount 는 적재하지 않는다(화면·API 는 환자 데이터에서 집계)."""
    if is_seeded(DATASET) and not force:
        return {"genes": 0, "variants": 0, "skipped": True, "total": Variant.objects.count()}
    path = Path(path or settings.SEED_DIR / "genomics.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    genes_created = vars_created = 0
    with transaction.atomic():
        by_name = {g.gene: g for g in ResistanceGene.objects.all()}
        for i, g in enumerate(data["resistanceGenes"]):
            if g["gene"] in by_name:
                continue
            start, end, strand = GENE_COORDS[g["gene"]]
            by_name[g["gene"]] = ResistanceGene.objects.create(
                gene=g["gene"], full_name=g["fullName"], associated_drugs=list(g["associatedDrugs"]),
                known_variants=int(g.get("knownVariants") or 0), start=start, end=end, strand=strand, order=i)
            genes_created += 1
        for row in Variant.objects.filter(label__in=list(EXTRA_VARIANT_DRUGS)):  # 기존 행 연관 약제 보정(멱등)
            want = list(dict.fromkeys(variant_drugs(row) + EXTRA_VARIANT_DRUGS[row.label]))
            if want != row.drugs:
                row.drugs = want
                row.save(update_fields=["drugs"])
        have = set(Variant.objects.values_list("label", flat=True))
        for i, v in enumerate(data["variants"]):
            label = mutation_label(v["gene"], v.get("aminoAcidChange"))
            if label in have or v["gene"] not in by_name:
                continue
            Variant.objects.create(
                gene=by_name[v["gene"]], position=int(v["position"]), ref_allele=v["refAllele"],
                alt_allele=v["altAllele"], amino_acid_change=v.get("aminoAcidChange") or "",
                drug_resistance=v["drugResistance"], annotation=v.get("annotation") or "", label=label,
                drugs=list(dict.fromkeys([v["drugResistance"]] + EXTRA_VARIANT_DRUGS.get(label, []))),
                confidence=SEED_CONFIDENCE.get(label, Variant.Confidence.MODERATE), order=i)
            have.add(label)
            vars_created += 1
        reset_sequences(ResistanceGene, Variant)
        mark_seeded(DATASET, {"genes": genes_created, "variants": vars_created})
    return {"genes": genes_created, "variants": vars_created, "skipped": False, "total": Variant.objects.count()}
