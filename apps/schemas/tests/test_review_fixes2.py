"""3a 2차 리뷰: 정규식 시간 예산(검사 1회 단위)·패턴별 1회 timeout, 탐침 강화, 정확한 정수 파싱, structure_rev 분리."""
import json
import time

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.schemas import services, validation
from apps.schemas.models import PendingEntry, Schema, SchemaField, SchemaRecord

from .conftest import simple_payload

pytestmark = pytest.mark.django_db
EVIL = r"^(z|zz)+$"
VALUE = "z" * 60 + "!"


def _schema(user, n, patterns):
    fields = [{"fieldId": f"k{i}", "fieldName": f"f{i}", "displayName": f"F{i}", "dataType": "TEXT"} for i in range(n)]
    s = services.save_definition(user, simple_payload(fields=fields), action="publish")
    for i, f in enumerate(s.fields.order_by("order")):  # 정의 검사를 거치지 않은(예전) 위험 패턴이 남아 있는 상황
        f.constraints = {"pattern": patterns[i % len(patterns)]}
        f.save()
    return s


def test_csv_10000_rows_20_fields_bounded(researcher_client, demo_users):
    s = _schema(demo_users["researcher"], 20, [EVIL])
    head = "condition_id," + ",".join(f"f{i}" for i in range(20)) + "\n"
    row = "A1," + ",".join([VALUE] * 20) + "\n"
    data = (head + row * 10000).encode()
    t = time.monotonic()
    r = researcher_client.post(f"/schemas/{s.schema_id}/data/check",
                               {"mode": "csv", "file": SimpleUploadedFile("big.csv", data, "text/csv")})
    elapsed = time.monotonic() - t
    assert r.status_code == 302 and elapsed < 3, elapsed
    assert PendingEntry.objects.exists(), [m.message for m in r.wsgi_request._messages]
    p = PendingEntry.objects.get()
    assert p.errors and "정규식 검사 시간 초과" in p.errors[0]["ko"]
    assert SchemaRecord.objects.count() == 0


def test_form_200_fields_bounded(researcher_client, demo_users):
    s = _schema(demo_users["researcher"], 200, [EVIL])
    post = {"mode": "form", "link_value": "A1", **{f"f__f{i}": VALUE for i in range(200)}}
    t = time.monotonic()
    researcher_client.post(f"/schemas/{s.schema_id}/data/check", post)
    assert time.monotonic() - t < 3
    assert len(PendingEntry.objects.get().errors) == 200


def test_budget_exhaustion_with_distinct_patterns(researcher_client, demo_users, settings):
    settings.SCHEMA_REGEX_BUDGET_SECONDS = 0.35
    patterns = [rf"^(z|zz)+{'z' * i}$" for i in range(20)]  # 서로 다른 위험 패턴 — 패턴별 1회 timeout 으로는 못 막음
    s = _schema(demo_users["researcher"], 20, patterns)
    post = {"mode": "form", "link_value": "A1", **{f"f__f{i}": VALUE for i in range(20)}}
    t = time.monotonic()
    researcher_client.post(f"/schemas/{s.schema_id}/data/check", post)
    assert time.monotonic() - t < 1.0
    msgs = [e["ko"] for e in PendingEntry.objects.get().errors]
    assert len(msgs) == 20 and any("시간 예산 초과" in m for m in msgs)


def test_pattern_timed_out_once_is_not_rerun():
    ctx = validation.CheckContext(budget=5)
    with pytest.raises(validation.RegexTimeout):
        ctx.search(EVIL, VALUE)
    t = time.monotonic()
    for _ in range(100):
        with pytest.raises(validation.RegexTimeout):
            ctx.search(EVIL, VALUE)
    assert time.monotonic() - t < 0.05 and ctx.remaining > 4.8
    # 컨텍스트는 검사마다 새로 — 다른 검사에는 영향 없음
    assert validation.CheckContext().search(r"^z+!$", VALUE) is True


def test_definition_probes_and_budget(researcher_client):
    for p in [r"^(z|zz)+$", r"^(b+)+$", r"(x+x+)+y", r"(.*a){12}x", r"^(\w+\s?)*$"]:
        assert validation.check_user_regex(p) is not None, p
    for p in [r"^Rv\d{4}[a-c]?$", r"^[A-Z]{2}-\d{4}-\d{4}$", r"^(ICD|SNOMED)-\w+$", r"(.*a){12}"]:
        assert validation.check_user_regex(p) is None, p  # (.*a){12} 는 regex 모듈이 선형에 가깝게 처리
    fields = [{"fieldName": f"f{i}", "displayName": "x", "dataType": "TEXT", "constraints": {"pattern": rf"^(z|zz)+{'z' * i}$"}}
              for i in range(200)]
    t = time.monotonic()
    r = researcher_client.post("/schemas/create", {"payload": json.dumps(simple_payload(fields=fields)), "action": "draft"})
    assert r.status_code == 400 and time.monotonic() - t < 3
    assert "시간 예산 초과" in r.content.decode()


@pytest.mark.parametrize("raw,ok", [("9007199254740993", False), ("9007199254740992", True), ("-9007199254740993", False),
                                    ("9007199254740992.0", True), ("1e999999999", False), ("12345678901234567890", False)])
def test_integer_exact_parsing(raw, ok):
    f = {"data_type": "INTEGER", "constraints": {}, "display_name": "", "display_name_en": ""}
    val, err = validation.coerce_value(f, raw)
    assert (err is None) == ok
    if ok:
        assert val == int(float(raw)) and isinstance(val, int)


# ── structure_rev: 구조 변경만 진행 중인 제출을 무효화 ──
def _check(client, s):
    client.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__score": "3"})
    return PendingEntry.objects.latest("pk")


def test_meta_changes_do_not_block_submit(researcher_client, published_schema, demo_users):
    s = published_schema
    p = _check(researcher_client, s)
    services.change_visibility(s, "shared", demo_users["researcher"])
    services.save_definition(demo_users["researcher"], simple_payload(name="이름만 변경", visibility="shared"), schema=s)
    s.refresh_from_db()
    assert s.version == "1.1"
    researcher_client.post(f"/schemas/{s.schema_id}/data/submit/{p.pk}")
    assert SchemaRecord.objects.count() == 1


def test_structure_change_blocks_submit(researcher_client, published_schema, demo_users):
    s = published_schema
    p = _check(researcher_client, s)
    payload = simple_payload()
    payload["fields"][0]["constraints"] = {"min": 0, "max": 2}  # 제약 변경
    services.save_definition(demo_users["researcher"], payload, schema=s)
    researcher_client.post(f"/schemas/{s.schema_id}/data/submit/{p.pk}")
    assert SchemaRecord.objects.count() == 0


def test_status_change_still_blocks_submit(researcher_client, published_schema, demo_users):
    s = published_schema
    p = _check(researcher_client, s)
    services.transition(s, "archive", demo_users["researcher"])
    researcher_client.post(f"/schemas/{s.schema_id}/data/submit/{p.pk}")
    assert SchemaRecord.objects.count() == 0
    services.transition(Schema.objects.get(pk=s.pk), "restore", demo_users["researcher"])
    researcher_client.post(f"/schemas/{s.schema_id}/data/submit/{p.pk}")  # 다시 발행되면(구조 그대로) 제출 가능
    assert SchemaRecord.objects.count() == 1
    _ = SchemaField
