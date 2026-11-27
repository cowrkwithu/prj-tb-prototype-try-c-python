"""3c 1차 리뷰 재현 테스트 — API 조건 트리 스칼라 children(500), 인증 실패 요청 제한, CSV 셀 C0 제어문자."""
import csv
import io
import json

import pytest

from apps.datasharing import services
from apps.uploads.services import csvio

from .conftest import auth, make_dar

pytestmark = pytest.mark.django_db


def _post(client, raw, payload):
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    return client.post("/api/v1/cohorts", body, content_type="application/json", **auth(raw))


# ─── 1. children 이 참으로 평가되는 스칼라면 TypeError → 500 ───
@pytest.mark.parametrize("conditions", [
    {"children": 5}, {"children": True}, [{"children": 1.5}], {"children": "abc"}, [{"children": {"children": 7}}],
    {"children": {"field": "genomics.lineage", "operator": "in", "value": ["L2"]}},
])
def test_scalar_children_is_400_not_500(client, api_key, seeded_patients, conditions):
    r = _post(client, api_key, {"name": "x", "conditions": conditions})
    assert r.status_code in (400, 403), (conditions, r.status_code)
    assert r.json()["status"] in ("bad_request", "forbidden")


def test_condition_types_ignores_non_container_children():
    from apps.datasharing.api import _condition_types

    assert _condition_types({"children": 5}) == set()
    assert _condition_types([{"children": True}, {"field": "imaging.hasCxr"}]) == {"imaging"}
    assert _condition_types({"children": {"field": "genomics.lineage"}}) == {"genomics"}
    deep = {"field": "genomics.x"}
    for _ in range(5000):  # 깊은 중첩도 재귀 한도 없이 처리
        deep = {"children": [deep]}
    assert _condition_types(deep) == {"genomics"}


def test_condition_types_still_enforces_dar(client, demo_users, seeded_patients):
    make_dar(demo_users["researcher"], ["clinical"], "approved")
    _, raw = services.issue_key(demo_users["researcher"], "kim@snubh.org", "SNUBH")
    r = _post(client, raw, {"name": "x", "conditions": {"logic": "AND", "children": [
        {"field": "genomics.lineage", "operator": "in", "value": ["L2"]}]}})
    assert r.status_code == 403 and r.json()["required"] == ["genomics"]


# ─── 6. 인증 실패 요청 제한(IP 단위, REMOTE_ADDR) ───
def test_auth_failure_rate_limited_per_ip(client, api_key, seeded_patients, settings):
    settings.API_AUTH_FAIL_LIMIT_PER_MINUTE = 3
    codes = [client.get("/api/v1/cases", HTTP_AUTHORIZATION="Bearer tbk_" + "x" * 30).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    r = client.get("/api/v1/cases")  # 키 없음도 같은 IP 계수
    assert r.status_code == 429 and r.json()["status"] == "rate_limited" and 1 <= int(r["Retry-After"]) <= 61
    # 차단된 IP 에서는 올바른 키도 그 창 동안 429(무차별 대입 확인 방지)
    assert client.get("/api/v1/cases", **auth(api_key)).status_code == 429
    # X-Forwarded-For 는 신뢰하지 않는다 — 헤더를 바꿔도 같은 IP 로 센다
    assert client.get("/api/v1/cases", HTTP_X_FORWARDED_FOR="10.9.9.9").status_code == 429
    # 다른 REMOTE_ADDR 은 영향 없음
    assert client.get("/api/v1/cases", REMOTE_ADDR="10.0.0.2", **auth(api_key)).status_code == 200
    assert client.get("/api/v1/cases", REMOTE_ADDR="10.0.0.2").status_code == 401


def test_successful_requests_do_not_count_as_failures(client, api_key, seeded_patients, settings):
    settings.API_AUTH_FAIL_LIMIT_PER_MINUTE = 2
    settings.API_RATE_LIMIT_PER_MINUTE = 100
    for _ in range(5):
        assert client.get("/api/v1/cases", **auth(api_key)).status_code == 200
    assert client.get("/api/v1/cases").status_code == 401


# ─── 7. CSV 셀의 C0 제어문자 ───
@pytest.mark.parametrize("value,expected", [
    ("\x01=1+1", "'=1+1"), ("\x00\x1f+cmd", "'+cmd"), ("a\x07b", "ab"), ("\x0b-1", "-1"), ("\x0c@x", "'@x"),
    ("\x7f=A1", "'=A1"), ("line1\nline2", "line1\nline2"), ("\tx", "'\tx"), ("\x01\tx", "'\tx"), ("-3.5", "-3.5"),
])
def test_safe_cell_strips_c0_controls_before_prefix(value, expected):
    assert csvio.safe_cell(value) == expected


def test_datasharing_text_and_csv_export(researcher_client, demo_users, seeded_patients):
    from apps.cases.models import Patient, Regimen, RegimenDrug

    assert services._text("\x01=1+1") == "'=1+1"
    assert services._text(["\x02@a", "b"]) == "'@a;b"
    make_dar(demo_users["researcher"], ["clinical"], "approved")
    p = Patient.objects.order_by("id").first()
    reg = Regimen.objects.create(patient_id=p.pk)
    RegimenDrug.objects.create(regimen=reg, drug_name="\x01=1+1", drug_code="H", start_day=0, end_day=5, dosage="\x03+2")
    r = researcher_client.get("/data/datasets/regimens/download.csv")
    rows = list(csv.reader(io.StringIO(b"".join(r.streaming_content).decode("utf-8-sig"))))
    row = next(x for x in rows[1:] if x[0] == p.condition_id and "'=1+1" in x)
    assert "'+2" in row and not any(any(ord(ch) < 32 and ch not in "\t\n\r" for ch in c) for c in row)
