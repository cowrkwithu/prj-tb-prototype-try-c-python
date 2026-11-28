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


@pytest.fixture
def regex_runs(monkeypatch):
    """실제로 실행된 사용자 정규식 검색 수(검사 컨텍스트가 바로 거부한 호출은 세지 않음).
    벽시계 시간 대신 '실행 횟수'로 복잡도 상한을 확인한다 — 부하에 흔들리지 않게(5단계 리뷰)."""
    real = validation.compile_user_regex
    runs = []

    class Counted:
        def __init__(self, rx):
            self.rx = rx

        def search(self, *a, **kw):
            runs.append(1)
            return self.rx.search(*a, **kw)

    monkeypatch.setattr(validation, "compile_user_regex", lambda p: Counted(real(p)))
    return runs


def _schema(user, n, patterns):
    fields = [{"fieldId": f"k{i}", "fieldName": f"f{i}", "displayName": f"F{i}", "dataType": "TEXT"} for i in range(n)]
    s = services.save_definition(user, simple_payload(fields=fields), action="publish")
    for i, f in enumerate(s.fields.order_by("order")):  # 정의 검사를 거치지 않은(예전) 위험 패턴이 남아 있는 상황
        f.constraints = {"pattern": patterns[i % len(patterns)]}
        f.save()
    return s


def test_csv_10000_rows_20_fields_bounded(researcher_client, demo_users, regex_runs):
    s = _schema(demo_users["researcher"], 20, [EVIL])
    head = "condition_id," + ",".join(f"f{i}" for i in range(20)) + "\n"
    row = "A1," + ",".join([VALUE] * 20) + "\n"
    data = (head + row * 10000).encode()
    t = time.monotonic()
    r = researcher_client.post(f"/schemas/{s.schema_id}/data/check",
                               {"mode": "csv", "file": SimpleUploadedFile("big.csv", data, "text/csv")})
    elapsed = time.monotonic() - t
    # 위험 패턴은 첫 시간 초과 뒤 다시 실행하지 않는다 — 20만 칸(10000행×20) 중 실제 실행은 1번.
    # 시간은 상한 없이 매번 실행할 때(20만×0.1초)와 구분되는 넉넉한 값만 확인(부하 시 흔들림 방지)
    assert r.status_code == 302 and len(regex_runs) == 1 and elapsed < 30, (len(regex_runs), elapsed)
    assert PendingEntry.objects.exists(), [m.message for m in r.wsgi_request._messages]
    p = PendingEntry.objects.get()
    assert p.errors and "정규식 검사 시간 초과" in p.errors[0]["ko"]
    assert SchemaRecord.objects.count() == 0


def test_form_200_fields_bounded(researcher_client, demo_users, regex_runs):
    s = _schema(demo_users["researcher"], 200, [EVIL])
    post = {"mode": "form", "link_value": "A1", **{f"f__f{i}": VALUE for i in range(200)}}
    researcher_client.post(f"/schemas/{s.schema_id}/data/check", post)
    assert len(regex_runs) == 1  # 같은 위험 패턴 200칸 — 실행 1번(나머지는 즉시 같은 오류)
    assert len(PendingEntry.objects.get().errors) == 200


def test_budget_exhaustion_with_distinct_patterns(researcher_client, demo_users, settings, regex_runs):
    settings.SCHEMA_REGEX_BUDGET_SECONDS = 0.35
    patterns = [rf"^(z|zz)+{'z' * i}$" for i in range(20)]  # 서로 다른 위험 패턴 — 패턴별 1회 timeout 으로는 못 막음
    s = _schema(demo_users["researcher"], 20, patterns)
    post = {"mode": "form", "link_value": "A1", **{f"f__f{i}": VALUE for i in range(20)}}
    researcher_client.post(f"/schemas/{s.schema_id}/data/check", post)
    msgs = [e["ko"] for e in PendingEntry.objects.get().errors]
    assert len(msgs) == 20 and any("시간 예산 초과" in m for m in msgs)
    # 실행마다 시간 초과(0.1초)가 예산(0.35초)을 깎으므로 실제 실행은 최대 4번 — 부하로 느려지면 더 적어진다
    # (벽시계 단언 대신 실행 횟수 상한: 예산이 없으면 20번)
    assert 1 <= len(regex_runs) <= 4, len(regex_runs)


def test_pattern_timed_out_once_is_not_rerun():
    ctx = validation.CheckContext(budget=5)
    with pytest.raises(validation.RegexTimeout):
        ctx.search(EVIL, VALUE)
    before = ctx.remaining
    for _ in range(100):
        with pytest.raises(validation.RegexTimeout):
            ctx.search(EVIL, VALUE)
    # 다시 실행하지 않으므로 예산이 전혀 줄지 않는다(벽시계 0.05초 단언 대신 — 부하에 흔들리지 않게)
    assert ctx.remaining == before and ctx.remaining > 4.8
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
    # 총 예산(SCHEMA_REGEX_BUDGET_SECONDS=2초)이 상한 — 예산이 없으면 200개×탐침×0.1초(20초 이상).
    # 예산과 구분되는 넉넉한 값으로 확인(이전 3초는 예산과 1초 차이라 부하 시 흔들렸다)
    assert r.status_code == 400 and time.monotonic() - t < 10
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
