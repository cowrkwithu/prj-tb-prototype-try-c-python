"""DEPOT 데이터 탐색 서비스 — 시드, 저장 코호트 CRUD, 분석 집계, CSV 내보내기, 변이→코호트 연계.

원본 pages/Depot/{index,CohortAnalysis,SavedCohorts}.tsx, contexts/CohortProvider.tsx, utils/variantCohort.ts.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.cases.models import Patient
from apps.cases.services import data_label
from apps.core.seeding import is_seeded, mark_seeded, reset_sequences
from apps.uploads.services.csvio import safe_cell

from . import conditions as cond
from .models import Cohort

DATASET = "cohorts"
SEED_OWNER = "researcher"  # 원본 데모의 연구원 화면에서 만든 데이터셋으로 본다


# ═══════════════════════════════════════════════════════════
#  시드 (data/seed/cohorts.json ← 원본 src/data/cohorts.ts)
# ═══════════════════════════════════════════════════════════
def load_cohorts(path: Path | None = None, force: bool = False) -> dict:
    """저장 코호트 시드를 1회 적재한다(SeedLog). force=True 면 없는 id 만 다시 만든다."""
    if is_seeded(DATASET) and not force:
        return {"created": 0, "skipped": True, "total": Cohort.objects.count()}
    from apps.accounts.models import User

    path = Path(path or settings.SEED_DIR / "cohorts.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    owner = User.objects.filter(username=SEED_OWNER).first() or (
        User.objects.filter(role=User.Role.ADMIN).order_by("id").first()
        or User.objects.filter(is_superuser=True).order_by("id").first())
    if owner is None:
        # 소유자 없는 코호트는 만들지 않는다 — 적재 기록도 남기지 않아 계정이 생긴 뒤 다시 적재된다
        return {"created": 0, "skipped": False, "owner": "없음 — 건너뜀(계정 생성 후 다시 실행)",
                "total": Cohort.objects.count()}
    created = 0
    with transaction.atomic():
        have = set(Cohort.objects.values_list("id", flat=True))
        for c in data:
            if c["id"] in have:
                continue
            Cohort.objects.create(
                id=c["id"], name=c["name"], name_en=c.get("nameEn") or "",
                description=c.get("description") or "", description_en=c.get("descriptionEn") or "",
                owner=owner, conditions=cond.validate(c["conditions"]),
                snapshot_ids=[int(i) for i in c.get("patientIds", [])],
                snapshot_countries=snapshot_countries([int(i) for i in c.get("patientIds", [])]),
                created_date=date.fromisoformat(c["createdDate"]),
            )
            created += 1
        reset_sequences(Cohort)
        mark_seeded(DATASET, {"created": created})
    return {"created": created, "skipped": False, "total": Cohort.objects.count()}


# ═══════════════════════════════════════════════════════════
#  저장 코호트
# ═══════════════════════════════════════════════════════════
def localized(c: Cohort, lang: str) -> dict:
    """원본 cohortText — 영어 화면이면 *_en(없으면 한국어)."""
    if lang == "en":
        return {"name": c.name_en or c.name, "description": c.description_en or c.description}
    return {"name": c.name, "description": c.description}


def can_edit(user, c: Cohort) -> bool:
    """이름 변경·삭제·CSV 내보내기는 소유자만(관리자도 남의 데이터셋은 바꾸지 않는다)."""
    return bool(user and user.is_authenticated and c.owner_id == user.pk)


def visible_cohorts():
    """원본은 모든 사용자가 같은 목록을 본다 → 로그인 사용자 전체 공개."""
    return Cohort.objects.select_related("owner").all()


def builder_state(c: Cohort) -> dict:
    """불러오기 시 빌더 복원 형태: 폼으로 나타낼 수 있으면 filters, 아니면 None(고급 조건 — 서버가 loaded=<id> 로 복원)."""
    tree = cond.validate(c.conditions)
    f = cond.tree_to_filters(tree)
    return {"filters": f, "params": cond.filters_to_params(f) if f is not None else [("loaded", str(c.pk))]}


def load_url(c: Cohort) -> str:
    """불러오기 주소 — 조건 원문은 싣지 않는다(서버가 저장된 조건을 복원)."""
    return f"/depot?loaded={c.pk}&run=1&panel=saved"


# ═══════════════════════════════════════════════════════════
#  조건 해석 단일 진입점 — 뷰는 이 함수만 쓴다(validate·ConditionError 를 뷰에서 다루지 않음)
# ═══════════════════════════════════════════════════════════
FORM_KEYS = frozenset(set(cond.PARAM.values()) | {"logic", "age_min", "age_max", "lineage", "cxr_min"})


def stored_tree(c: Cohort) -> dict | None:
    """저장된 조건을 검증해 돌려준다(손상된 조건이면 None)."""
    try:
        return cond.validate(c.conditions)
    except cond.ConditionError:
        return None


def resolve_conditions(qd, lang: str) -> dict:
    """쿼리(GET 또는 저장 폼의 query) → 조건 상태. 어떤 입력에도 예외를 내지 않는다.

    우선순위: tree=JSON(고급 조건) > 빌더 폼 파라미터 > loaded=<id>(저장 데이터셋의 조건을 서버가 복원).
    반환: filters(폼 상태, 고급 조건이면 None), tree(검증된 트리), error(문구 또는 None),
          source("tree" | "form" | "cohort" | "none"), params(주소·저장 폼용 상태 파라미터), cohort(조건 출처 데이터셋)
    오류면 '조건 없음' 상태 + 오류 문구 — 화면은 오류를 보이고 저장 버튼을 막는다.
    """
    def failed(ko, en):
        f = cond.default_filters()
        return {"filters": f, "tree": {"op": "AND", "children": []}, "error": en if lang == "en" else ko,
                "source": "none", "params": [], "cohort": None}

    try:
        if qd.get("tree"):
            tree = cond.validate(cond.parse_json(qd.get("tree")))
            return {"filters": None, "tree": tree, "error": None, "source": "tree",
                    "params": [("tree", cond.serialize(tree))], "cohort": None}
        if FORM_KEYS.intersection(qd.keys()):
            f = cond.filters_from_query(qd)
            return {"filters": f, "tree": cond.filters_to_tree(f), "error": None, "source": "form",
                    "params": cond.filters_to_params(f), "cohort": None}
        if "loaded" in qd:
            cid = cond.parse_uint(qd.get("loaded"))
            c = Cohort.objects.filter(pk=cid).first() if cid is not None else None
            if c is None:
                return failed("불러온 데이터셋을 찾을 수 없습니다.", "The loaded dataset could not be found.")
            tree = cond.validate(c.conditions)
            # 조건 출처는 이 데이터셋 — 주소·저장 폼에는 loaded=<id> 만 싣는다.
            # 폼으로 나타낼 수 있으면 폼 컨트롤에도 채운다(사용자가 바꾸면 그때부터 일반 폼 조건).
            return {"filters": cond.tree_to_filters(tree), "tree": tree, "error": None, "source": "cohort",
                    "params": [("loaded", str(c.pk))], "cohort": c}
        f = cond.filters_from_query(qd)
        return {"filters": f, "tree": cond.filters_to_tree(f), "error": None, "source": "form",
                "params": cond.filters_to_params(f), "cohort": None}
    except cond.ConditionError as e:
        return failed(e.ko, e.en)


SNAPSHOT_CHUNK = 500  # 스냅샷 id 로 환자를 조회할 때 한 쿼리의 최대 파라미터 수


def snapshot_countries(ids: list[int]) -> list[str]:
    """스냅샷 환자들의 국적(처음 나온 순서). id 를 SNAPSHOT_CHUNK 개씩 나눠 조회 — 파라미터 수가 스냅샷 크기에 비례하지 않는다."""
    ids = [i for i in ids or [] if isinstance(i, int) and not isinstance(i, bool)]
    country_of = {}
    for k in range(0, len(ids), SNAPSHOT_CHUNK):
        country_of.update(Patient.objects.filter(pk__in=ids[k:k + SNAPSHOT_CHUNK]).values_list("id", "country"))
    return list(dict.fromkeys(country_of[i] for i in ids if i in country_of))


def saved_cards(user, lang: str) -> list[dict]:
    """저장 목록 카드. 국적 태그는 저장 시 요약해 둔 값(없으면 한 번 계산해 저장) — 조회 1회(코호트+소유자)."""
    cohorts = list(visible_cohorts())
    cards = []
    for c in cohorts:
        if c.snapshot_countries is None:
            c.snapshot_countries = snapshot_countries(c.snapshot_ids)
            Cohort.objects.filter(pk=c.pk).update(snapshot_countries=c.snapshot_countries)
        countries = c.snapshot_countries
        try:
            tree = cond.validate(c.conditions)
            lines = cond.condition_lines(tree, lang)
        except cond.ConditionError:
            lines = []
        cards.append({
            "obj": c, **localized(c, lang),
            "count": c.snapshot_count,
            "countries": [data_label(x, lang) for x in countries],
            "lines": lines,
            "mine": can_edit(user, c),
            "owner_name": c.owner.shown_name if c.owner else "—",
            "load_url": load_url(c),
        })
    return cards


def _auto_names(f: dict | None, tree: dict, n: int) -> dict:
    """원본 handleSaveDataset 의 자동 이름·설명(한·영 동시 저장)."""
    if f is not None:
        ko, en = cond.active_conditions(f, "ko"), cond.active_conditions(f, "en")
    else:
        ko, en = [cond.describe_tree(tree, "ko")], [cond.describe_tree(tree, "en")]
        ko, en = [x for x in ko if x], [x for x in en if x]
    logic = tree["op"]
    return {
        "name": (f"데이터셋 — {' · '.join(ko[:2])}" if ko else f"데이터셋 — {n}명")[:200],
        "name_en": (f"Dataset — {' · '.join(en[:2])}" if en else f"Dataset — {n} subjects")[:200],
        "description": (f"데이터 탐색 결과로 저장된 데이터셋. 조건({logic}): {' / '.join(ko)}" if ko
                        else "데이터 탐색 결과로 저장된 데이터셋. 조건 없음"),
        "description_en": (f"Dataset saved from Data Explorer results. Conditions ({logic}): {' / '.join(en)}" if en
                           else "Dataset saved from Data Explorer results. No conditions"),
    }


def save_cohort(owner, tree: dict, filters: dict | None = None, name: str = "") -> Cohort:
    """현재 조건과 그 시점 대상자(스냅샷)를 저장한다. 이름을 주지 않으면 원본처럼 조건으로 자동 생성."""
    tree = cond.validate(tree)
    pairs = list(cond.matching_patients(tree).values_list("id", "country"))
    ids = [i for i, _ in pairs]
    names = _auto_names(filters, tree, len(ids))
    name = clean_name(name) if name else ""
    if name:
        names["name"], names["name_en"] = name, ""
    return Cohort.objects.create(owner=owner, conditions=tree, snapshot_ids=ids,
                                 snapshot_countries=list(dict.fromkeys(c for _, c in pairs)),
                                 created_date=timezone.localdate(), **names)


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_name(v: str) -> str:
    return _CTRL.sub(" ", (v or "")).strip()[:200]


def rename_cohort(c: Cohort, name: str, name_en: str = "") -> None:
    name = clean_name(name)
    if not name:
        raise ValueError("empty")
    c.name, c.name_en = name, clean_name(name_en)
    c.save(update_fields=["name", "name_en", "updated_at"])


# ═══════════════════════════════════════════════════════════
#  CSV 내보내기 (2단계 csvio.safe_cell 로 수식 주입 방어)
# ═══════════════════════════════════════════════════════════
EXPORT_COLUMNS = [
    ("condition_id", "condition_id"), ("age", "age"), ("sex", "sex"), ("country", "country"), ("site", "site"),
    ("diagnosis_type", "diagnosis_type"), ("treatment_outcome", "treatment_outcome"),
    ("treatment_duration", "treatment_duration"), ("comorbidities", "comorbidities"),
    ("has_sequence", "has_sequence"), ("lineage", "lineage"), ("resistance_mutations", "resistance_mutations"),
    ("has_cxr", "has_cxr"), ("has_ct", "has_ct"), ("has_pet_ct", "has_pet_ct"), ("cxr_count", "cxr_count"),
    ("ct_count", "ct_count"), ("pet_ct_count", "pet_ct_count"), ("cxr_findings", "cxr_findings"),
    ("registration_date", "registration_date"),
]


def export_csv(c: Cohort, tree: dict) -> str:
    """현재 조건(검증된 tree — stored_tree(c))으로 다시 계산한 대상자를 CSV 로. 조회 1회."""
    cols = [k for k, _ in EXPORT_COLUMNS]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for row in cond.matching_patients(tree).values_list(*cols):
        out = []
        for v in row:
            if isinstance(v, list):
                v = ";".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = "Y" if v else "N"
            out.append(safe_cell(v))
        w.writerow(out)
    return "﻿" + buf.getvalue()


# ═══════════════════════════════════════════════════════════
#  분석 집계 (원본 CohortAnalysis 5탭) — 한 번 조회한 값으로 계산
# ═══════════════════════════════════════════════════════════
ANALYSIS_FIELDS = ("id", "condition_id", "age", "sex", "country", "diagnosis_type", "treatment_outcome",
                   "treatment_duration", "comorbidities", "has_sequence", "lineage", "resistance_mutations",
                   "has_cxr", "has_ct", "has_pet_ct", "cxr_count", "cxr_findings")
OUTCOME_ORDER = ["Cured", "Completed", "Failed", "Died", "LTFU"]
DIAG_COLORS = {"DS-TB": "#10B981", "MDR-TB": "#F59E0B", "Pre-XDR-TB": "#F97316", "XDR-TB": "#EF4444"}
OUTCOME_COLORS = {"Cured": "#10B981", "Completed": "#2A7F8E", "Failed": "#EF4444", "Died": "#1B3A5C",
                  "LTFU": "#94A3B8", "Ongoing": "#CBD5E1"}
COLORS = ["#2A7F8E", "#1B3A5C", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#EC4899", "#6366F1"]
PATIENT_LIST_LIMIT = 50


def fetch_rows(qs) -> list[dict]:
    return list(qs.values(*ANALYSIS_FIELDS))


def summary(rows, lang) -> dict:
    n = len(rows)
    m = sum(1 for r in rows if r["sex"] == "M")
    countries = Counter(r["country"] for r in rows)
    country_sorted = sorted(countries.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "n": n,
        "avg_age": round(sum(r["age"] for r in rows) / n) if n else 0,
        "male_ratio": round(m / n * 100) if n else 0,
        "seq": sum(1 for r in rows if r["has_sequence"]),
        "sex": {"labels": ["Male", "Female"] if lang == "en" else ["남성", "여성"], "data": [m, n - m]},
        "country": {"labels": [data_label(c, lang) for c, _ in country_sorted], "data": [v for _, v in country_sorted]},
        "patients": [{"id": r["id"], "condition_id": r["condition_id"], "age": r["age"], "sex": r["sex"],
                      "country": data_label(r["country"], lang), "diagnosis_type": r["diagnosis_type"],
                      "outcome": r["treatment_outcome"]} for r in rows[:PATIENT_LIST_LIMIT]],
        "more": max(0, n - PATIENT_LIST_LIMIT),
    }


def clinical(rows) -> dict:
    diag = Counter(r["diagnosis_type"] for r in rows)
    diag_keys = list(diag)  # 원본: 처음 나온 순서
    by = {d: Counter() for d in diag_keys}
    for r in rows:
        by[r["diagnosis_type"]][r["treatment_outcome"]] += 1
    outcomes = list(OUTCOME_ORDER)
    if any(c.get("Ongoing") for c in by.values()):
        outcomes.append("Ongoing")  # 업로드로 생기는 '치료 중' 도 보이게
    return {
        "diag": {"labels": diag_keys, "data": [diag[k] for k in diag_keys],
                 "colors": [DIAG_COLORS.get(k, "#94A3B8") for k in diag_keys]},
        "outcome": {"labels": diag_keys,
                    "series": [{"label": o, "data": [by[d].get(o, 0) for d in diag_keys],
                                "color": OUTCOME_COLORS[o]} for o in outcomes]},
        "table": [{"type": d, "counts": [by[d].get(o, 0) for o in outcomes], "total": diag[d]} for d in diag_keys],
    }


def genomics(rows) -> dict:
    seq = [r for r in rows if r["has_sequence"]]
    muts = Counter(m for r in seq for m in (r["resistance_mutations"] or []))
    top = sorted(muts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    lineages = Counter(r["lineage"] for r in seq if r["lineage"])
    lin = sorted(lineages.items(), key=lambda kv: (-kv[1], kv[0]))
    n = len(seq)
    return {
        "seq_count": n,
        "lineage": {"labels": [k for k, _ in lin], "data": [v for _, v in lin]},
        "mutations": [{"mutation": m, "count": c, "ratio": c / n * 100 if n else 0,
                       "alpha": round(min(c / n * 3, 1), 3) if n else 0,
                       "width": round(min(max(c / n * 100 * 3, 10), 100), 1) if n else 10} for m, c in top],
    }


def imaging(rows, lang) -> dict:
    cxr = [r for r in rows if r["has_cxr"]]
    findings = Counter(f for r in cxr for f in (r["cxr_findings"] or []))
    fs = sorted(findings.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "cxr": len(cxr), "ct": sum(1 for r in rows if r["has_ct"]), "pet": sum(1 for r in rows if r["has_pet_ct"]),
        "cxr_images": sum(r["cxr_count"] for r in cxr),
        "findings": {"labels": [data_label(k, lang) for k, _ in fs], "data": [v for _, v in fs],
                     "colors": [COLORS[i % len(COLORS)] for i in range(len(fs))]},
    }


# ── 통계 검정 (원본은 고정 p-value 모의값 → 실제 검정으로 계산) ──
FAVORABLE = {"Cured", "Completed"}
UNFAVORABLE = {"Failed", "Died", "LTFU"}


def _log_gamma(x: float) -> float:
    return math.lgamma(x)


def incomplete_beta(x: float, a: float, b: float) -> float:
    """정규화 불완전 베타 함수 I_x(a, b) — 원본 incompleteBeta 와 같은 연분수 전개."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(_log_gamma(a + b) - _log_gamma(a) - _log_gamma(b) + a * math.log(x) + b * math.log(1 - x))

    def cf(xx, aa, bb):
        tiny = 1e-30
        c, d = 1.0, 1 - (aa + bb) * xx / (aa + 1)
        d = 1 / (d if abs(d) > tiny else tiny)
        h = d
        for m in range(1, 201):
            m2 = 2 * m
            num = m * (bb - m) * xx / ((aa + m2 - 1) * (aa + m2))
            d = 1 + num * d
            d = tiny if abs(d) < tiny else d
            c = 1 + num / c
            c = tiny if abs(c) < tiny else c
            d = 1 / d
            h *= d * c
            num = -(aa + m) * (aa + bb + m) * xx / ((aa + m2) * (aa + m2 + 1))
            d = 1 + num * d
            d = tiny if abs(d) < tiny else d
            c = 1 + num / c
            c = tiny if abs(c) < tiny else c
            d = 1 / d
            delta = d * c
            h *= delta
            if abs(delta - 1) < 3e-12:
                break
        return h

    if x < (a + 1) / (a + b + 2):
        return front * cf(x, a, b) / a
    return 1 - front * cf(1 - x, b, a) / b


def welch_t(x: list[float], y: list[float]):
    """Welch t-검정 (t, 양측 p). 표본이 2 미만이거나 분산이 모두 0 이면 None."""
    n1, n2 = len(x), len(y)
    if n1 < 2 or n2 < 2:
        return None
    m1, m2 = sum(x) / n1, sum(y) / n2
    v1 = sum((a - m1) ** 2 for a in x) / (n1 - 1)
    v2 = sum((a - m2) ** 2 for a in y) / (n2 - 1)
    se2 = v1 / n1 + v2 / n2
    if se2 == 0:
        return None
    t = (m1 - m2) / math.sqrt(se2)
    df = se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    return t, incomplete_beta(df / (df + t * t), df / 2, 0.5)


def _lcomb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact(a: int, b: int, c: int, d: int):
    """2×2 Fisher 정확검정 (오즈비, 양측 p). [[a, b], [c, d]]

    초기하 확률을 lgamma 로그값으로 계산한다(O(n) 부동소수 — 큰 정수 이항계수를 만들지 않음).
    양측 p = 관측 확률 이하인 모든 표의 확률 합(scipy 와 같은 상대 허용오차 1e-7).
    """
    r1, r2, c1 = a + b, c + d, a + c
    n = r1 + r2
    lo, hi = max(0, c1 - r2), min(r1, c1)
    base = _lcomb(n, c1)
    logp = [_lcomb(r1, x) + _lcomb(r2, c1 - x) - base for x in range(lo, hi + 1)]
    lp_obs = logp[a - lo]
    limit = lp_obs + math.log1p(1e-7)
    p = math.fsum(math.exp(v) for v in logp if v <= limit)
    odds = ((a + 0.5) * (d + 0.5)) / ((b + 0.5) * (c + 0.5))  # 0 칸 보정(Haldane)
    return odds, min(p, 1.0)


STAT_VARS = [
    # (ko, en, 종류, 값 함수)
    ("성별 (M/F)", "Sex (M/F)", "fisher", lambda r: r["sex"] == "M"),
    ("평균 연령", "Mean Age", "t", lambda r: r["age"]),
    ("약제내성 유형 (DS vs DR)", "Drug Resistance Type (DS vs DR)", "fisher", lambda r: r["diagnosis_type"] != "DS-TB"),
    ("치료 기간", "Treatment Duration", "t", lambda r: r["treatment_duration"]),
    ("CXR 공동 유무", "CXR Cavity", "fisher", lambda r: "공동" in (r["cxr_findings"] or [])),
    ("Lineage (L2 vs others)", "Lineage (L2 vs others)", "fisher", lambda r: (r["lineage"] or "").startswith("L2")),
    ("동반질환 유무", "Has Comorbidity", "fisher", lambda r: bool(r["comorbidities"])),
    ("시퀀스 보유 여부", "Has Sequence", "fisher", lambda r: bool(r["has_sequence"])),
    ("CT 보유 여부", "Has CT", "fisher", lambda r: bool(r["has_ct"])),
]


def statistics(rows, lang) -> dict:
    """각 변수와 치료 결과(성공: Cured·Completed / 실패: Failed·Died·LTFU)의 연관성 검정.

    원본처럼 데이터셋 안에서 값이 한 가지뿐인 변수는 뺀다. 치료 중(Ongoing)은 결과 미확정이라 제외.
    """
    fav = [r for r in rows if r["treatment_outcome"] in FAVORABLE]
    unf = [r for r in rows if r["treatment_outcome"] in UNFAVORABLE]
    result = {"n_fav": len(fav), "n_unf": len(unf), "rows": [], "testable": bool(fav and unf)}
    if not result["testable"]:
        return result
    out = []
    for ko, en, kind, get in STAT_VARS:
        values = [get(r) for r in fav + unf]
        if len(set(values)) < 2:
            continue
        if kind == "t":
            res = welch_t([get(r) for r in fav], [get(r) for r in unf])
            if res is None:  # 한 군이 2명 미만이거나 두 군 분산이 모두 0 → 행은 남기고 '검정 불가'
                out.append({"variable": en if lang == "en" else ko, "test": "T-test (Welch)", "stat": "—",
                            "p": None, "p_text": "N/A" if lang == "en" else "검정 불가", "significant": False})
                continue
            stat, p = res
        else:
            a = sum(1 for r in fav if get(r))
            b = len(fav) - a
            c = sum(1 for r in unf if get(r))
            d = len(unf) - c
            stat, p = fisher_exact(a, b, c, d)
        out.append({"variable": en if lang == "en" else ko, "test": "T-test (Welch)" if kind == "t" else "Fisher Exact",
                    "stat": f"{stat:.3f}", "p": p, "p_text": "< 0.001" if p < 0.001 else f"{p:.3f}",
                    "significant": p < 0.05})
    result["rows"] = sorted(out, key=lambda x: (x["p"] is None, x["p"] or 0))
    return result


TABS = [
    ("summary", "요약", "Summary", "대상자 수와 국적·성별·연령 등 데이터셋 개요를 봅니다.",
     "View an overview of the dataset: subject count, nationality, sex, age and more."),
    ("clinical", "임상 비교", "Clinical", "약제내성 유형·치료결과 등 임상 정보 분포를 비교합니다.",
     "Compare distributions of clinical data such as resistance type and treatment outcome."),
    ("genomic", "유전체", "Genomic", "Lineage와 내성 변이 등 유전체 분석 결과를 봅니다.",
     "View genomic results such as lineages and resistance mutations."),
    ("imaging", "영상", "Imaging", "CXR·CT·PET-CT 보유 현황과 영상 소견 분포를 봅니다.",
     "View CXR, CT and PET-CT availability and imaging findings."),
    ("stats", "통계", "Statistics", "주요 변수와 치료 결과의 연관성에 대한 통계 검정 결과를 봅니다.",
     "View statistical test results for associations between key variables and treatment outcome."),
]
TAB_KEYS = [t[0] for t in TABS]


def analyze(rows, tab: str, lang: str) -> dict:
    if tab == "clinical":
        return clinical(rows)
    if tab == "genomic":
        return genomics(rows)
    if tab == "imaging":
        return imaging(rows, lang)
    if tab == "stats":
        return statistics(rows, lang)
    return summary(rows, lang)


def mutation_options() -> list[str]:
    """원본: 환자 데이터에 실제로 있는 내성 변이만 선택지로(빈 코호트 방지). 조회 1회."""
    seen = set()
    for lst in Patient.objects.values_list("resistance_mutations", flat=True):
        for m in lst or []:
            if isinstance(m, str):
                seen.add(m)
    return sorted(seen)


# ═══════════════════════════════════════════════════════════
#  변이 → 코호트 연계 (원본 utils/variantCohort.ts) — 4단계 GAP 화면이 쓰는 인터페이스
# ═══════════════════════════════════════════════════════════
def mutation_label(gene: str, amino_acid_change: str | None) -> str:
    """{'gene': 'inhA', 'aminoAcidChange': 'c-15t (promoter)'} → 'inhA c-15t'"""
    aa = re.sub(r"\s*\(.*\)\s*$", "", amino_acid_change or "").strip()
    return f"{gene} {aa}" if aa else gene


def mutation_labels(variants) -> list[str]:
    return list(dict.fromkeys(mutation_label(v["gene"], v.get("aminoAcidChange")) for v in variants))


def depot_url_for_mutations(labels: list[str]) -> str:
    """데이터 탐색으로 넘기는 주소 (`/depot?mutations=rpoB S450L,katG S315T`)."""
    return "/depot?" + urlencode({"mutations": ",".join(labels)}, quote_via=quote)


def mutation_cohort_name(labels: list[str], lang: str = "ko") -> str:
    if lang == "en":
        if len(labels) == 1:
            return f"Dataset with {labels[0]}"
        k = len(labels) - 1
        return f"Dataset with {labels[0]} and {k} more mutation{'s' if k > 1 else ''}"
    if len(labels) == 1:
        return f"{labels[0]} 보유 데이터셋"
    return f"{labels[0]} 외 {len(labels) - 1}건 변이 보유 데이터셋"


def save_mutation_cohort(owner, labels: list[str]) -> Cohort:
    """변이 검색 결과를 '하나라도 보유' 조건의 저장 코호트로 만든다(원본 findPatientIdsByMutations: OR)."""
    f = cond.default_filters()
    f["mutations"] = list(dict.fromkeys(labels))
    tree = cond.filters_to_tree(f)
    c = save_cohort(owner, tree, f)
    c.name, c.name_en = mutation_cohort_name(labels, "ko")[:200], mutation_cohort_name(labels, "en")[:200]
    c.save(update_fields=["name", "name_en"])
    return c
