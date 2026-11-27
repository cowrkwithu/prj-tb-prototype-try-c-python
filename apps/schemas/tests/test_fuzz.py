"""데이터 정의·관리자 데이터사전/구조 관리 URL 전체 퍼징 — 어떤 입력에도 500 을 내지 않는다(CLAUDE.md 코딩 규칙).

패턴은 apps/cohorts/tests/test_review_fixes3.py 를 따른다. JSON 이스케이프로 들어오는 서로게이트·NUL 도 포함.
"""
import json

import pytest

from apps.schemas.models import Schema

pytestmark = pytest.mark.django_db

ATTACKS = {
    "long": "가" * 5000,
    "nul": "a\x00b",
    "control": "\r\n=cmd|' /C calc'!A0",
    "html": "<script>alert(1)</script>\"'><img src=x onerror=1>",
    "unicode-digits": "²٣１",
    "negative": "-1",
    "huge-int": "9" * 60,
    "float": "1e308",
    "broken-json": "{not json",
    "deep-json": "[" * 5000,
    "json-surrogate-value": '{"meta": {"name": "a\\udfff"}, "fields": []}',
    "json-surrogate-key": '{"meta": {"na\\ud800me": "x"}, "fields": [{"fi\\udfffeldName": "a"}]}',
    "json-nul": '{"meta": {"name": "a\\u0000b"}, "fields": [{"fieldName": "a", "displayName": "\\u0000", "dataType": "TEXT"}]}',
    "json-field-surrogate": json.dumps({"meta": {"name": "S"}, "fields": [
        {"fieldName": "a", "displayName": "x", "dataType": "SELECT", "constraints": {"options": [{"value": "v"}]}}]}
    ).replace('"v"', '"v\\udfff"'),
    "path-traversal": "../../etc/passwd",
}

GET_PARAMS = ("q", "page", "sort", "dir", "tab", "category", "status", "visibility", "method", "row", "pending",
              "cat", "dep", "view", "id", "action", "back", "field")
GET_URLS = ["/schemas", "/schemas/schema-001", "/schemas/schema-001/data", "/schemas/select", "/schemas/import",
            "/schemas/builder/dictionary", "/schemas/case/KR-2024-0001", "/schemas/schema-001/records.csv",
            "/schemas/schema-001/template.csv", "/schemas/schema-001/export.json", "/schemas/schema-001/records/row-ae-001",
            "/schemas/schema-004/edit", "/schemas/create", "/admin/schemas", "/admin/schemas/schema-001",
            "/admin/schemas/schema-001/confirm", "/admin/dictionary", "/admin/dictionary/category/modal",
            "/admin/dictionary/entry/modal"]


@pytest.fixture
def world(seeded_patients, seeded_schemas):
    return Schema.objects.get(schema_id="schema-001")


def _ok(r, label):
    assert r.status_code < 500, (label, r.status_code)


# 원시(raw) 서로게이트 폼 값은 테스트 클라이언트가 인코딩하지 못한다 — core 미들웨어 테스트(test_safe_input)가 400 을 확인
@pytest.mark.parametrize("name", list(ATTACKS))
def test_no_500_on_get_urls(admin_client_portal, world, name):
    v = ATTACKS[name]
    for url in GET_URLS:
        _ok(admin_client_portal.get(url, {k: v for k, v in ((p, v) for p in GET_PARAMS)}), (url, name))
        _ok(admin_client_portal.get(url, {"q": v}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="schema-grid"), (url, name, "hx"))
    _ok(admin_client_portal.get("/schemas/schema-001", {"tab": "data", "q": v},
                                HTTP_HX_REQUEST="true", HTTP_HX_TARGET="schema-records"), name)


@pytest.mark.parametrize("name", list(ATTACKS))
def test_no_500_on_post_urls(admin_client_portal, researcher_client, world, name):
    v = ATTACKS[name]
    a, r = admin_client_portal, researcher_client
    n = Schema.objects.count()
    posts = [
        (r, "/schemas/create", {"payload": v, "action": v, "rev": v}),
        (a, "/schemas/schema-004/edit", {"payload": v, "action": "save", "rev": v}),
        (r, "/schemas/builder/preview", {"payload": v, "fill": "1"}),
        (r, "/schemas/import", {"confirm": "1", "json": v}),
        (r, "/schemas/schema-001/data/check", {"mode": v, "link_value": v, "f__adverse_event_id": v, "f__onset_day": v,
                                                "f__event_category": v, "record_id": v}),
        (r, "/schemas/schema-003/data/check", {"mode": "form", "link_value": "KR-2024-0001", "f__survey_date": v,
                                                "f__physical_function": v, "f__sleep_quality": v, "f__notes": v}),
        (a, "/schemas/schema-001/data/check", {"mode": "edit", "record_id": "row-ae-001", "link_value": v, "f__description": v}),
        (r, "/schemas/schema-001/status", {"action": v}),
        (r, "/schemas/schema-001/duplicate", {"x": v}),
        (a, "/admin/schemas/schema-004/action", {"action": v, "note": v, "back": v}),
        (a, "/admin/schemas/schema-001/visibility", {"visibility": v, "back": v}),
        (a, "/admin/dictionary/category/save", {"code": v, "name": v, "description": v, "icon": v, "color": v}),
        (a, "/admin/dictionary/category/save", {"id": v, "name": v}),
        (a, "/admin/dictionary/entry/save", {"cat": "1", "field_name": "fz_x", "display_name": v, "data_type": v,
                                             "min": v, "max": v, "precision": v, "maxLength": v, "options": v}),
        (a, "/admin/dictionary/entry/save", {"id": "13", "display_name": v, "min": v, "unit": v}),
        (a, "/admin/dictionary/entry/save", {"id": v, "cat": v}),
        (a, "/admin/dictionary/entry/1/deprecate", {"restore": v, "dep": v}),
    ]
    for client, url, data in posts:
        _ok(client.post(url, data), (url, name))
    assert Schema.objects.count() >= n


@pytest.mark.parametrize("payload", [
    '{"meta": {"name": "a\\udfff"}, "fields": [{"fieldName": "a", "displayName": "x", "dataType": "TEXT"}]}',
    '{"meta": {"name": "S", "tags": ["\\ud800"]}, "fields": [{"fieldName": "a", "displayName": "x", "dataType": "TEXT"}]}',
    '{"meta": {"name": "S"}, "fields": [{"fieldName": "a", "displ\\udfffayName": "x", "dataType": "TEXT"}]}',
    '{"meta": {"name": "a\\u0000b"}, "fields": [{"fieldName": "a", "displayName": "x", "dataType": "TEXT"}]}',
])
def test_surrogate_and_nul_json_rejected_without_saving(researcher_client, demo_users, payload):
    r = researcher_client.post("/schemas/create", {"payload": payload, "action": "draft"})
    assert r.status_code in (302, 400) and Schema.objects.count() == 0
    r = researcher_client.post("/schemas/builder/preview", {"payload": payload})
    assert r.status_code == 200
    r = researcher_client.post("/schemas/import", {"confirm": "1", "json": payload})
    assert r.status_code == 200 and "import-error" in r.content.decode() and Schema.objects.count() == 0
    from django.core.files.uploadedfile import SimpleUploadedFile
    r = researcher_client.post("/schemas/import", {"file": SimpleUploadedFile("s.json", payload.encode(), "application/json")})
    assert r.status_code == 200 and "import-error" in r.content.decode()


def test_nul_in_entry_values_rejected(researcher_client, published_schema):
    from apps.schemas.models import PendingEntry

    s = published_schema
    researcher_client.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__score": "3", "f__note": "a\x00b"})
    p = PendingEntry.objects.get()
    assert any(e["field"] == "note" for e in p.errors)
    page = researcher_client.get(f"/schemas/{s.schema_id}/data?method=form&pending={p.pk}")
    assert page.status_code == 200 and "\x00" not in page.content.decode()


# ── 구조 정의 JSON 의 각 키에 형식이 어긋난 값(3a 3차 리뷰) ─────────────────────
# JSON 리터럴 그대로 넣는다: 거대 정수·1e400(→ inf)·NaN 은 json.dumps 로 만들 수 없는 표기
TYPE_VALUES = {"list": '[1, "a"]', "list-of-dict": '[{"a": 1}]', "dict": '{"a": [1]}', "null": "null", "true": "true",
               "false": "false", "huge-int": "1" + "0" * 400, "1e400": "1e400", "-1e400": "-1e400", "nan": "NaN",
               "empty-str": '""'}
META_KEYS = ["name", "nameEn", "name_en", "description", "descriptionEn", "description_en", "category", "visibility",
             "linkFieldName", "linkField", "tags", "tagsEn", "fieldGroups", "validationRules"]
FIELD_KEYS = ["fieldId", "fieldName", "displayName", "displayNameEn", "description", "descriptionEn", "dataType",
              "constraints", "isRequired", "isUnique", "group", "tags", "sourceRef", "defaultValue", "order"]
CONSTRAINT_FUZZ_KEYS = sorted({"minLength", "maxLength", "pattern", "patternMessage", "patternMessageEn", "min", "max",
                               "unit", "precision", "minDate", "maxDate", "options", "maxSelections", "allowedFileTypes",
                               "maxFileSizeMB", "refDictionary", "refField", "formula", "codeSystem", "codeSystemEn"})
ALL_TYPES = ["TEXT", "TEXTAREA", "INTEGER", "DECIMAL", "NUMERIC_RANGE", "BOOLEAN", "DATE", "DATETIME", "SELECT",
             "MULTISELECT", "FILE", "REFERENCE", "CALCULATED", "CODED"]
MARK = '"__FUZZ__"'


def _fuzz_base():
    fields = []
    for i, t in enumerate(ALL_TYPES):
        cons = {"options": [{"value": "a"}]} if t in ("SELECT", "MULTISELECT") else {}
        if t == "CALCULATED":
            cons = {"formula": "f2 + 1"}
        fields.append({"fieldId": f"z{i}", "fieldName": f"f{i}", "displayName": t, "dataType": t, "constraints": cons})
    return {"meta": {"name": "퍼징", "category": "clinical_ext", "visibility": "private", "linkFieldName": "condition_id"},
            "fields": fields}


def _fuzz_texts(where, key):
    """(빌더용 JSON, 내보내기 형식 JSON) — where: meta | field | constraint."""
    base = _fuzz_base()
    if where == "meta":
        base["meta"][key] = "__FUZZ__"
    elif where == "field":
        base["fields"][0][key] = "__FUZZ__"
        base["fields"][3][key] = "__FUZZ__"
        base["fields"][8][key] = "__FUZZ__"
    else:
        for f in base["fields"]:
            f["constraints"][key] = "__FUZZ__"
    flat = {**base["meta"], "fields": base["fields"]}
    texts = [json.dumps(base, ensure_ascii=False), json.dumps(flat, ensure_ascii=False)]
    if where == "meta":  # meta 는 정상이고 최상위(가져오기 미리보기가 그리는 값)만 어긋난 경우
        wrapped = _fuzz_base()
        wrapped.update({"name": "퍼징", key: "__FUZZ__"})
        texts.append(json.dumps(wrapped, ensure_ascii=False))
    return texts


@pytest.mark.parametrize("where,key", [("meta", k) for k in META_KEYS] + [("field", k) for k in FIELD_KEYS]
                         + [("constraint", k) for k in CONSTRAINT_FUZZ_KEYS])
def test_definition_type_fuzz_no_500(researcher_client, where, key):
    from django.core.files.uploadedfile import SimpleUploadedFile

    c = researcher_client
    texts = _fuzz_texts(where, key)
    for vname, lit in TYPE_VALUES.items():
        p = texts[0].replace(MARK, lit)
        for action in ("draft", "save"):
            _ok(c.post("/schemas/create", {"payload": p, "action": action}), (where, key, vname, action))
        _ok(c.post("/schemas/builder/preview", {"payload": p, "fill": "1"}), (where, key, vname, "preview"))
        for n, text in enumerate(texts):
            text = text.replace(MARK, lit)
            _ok(c.post("/schemas/import", {"confirm": "1", "json": text}), (where, key, vname, "import", n))
            up = SimpleUploadedFile("s.json", text.encode(), "application/json")
            _ok(c.post("/schemas/import", {"file": up}), (where, key, vname, "import-preview", n))
    # 저장된(임시저장 포함) 구조는 모두 화면·내보내기가 열린다
    for s in Schema.objects.all():
        for url in (f"/schemas/{s.schema_id}", f"/schemas/{s.schema_id}/edit", f"/schemas/{s.schema_id}/export.json"):
            _ok(c.get(url), (where, key, url))
