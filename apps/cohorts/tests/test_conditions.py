"""조건 트리: 허용 목록 검증, 깊이·노드 제한, ORM 변환 결과를 seed 독립 계산과 대조."""
import json

import pytest
from django.http import QueryDict

from apps.cohorts import conditions as cond

from .helpers import SEED, expected_ids

pytestmark = pytest.mark.django_db


def L(field, op, value):
    return {"field": field, "operator": op, "value": value}


def ids(tree):
    return list(cond.matching_patients(cond.validate(tree)).values_list("id", flat=True))


# ── 필드·연산자 하나씩 (원본 cohortFilters 의 모든 항목) ──
SINGLE = [
    L("demographics.country", "eq", "한국"),
    L("demographics.country", "in", ["몽골", "베트남", "남아공"]),
    L("demographics.age", "between", [30, 50]),
    L("demographics.age", "gte", 60),
    L("demographics.age", "lte", 25),
    L("demographics.age", "eq", 45),
    L("demographics.sex", "in", ["F"]),
    L("demographics.sex", "eq", "M"),
    L("clinical.diagnosisType", "in", ["MDR-TB", "XDR-TB"]),
    L("clinical.treatmentOutcome", "in", ["Failed", "Died", "LTFU"]),
    L("clinical.comorbidities", "in", ["당뇨"]),
    L("clinical.comorbidities", "in", ["HIV", "COPD", "폐암"]),
    L("genomics.hasSequence", "eq", True),
    L("genomics.hasSequence", "eq", False),
    L("genomics.lineage", "eq", "L2.2.1"),
    L("genomics.hasMutation", "eq", True),
    L("genomics.hasMutation", "eq", False),
    L("genomics.resistanceMutations", "in", ["rpoB S450L"]),
    L("genomics.resistanceMutations", "in", ["katG S315T", "inhA c-15t", "gyrA D94G"]),
    L("imaging.hasCxr", "eq", True),
    L("imaging.hasCt", "eq", False),
    L("imaging.hasPetCt", "eq", True),
    L("imaging.cxrCount", "gte", 3),
    L("imaging.ctCount", "between", [1, 2]),
    L("imaging.petCtCount", "lte", 0),
    L("imaging.cxrFindings", "in", ["공동"]),
    L("imaging.cxrFindings", "in", ["흉수", "석회화", "무기폐"]),
]


@pytest.mark.parametrize("leaf", SINGLE, ids=lambda n: f"{n['field']}-{n['operator']}-{n['value']}")
def test_each_filter_matches_independent_seed_evaluation(seeded_patients, leaf):
    exp = expected_ids([leaf])
    assert ids([leaf]) == exp


def test_nonempty_expectations_sanity(seeded_patients):
    non_trivial = [n for n in SINGLE if 0 < len(expected_ids([n])) < len(SEED)]
    assert len(non_trivial) >= len(SINGLE) - 3


NESTED = [
    # AND/OR 중첩: (한국 OR 몽골) AND (MDR OR 동반질환 당뇨) AND NOT-ish(시퀀스 없음)
    {"op": "AND", "children": [
        {"op": "OR", "children": [L("demographics.country", "eq", "한국"), L("demographics.country", "eq", "몽골")]},
        {"op": "OR", "children": [L("clinical.diagnosisType", "eq", "MDR-TB"), L("clinical.comorbidities", "in", ["당뇨"])]},
        L("genomics.hasSequence", "eq", True),
    ]},
    # OR 안의 AND 그룹 두 개 + JSON 다중값 둘
    {"op": "OR", "children": [
        {"op": "AND", "children": [L("imaging.cxrFindings", "in", ["공동"]), L("demographics.age", "gte", 50)]},
        {"op": "AND", "children": [L("genomics.resistanceMutations", "in", ["rpoB S450L"]),
                                   L("demographics.sex", "eq", "F")]},
    ]},
    # 4단계 중첩
    {"op": "AND", "children": [
        {"op": "OR", "children": [
            {"op": "AND", "children": [
                {"op": "OR", "children": [L("clinical.treatmentOutcome", "eq", "Cured"),
                                          L("clinical.treatmentOutcome", "eq", "Completed")]},
                L("imaging.hasCt", "eq", True)]},
            L("genomics.hasMutation", "eq", True)]},
        L("demographics.age", "between", [20, 70])]},
]


@pytest.mark.parametrize("tree", NESTED)
def test_nested_and_or(seeded_patients, tree):
    exp = expected_ids(tree)
    assert 0 < len(exp) < len(SEED)
    assert ids(tree) == exp


def test_empty_tree_and_empty_groups_match_everyone(seeded_patients):
    everyone = sorted(p["id"] for p in SEED)
    assert ids([]) == everyone
    assert ids({"op": "OR", "children": []}) == everyone
    # 원본: 조건이 하나도 없는 그룹은 '전체'. OR 안의 빈 그룹 → 전체
    assert ids({"op": "OR", "children": [L("demographics.sex", "eq", "F"), {"op": "AND", "children": []}]}) == everyone
    # AND 안의 빈 그룹은 영향 없음
    assert ids({"op": "AND", "children": [L("demographics.sex", "eq", "F"), {"op": "OR", "children": []}]}) == \
        expected_ids([L("demographics.sex", "eq", "F")])


def test_seed_cohort_conditions_match_seed_definition(seeded_patients):
    from django.conf import settings
    seed = json.loads((settings.SEED_DIR / "cohorts.json").read_text(encoding="utf-8"))
    for c in seed:
        assert ids(c["conditions"]) == expected_ids(c["conditions"])


# ── 허용 목록 / 형식 거부 ──
@pytest.mark.parametrize("bad", [
    L("demographics.site", "eq", "서울대병원"),             # 허용 목록 밖 필드
    L("condition_id", "eq", "KR-2024-0001"),
    L("id", "in", [1, 2]),
    L("demographics.country__startswith", "eq", "한"),
    L("demographics.age", "regex", ".*"),                  # 허용 밖 연산자
    L("demographics.country", "gte", "한국"),
    L("clinical.comorbidities", "eq", "당뇨"),
    L("demographics.country", "eq", "Atlantis"),           # 선택지 밖 값
    L("demographics.age", "between", [10]),
    L("demographics.age", "gte", "ten"),
    L("demographics.age", "gte", 999),
    L("genomics.hasSequence", "eq", "yes"),
    L("genomics.lineage", "eq", "L2'; DROP TABLE x;--"),
    L("clinical.comorbidities", "in", []),
    L("clinical.comorbidities", "in", ["x"] * 51),
    L("clinical.comorbidities", "in", ["<script>"]),
    {"field": "demographics.sex", "operator": "eq", "value": "M", "extra": 1},
    {"op": "XOR", "children": []},
    {"op": "AND", "children": "nope"},
    {"op": "AND", "children": [], "field": "x"},
    "string",
    [1, 2],
])
def test_rejects_outside_allowlist(bad):
    with pytest.raises(cond.ConditionError):
        cond.validate(bad)


def _deep(depth):
    node = L("demographics.sex", "eq", "M")
    for _ in range(depth):
        node = {"op": "AND", "children": [node]}
    return node


def test_depth_limit():
    cond.validate(_deep(cond.MAX_DEPTH))
    with pytest.raises(cond.ConditionError) as e:
        cond.validate(_deep(cond.MAX_DEPTH + 1))
    assert "중첩" in e.value.ko


def test_node_limit():
    ok = {"op": "OR", "children": [L("demographics.age", "eq", i) for i in range(cond.MAX_NODES - 1)]}
    cond.validate(ok)
    big = {"op": "OR", "children": [L("demographics.age", "eq", i % 150) for i in range(cond.MAX_NODES)]}
    with pytest.raises(cond.ConditionError):
        cond.validate(big)
    wide = {"op": "OR", "children": [{"op": "AND", "children": [L("demographics.age", "eq", 1)] * 30}] * 30}
    with pytest.raises(cond.ConditionError):
        cond.validate(wide)


def test_parse_json_limits():
    with pytest.raises(cond.ConditionError):
        cond.parse_json("x" * (cond.MAX_JSON_CHARS + 1))
    with pytest.raises(cond.ConditionError):
        cond.parse_json("{not json")
    deep = '{"op":"AND","children":[' * 3000
    with pytest.raises(cond.ConditionError):
        cond.validate(cond.parse_json(deep[: cond.MAX_JSON_CHARS]))
    # 깊은 중첩 원문: RecursionError 가 아니라 ConditionError 로 끝나야 한다
    nested = "[" * 7000 + "]" * 7000
    with pytest.raises(cond.ConditionError):
        cond.parse_json(nested[: cond.MAX_JSON_CHARS])


# ── 빌더 폼 ↔ 트리 ──
def test_query_to_filters_drops_disallowed_values():
    qd = QueryDict("country=한국&country=Atlantis&sex=X&sex=F&diag=MDR-TB&seq=1&mut=0&age_min=20&age_max=999"
                   "&lineage=L2.2.1&lineage_x=1&cxr_min=3&logic=OR&finding=공동&finding=<b>")
    f = cond.filters_from_query(qd)
    assert f["countries"] == ["한국"] and f["sex"] == ["F"] and f["diagnosisTypes"] == ["MDR-TB"]
    assert f["hasSequence"] is True and f["hasMutation"] is False and f["ageRange"] == [20, 150]
    assert f["cxrMin"] == 3 and f["logic"] == "OR" and f["cxrFindings"] == ["공동"]


def test_gap_mutations_comma_param():
    f = cond.filters_from_query(QueryDict("mutations=rpoB S450L,katG S315T"))
    assert f["mutations"] == ["rpoB S450L", "katG S315T"]


def test_filters_roundtrip_and_seed_trees_restore():
    f = cond.filters_from_query(QueryDict("country=한국&diag=MDR-TB&diag=XDR-TB&seq=1&cxr_min=3&age_min=10&logic=OR"))
    tree = cond.filters_to_tree(f)
    back = cond.tree_to_filters(tree)
    assert cond.filters_to_tree(back) == tree
    # 원본 시드 3개 조건은 모두 빌더 폼으로 복원된다(국가 eq → 목록, cxrCount gte → CXR 최소 장수)
    from django.conf import settings
    seed = json.loads((settings.SEED_DIR / "cohorts.json").read_text(encoding="utf-8"))
    for c in seed:
        assert cond.tree_to_filters(cond.validate(c["conditions"])) is not None
    # 중첩 그룹은 폼으로 나타낼 수 없다
    assert cond.tree_to_filters(cond.validate(NESTED[0])) is None


def test_original_or_logic_semantics(seeded_patients):
    """원본 filterPatients: 설정한 조건들 사이에 AND/OR, 다중 선택 값 사이는 OR."""
    f = cond.filters_from_query(QueryDict("country=인도&outcome=Died&logic=OR"))
    got = ids(cond.filters_to_tree(f))
    exp = sorted(p["id"] for p in SEED
                 if p["demographics"]["country"] == "인도" or p["clinical"]["treatmentOutcome"] == "Died")
    assert got == exp
