"""3a 3차 리뷰: 정규식 컴파일 DoS(정적 검사), 정의 JSON 타입 혼동 500, 고유값 조회식 분해, 발행 strict 재검증,
가져오기 비공개 강제."""
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.schemas import services, validation
from apps.schemas.models import PendingEntry, Schema, SchemaField, SchemaRecord

from .conftest import simple_payload

pytestmark = pytest.mark.django_db

BOMB = "(?:(?:a{1000}){1000}){1000}"  # 27자 — 그대로 컴파일하면 메모리 고갈


@pytest.fixture
def no_compile(monkeypatch):
    """거부돼야 할 패턴이 regex.compile 에 닿으면 실패(실제로 컴파일하면 테스트 기계가 위험하다)."""
    import regex

    real = regex.compile
    blocked = set()

    def spy(pattern, *a, **kw):
        if pattern in blocked:
            raise AssertionError(f"compiled unsafe pattern: {pattern!r}")
        return real(pattern, *a, **kw)

    monkeypatch.setattr(regex, "compile", spy)
    validation.compile_cache_clear()
    return blocked


# ── 1. 정규식 컴파일 전 정적 검사 ─────────────────────────────
UNSAFE = [
    BOMB,
    "a{1001}",
    "x{0,5000}",
    "a{,1001}",
    "a{1001,}",
    "(?:a{1000}){11}",                         # 곱 11,000
    "(?:a{101,}){100}",                        # {n,} 은 하한 기준 → 10,100
    "(?:(?:a{10}){10}){101}",                  # 3단 중첩 10,100
    "(?:a{1000}){10}(?:a{1000}){10}",          # 각 10,000 이지만 펼친 크기 20,000
    "(?x)(?:a{100}) {101}",                    # verbose 모드 공백 우회
    "(?:a{100}){e<=1}{101}",                   # 퍼지 제약 뒤 반복 우회
    "(?:a{1000}){1000}" * 12,                  # 리뷰 재현(길이 상한 이내 일부만 사용돼도 거부)
]
SAFE = [
    r"^Rv\d{4}[a-c]?$",                        # 원본 시드 패턴
    r"^[A-Z]{2}-\d{4}-\d{4}$",
    r"^(ICD|SNOMED)-\w+$",
    r"\{1001\}",                               # 이스케이프된 중괄호
    r"[{1001}]",                               # 문자 클래스 안 중괄호
    r"[\]{]{3}",
    r"^\d{1000}$",
    r"(?:a{100}){100}",                        # 곱 정확히 10,000
    r"(?:[A-Z]+-){1,3}\d+",
    r"\p{L}{3}",
    r"a{2}?b{3}",                              # (4차: possessive {3}+ ·(?x) 는 허용 문법 밖 → test_review_fixes4)
    r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$",
]


@pytest.mark.parametrize("pattern", UNSAFE)
def test_unsafe_pattern_rejected_before_compile(no_compile, pattern):
    no_compile.add(pattern)
    assert validation.check_user_regex(pattern) is not None
    with pytest.raises(validation.UnsafePattern):
        validation.compile_user_regex(pattern)
    assert pattern not in validation.compile_cache_patterns()  # 거부된 패턴은 캐시에 남지 않는다


# a{10000} 꼴: 9,999자 'a' + 꼬리 입력에서 위치마다 10,000자 비교 — 1,000자 TEXT 에서는 즉시 실패(길이 부족)
TEXTAREA_SLOW = {r"(?:a{100}){100}"}


@pytest.mark.parametrize("pattern", SAFE)
def test_safe_patterns_still_pass(pattern):
    assert validation.regex_static_problem(pattern) is None, pattern
    assert validation.check_user_regex(pattern, max_len=validation.MAX_TEXT_DEFAULT["TEXT"]) is None, pattern
    if pattern in TEXTAREA_SLOW:  # 5차 리뷰: 탐침 = 실제 최대 입력 길이 — TEXTAREA(10,000자) 입력에서는 실제로 느리다
        problem = validation.check_user_regex(pattern)  # 기계 속도에 따라 0.1초 timeout 을 넘거나 못 넘는다
        assert problem is None or "시간 초과" in problem, pattern
    else:
        assert validation.check_user_regex(pattern) is None, pattern


def test_seed_patterns_pass_and_seed_schemas_publishable(seeded_schemas):
    pats = [f.constraints["pattern"] for f in SchemaField.objects.all() if f.constraints.get("pattern")]
    assert pats
    for p in pats:
        assert validation.check_user_regex(p) is None, p


def test_compile_resource_errors_become_definition_errors(monkeypatch):
    import regex

    for exc in (MemoryError, OverflowError, RecursionError):
        def boom(*a, _exc=exc, **kw):
            raise _exc()

        monkeypatch.setattr(regex, "compile", boom)
        validation.compile_cache_clear()
        problem = validation.regex_problem(f"abc{exc.__name__}")
        assert problem and "컴파일" in problem["ko"] and problem["en"]


def test_compile_cache_is_small_and_holds_only_checked():
    assert validation.COMPILE_CACHE_MAX == 64


def _bomb_payload():
    return simple_payload(fields=[{"fieldId": "k1", "fieldName": "code", "displayName": "코드", "dataType": "TEXT",
                                   "constraints": {"pattern": BOMB}}])


def test_unsafe_pattern_rejected_on_all_definition_endpoints(researcher_client, no_compile):
    no_compile.add(BOMB)
    c = researcher_client
    payload = json.dumps(_bomb_payload())
    for action in ("draft", "save", "publish"):
        r = c.post("/schemas/create", {"payload": payload, "action": action})
        assert r.status_code == 400 and "정규식이 너무 큽니다" in r.content.decode()
    assert Schema.objects.count() == 0
    r = c.post("/schemas/builder/preview", {"payload": payload, "fill": "1"})
    assert r.status_code == 200
    exported = {"name": "X", "fields": _bomb_payload()["fields"]}
    r = c.post("/schemas/import", {"file": SimpleUploadedFile("s.json", json.dumps(exported).encode(), "application/json")})
    assert r.status_code == 200 and "import-error" in r.content.decode()
    r = c.post("/schemas/import", {"confirm": "1", "json": json.dumps(exported)})
    assert r.status_code == 200 and "import-error" in r.content.decode()
    assert Schema.objects.count() == 0


def test_legacy_unsafe_pattern_in_db_is_pattern_error(researcher_client, demo_users, no_compile):
    no_compile.add(BOMB)
    s = services.save_definition(demo_users["researcher"], simple_payload(), action="publish")
    f = s.fields.get(field_name="note")
    f.constraints = {"pattern": BOMB}  # 정적 검사 이전에 저장된 위험 패턴
    f.save()
    r = researcher_client.post(f"/schemas/{s.schema_id}/data/check",
                               {"mode": "form", "link_value": "A1", "f__score": "3", "f__note": "aaa"})
    assert r.status_code == 302
    errs = PendingEntry.objects.get().errors
    assert any(e["field"] == "note" and "정규식 패턴 오류" in e["ko"] for e in errs)
    csv = b"condition_id,score,note\nA1,3,aaa\n"
    r = researcher_client.post(f"/schemas/{s.schema_id}/data/check",
                               {"mode": "csv", "file": SimpleUploadedFile("d.csv", csv, "text/csv")})
    assert r.status_code == 302
    assert SchemaRecord.objects.count() == 0


# ── 2. 정의 JSON 타입 혼동 ─────────────────────────────────
BIG = "1" + "0" * 400


def _variants():
    """(라벨, 빌더용 JSON 텍스트, 내보내기 형식 JSON 텍스트). 1e400·NaN 등은 JSON 리터럴 그대로 넣는다."""
    base = simple_payload()
    out = []

    def emit(label, p, lit=None):
        nested = json.dumps(p)
        flat = json.dumps({**p["meta"], "fields": p["fields"]})
        if lit is not None:
            nested, flat = nested.replace('"__X__"', lit), flat.replace('"__X__"', lit)
        out.append((label, nested, flat))

    for key, val in [("category", ["clinical_ext"]), ("category", {"a": 1}), ("visibility", ["public"]),
                     ("visibility", {"a": 1}), ("linkFieldName", ["condition_id"]), ("linkFieldName", {"x": 1}),
                     ("linkField", "condition_id"), ("linkField", ["condition_id"]), ("linkField", {"fieldName": ["x"]}),
                     ("linkField", {"fieldName": {"x": 1}})]:
        p = json.loads(json.dumps(base))
        if key == "linkField":
            p["meta"].pop("linkFieldName", None)
        p["meta"][key] = val
        emit(f"meta.{key}={val!r}", p)
    for val in (["INTEGER"], {"t": 1}):
        p = json.loads(json.dumps(base))
        p["fields"][0]["dataType"] = val
        emit(f"dataType={val!r}", p)
    for k in ("min", "max", "maxLength", "precision"):
        for lit in (BIG, "1e400", "-1e400", "NaN", "Infinity"):
            p = json.loads(json.dumps(base))
            p["fields"][0]["constraints"][k] = "__X__"
            p["fields"][1]["constraints"][k] = "__X__"
            emit(f"{k}={lit[:8]}", p, lit)
    return out


VARIANTS = _variants()


@pytest.mark.parametrize("label,payload,flat", VARIANTS, ids=[v[0] for v in VARIANTS])
def test_type_confused_definition_no_500(researcher_client, label, payload, flat):
    """형식이 어긋난 값은 정의 오류(400·import-error)거나, 무시해도 되는 값(문자열 linkField, 해당 타입에 없는 제약)이면
    정상 처리 — 어느 쪽이든 500 은 없다(테스트 클라이언트는 뷰 예외를 그대로 올린다)."""
    c = researcher_client
    for action in ("draft", "save"):
        r = c.post("/schemas/create", {"payload": payload, "action": action})
        assert r.status_code in (302, 400), (label, action)
    assert c.post("/schemas/builder/preview", {"payload": payload}).status_code == 200
    for text in (payload, flat):
        r = c.post("/schemas/import", {"confirm": "1", "json": text})
        assert r.status_code in (200, 204), label
        r = c.post("/schemas/import", {"file": SimpleUploadedFile("s.json", text.encode(), "application/json")})
        assert r.status_code == 200, label
    assert all(s.visibility == Schema.Visibility.PRIVATE for s in Schema.objects.filter(name="테스트 구조")
               if s.logs.filter(action="import").exists())


@pytest.mark.parametrize("label,payload,flat", [v for v in VARIANTS if not v[0].startswith(("meta.linkField=", "precision"))],
                         ids=[v[0] for v in VARIANTS if not v[0].startswith(("meta.linkField=", "precision"))])
def test_type_confused_values_are_definition_errors(label, payload, flat):
    from apps.core.safejson import loads_user_json

    for text in (payload, flat):
        _, _, errs = validation.clean_definition(loads_user_json(text), strict=False)
        assert errs, label


def test_num_rejects_overflow_and_nonfinite():
    for v in (10 ** 400, float("inf"), float("nan"), "1e400", "inf", "-inf", "nan", [1], {"a": 1}, 1e16):
        with pytest.raises(ValueError):
            validation._num(v)
    assert validation._num("12") == 12 and validation._num(1.5) == 1.5


# ── 4. 고유값 조회식 ─────────────────────────────────────
LOOKUP_NAMES = ["a__b", "a_", "contains", "in", "isnull", "exact", "keys", "has_key", "plain"]


def _unique_schema(user, dtype="TEXT", names=LOOKUP_NAMES, constraints=None):
    fields = [{"fieldId": f"u{i}", "fieldName": f"fld{i}", "displayName": n, "dataType": dtype, "isUnique": True,
               "constraints": constraints or {}} for i, n in enumerate(names)]
    s = services.save_definition(user, simple_payload(fields=fields), action="publish")
    for i, n in enumerate(names):  # 새 이름 규칙 이전에 저장된(또는 조회명과 같은) 필드명
        SchemaField.objects.filter(schema=s, field_name=f"fld{i}").update(field_name=n)
    return s


def _rec(s, data, rid):
    return SchemaRecord.objects.create(schema=s, record_id=rid, condition_id="X1", data=data, author_label="x",
                                       created_at=s.created_at, updated_at=s.created_at)


@pytest.mark.parametrize("name", LOOKUP_NAMES)
def test_unique_check_with_lookup_like_field_names(demo_users, name):
    s = _unique_schema(demo_users["researcher"], names=[name])
    _rec(s, {name: "dup"}, "REC-EXIST-0001")
    fields = list(s.fields.all())
    issues = []
    services._check_unique(s, fields, [{"data": {name: "dup"}, "_row": 1}], issues)
    assert [i["field"] for i in issues] == [name]
    issues = []
    services._check_unique(s, fields, [{"data": {name: "other"}, "_row": 1}], issues)
    assert issues == []


@pytest.mark.parametrize("dtype,stored,same,different,cons", [
    ("BOOLEAN", True, True, False, {}),
    ("INTEGER", 5, 5, 6, {}),
    ("DECIMAL", 1.5, 1.5, 2.5, {}),
    ("DATE", "2026-01-01", "2026-01-01", "2026-01-02", {}),
    ("SELECT", "x", "x", "y", {"options": [{"value": "x"}, {"value": "y"}]}),
])
def test_unique_check_types_kept(demo_users, dtype, stored, same, different, cons):
    names = ["a__b", "plain"]
    s = _unique_schema(demo_users["researcher"], dtype=dtype, names=names, constraints=cons)
    _rec(s, {n: stored for n in names}, "REC-EXIST-0002")
    fields = list(s.fields.all())
    for n in names:
        issues = []
        services._check_unique(s, fields, [{"data": {n: same}, "_row": 1}], issues)
        assert len(issues) == 1, (dtype, n)
        issues = []
        services._check_unique(s, fields, [{"data": {n: different}, "_row": 1}], issues)
        assert issues == [], (dtype, n)
    if dtype == "INTEGER":  # JSON 1 과 true 는 다른 값
        _rec(s, {"plain": True}, "REC-EXIST-0003")
        issues = []
        services._check_unique(s, fields, [{"data": {"plain": 1}, "_row": 1}], issues)
        assert issues == []


def test_unique_end_to_end_double_underscore_name(researcher_client, demo_users):
    s = _unique_schema(demo_users["researcher"], names=["a__b"])
    post = {"mode": "form", "link_value": "A1", "f__a__b": "same"}
    c = researcher_client
    c.post(f"/schemas/{s.schema_id}/data/check", post)
    c.post(f"/schemas/{s.schema_id}/data/check", post)
    p1, p2 = PendingEntry.objects.order_by("pk")
    assert not p1.errors and not p2.errors
    c.post(f"/schemas/{s.schema_id}/data/submit/{p1.pk}")
    assert SchemaRecord.objects.filter(schema=s).count() == 1
    c.post(f"/schemas/{s.schema_id}/data/submit/{p2.pk}")  # 제출 재검증에서 막힌다
    assert SchemaRecord.objects.filter(schema=s).count() == 1
    c.post(f"/schemas/{s.schema_id}/data/check", post)
    assert PendingEntry.objects.order_by("-pk").first().errors


@pytest.mark.parametrize("bad", ["a__b", "a_", "ab__", "x___y"])
def test_new_definitions_reject_double_and_trailing_underscore(demo_users, bad):
    fields = [{"fieldId": "k1", "fieldName": bad, "displayName": "X", "dataType": "TEXT"}]
    with pytest.raises(services.SchemaError) as ei:
        services.save_definition(demo_users["researcher"], simple_payload(fields=fields), action="draft")
    assert any("연속 밑줄" in e["ko"] for e in ei.value.errors)


def test_existing_legacy_field_name_still_editable(demo_users):
    s = _unique_schema(demo_users["researcher"], names=["a__b"])
    _rec(s, {"a__b": "v"}, "REC-EXIST-0004")
    obj = services.schema_to_json(s)
    obj["description"] = "수정"
    saved = services.save_definition(demo_users["researcher"], obj, schema=s, action="save")
    assert saved.description == "수정" and saved.fields.filter(field_name="a__b").exists()


def test_dictionary_field_names_follow_new_rule(loaded_dictionary):
    from apps.dictionary.models import DictionaryField

    bad = [n for n in DictionaryField.objects.values_list("field_name", flat=True) if "__" in n or n.endswith("_")]
    assert bad == []  # 빌더가 데이터사전 항목을 그대로 가져와도 새 이름 규칙에 걸리지 않는다


# ── 6. 발행 전이 strict 재검증 ───────────────────────────────
def _draft_with(user, field):
    s = services.save_definition(user, simple_payload(fields=[field]), action="draft")
    assert s.status == Schema.Status.DRAFT
    return s


@pytest.mark.parametrize("field", [
    {"fieldId": "k1", "fieldName": "pick", "displayName": "선택", "dataType": "SELECT", "isRequired": True,
     "constraints": {"options": []}},
    {"fieldId": "k1", "fieldName": "calc", "displayName": "계산", "dataType": "CALCULATED", "constraints": {}},
])
def test_publish_transition_runs_strict_validation(client, demo_users, field):
    s = _draft_with(demo_users["researcher"], field)
    client.force_login(demo_users["researcher"])
    r = client.post(f"/schemas/{s.schema_id}/status", {"action": "publish"})
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.status == Schema.Status.DRAFT
    msgs = [m.message for m in r.wsgi_request._messages]
    assert any("발행할 수 없습니다" in m for m in msgs)
    client.force_login(demo_users["admin"])
    r = client.post(f"/admin/schemas/{s.schema_id}/action", {"action": "publish"})
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.status == Schema.Status.DRAFT
    with pytest.raises(services.SchemaError) as ei:
        services.transition(s, "publish", demo_users["admin"])
    assert "Cannot publish" in ei.value.en and ei.value.errors


def test_publish_transition_still_works_for_valid_draft(researcher_client, demo_users):
    s = services.save_definition(demo_users["researcher"], simple_payload(), action="draft")
    r = researcher_client.post(f"/schemas/{s.schema_id}/status", {"action": "publish"})
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.status == Schema.Status.PUBLISHED


def test_seed_draft_publishable(seeded_schemas, demo_users):
    for s in Schema.objects.filter(status=Schema.Status.DRAFT):
        assert services.publish_problems(s) == [], s.schema_id


# ── 7. 가져오기는 항상 비공개 ─────────────────────────────────
@pytest.mark.parametrize("shape", ["meta", "flat"])
def test_import_always_private(researcher_client, shape):
    fields = simple_payload()["fields"]
    if shape == "meta":
        obj = {"name": "공개 시도", "fields": fields, "meta": {"name": "공개 시도", "visibility": "public"}}
    else:
        obj = {"name": "공개 시도", "visibility": "public", "fields": fields}
    r = researcher_client.post("/schemas/import", {"confirm": "1", "json": json.dumps(obj)})
    assert r.status_code == 204
    assert Schema.objects.get().visibility == Schema.Visibility.PRIVATE


# ── 5. 짧은 ID 를 페이지 전체에서 'not in' 으로 검사하던 불안정 테스트 ─────────────
def test_short_id_check_was_flaky_and_new_id_cannot_collide(client, published_schema, demo_users, other_researcher,
                                                           monkeypatch):
    """CSRF 토큰(영숫자 64자)에 'R1' 이 우연히 들어가면 예전 검사(assert "R1" not in page)가 실패했다.
    토큰을 강제로 'R1…' 로 만들어도 새 검사(하이픈이 든 긴 ID — 토큰 알파벳 밖)는 흔들리지 않는다."""
    from django.middleware import csrf

    monkeypatch.setattr(csrf, "_mask_cipher_secret", lambda secret: "R1" * 32)
    s = published_schema
    _rec(s, {"score": 3}, "REC-OTHERS-ROW-QZ7X")
    client.force_login(other_researcher)
    page = client.get(f"/schemas/{s.schema_id}/data?method=edit").content.decode()
    assert "R1R1" in page  # 예전 검사라면 여기서 실패
    assert "REC-OTHERS-ROW-QZ7X" not in page


@pytest.mark.parametrize("key", ["category", "version", "nameEn", "description", "descriptionEn"])
@pytest.mark.parametrize("val", [[1], {"a": 1}, None, True, 10 ** 30])
def test_import_preview_with_meta_wrapper_and_odd_top_level_values(researcher_client, key, val):
    """meta 객체가 있으면 검증은 meta 를 보지만, 미리보기 화면은 최상위 값을 그린다 — 형식이 어긋나도 500 금지."""
    obj = {"name": "가져오기", "fields": simple_payload()["fields"], "meta": {"name": "가져오기"}, key: val}
    r = researcher_client.post("/schemas/import", {"file": SimpleUploadedFile("s.json", json.dumps(obj).encode(),
                                                                             "application/json")})
    assert r.status_code == 200
