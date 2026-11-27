import json

import pytest
from django.conf import settings

from scripts.convert_mock_ts import TsLiteralParser, TsParseError, extract_exports


def val(src):
    return extract_exports(f"export const X = {src}")["X"]


def test_parser_handles_ts_literals():
    src = """export const X: Foo[] = [
      // comment
      { id: 1, 'a-b': "x\\"y", c: [true, false, null, undefined], d: -1.5, e: `t`, /* c */
        validationRules: { f: isValid, g: (v) => ['A', 'B'].includes(v), h: (v) => /^(S|E)\\d+$/.test(v) } },
    ]"""
    out = extract_exports(src)["X"]
    assert out == [{"id": 1, "a-b": 'x"y', "c": [True, False, None, None], "d": -1.5, "e": "t",
                    "validationRules": {"f": {"$expr": "isValid"},
                                        "g": {"$expr": "(v) => ['A', 'B'].includes(v)"},
                                        "h": {"$expr": "(v) => /^(S|E)\\d+$/.test(v)"}}}]
    assert TsLiteralParser("'\\u00e9'").value() == "é"


@pytest.mark.parametrize("src,expected", [
    ("1_000", 1000), ("0x1F", 31), ("0X1f", 31), ("0b101", 5), ("0o17", 15), (".5", 0.5),
    ("+5", 5), ("- 1", -1), ("-0.25", -0.25), ("1e3", 1000.0), ("1_000.5", 1000.5), ("[1, -2, +.5]", [1, -2, 0.5]),
])
def test_numbers(src, expected):
    assert val(src) == expected


def test_function_type_annotation():
    assert extract_exports("export const f: (a: number) => void = { a: 1 }")["f"] == {"a": 1}
    assert extract_exports("export const r: Record<string, () => boolean> = [2]")["r"] == [2]


def test_crlf_line_continuation_and_crlf_source():
    assert val("'ab\\\r\ncd'") == "abcd"
    assert val("'ab\\\ncd'") == "abcd"
    src = "export const X = {\r\n  a: 1, // c\r\n  b: 'x',\r\n}\r\n"
    assert extract_exports(src)["X"] == {"a": 1, "b": "x"}


@pytest.mark.parametrize("src", [
    "{ a: isValid }",                    # $expr 허용 목록 밖
    "{ a: (v) => v }",
    "[foo]",
    "{ a }",                             # 단축 프로퍼티(허용 목록 밖)
    "{ a: 1 + 2 }",                      # 연산식
    "{ a: 1 b: 2 }",                     # 쉼표 누락
    "{ a: 1, a: 2 }",                    # 중복 키
    "{ a: 'abc }",                       # 닫히지 않은 문자열
    "{ a: 'ab\ncd' }",                   # 따옴표 문자열 내 줄바꿈
    "{ a: 0x }",                         # 잘못된 숫자
    "{ a: 1.2.3 }",
    "[1,,2]",                            # hole
    "{ a: `x${y}` }",                    # 템플릿 보간
    "{ a: 1 } as const",
])
def test_rejects_unsupported(src):
    with pytest.raises(TsParseError):
        val(src)


def test_keyword_at_end_of_input_and_comment_after():
    # 예전: 문자열 끝의 true/false 에서 IndexError, 값 뒤 주석이 있으면 $expr 로 오변환
    assert val("true") is True
    assert val("false") is False
    assert val("[true /* c */, false // x\n]") == [True, False]
    assert val("{ a: null /* c */ }") == {"a": None}
    assert val("{ validationRules: { a: (v) => v /* , */ > 1 } }") == {"validationRules": {"a": {"$expr": "(v) => v /* , */ > 1"}}}


def test_errors_are_parse_errors_not_index_errors():
    for src in ("'abc", "{ a: ", "[1, 2", "'\\"):
        with pytest.raises(TsParseError):
            val(src)


def test_converted_seed_files_exist():
    seed = settings.SEED_DIR
    expected = {"patients.json": 50, "cohorts.json": 3}
    for name, n in expected.items():
        assert len(json.loads((seed / name).read_text(encoding="utf-8"))) == n
    g = json.loads((seed / "genomics.json").read_text(encoding="utf-8"))
    assert len(g["variants"]) == 20 and len(g["resistanceGenes"]) == 10
    ss = json.loads((seed / "sampleSchemas.json").read_text(encoding="utf-8"))
    assert len(ss["SAMPLE_SCHEMAS"]) == 4 and len(ss["SAMPLE_DYNAMIC_DATA"]) == 14
    us = json.loads((seed / "uploadSchemas.json").read_text(encoding="utf-8"))
    assert len(us) == 5
    for name in ("statistics.json", "rapImaging.json"):
        assert (seed / name).exists()


def test_reconversion_matches_seed_files(tmp_path, monkeypatch):
    """원본 저장소가 있으면 다시 변환해 data/seed 의 JSON 7개와 바이트 단위로 같은지 확인."""
    from scripts import convert_mock_ts as conv

    if not (conv.DEFAULT_SRC / "src" / "data").is_dir():
        pytest.skip("원본 저장소 없음")
    monkeypatch.setattr(conv, "OUT_DIR", tmp_path)
    conv.main([])
    for name in ("patients", "genomics", "cohorts", "statistics", "rapImaging", "sampleSchemas", "uploadSchemas"):
        assert (tmp_path / f"{name}.json").read_bytes() == (settings.SEED_DIR / f"{name}.json").read_bytes(), name


@pytest.mark.parametrize("src,expected", [
    ("export const N = 5;", {"N": 5}),
    ("export const B = true;", {"B": True}),
    ("export const B = false", {"B": False}),
    ("export const U = undefined;\n", {"U": None}),
    ("export const N = 5\nexport const M = {a:1}", {"N": 5, "M": {"a": 1}}),
    ("export const N = -0x10 // c\nexport const M = [true]", {"N": -16, "M": [True]}),
    ("export const S = 'x';\r\nexport const T = [1]", {"S": "x", "T": [1]}),
])
def test_top_level_scalar_exports(src, expected):
    assert extract_exports(src) == expected


@pytest.mark.parametrize("src", [
    "export const N = 5 6",
    "export const N = 5 as const",
    "export const N = true as const",
    "export const N = {a:1} x",
    "export const N = 5\n+ 3",
    "export const N = [1;2]",
    "export const N = { a: 1; b: 2 }",
])
def test_top_level_trailing_garbage_rejected(src):
    with pytest.raises(TsParseError):
        extract_exports(src)
