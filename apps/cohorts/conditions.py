"""코호트 조건 트리 — 허용 목록 검증 · ORM 쿼리(Q) 변환 · 빌더 폼 ↔ 트리 변환.

원본 pages/Depot/cohortFilters.ts(CohortFilters, toCohortConditions, getActiveConditions)와
pages/Depot/index.tsx filterPatients 를 서버측으로 옮겼다.

조건 트리 형식
  그룹: {"op": "AND" | "OR", "children": [노드, ...]}
  조건: {"field": "<허용 필드>", "operator": "<허용 연산자>", "value": ...}
원본의 평면 목록([조건, ...])은 AND 그룹으로 받아들인다.

안전 규칙
  - 필드명·연산자는 FIELDS 허용 목록으로만 모델 컬럼에 매핑한다(사용자 입력을 ORM 키워드로 직접 쓰지 않음).
  - 값은 필드 종류별로 형식·길이·개수를 검증한다.
  - 트리 깊이(MAX_DEPTH)·노드 수(MAX_NODES)·원문 길이(MAX_JSON_CHARS)를 제한한다.
  - JSONField 다중값(동반질환·내성변이·CXR 소견)의 '하나라도 포함' 은 커스텀 Lookup `has_any_value`
    (apps.cohorts.lookups — SQLite json_each / PostgreSQL ?|)로 DB 안에서 평가한다.
    SQL 파라미터 수는 선택한 값 개수에만 비례하고(트리 전체 MAX_TOTAL_VALUES 이하), 환자 수와 무관하다.
"""
from __future__ import annotations

import json
import re
from django.db.models import Q

from apps.cases.models import Patient
from apps.core.safejson import loads_user_json
from apps.cases.services import data_label

MAX_DEPTH = 5          # 루트 그룹 = 1
MAX_NODES = 100        # 그룹 + 조건 전체
MAX_LIST = 50          # 조건 하나의 값 개수
MAX_STR = 100          # 문자열 값 길이
MAX_JSON_CHARS = 8000  # URL·폼으로 받는 트리 원문 길이
MAX_TOTAL_VALUES = 500  # 트리 전체 값 개수(= SQL 파라미터 수 상한의 근거)
_UINT = re.compile(r"[0-9]{1,10}")


def parse_uint(v) -> int | None:
    """정수 쿼리 파라미터: ASCII 숫자 1~10자리만(유니코드 숫자 '²'·'٣' 등 거부)."""
    if isinstance(v, str) and _UINT.fullmatch(v.strip()):
        return int(v.strip())
    return None

GROUP_OPS = ("AND", "OR")


class ConditionError(ValueError):
    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en

    def text(self, lang: str) -> str:
        return self.en if lang == "en" else self.ko


# ── 허용 필드 ───────────────────────────────────────────────
# kind: choice(문자열, 선택지 고정) / str(자유 문자열, 패턴) / int / bool / json_any(JSON 목록 중 하나라도 포함)
#       has_list(JSON 목록이 비었는지 여부 — 원본 hasMutation)
OUTCOMES = [c for c, _ in Patient.Outcome.choices]
DIAG_TYPES = [c for c, _ in Patient.DiagnosisType.choices]
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f<>\ud800-\udfff]{1,%d}$" % MAX_STR)
_LINEAGE_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")

FIELDS: dict[str, dict] = {
    "demographics.country": {"col": "country", "kind": "choice", "ops": ("eq", "in"), "choices": Patient.COUNTRIES,
                             "label": ("국적", "Nationality")},
    "demographics.age": {"col": "age", "kind": "int", "ops": ("eq", "gte", "lte", "between"), "min": 0, "max": 150,
                         "label": ("연령", "Age")},
    "demographics.sex": {"col": "sex", "kind": "choice", "ops": ("eq", "in"), "choices": ["M", "F"],
                         "label": ("성별", "Sex")},
    "clinical.diagnosisType": {"col": "diagnosis_type", "kind": "choice", "ops": ("eq", "in"), "choices": DIAG_TYPES,
                               "label": ("내성", "Resistance")},
    "clinical.treatmentOutcome": {"col": "treatment_outcome", "kind": "choice", "ops": ("eq", "in"),
                                  "choices": OUTCOMES, "label": ("결과", "Outcome")},
    "clinical.comorbidities": {"col": "comorbidities", "kind": "json_any", "ops": ("in",),
                               "label": ("동반질환", "Comorbidities")},
    "genomics.hasSequence": {"col": "has_sequence", "kind": "bool", "ops": ("eq",), "label": ("시퀀스", "Sequence")},
    "genomics.lineage": {"col": "lineage", "kind": "str", "ops": ("eq",), "pattern": _LINEAGE_RE,
                         "label": ("Lineage", "Lineage")},
    "genomics.hasMutation": {"col": "resistance_mutations", "kind": "has_list", "ops": ("eq",),
                             "label": ("내성 변이 보유", "Has Mutation")},
    "genomics.resistanceMutations": {"col": "resistance_mutations", "kind": "json_any", "ops": ("in",),
                                     "label": ("변이", "Mutations")},
    "imaging.hasCxr": {"col": "has_cxr", "kind": "bool", "ops": ("eq",), "label": ("CXR", "CXR")},
    "imaging.hasCt": {"col": "has_ct", "kind": "bool", "ops": ("eq",), "label": ("CT", "CT")},
    "imaging.hasPetCt": {"col": "has_pet_ct", "kind": "bool", "ops": ("eq",), "label": ("PET-CT", "PET-CT")},
    "imaging.cxrCount": {"col": "cxr_count", "kind": "int", "ops": ("eq", "gte", "lte", "between"), "min": 0,
                         "max": 100000, "label": ("CXR 장수", "CXR Count")},
    "imaging.ctCount": {"col": "ct_count", "kind": "int", "ops": ("eq", "gte", "lte", "between"), "min": 0,
                        "max": 100000, "label": ("CT 장수", "CT Count")},
    "imaging.petCtCount": {"col": "pet_ct_count", "kind": "int", "ops": ("eq", "gte", "lte", "between"), "min": 0,
                           "max": 100000, "label": ("PET-CT 장수", "PET-CT Count")},
    "imaging.cxrFindings": {"col": "cxr_findings", "kind": "json_any", "ops": ("in",),
                            "label": ("CXR 소견", "CXR Findings")},
}
JSON_COLUMNS = ("comorbidities", "resistance_mutations", "cxr_findings")
_INT_OPS = {"eq": "exact", "gte": "gte", "lte": "lte"}


# ═══════════════════════════════════════════════════════════
#  검증 · 정규화
# ═══════════════════════════════════════════════════════════
def parse_json(text: str):
    """URL·폼으로 받은 트리 원문을 파싱한다(길이 제한, 깊은 중첩으로 인한 RecursionError 도 거부)."""
    if text is None or not str(text).strip():
        return None
    text = str(text)
    # 원문은 공백 등을 감안해 넉넉히 받고, 실제 상한은 validate() 가 정규화·직렬화한 결과로 검사한다
    if len(text) > MAX_JSON_CHARS * 4:
        raise ConditionError(f"조건이 너무 깁니다(최대 {MAX_JSON_CHARS:,}자).",
                             f"Conditions are too long (max {MAX_JSON_CHARS:,} characters).")
    try:
        return loads_user_json(text)  # 서로게이트·NUL 이 든 문자열·키는 ValueError
    except (ValueError, RecursionError):
        raise ConditionError("조건 형식이 올바르지 않습니다.", "Malformed conditions.") from None


def _bad(ko, en):
    raise ConditionError(ko, en)


def _str_value(v, spec, fname):
    if not isinstance(v, str):
        _bad(f"{fname}: 문자열 값이 필요합니다.", f"{fname}: a string value is required.")
    v = v.strip()
    if spec["kind"] == "choice":
        if v not in spec["choices"]:
            _bad(f"{fname}: 허용되지 않은 값입니다.", f"{fname}: value not allowed.")
    elif spec["kind"] == "str":
        if not spec["pattern"].match(v):
            _bad(f"{fname}: 값 형식이 올바르지 않습니다.", f"{fname}: invalid value format.")
    elif not _LABEL_RE.match(v):
        _bad(f"{fname}: 값 형식이 올바르지 않습니다.", f"{fname}: invalid value format.")
    return v


def _int_value(v, spec, fname):
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, str) and re.fullmatch(r"[0-9]{1,6}", v.strip()):
            v = int(v.strip())
        else:
            _bad(f"{fname}: 정수 값이 필요합니다.", f"{fname}: an integer value is required.")
    if not spec["min"] <= v <= spec["max"]:
        _bad(f"{fname}: 값 범위를 벗어났습니다({spec['min']}~{spec['max']}).",
             f"{fname}: value out of range ({spec['min']}–{spec['max']}).")
    return v


def _leaf(node: dict) -> dict:
    extra = set(node) - {"field", "operator", "value"}
    if extra:
        _bad("조건에 알 수 없는 키가 있습니다.", "Unknown keys in condition.")
    fname, op = node.get("field"), node.get("operator")
    if not isinstance(fname, str) or fname not in FIELDS:
        _bad("허용되지 않은 조건 항목입니다.", "Condition field not allowed.")
    spec = FIELDS[fname]
    if op not in spec["ops"]:
        _bad(f"{fname}: 허용되지 않은 연산자입니다.", f"{fname}: operator not allowed.")
    v = node.get("value")
    kind = spec["kind"]
    if kind in ("bool", "has_list"):
        if not isinstance(v, bool):
            _bad(f"{fname}: true/false 값이 필요합니다.", f"{fname}: a true/false value is required.")
    elif kind == "int":
        if op == "between":
            if not isinstance(v, list) or len(v) != 2:
                _bad(f"{fname}: [최소, 최대] 값이 필요합니다.", f"{fname}: [min, max] is required.")
            v = [_int_value(x, spec, fname) for x in v]
        else:
            v = _int_value(v, spec, fname)
    elif op == "in":
        if not isinstance(v, list) or not v:
            _bad(f"{fname}: 값 목록이 필요합니다.", f"{fname}: a list of values is required.")
        if len(v) > MAX_LIST:
            _bad(f"{fname}: 값은 최대 {MAX_LIST}개입니다.", f"{fname}: at most {MAX_LIST} values.")
        v = list(dict.fromkeys(_str_value(x, spec, fname) for x in v))
    else:
        v = _str_value(v, spec, fname)
    return {"field": fname, "operator": op, "value": v}


def validate(tree) -> dict:
    """트리를 검증·정규화해 {"op", "children"} 루트 그룹으로 돌려준다. 위반이면 ConditionError."""
    if tree is None:
        return {"op": "AND", "children": []}
    if isinstance(tree, list):
        tree = {"op": "AND", "children": tree}
    count = [0]

    def walk(node, depth):
        count[0] += 1
        if count[0] > MAX_NODES:
            _bad(f"조건이 너무 많습니다(최대 {MAX_NODES}개).", f"Too many conditions (max {MAX_NODES}).")
        if not isinstance(node, dict):
            _bad("조건 형식이 올바르지 않습니다.", "Malformed conditions.")
        if "op" in node or "children" in node:
            if depth > MAX_DEPTH:
                _bad(f"조건 그룹은 최대 {MAX_DEPTH}단계까지 중첩할 수 있습니다.",
                     f"Condition groups can be nested at most {MAX_DEPTH} levels.")
            if set(node) - {"op", "children"}:
                _bad("조건 그룹에 알 수 없는 키가 있습니다.", "Unknown keys in condition group.")
            op = node.get("op")
            children = node.get("children")
            if op not in GROUP_OPS:
                _bad("조건 결합은 AND 또는 OR 만 쓸 수 있습니다.", "Groups must combine with AND or OR.")
            if not isinstance(children, list):
                _bad("조건 그룹 형식이 올바르지 않습니다.", "Malformed condition group.")
            if len(children) > MAX_NODES:
                _bad(f"조건이 너무 많습니다(최대 {MAX_NODES}개).", f"Too many conditions (max {MAX_NODES}).")
            return {"op": op, "children": [walk(c, depth + 1) for c in children]}
        return _leaf(node)

    root = walk(tree, 1)
    if _total_values(root) > MAX_TOTAL_VALUES:
        _bad(f"조건 값이 너무 많습니다(최대 {MAX_TOTAL_VALUES}개).", f"Too many condition values (max {MAX_TOTAL_VALUES}).")
    if "op" not in root:  # 루트가 조건 하나
        root = {"op": "AND", "children": [root]}
    # 길이 상한은 정규화 후 직렬화 결과 기준 — 화면에 열리는 조건은 저장·불러오기도 된다
    if len(serialize(root)) > MAX_JSON_CHARS:
        _bad(f"조건이 너무 깁니다(최대 {MAX_JSON_CHARS:,}자).",
             f"Conditions are too long (max {MAX_JSON_CHARS:,} characters).")
    return root


def serialize(tree) -> str:
    """정규화된 트리의 표준 직렬화(주소·저장·길이 검사 공통)."""
    return json.dumps(tree, ensure_ascii=False, separators=(",", ":"))


def _total_values(node) -> int:
    if "op" in node:
        return sum(_total_values(c) for c in node["children"])
    return len(node["value"]) if isinstance(node["value"], list) else 1


def count_nodes(tree) -> tuple[int, int]:
    """(노드 수, 깊이)."""
    if "op" not in tree:
        return 1, 0
    n, d = 1, 0
    for c in tree["children"]:
        cn, cd = count_nodes(c)
        n += cn
        d = max(d, cd)
    return n, d + 1


# ═══════════════════════════════════════════════════════════
#  ORM 변환
# ═══════════════════════════════════════════════════════════
class _Compiler:
    """트리 → Q. 결과 None 은 '전체(조건 없음)'."""

    def json_any(self, col: str, values: list[str]) -> Q:
        return Q(**{f"{col}__has_any_value": values})

    def leaf(self, node) -> Q:
        spec = FIELDS[node["field"]]
        col, op, v, kind = spec["col"], node["operator"], node["value"], spec["kind"]
        if kind == "json_any":
            return self.json_any(col, v)
        if kind == "has_list":
            empty = Q(**{col: []}) | Q(**{f"{col}__isnull": True})
            return ~empty if v else empty
        if op == "in":
            return Q(**{f"{col}__in": v})
        if op == "between":
            return Q(**{f"{col}__gte": v[0], f"{col}__lte": v[1]})
        if kind == "int":
            return Q(**{f"{col}__{_INT_OPS[op]}": v})
        return Q(**{col: v})

    def node(self, node) -> Q | None:
        if "op" not in node:
            return self.leaf(node)
        parts = [self.node(c) for c in node["children"]]
        if node["op"] == "AND":
            parts = [p for p in parts if p is not None]
            if not parts:
                return None
            q = parts[0]
            for p in parts[1:]:
                q &= p
            return q
        # OR: 원본처럼 하위 조건이 하나도 없거나 '전체' 인 항목이 있으면 전체
        if not parts or any(p is None for p in parts):
            return None
        q = parts[0]
        for p in parts[1:]:
            q |= p
        return q


def to_q(tree: dict) -> Q | None:
    return _Compiler().node(tree)


def matching_patients(tree: dict):
    """검증된 트리에 맞는 환자 QuerySet (조건이 없으면 전체 — 원본 filterPatients)."""
    q = to_q(tree)
    qs = Patient.objects.all()
    if q is not None:
        qs = qs.filter(q)
    return qs.order_by("id")


# ═══════════════════════════════════════════════════════════
#  빌더 폼 상태 (원본 CohortFilters) ↔ 트리
# ═══════════════════════════════════════════════════════════
COUNTRY_OPTIONS = ["한국", "몽골", "베트남", "인도", "남아공", "인도네시아", "필리핀", "미얀마"]
DIAG_OPTIONS = ["DS-TB", "MDR-TB", "Pre-XDR-TB", "XDR-TB"]
OUTCOME_OPTIONS = ["Cured", "Completed", "Failed", "Died", "LTFU"]
COMORB_OPTIONS = ["당뇨", "HIV", "COPD", "만성신부전", "간경변", "알코올 의존", "고혈압", "폐암"]
FINDING_OPTIONS = ["결절", "침윤", "공동", "흉수", "섬유화", "석회화", "무기폐", "기관지확장"]
LINEAGE_OPTIONS = ["L1.1.1", "L1.2.1", "L2.2.1", "L3.1.1", "L4.0", "L4.2.2", "L4.3.4.2"]

# 빌더 화면에 칩으로 보이는 선택지(변이·Lineage 는 선택된 값을 덧붙여 보이므로 제한 없음)
UI_OPTIONS = {"countries": COUNTRY_OPTIONS, "sex": ["M", "F"], "diagnosisTypes": DIAG_OPTIONS,
              "outcomes": OUTCOME_OPTIONS, "comorbidities": COMORB_OPTIONS, "cxrFindings": FINDING_OPTIONS}

LIST_KEYS = ("countries", "sex", "diagnosisTypes", "outcomes", "comorbidities", "mutations", "cxrFindings")
TRI_KEYS = ("hasSequence", "hasMutation", "hasCxr", "hasCt", "hasPetCt")
# 폼 키 → (트리 필드, 목록 연산자)
FORM_FIELD = {
    "countries": "demographics.country", "sex": "demographics.sex", "diagnosisTypes": "clinical.diagnosisType",
    "outcomes": "clinical.treatmentOutcome", "comorbidities": "clinical.comorbidities",
    "mutations": "genomics.resistanceMutations", "cxrFindings": "imaging.cxrFindings",
    "hasSequence": "genomics.hasSequence", "hasMutation": "genomics.hasMutation", "hasCxr": "imaging.hasCxr",
    "hasCt": "imaging.hasCt", "hasPetCt": "imaging.hasPetCt",
}
# URL 쿼리 파라미터 이름
PARAM = {
    "countries": "country", "sex": "sex", "diagnosisTypes": "diag", "outcomes": "outcome",
    "comorbidities": "comorb", "mutations": "mutations", "cxrFindings": "finding",
    "hasSequence": "seq", "hasMutation": "mut", "hasCxr": "cxr", "hasCt": "ct", "hasPetCt": "pet",
}


def default_filters() -> dict:
    return {"countries": [], "ageRange": [0, 100], "sex": [], "diagnosisTypes": [], "outcomes": [],
            "comorbidities": [], "hasSequence": None, "lineage": "", "hasMutation": None, "mutations": [],
            "hasCxr": None, "hasCt": None, "hasPetCt": None, "cxrFindings": [], "cxrMin": None, "logic": "AND"}


def _tri(v):
    return True if v == "1" else False if v == "0" else None


def _clamp_int(v, lo, hi, default):
    n = parse_uint(v)
    return default if n is None else min(max(n, lo), hi)


def filters_from_query(qd) -> dict:
    """GET 파라미터 → 폼 상태. 값은 필드 검증 규칙으로 걸러(허용 밖 값은 버림) 빌더에 그대로 되돌린다."""
    f = default_filters()
    # 다중값 누적 제한(조기 거부): 값 개수·글자 수 합이 트리 상한을 넘으면 정규화 전에 거부
    total_n = total_chars = 0
    for key in LIST_KEYS:
        raw = qd.getlist(PARAM[key])
        total_n += len(raw)
        total_chars += sum(len(v) for v in raw)
    if total_n > MAX_TOTAL_VALUES or total_chars > MAX_JSON_CHARS:
        _bad("선택한 조건 값이 너무 많거나 깁니다.", "Too many or too long condition values.")
    for key in LIST_KEYS:
        raw = qd.getlist(PARAM[key])
        if key == "mutations" and len(raw) == 1 and "," in raw[0]:
            raw = raw[0].split(",")  # 원본 GAP 연계: ?mutations=rpoB S450L,katG S315T
        spec = FIELDS[FORM_FIELD[key]]
        vals = []
        for v in raw[:MAX_LIST]:
            try:
                vals.append(_str_value(v, spec, key))
            except ConditionError:
                continue
        f[key] = list(dict.fromkeys(vals))
    for key in TRI_KEYS:
        f[key] = _tri(qd.get(PARAM[key]))
    lo = _clamp_int(qd.get("age_min"), 0, 150, 0)
    hi = _clamp_int(qd.get("age_max"), 0, 150, 100)
    f["ageRange"] = [lo, hi]
    lineage = (qd.get("lineage") or "").strip()
    f["lineage"] = lineage if _LINEAGE_RE.match(lineage or "-") and lineage else ""
    cmin = (qd.get("cxr_min") or "").strip()
    f["cxrMin"] = _clamp_int(cmin, 0, 100000, None) if cmin else None
    f["logic"] = "OR" if qd.get("logic") == "OR" else "AND"
    return f


def filters_to_tree(f: dict) -> dict:
    """원본 toCohortConditions (+ CXR 최소 장수). 결합은 f['logic'] 루트 그룹."""
    c = []
    if f["countries"]:
        c.append({"field": "demographics.country", "operator": "in", "value": f["countries"]})
    if f["ageRange"][0] > 0 or f["ageRange"][1] < 100:
        c.append({"field": "demographics.age", "operator": "between", "value": list(f["ageRange"])})
    for key in ("sex", "diagnosisTypes", "outcomes", "comorbidities"):
        if f[key]:
            c.append({"field": FORM_FIELD[key], "operator": "in", "value": f[key]})
    if f["hasSequence"] is not None:
        c.append({"field": "genomics.hasSequence", "operator": "eq", "value": f["hasSequence"]})
    if f["lineage"]:
        c.append({"field": "genomics.lineage", "operator": "eq", "value": f["lineage"]})
    if f["hasMutation"] is not None:
        c.append({"field": "genomics.hasMutation", "operator": "eq", "value": f["hasMutation"]})
    if f["mutations"]:
        c.append({"field": "genomics.resistanceMutations", "operator": "in", "value": f["mutations"]})
    for key in ("hasCxr", "hasCt", "hasPetCt"):
        if f[key] is not None:
            c.append({"field": FORM_FIELD[key], "operator": "eq", "value": f[key]})
    if f["cxrMin"] is not None:
        c.append({"field": "imaging.cxrCount", "operator": "gte", "value": f["cxrMin"]})
    if f["cxrFindings"]:
        c.append({"field": "imaging.cxrFindings", "operator": "in", "value": f["cxrFindings"]})
    return validate({"op": f["logic"], "children": c})


_REVERSE = {v: k for k, v in FORM_FIELD.items()}


def tree_to_filters(tree: dict) -> dict | None:
    """저장 코호트를 빌더 폼으로 복원한다. 폼으로 나타낼 수 없는 트리(중첩 그룹, 같은 항목 반복 등)는 None."""
    f = default_filters()
    f["logic"] = tree["op"]
    seen = set()
    for n in tree["children"]:
        if "op" in n or n["field"] in seen:
            return None
        seen.add(n["field"])
        fld, op, v = n["field"], n["operator"], n["value"]
        if fld == "demographics.age":
            if op == "between":
                f["ageRange"] = list(v)
            elif op == "gte":
                f["ageRange"] = [v, 150]
            elif op == "lte":
                f["ageRange"] = [0, v]
            else:
                f["ageRange"] = [v, v]
            if not (f["ageRange"][0] > 0 or f["ageRange"][1] < 100):
                return None  # [0, 100+] 은 폼에서 '조건 없음' 이라 같은 뜻이 아니다
        elif fld == "genomics.lineage":
            f["lineage"] = v
        elif fld == "imaging.cxrCount" and op == "gte":
            f["cxrMin"] = v
        elif fld in _REVERSE:
            key = _REVERSE[fld]
            if key in TRI_KEYS:
                f[key] = v
            else:
                f[key] = [v] if op == "eq" else list(v)
                shown = UI_OPTIONS.get(key)
                if shown is not None and not set(f[key]) <= set(shown):
                    return None  # 화면 선택지에 없는 값 — 폼으로 복원하면 다음 변경 때 빠지므로 고급 조건으로
        else:
            return None
    return f


def filters_to_params(f: dict) -> list[tuple[str, str]]:
    """폼 상태 → URL 쿼리 파라미터 쌍 (기본값은 생략)."""
    pairs = []
    if f["logic"] == "OR":
        pairs.append(("logic", "OR"))
    for key in ("countries",):
        pairs += [(PARAM[key], v) for v in f[key]]
    if f["ageRange"][0] > 0:
        pairs.append(("age_min", str(f["ageRange"][0])))
    if f["ageRange"][1] != 100:
        pairs.append(("age_max", str(f["ageRange"][1])))
    for key in ("sex", "diagnosisTypes", "outcomes", "comorbidities"):
        pairs += [(PARAM[key], v) for v in f[key]]
    if f["hasSequence"] is not None:
        pairs.append(("seq", "1" if f["hasSequence"] else "0"))
    if f["lineage"]:
        pairs.append(("lineage", f["lineage"]))
    if f["hasMutation"] is not None:
        pairs.append(("mut", "1" if f["hasMutation"] else "0"))
    pairs += [("mutations", v) for v in f["mutations"]]
    for key in ("hasCxr", "hasCt", "hasPetCt"):
        if f[key] is not None:
            pairs.append((PARAM[key], "1" if f[key] else "0"))
    if f["cxrMin"] is not None:
        pairs.append(("cxr_min", str(f["cxrMin"])))
    pairs += [("finding", v) for v in f["cxrFindings"]]
    return pairs


# ═══════════════════════════════════════════════════════════
#  표시 문구 (원본 getActiveConditions + 저장 목록의 조건 요약)
# ═══════════════════════════════════════════════════════════
def _vals(values, lang):
    return ", ".join(data_label(v, lang) for v in values)


def active_conditions(f: dict, lang: str) -> list[str]:
    en = lang == "en"
    c = []
    if f["countries"]:
        c.append(f"{'Nationality' if en else '국적'}: {_vals(f['countries'], lang)}")
    lo, hi = f["ageRange"]
    if lo > 0 or hi < 100:
        c.append(f"Age: {lo}–{hi}" if en else f"연령: {lo}~{hi}세")
    if f["sex"]:
        c.append(f"{'Sex' if en else '성별'}: {', '.join(f['sex'])}")
    if f["diagnosisTypes"]:
        c.append(f"{'Resistance' if en else '내성'}: {', '.join(f['diagnosisTypes'])}")
    if f["outcomes"]:
        c.append(f"{'Outcome' if en else '결과'}: {', '.join(f['outcomes'])}")
    if f["comorbidities"]:  # 원본 getActiveConditions 에는 빠져 있었으나 조건 칩에 보이도록 추가
        c.append(f"{'Comorbidities' if en else '동반질환'}: {_vals(f['comorbidities'], lang)}")
    if f["hasSequence"] is not None:
        c.append(f"{'Sequence' if en else '시퀀스'}: {'Yes' if f['hasSequence'] else 'No'}")
    if f["lineage"]:
        c.append(f"Lineage: {f['lineage']}")
    if f["hasMutation"] is not None:
        c.append(f"{'Has Mutation' if en else '내성 변이 보유'}: {'Yes' if f['hasMutation'] else 'No'}")
    if f["mutations"]:
        c.append(f"{'Mutations' if en else '변이'}: {', '.join(f['mutations'])}")
    for key, lab in (("hasCxr", "CXR"), ("hasCt", "CT"), ("hasPetCt", "PET-CT")):
        if f[key] is not None:
            c.append(f"{lab}: {'Yes' if f[key] else 'No'}")
    if f["cxrMin"] is not None:
        c.append(f"CXR ≥ {f['cxrMin']}" + ("" if en else "장"))
    if f["cxrFindings"]:
        c.append(f"{'CXR Findings' if en else 'CXR 소견'}: {_vals(f['cxrFindings'], lang)}")
    return c


_OP_SYMBOL = {"eq": "=", "gte": "≥", "lte": "≤", "in": "∈", "between": "∈"}


def describe_leaf(n: dict, lang: str) -> str:
    spec = FIELDS[n["field"]]
    label = spec["label"][1 if lang == "en" else 0]
    v = n["value"]
    if isinstance(v, bool):
        val = "Yes" if v else "No"
        return f"{label}: {val}"
    if n["operator"] == "between":
        return f"{label}: {v[0]}–{v[1]}"
    if isinstance(v, list):
        return f"{label}: {_vals(v, lang)}"
    if n["operator"] == "eq":
        return f"{label}: {data_label(v, lang) if isinstance(v, str) else v}"
    return f"{label} {_OP_SYMBOL[n['operator']]} {v}"


def describe_tree(tree: dict, lang: str, top: bool = True) -> str:
    if "op" not in tree:
        return describe_leaf(tree, lang)
    parts = [describe_tree(c, lang, False) for c in tree["children"]]
    if not parts:
        return "" if top else "(—)"
    s = f" {tree['op']} ".join(parts)
    return s if top or len(parts) == 1 else f"({s})"


def condition_lines(tree: dict, lang: str) -> list[str]:
    """저장 목록 카드의 조건 요약(원본: `field operator value` 한 줄씩). 중첩 그룹은 한 줄로 요약."""
    out = []
    for c in tree["children"]:
        if "op" in c:
            out.append(describe_tree(c, lang, False))
        else:
            v = c["value"]
            if isinstance(v, list):
                shown = json.dumps([data_label(x, lang) if isinstance(x, str) else x for x in v], ensure_ascii=False)
            elif isinstance(v, str):
                shown = json.dumps(data_label(v, lang), ensure_ascii=False)
            else:
                shown = json.dumps(v)
            out.append(f"{c['field'].split('.')[-1]} {c['operator']} {shown}")
    return out
