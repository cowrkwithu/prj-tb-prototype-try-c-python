"""3b 2차 리뷰 재현 테스트.

1. 긴 조건 저장 → 리다이렉트 주소에 조건 원문을 실어 DisallowedRedirect(400)인데 코호트는 이미 생성되던 문제.
2. has_any_value 가 스칼라 문자열 JSON 값에도 일치하던 문제(json_type = 'array' 조건).
3. 길이 상한을 정규화·직렬화 후 기준으로 통일 — 화면에 열리는 조건은 저장도 된다.
"""
import json
import re
from datetime import date
from urllib.parse import quote

import pytest
from django.core.exceptions import DisallowedRedirect
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.cases.models import Patient
from apps.cohorts import conditions as cond
from apps.cohorts import views
from apps.cohorts.models import Cohort

pytestmark = pytest.mark.django_db


def reviewer_tree():
    """리뷰어 재현 트리: 95자 한글 값 50개(동반질환) + sex=F."""
    values = [chr(0xAC00 + i) + "나" * 94 for i in range(50)]
    return {"op": "AND", "children": [
        {"field": "clinical.comorbidities", "operator": "in", "value": values},
        {"field": "demographics.sex", "operator": "eq", "value": "F"}]}


def count_in(body):
    return int(re.search(r'data-testid="match-count"[^>]*>(\d+)<', body).group(1))


def test_long_tree_save_redirects_short_and_restores(researcher_client, seeded_patients):
    tree = cond.validate(reviewer_tree())
    text = cond.serialize(tree)
    assert 5000 < len(text) <= cond.MAX_JSON_CHARS
    # 값 하나를 가진 환자를 만들어 결과가 0이 아니게
    p = Patient.objects.filter(sex="F").first()
    p.comorbidities = [tree["children"][0]["value"][7]]
    p.save()

    page = researcher_client.get("/depot?tree=" + quote(text))
    b = page.content.decode()
    assert page.status_code == 200 and count_in(b) == 1
    assert 'data-testid="url-too-long"' in b  # 정규화된 주소가 8,000자를 넘어 기록하지 않음을 안내
    r = researcher_client.get("/depot?tree=" + quote(text) + "&run=", HTTP_HX_REQUEST="true",
                              HTTP_HX_TARGET="depot-result")
    assert r["HX-Push-Url"] == "false"

    n = Cohort.objects.count()
    r = researcher_client.post("/depot/cohorts/save", {"query": "tree=" + quote(text), "tab": "clinical"})
    assert r.status_code == 302
    c = Cohort.objects.order_by("-id").first()
    assert Cohort.objects.count() == n + 1
    assert r["Location"] == f"/depot?loaded={c.pk}&run=1&tab=clinical&panel=saved" and len(r["Location"]) < 100
    assert c.conditions == tree and c.snapshot_ids == [p.pk]

    # 불러오기: 짧은 주소로 서버가 같은 조건을 복원(고급 조건 모드, 숨은 입력은 loaded)
    b = researcher_client.get(r["Location"]).content.decode()
    assert count_in(b) == 1 and 'data-testid="tree-mode"' in b
    assert f'<input type="hidden" name="loaded" value="{c.pk}">' in b and 'name="tree"' not in b
    assert 'data-testid="url-too-long"' not in b
    # 저장 카드의 불러오기 링크도 조건 원문 없이
    saved = researcher_client.get("/depot?panel=saved").content.decode()
    assert f'href="/depot?loaded={c.pk}&amp;run=1&amp;panel=saved"' in saved
    # 고급 조건 화면에서 조건 변경 없이 HTMX 갱신(폼은 loaded 만 보냄)해도 같은 조건
    r = researcher_client.get(f"/depot?loaded={c.pk}&run=1&tab=stats&panel=", HTTP_HX_REQUEST="true",
                              HTTP_HX_TARGET="depot-result")
    assert count_in(r.content.decode()) == 1 and r["HX-Push-Url"] == f"/depot?loaded={c.pk}&run=1&tab=stats"
    # 불러온 고급 조건을 다시 저장 → 같은 조건의 새 데이터셋
    researcher_client.post("/depot/cohorts/save", {"query": f"loaded={c.pk}"})
    assert Cohort.objects.order_by("-id").first().conditions == tree


def test_form_params_take_precedence_over_loaded(researcher_client, seeded_patients, demo_users):
    from apps.cohorts import services
    services.load_cohorts()
    b = researcher_client.get("/depot?loaded=1&country=몽골&logic=AND").content.decode()
    assert count_in(b) == Patient.objects.filter(country="몽골").count()
    b = researcher_client.get("/depot?loaded=1").content.decode()  # 시드 1: 폼으로 복원
    assert re.search(r'name="country" value="한국" checked', b) and 'data-testid="tree-mode"' not in b


def test_save_is_atomic_when_response_fails(researcher_client, seeded_patients, monkeypatch):
    def boom(url):
        raise DisallowedRedirect("too long")

    monkeypatch.setattr(views, "redirect", boom)
    n = Cohort.objects.count()
    r = researcher_client.post("/depot/cohorts/save", {"query": "country=한국"})
    assert r.status_code == 400  # SuspiciousOperation → 400, 저장은 롤백
    assert Cohort.objects.count() == n


def test_has_any_value_requires_array(seeded_patients):
    arr = Patient.objects.get(pk=1)
    arr.comorbidities = ["당뇨"]
    arr.save()
    scalar = Patient.objects.get(pk=2)
    scalar.comorbidities = "당뇨"  # 비정상 스칼라 문자열
    scalar.save()
    obj = Patient.objects.get(pk=3)
    obj.comorbidities = {"당뇨": 1}
    obj.save()
    with CaptureQueriesContext(connection) as ctx:
        ids = set(Patient.objects.filter(comorbidities__has_any_value=["당뇨"]).values_list("id", flat=True))
    # DB 안에서 배열 여부를 확인하는 백엔드별 SQL(lookups.HasAnyValue.as_sqlite / as_postgresql)
    marker = {"sqlite": "json_type", "postgresql": "jsonb_typeof"}[connection.vendor]
    assert marker in ctx[0]["sql"]
    assert 1 in ids and 2 not in ids and 3 not in ids


def test_length_limit_uses_normalized_serialization(researcher_client, seeded_patients):
    # 원문은 공백 때문에 8,000자를 넘지만 정규화하면 상한 안 → 열리고 저장도 된다
    tree = reviewer_tree()
    pretty = json.dumps(tree, ensure_ascii=False, indent=40)
    assert len(pretty) > cond.MAX_JSON_CHARS >= len(cond.serialize(cond.validate(tree)))
    assert researcher_client.get("/depot?tree=" + quote(pretty)).status_code == 200
    n = Cohort.objects.count()
    researcher_client.post("/depot/cohorts/save", {"query": "tree=" + quote(pretty)})
    assert Cohort.objects.count() == n + 1

    # 원문은 상한 안이지만 정규화(평면 목록 → AND 그룹)로 상한을 넘는 트리 → 화면·저장 모두 같은 기준으로 거부
    vals = [chr(0xAC00 + i) + "다" * 99 for i in range(50)]
    leaves = [{"field": "clinical.comorbidities", "operator": "in", "value": vals},
              {"field": "imaging.cxrFindings", "operator": "in", "value": ["공동"]}]
    raw = json.dumps(leaves, ensure_ascii=False, separators=(",", ":"))
    pad = cond.MAX_JSON_CHARS - len(raw)
    assert pad > 0
    # 원문 길이를 정확히 상한(8,000자)에 맞춘다 → 정규화(AND 그룹으로 감쌈) 후에는 상한 초과
    def dump(x):
        return json.dumps(x, ensure_ascii=False, separators=(",", ":"))

    def lin(k):
        return {"field": "genomics.lineage", "operator": "eq", "value": "L" + "1" * k}

    hs = {"field": "genomics.hasSequence", "operator": "eq", "value": True}
    extra = []
    while len(dump(leaves + extra + [hs, lin(0), lin(0)])) <= cond.MAX_JSON_CHARS:
        extra.append(hs)
    slack = cond.MAX_JSON_CHARS - len(dump(leaves + extra + [lin(0), lin(0)]))
    assert 0 <= slack <= 78
    extra += [lin(min(slack, 39)), lin(slack - min(slack, 39))]
    raw = dump(leaves + extra)
    assert len(raw) == cond.MAX_JSON_CHARS
    assert len(cond.serialize({"op": "AND", "children": leaves + extra})) > cond.MAX_JSON_CHARS
    with pytest.raises(cond.ConditionError):
        cond.validate(cond.parse_json(raw))
    assert researcher_client.get("/depot?tree=" + quote(raw)).status_code == 400
    n = Cohort.objects.count()
    researcher_client.post("/depot/cohorts/save", {"query": "tree=" + quote(raw)})
    assert Cohort.objects.count() == n


def test_values_outside_ui_options_are_kept(researcher_client, seeded_patients):
    """화면 선택지에 없는 값: 주소로 받으면 칩을 덧붙여 보이고(다음 변경 때 유지), 저장 조건은 폼 대신 고급 조건으로 복원."""
    b = researcher_client.get("/depot?comorb=B형 간염&outcome=Ongoing&country=기타").content.decode()
    assert re.search(r'name="comorb" value="B형 간염" checked', b)
    assert re.search(r'name="outcome" value="Ongoing" checked', b)
    assert re.search(r'name="country" value="기타" checked', b)
    tree = cond.validate([{"field": "clinical.comorbidities", "operator": "in", "value": ["B형 간염"]}])
    assert cond.tree_to_filters(tree) is None
    assert cond.tree_to_filters(cond.validate(
        [{"field": "clinical.comorbidities", "operator": "in", "value": ["당뇨"]}])) is not None
