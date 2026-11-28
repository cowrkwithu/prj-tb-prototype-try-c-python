"""3a 1차 리뷰 지적 재현 테스트 (CONFIRMED 1~4, PLAUSIBLE 5~7, NIT, 헤더 추가 지시)."""
import io
import json
import re
import time
import zipfile
from html.parser import HTMLParser

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from apps.dictionary.models import DictionaryField
from apps.schemas import services, validation
from apps.schemas.models import PendingEntry, Schema, SchemaField, SchemaFile, SchemaRecord

from .conftest import simple_payload

pytestmark = pytest.mark.django_db
EVIL = r"^(a+)+$"
EVIL_INPUT = "a" * 5000 + "!"


# ═══ 1. 항목 수정 모달: 무변경 저장 시 항목이 바뀌지 않는다 ═══
class _ModalForm(HTMLParser):
    """브라우저+Alpine 이 실제로 보낼 폼 값을 흉내: <template x-if="sets.X.includes(dt)"> 는 현재 타입일 때만 포함."""

    def __init__(self, sets, dt):
        super().__init__()
        self.sets, self.dt, self.stack, self.data, self.select = sets, dt, [], {}, None

    def _active(self):
        return all(self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "template":
            m = re.search(r"sets\.(\w+)\.includes\(dt\)", a.get("x-if", ""))
            self.stack.append(self.dt in self.sets[m.group(1)] if m else True)
        elif not self._active():
            return
        elif tag == "input" and a.get("name") and a.get("type") not in ("checkbox", "radio") and "disabled" not in a:
            self.data[a["name"]] = a.get("value", "")
        elif tag == "select" and a.get("name"):
            self.select = a["name"]
        elif tag == "option" and self.select and "selected" in a:
            self.data[self.select] = a.get("value")

    def handle_endtag(self, tag):
        if tag == "template" and self.stack:
            self.stack.pop()
        if tag == "select":
            self.select = None


def _unchanged_save(client, entry):
    html = client.get(f"/admin/dictionary/entry/modal?id={entry.pk}").content.decode()
    assert "entry-modal-types" in html and "|safe" not in html
    head = html.split("<div class=\"modal-backdrop\"", 1)[1].split(">", 1)[0]
    assert "[" not in head and "{{" not in head  # 속성 안에 JSON 을 직접 넣지 않는다
    sets = json.loads(re.search(r'id="entry-modal-types"[^>]*>(.*?)</script>', html, re.S).group(1))
    dt = re.search(r'data-dt="([A-Z_]+)"', html).group(1)
    form = _ModalForm(sets, dt)
    form.feed(html)
    client.post("/admin/dictionary/entry/save", form.data)
    return form.data


def test_entry_modal_unchanged_save_keeps_decimal_and_constraints(admin_client_portal, loaded_dictionary):
    e = DictionaryField.objects.get(version=loaded_dictionary, dictionary_id="TB.U.PAT.013")
    e.constraints = {"precision": 1, "min": 0, "max": 300, "unit": "kg", "codeSystem": "X"}
    e.save()
    before = (e.data_type, dict(e.constraints), list(e.options), e.display_name)
    sent = _unchanged_save(admin_client_portal, e)
    assert sent["data_type"] == "DECIMAL" and sent["precision"] == "1"
    e.refresh_from_db()
    assert (e.data_type, e.constraints, e.options, e.display_name) == before


def test_entry_modal_unchanged_save_keeps_select_options(admin_client_portal, loaded_dictionary):
    e = DictionaryField.objects.get(version=loaded_dictionary, dictionary_id="TB.U.PAT.005")
    before = (e.data_type, dict(e.constraints), list(e.options))
    _unchanged_save(admin_client_portal, e)
    e.refresh_from_db()
    assert (e.data_type, e.constraints, e.options) == before  # 영문 라벨(labelEn)까지 보존


def test_edit_entry_missing_keys_kept_and_blank_keys_removed(admin_client_portal, loaded_dictionary):
    e = DictionaryField.objects.get(version=loaded_dictionary, dictionary_id="TB.U.PAT.013")
    e.constraints = {"precision": 1, "min": 0, "max": 300}
    e.save()
    admin_client_portal.post("/admin/dictionary/entry/save", {"id": e.pk, "display_name": e.display_name})
    e.refresh_from_db()
    assert e.data_type == "DECIMAL" and e.constraints == {"precision": 1, "min": 0, "max": 300}
    admin_client_portal.post("/admin/dictionary/entry/save", {"id": e.pk, "display_name": e.display_name, "max": ""})
    e.refresh_from_db()
    assert e.constraints == {"precision": 1, "min": 0}
    admin_client_portal.post("/admin/dictionary/entry/save", {"id": e.pk, "display_name": e.display_name, "data_type": "TEXT"})
    e.refresh_from_db()
    assert e.data_type == "TEXT" and e.constraints == {}  # 명시적 타입 변경일 때만 맞지 않는 제약 제거


# ═══ 2. ReDoS — 세 경로 모두 0.5초 안에 오류 ═══
def _evil_field(default=None):
    # ^(a+)+$ 는 regex 모듈에서 입력 길이의 제곱으로 느려진다 — TEXT(최대 1,000자)에서는 무해하고(5차 리뷰: 탐침 길이 =
    # 타입별 최대 입력 길이) TEXTAREA(최대 10,000자)에서 위험하다. 그래서 위험이 실제로 있는 TEXTAREA 로 검사한다.
    f = {"fieldId": "k", "fieldName": "code", "displayName": "코드", "dataType": "TEXTAREA", "constraints": {"pattern": EVIL}}
    if default:
        f["defaultValue"] = default
    return f


def test_redos_definition_rejected_fast_preview_and_draft(researcher_client):
    p = simple_payload(fields=[_evil_field(EVIL_INPUT[:900])])
    # 시간 단언은 넉넉하게(부하 시 흔들림 방지) — 거부 자체는 아래 '너무 복잡합니다' 문구가 확인한다
    t = time.monotonic()
    r = researcher_client.post("/schemas/builder/preview", {"payload": json.dumps(p)})
    assert r.status_code == 200 and time.monotonic() - t < 2
    t = time.monotonic()
    r = researcher_client.post("/schemas/create", {"payload": json.dumps(p), "action": "draft"})
    assert r.status_code == 400 and time.monotonic() - t < 2
    assert "정규식이 너무 복잡합니다" in r.content.decode()
    assert Schema.objects.count() == 0


def test_redos_data_check_times_out_to_error(researcher_client, demo_users):
    s = services.save_definition(demo_users["researcher"], simple_payload(fields=[
        {"fieldId": "k", "fieldName": "code", "displayName": "코드", "dataType": "TEXTAREA"}]), action="publish")
    # 정의 검사를 거치지 않은(예: 예전 데이터) 위험 패턴이 남아 있어도 실행 시간은 제한된다
    SchemaField.objects.filter(schema=s).update(constraints={"pattern": EVIL})
    t = time.monotonic()
    researcher_client.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__code": EVIL_INPUT})
    assert time.monotonic() - t < 2  # 제한 자체는 아래 '시간 초과' 오류가 확인(넉넉한 벽시계 — 부하 시 흔들림 방지)
    p = PendingEntry.objects.get()
    assert p.errors and "정규식 검사 시간 초과" in p.errors[0]["ko"]


def test_no_user_regex_runs_through_stdlib_re():
    """사용자 정의 정규식(제약조건 pattern)은 regex + timeout 으로만 실행된다."""
    from pathlib import Path

    src = (Path(validation.__file__)).read_text(encoding="utf-8")
    assert 're.search(c["pattern"]' not in src and "re.compile(p)" not in src
    assert "user_regex_search(c[\"pattern\"]" in src


# ═══ 3. 수식 오버플로 — 500 대신 계산 불가/입력 오류 ═══
def test_overflow_inputs_and_formulas(researcher_client, demo_users):
    fields = [{"fieldId": "a", "fieldName": "a", "displayName": "A", "dataType": "INTEGER"},
              {"fieldId": "d", "fieldName": "d", "displayName": "D", "dataType": "DECIMAL"},
              {"fieldId": "c", "fieldName": "sq", "displayName": "SQ", "dataType": "CALCULATED", "constraints": {"formula": "a*a*a*a"}}]
    p = simple_payload(fields=fields)
    p["meta"]["validationRules"] = [{"name": "r", "expression": "a*a/3 > 1", "message": "m"}]
    s = services.save_definition(demo_users["researcher"], p, action="publish")
    c = researcher_client
    for a, d in [("1e308", "1"), ("1", "1e308"), (str(2 ** 53), "1"), ("9" * 400, "1")]:
        r = c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__a": a, "f__d": d})
        assert r.status_code == 302
    errs = [e["field"] for p in PendingEntry.objects.order_by("pk") for e in p.errors]
    assert errs.count("a") == 2 and errs.count("d") == 1  # 1e308·9…9 → 범위 오류, 2^53 은 허용
    big = PendingEntry.objects.order_by("pk")[2]
    assert "sq" not in big.rows[0]["data"]  # (2^53)^4 → 중간 결과 상한 초과 → 계산 불가
    tree = validation.parse_expression("a*a/3 > 1")
    with pytest.raises(validation.Missing):
        validation.evaluate(tree, {"a": 10 ** 200})
    with pytest.raises(validation.Missing):
        validation.evaluate(validation.parse_expression("a/b"), {"a": 1e300, "b": 1e-300})
    _, fields_clean, errs = validation.clean_definition(simple_payload(fields=[
        {"fieldName": "x", "displayName": "x", "dataType": "INTEGER", "constraints": {"min": 1e308}}]))
    assert errs


# ═══ 4. 소속 기관은 본인이 바꿀 수 없다 ═══
def test_profile_cannot_change_organization(client, demo_users, other_researcher):
    shared = services.save_definition(demo_users["researcher"], simple_payload(visibility="shared"), action="draft")
    client.force_login(other_researcher)
    r = client.post("/profile", {"display_name": "다른", "organization": "분당서울대병원", "email": "o@example.org"})
    other_researcher.refresh_from_db()
    assert other_researcher.organization == "몽골국립의료원" and other_researcher.display_name == "다른"
    assert client.get(f"/schemas/{shared.schema_id}").status_code == 404
    assert 'name="organization"' not in client.get("/profile").content.decode()


# ═══ 5. 검사 후 구조가 바뀌면 제출 거부 / 필드 삭제 저장은 같은 트랜잭션에서 데이터 재확인 ═══
def test_submit_rejected_when_schema_changed_after_check(researcher_client, published_schema, demo_users):
    s = published_schema
    c = researcher_client
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__score": "3", "f__note": "x"})
    p = PendingEntry.objects.get()
    # 그 사이 'note' 필드를 지운 구조 저장(데이터가 아직 없으므로 허용)
    services.save_definition(demo_users["researcher"], simple_payload(fields=[simple_payload()["fields"][0]]), schema=s)
    c.post(f"/schemas/{s.schema_id}/data/submit/{p.pk}")
    assert SchemaRecord.objects.count() == 0
    p.refresh_from_db()
    assert not p.consumed  # 다시 검사하도록 되돌림


def test_field_delete_rechecks_data_inside_transaction(published_schema, demo_users, monkeypatch):
    s = published_schema
    real = services.fields_with_data

    def racing(schema):  # 확인 직전에 다른 요청이 데이터를 넣은 상황
        SchemaRecord.objects.create(schema=schema, record_id="R9", condition_id="X", data={"note": "n"},
                                    author_label="x", created_at=schema.created_at, updated_at=schema.created_at)
        return real(schema)

    monkeypatch.setattr(services, "fields_with_data", racing)
    with pytest.raises(services.SchemaError):
        services.save_definition(demo_users["researcher"], simple_payload(fields=[simple_payload()["fields"][0]]), schema=s)
    assert s.fields.filter(field_name="note").exists()


# ═══ 6. CUSTOM ID — 최대값 기반 채번 + 충돌 재시도 ═══
def test_custom_id_max_based_and_retry(admin_client_portal, loaded_dictionary, monkeypatch):
    from apps.dictionary import admin_services
    from apps.dictionary.models import DictionaryCategory

    cat = DictionaryCategory.objects.get(version=loaded_dictionary, code="pat")
    DictionaryField.objects.create(version=loaded_dictionary, category=cat, dictionary_id="CUSTOM.PAT.007",
                                   field_name="c7", display_name="c7", data_type="TEXT", is_standard=False)
    f = admin_services.add_entry(None, cat, {"field_name": "c8", "display_name": "c8", "data_type": "TEXT"})
    assert f.dictionary_id == "CUSTOM.PAT.008"
    real = admin_services._next_custom_id
    calls = []

    def stale(ver, c):  # 첫 시도는 이미 쓰인 번호(동시 채번 경합)
        calls.append(1)
        return "CUSTOM.PAT.008" if len(calls) == 1 else real(ver, c)

    monkeypatch.setattr(admin_services, "_next_custom_id", stale)
    g = admin_services.add_entry(None, cat, {"field_name": "c9", "display_name": "c9", "data_type": "TEXT"})
    assert g.dictionary_id == "CUSTOM.PAT.009" and len(calls) == 2


# ═══ 7. 첨부 디스크 소진 방지 ═══
def _file_schema(user, **cons):
    return services.save_definition(user, simple_payload(fields=[
        {"fieldId": "f", "fieldName": "doc", "displayName": "문서", "dataType": "FILE", "constraints": cons},
        {"fieldId": "n", "fieldName": "n", "displayName": "N", "dataType": "INTEGER", "isRequired": True}]), action="publish")


def test_pending_attachment_quota_and_failed_check_cleanup(researcher_client, demo_users, settings):
    s = _file_schema(demo_users["researcher"])
    c = researcher_client
    # 검사 실패(필수 n 없음) → 첨부는 즉시 삭제
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__doc": SimpleUploadedFile("a.txt", b"x" * 10)})
    assert SchemaFile.objects.count() == 0 and not list((settings.MEDIA_ROOT / "schemas").rglob("*.txt"))
    settings.SCHEMA_PENDING_MAX_BYTES = 25
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "n": "1", "f__n": "1",
                                                   "f__doc": SimpleUploadedFile("a.txt", b"x" * 20)})
    assert SchemaFile.objects.count() == 1
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__n": "1",
                                                   "f__doc": SimpleUploadedFile("b.txt", b"x" * 20)})
    assert "제출하지 않은 첨부가 너무 많습니다" in PendingEntry.objects.latest("pk").errors[0]["ko"]
    assert SchemaFile.objects.count() == 1


def test_cleanup_media_removes_stale_schema_files(researcher_client, demo_users, settings):
    s = _file_schema(demo_users["researcher"])
    researcher_client.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__n": "1",
                                                                   "f__doc": SimpleUploadedFile("a.txt", b"x" * 20)})
    sf = SchemaFile.objects.get()
    path = settings.MEDIA_ROOT / sf.file.name
    orphan = settings.MEDIA_ROOT / "schemas" / "orphan.bin"
    orphan.write_bytes(b"z")
    assert path.exists()
    call_command("cleanup_media", "--min-age-minutes", "0", stdout=io.StringIO())
    assert not SchemaFile.objects.exists() and not path.exists() and not orphan.exists()


# ═══ NIT ═══
def test_file_magic_bytes_checked_for_known_types(researcher_client, demo_users):
    s = _file_schema(demo_users["researcher"], allowedFileTypes=[".pdf", ".png", ".txt"])
    c = researcher_client
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__n": "1",
                                                   "f__doc": SimpleUploadedFile("fake.pdf", b"MZ\x90\x00", "application/pdf")})
    assert "PDF 파일 형식이 아닙니다" in PendingEntry.objects.latest("pk").errors[0]["ko"]
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__n": "1",
                                                   "f__doc": SimpleUploadedFile("fake.png", b"GIF89a", "image/png")})
    assert "PNG" in PendingEntry.objects.latest("pk").errors[0]["ko"]
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__n": "1",
                                                   "f__doc": SimpleUploadedFile("note.txt", b"plain", "text/plain")})
    assert not PendingEntry.objects.latest("pk").errors  # 미지원 형식은 확장자 검사만


def test_xlsx_zip_bomb_rejected(admin_client_portal, loaded_dictionary, monkeypatch):
    from apps.dictionary import admin_services

    monkeypatch.setattr(admin_services, "XLSX_MAX_UNCOMPRESSED", 1024 * 1024)  # 테스트용으로 낮춘 상한
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml", b"0" * (3 * 1024 * 1024))
    r = admin_client_portal.post("/admin/dictionary/version/upload",
                                 {"file": SimpleUploadedFile("bomb.xlsx", buf.getvalue(), "application/octet-stream")})
    assert any("압축" in m.message for m in r.wsgi_request._messages)


def test_rule_not_operator_does_not_touch_string_literals():
    assert validation._js_ops_to_python("a != 'x!y && z' && !b") == "a != 'x!y && z'  and   not b"
    tree = validation.parse_expression("note == 'hi!' && !flag")
    assert validation.evaluate(tree, {"note": "hi!", "flag": False}) is True


def test_builder_mentions_python_regex(researcher_client):
    assert "Python 정규식 문법" in researcher_client.get("/schemas/create").content.decode()


# ═══ 헤더 추가: 시드 연결 키 변환 ═══
def test_seed_condition_ids_converted_and_linked(seeded_patients, loaded_dictionary):
    res = services.load_sample_schemas()
    assert res["link_unmatched"] == 6  # MN-2024-0015·VN-2024-0042(각 2), IN-2024-0023, IN-2024-0012 는 시드 환자에 없음
    assert not SchemaRecord.objects.filter(condition_id__startswith="COND-").exists()
    assert SchemaRecord.objects.get(record_id="row-ae-001").condition_id == "KR-2024-0001"
    assert services.convert_seed_condition_id("COND-2024-KR-0001") == "KR-2024-0001"
    assert services.convert_seed_condition_id("KR-2024-0001") == "KR-2024-0001"


def test_data_migration_converts_existing_rows(published_schema):
    from importlib import import_module

    from django.apps import apps as django_apps

    SchemaRecord.objects.create(schema=published_schema, record_id="row-x-1", condition_id="COND-2024-MN-0008", data={},
                                author_label="s", created_at=published_schema.created_at, updated_at=published_schema.created_at)
    SchemaRecord.objects.create(schema=published_schema, record_id="REC-1", condition_id="COND-2024-MN-0008", data={},
                                author_label="u", created_at=published_schema.created_at, updated_at=published_schema.created_at)
    import_module("apps.schemas.migrations.0002_convert_seed_condition_ids").forward(django_apps, None)
    assert SchemaRecord.objects.get(record_id="row-x-1").condition_id == "MN-2024-0008"
    assert SchemaRecord.objects.get(record_id="REC-1").condition_id == "COND-2024-MN-0008"  # 사용자 입력은 그대로
