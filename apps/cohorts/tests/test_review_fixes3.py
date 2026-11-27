"""3b 3차 리뷰: 조건 해석 단일 진입점(services.resolve_conditions) + 모든 URL 공격 입력 회귀 테스트(500 금지)."""
import json
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from apps.cohorts import conditions as cond
from apps.cohorts import services, views
from apps.cohorts.models import Cohort

pytestmark = pytest.mark.django_db

LONG = ["가" * 99 + chr(0xAC00 + i) for i in range(50)]


def _q(pairs):
    return urlencode(pairs, quote_via=quote)


def _deep(depth):
    node = {"field": "demographics.sex", "operator": "eq", "value": "M"}
    for _ in range(depth):
        node = {"op": "AND", "children": [node]}
    return node


ATTACKS = {
    "long-comorb-finding": _q([("comorb", v) for v in LONG] + [("finding", v) for v in LONG]),
    "long-every-list-field": _q([(k, v) for k in ("country", "sex", "diag", "outcome", "comorb", "mutations",
                                                  "finding") for v in LONG]),
    "long-mutations-comma": _q([("mutations", ",".join(LONG * 4))]),
    "values-501": _q([("comorb", f"v{i}") for i in range(501)]),
    "tree-values-501": _q([("tree", json.dumps([{"field": "clinical.comorbidities", "operator": "in",
                                                 "value": [f"v{i}-{j}" for j in range(50)]} for i in range(11)]))]),
    "unicode-digits": _q([("age_min", "²"), ("age_max", "٣"), ("cxr_min", "１"), ("loaded", "²"), ("tab", "²")]),
    "broken-json": _q([("tree", "{not json")]),
    "broken-json-brackets": _q([("tree", "[" * 5000)]),
    "tree-not-list": _q([("tree", '"string"')]),
    "deep-nesting": _q([("tree", json.dumps(_deep(cond.MAX_DEPTH + 3)))]),
    "missing-loaded": _q([("loaded", "999999")]),
    "loaded-huge": _q([("loaded", "9" * 40)]),
    "out-of-choice": _q([("country", "Atlantis"), ("outcome", "Foo"), ("diag", "<b>"), ("sex", "X"),
                         ("lineage", "<script>"), ("seq", "maybe"), ("logic", "XOR")]),
    "control-chars": _q([("comorb", "a\x00b"), ("mutations", "\r\n=cmd"), ("tree", "\x00")]),
    "disallowed-field-tree": _q([("tree", json.dumps([{"field": "id", "operator": "in", "value": [1]}]))]),
    "tree-and-form-and-loaded": _q([("tree", "[]"), ("country", "한국"), ("loaded", "1")]),
    # JSON 이스케이프로 들어오는 고립 서로게이트·NUL (4차 리뷰)
    "surrogate-value": _q([("tree", '[{"field":"clinical.comorbidities","operator":"in","value":["a\\udfff"]}]')]),
    "surrogate-high": _q([("tree", '[{"field":"genomics.resistanceMutations","operator":"in","value":["\\ud800x"]}]')]),
    "surrogate-key": _q([("tree", '[{"fi\\udfffeld":"x","operator":"eq","value":"M"}]')]),
    "surrogate-op": _q([("tree", '{"op":"AND\\udc00","children":[]}')]),
    "nul-value": _q([("tree", '[{"field":"clinical.comorbidities","operator":"in","value":["a\\u0000b"]}]')]),
    "nul-key": _q([("tree", '{"op":"AND","children":[],"\\u0000":1}')]),
}


@pytest.fixture
def world(seeded_patients, demo_users):
    services.load_cohorts()
    broken = Cohort.objects.create(name="broken", owner=demo_users["researcher"], created_date="2025-01-01",
                                   conditions={"field": "id", "operator": "in", "value": [1]}, snapshot_ids=[1])
    return {"own": Cohort.objects.get(pk=1), "broken": broken}


@pytest.mark.parametrize("name", list(ATTACKS))
def test_no_500_on_any_cohort_url(researcher_client, world, name):
    q = ATTACKS[name]
    c = world["own"]
    checks = [
        researcher_client.get(f"/depot?{q}"),
        researcher_client.get(f"/depot?{q}&run=1&tab=stats&panel=saved"),
        researcher_client.get(f"/depot?{q}&run=1", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="depot-result"),
        researcher_client.get(f"/depot?loaded={c.pk}&{q}&run=1"),
        researcher_client.get(f"/depot/cohorts/{c.pk}/export.csv?{q}"),
        researcher_client.post(f"/depot/cohorts/{c.pk}/rename?{q}", {"name": q[:300], "name_en": q, "next": q}),
    ]
    n = Cohort.objects.count()
    checks.append(researcher_client.post("/depot/cohorts/save", {"query": q, "tab": q[:20], "name": q[:500]}))
    for r in checks:
        assert r.status_code != 500 and r.status_code < 500, (name, r.status_code)
    if checks[-1].status_code == 400:
        assert Cohort.objects.count() == n  # 거부된 저장은 아무것도 만들지 않는다
    assert researcher_client.post(f"/depot/cohorts/{world['broken'].pk}/delete", {"next": q}).status_code == 302


def test_corrupted_stored_conditions_do_not_500(researcher_client, world):
    b = world["broken"]
    assert researcher_client.get("/depot?panel=saved").status_code == 200
    r = researcher_client.get(services.load_url(b))
    assert r.status_code == 400 and 'data-testid="cond-error"' in r.content.decode()
    assert researcher_client.get(f"/depot/cohorts/{b.pk}/export.csv").status_code == 400
    assert researcher_client.post("/depot/cohorts/save", {"query": f"loaded={b.pk}"}).status_code == 400


def test_reported_case_long_form_values(researcher_client, world):
    q = ATTACKS["long-comorb-finding"]
    r = researcher_client.get(f"/depot?{q}&run=1")
    body = r.content.decode()
    assert r.status_code == 400 and 'data-testid="cond-error"' in body
    assert 'data-testid="save-disabled"' in body and 'data-testid="save-dataset"' not in body
    r = researcher_client.get(f"/depot?{q}&run=1", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="depot-result")
    assert r.status_code == 200 and 'data-testid="cond-error"' in r.content.decode()
    n = Cohort.objects.count()
    assert researcher_client.post("/depot/cohorts/save", {"query": q}).status_code == 400
    assert Cohort.objects.count() == n
    # 조기 거부: 정규화 전에 폼 수집 단계에서
    from django.http import QueryDict
    with pytest.raises(cond.ConditionError):
        cond.filters_from_query(QueryDict(q))


def test_missing_loaded_shows_error_and_blocks_save(researcher_client, world):
    r = researcher_client.get("/depot?loaded=999999&run=1")
    body = r.content.decode()
    assert r.status_code == 400 and "불러온 데이터셋을 찾을 수 없습니다" in body
    assert 'data-testid="save-disabled"' in body and 'data-testid="loaded-note"' not in body
    r = researcher_client.get("/depot?loaded=999999&run=1", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="depot-result")
    assert r.status_code == 200 and "불러온 데이터셋을 찾을 수 없습니다" in r.content.decode()
    n = Cohort.objects.count()
    assert researcher_client.post("/depot/cohorts/save", {"query": "loaded=999999"}).status_code == 400
    assert Cohort.objects.count() == n


def test_loaded_note_only_when_conditions_come_from_cohort(researcher_client, world):
    b = researcher_client.get("/depot?loaded=1&run=1").content.decode()
    assert 'data-testid="loaded-note"' in b
    # 폼 조건이 우선하면 조건 출처는 데이터셋이 아니다 → 안내 없음
    b = researcher_client.get("/depot?loaded=1&country=몽골&run=1").content.decode()
    assert 'data-testid="loaded-note"' not in b
    # 데이터셋에서 온 조건으로 탭을 옮겨도 안내 유지(탭 링크가 loaded 를 싣는다)
    r = researcher_client.get("/depot?loaded=1&run=1&tab=clinical", HTTP_HX_REQUEST="true",
                              HTTP_HX_TARGET="depot-result")
    assert 'data-testid="loaded-note"' in r.content.decode() and r["HX-Push-Url"] == "/depot?loaded=1&run=1&tab=clinical"


def test_views_do_not_handle_conditions_directly():
    src = Path(views.__file__).read_text(encoding="utf-8")
    for word in ("validate(", "ConditionError", "parse_json(", "filters_from_query(", "filters_to_tree("):
        assert word not in src, word


@pytest.mark.parametrize("name", ["surrogate-value", "surrogate-high", "surrogate-key", "nul-value"])
def test_surrogate_json_three_paths(researcher_client, world, name):
    q = ATTACKS[name]
    assert "%5Cu" in q  # 실제로 JSON 이스케이프 문자열(백슬래시-u)로 보낸다
    r = researcher_client.get(f"/depot?{q}&run=1")
    assert r.status_code == 400 and 'data-testid="cond-error"' in r.content.decode()
    r = researcher_client.get(f"/depot?{q}&run=1", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="depot-result")
    assert r.status_code == 200 and 'data-testid="cond-error"' in r.content.decode()
    n = Cohort.objects.count()
    assert researcher_client.post("/depot/cohorts/save", {"query": q}).status_code == 400
    assert Cohort.objects.count() == n


def test_label_regex_rejects_surrogates():
    for v in ("a\udfff", "\ud800", "x\x00"):
        with pytest.raises(cond.ConditionError):
            cond.validate([{"field": "clinical.comorbidities", "operator": "in", "value": [v]}])
