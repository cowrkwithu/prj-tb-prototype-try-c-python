"""서버측 동적 폼 검증 — 원본 제약조건 종류 전부(필수·타입·길이·정규식·범위·소수 자릿수·날짜·선택지·최대 선택 수·
수치 범위·파일·계산·코드·검증 규칙·고유값) 와 구조 정의 검증."""
import pytest

from apps.schemas import validation as v


def F(name, dtype, required=False, **c):
    return {"field_name": name, "display_name": name, "display_name_en": "", "data_type": dtype, "constraints": c,
            "is_required": required, "is_unique": False}


def check(field, raw):
    data, link, issues = v.validate_entry([field], [], {field["field_name"]: raw}, "KR-2024-0001")
    return data.get(field["field_name"]), [i for i in issues if i["severity"] == "error"]


def test_required_and_link_value():
    data, link, issues = v.validate_entry([F("a", "TEXT", True)], [], {"a": "  "}, "")
    fields = {i["field"] for i in issues}
    assert fields == {"a", "condition_id"}
    _, _, issues = v.validate_entry([F("a", "TEXT")], [], {}, "bad id with space")
    assert issues and issues[0]["field"] == "condition_id"
    _, _, issues = v.validate_entry([F("a", "TEXT")], [], {}, "x" * 41)
    assert issues


@pytest.mark.parametrize("raw,ok", [("abc", True), ("a", False), ("abcdefghijk", False)])
def test_text_length(raw, ok):
    val, errs = check(F("t", "TEXT", minLength=2, maxLength=10), raw)
    assert (not errs) == ok


def test_text_pattern_and_message():
    f = F("g", "TEXT", pattern=r"^Rv\d{4}[a-c]?$", patternMessage="Rv 번호 형식", patternMessageEn="Rv format")
    assert check(f, "Rv0667") == ("Rv0667", [])
    val, errs = check(f, "rpoB")
    assert errs[0]["ko"] == "Rv 번호 형식" and errs[0]["en"] == "Rv format"


def test_text_control_characters_rejected():
    _, errs = check(F("t", "TEXT"), "a\x00b")
    assert errs


@pytest.mark.parametrize("raw,expected,ok", [("5", 5, True), ("5.0", 5, True), ("5.5", None, False), ("abc", None, False),
                                             ("-1", None, False), ("11", None, False), ("1e1", 10, True), ("nan", None, False)])
def test_integer_type_and_range(raw, expected, ok):
    val, errs = check(F("i", "INTEGER", min=0, max=10), raw)
    assert (not errs) == ok
    if ok:
        assert val == expected and isinstance(val, int)


def test_decimal_precision_and_range():
    f = F("d", "DECIMAL", min=0, max=10, precision=2)
    assert check(f, "8.25") == (8.25, [])
    assert check(f, "8.255")[1]
    assert check(f, "10.01")[1]
    assert check(f, "inf")[1]
    assert check(F("p", "DECIMAL", min=0, max=1, precision=6), 3e-06) == (3e-06, [])


def test_boolean():
    f = F("b", "BOOLEAN")
    assert check(f, "true")[0] is True and check(f, "아니오")[0] is False and check(f, False)[0] is False
    assert check(f, "maybe")[1]


def test_date_and_datetime_ranges():
    f = F("d", "DATE", minDate="2025-01-01", maxDate="2025-12-31")
    assert check(f, "2025-06-01") == ("2025-06-01", [])
    assert check(f, "2024-12-31")[1] and check(f, "2026-01-01")[1]
    assert check(f, "2025-02-30")[1] and check(f, "06/01/2025")[1]
    dt = F("t", "DATETIME", minDate="2025-01-01")
    assert check(dt, "2025-03-01 09:30") == ("2025-03-01T09:30", [])
    assert check(dt, "2025-03-01T25:00")[1]
    assert check(dt, "2024-03-01T09:00")[1]


OPTS = [{"value": "grade1", "label": "Grade 1 (경증)", "labelEn": "Grade 1 (Mild)"}, {"value": "grade2", "label": "Grade 2"},
        {"value": "grade3", "label": "Grade 3"}]


def test_select_options():
    f = F("s", "SELECT", options=OPTS)
    assert check(f, "grade2") == ("grade2", [])
    assert check(f, "Grade 1 (Mild)") == ("grade1", [])  # CSV 는 라벨도 허용
    assert check(f, "grade9")[1]


def test_multiselect_options_and_max():
    f = F("m", "MULTISELECT", options=OPTS, maxSelections=2)
    assert check(f, ["grade1", "grade3"]) == (["grade1", "grade3"], [])
    assert check(f, "grade1|grade2") == (["grade1", "grade2"], [])
    assert check(f, ["grade1", "grade2", "grade3"])[1]
    assert check(f, ["grade1", "grade1"])[1]
    assert check(f, ["nope"])[1]


def test_numeric_range():
    f = F("r", "NUMERIC_RANGE", min=0, max=100, precision=1)
    assert check(f, {"min": "1.5", "max": "3"}) == ({"min": 1.5, "max": 3}, [])
    assert check(f, "2~4") == ({"min": 2, "max": 4}, [])
    assert check(f, {"min": "5", "max": "1"})[1]
    assert check(f, "1~200")[1]
    assert check(f, "1-2")[1]


def test_file_requires_stored_attachment():
    f = F("f", "FILE", True)
    assert check(f, "evil.exe")[1]
    assert check(f, {"file_id": 3, "name": "a.pdf", "size": 10}) == ({"file_id": 3, "name": "a.pdf", "size": 10}, [])


def test_reference_and_coded_are_text():
    assert check(F("r", "REFERENCE"), "Isoniazid") == ("Isoniazid", [])
    assert check(F("c", "CODED", codeSystem="ICD-10"), "A15.0") == ("A15.0", [])
    assert check(F("c", "CODED"), "x" * 101)[1]


def test_calculated_is_computed_by_server_and_input_ignored():
    fields = [F("fev1_L", "DECIMAL", precision=2), F("fvc_L", "DECIMAL", precision=2),
              F("ratio", "CALCULATED", formula="fev1_L / fvc_L * 100", precision=1)]
    data, _, issues = v.validate_entry(fields, [], {"fev1_L": "2.5", "fvc_L": "3.2", "ratio": "999"}, "X1")
    assert not issues and data["ratio"] == 78.1
    data, _, _ = v.validate_entry(fields, [], {"fev1_L": "2.5", "fvc_L": "0"}, "X1")
    assert "ratio" not in data  # 0 으로 나누기 → 값 없음


def test_validation_rules():
    rules = [{"name": "회복일 검증", "expression": "recovery_days >= 0", "message": "회복 소요일은 0 이상이어야 합니다"}]
    data, _, issues = v.validate_entry([F("recovery_days", "INTEGER")], rules, {"recovery_days": "-3"}, "X1")
    assert issues[0]["ko"] == "회복 소요일은 0 이상이어야 합니다"
    _, _, issues = v.validate_entry([F("recovery_days", "INTEGER")], rules, {}, "X1")
    assert not issues  # 값이 없으면 규칙을 건너뛴다


def test_expression_sandbox_rejects_calls():
    for bad in ["__import__('os')", "a.b", "[1,2]", "lambda: 1", "2 ** 999999"]:
        with pytest.raises(v.DefinitionError):
            v.parse_expression(bad)


# ── 구조 정의 검증 ──
def payload(fields, **meta):
    return {"meta": {"name": "S", **meta}, "fields": fields}


def test_definition_errors():
    _, _, errs = v.clean_definition(payload([
        {"fieldName": "1bad", "displayName": "x", "dataType": "TEXT"},
        {"fieldName": "dup", "displayName": "x", "dataType": "TEXT"},
        {"fieldName": "DUP", "displayName": "x", "dataType": "TEXT"},
        {"fieldName": "ok", "displayName": "x", "dataType": "NOPE"},
        {"fieldName": "rx", "displayName": "x", "dataType": "TEXT", "constraints": {"pattern": "("}},
        {"fieldName": "sel", "displayName": "x", "dataType": "SELECT", "constraints": {"options": []}},
        {"fieldName": "calc", "displayName": "x", "dataType": "CALCULATED", "constraints": {"formula": "rx * 2"}},
        {"fieldName": "rng", "displayName": "x", "dataType": "INTEGER", "constraints": {"min": 5, "max": 1}},
        {"fieldName": "condition_id", "displayName": "x", "dataType": "TEXT"},
    ]))
    text = " | ".join(e["ko"] for e in errs)
    for needle in ["1bad", "중복 필드명", "dataType", "정규식", "선택지를 1개", "숫자 필드가 아닌", "최솟값이 최댓값", "연결 키"]:
        assert needle in text, needle


def test_definition_strips_unknown_constraint_keys_and_lenient_draft():
    meta, fields, errs = v.clean_definition(payload([
        {"fieldName": "s", "displayName": "x", "dataType": "SELECT", "constraints": {"options": [], "evil": "<script>"}}]), strict=False)
    assert not errs and "evil" not in fields[0]["constraints"]
    _, _, errs = v.clean_definition(payload([]), strict=True)
    assert errs  # 저장/발행은 필드 1개 이상
