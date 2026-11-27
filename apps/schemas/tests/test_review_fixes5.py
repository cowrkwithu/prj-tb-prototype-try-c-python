"""3a 5차 리뷰: ① 사용자 정규식이 regex 모듈 내부 캐시(regex.regex._cache)에 남지 않음·정의 1건 고유 패턴 상한·
예산 소진 후 컴파일 금지 ② 탐침 길이 = 필드 타입의 최대 입력 길이 ③ 실행 시 regex.error 는 fail-closed."""
import json

import pytest
import regex

from apps.schemas import validation
from apps.schemas.models import Schema

from .conftest import simple_payload

pytestmark = pytest.mark.django_db


def _internal_keys() -> set:
    return {k[0] for k in regex.regex._cache}


def _locale_keys() -> set:
    return {k[1] for k in regex.regex._locale_sensitive}


@pytest.fixture
def compile_spy(monkeypatch):
    """regex.compile 호출(패턴·인자)을 기록한다."""
    real = regex.compile
    calls = []

    def spy(pattern, *a, **kw):
        calls.append((pattern, kw.get("cache_pattern")))
        return real(pattern, *a, **kw)

    monkeypatch.setattr(regex, "compile", spy)
    validation.compile_cache_clear()
    yield calls
    validation.compile_cache_clear()


def _big(i: int) -> str:
    """리뷰 재현 패턴: 정적 상한 안(펼친 크기 9,9xx)이지만 컴파일 결과가 약 2MB."""
    x = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i % 26]
    return f"(?:(?:{x}{{10}}){{10}}){{99}}Y{i}"


# ── 1. HTTP 재현: 필드 200개 × 서로 다른 큰 패턴 3회 → 내부 캐시가 늘지 않는다 ──
def test_http_repro_does_not_grow_regex_internal_cache(researcher_client, compile_spy):
    pats = [_big(i) for i in range(200)]
    fields = [{"fieldId": f"k{i}", "fieldName": f"f{i}", "displayName": f"F{i}", "dataType": "TEXT",
               "constraints": {"pattern": p}} for i, p in enumerate(pats)]
    payload = json.dumps(simple_payload(fields=fields))
    before = len(regex.regex._cache)
    for _ in range(3):
        r = researcher_client.post("/schemas/create", {"payload": payload, "action": "draft"})
        assert r.status_code == 400
        assert f"최대 {validation.MAX_DEFINITION_PATTERNS}개" in r.content.decode()
    assert len(regex.regex._cache) <= before
    assert not set(pats) & _internal_keys()
    assert not set(pats) & _locale_keys()
    assert Schema.objects.count() == 0
    # 컴파일은 모두 cache_pattern=False, 요청마다 고유 패턴 상한 이하, 앱 LRU 는 상한 유지
    assert compile_spy and all(cp is False for _, cp in compile_spy)
    assert len({p for p, _ in compile_spy}) <= validation.MAX_DEFINITION_PATTERNS
    assert len(validation.compile_cache_patterns()) <= validation.COMPILE_CACHE_MAX


def test_rejected_and_passed_patterns_never_in_internal_cache(compile_spy):
    passed, timed_out, bad_syntax = r"^q5pass\d+$", r"^(z|zz)+$", "()*"
    assert validation.regex_problem(passed) is None
    assert "시간 초과" in validation.regex_problem(timed_out)["ko"]
    validation.regex_problem(bad_syntax)  # 허용 문법이지만 regex 가 거부할 수도 있는 패턴 — 결과와 무관하게 캐시 금지
    validation.compile_cache_clear()
    validation.compile_user_regex(passed)  # 실행 경로
    with pytest.raises(validation.RegexTimeout):
        validation.CheckContext().search(timed_out, "z" * 40 + "!")
    for p in (passed, timed_out, bad_syntax):
        assert p not in _internal_keys() and p not in _locale_keys(), p
    assert all(cp is False for _, cp in compile_spy)


def test_no_module_level_regex_calls_with_user_patterns():
    """사용자 정규식은 컴파일 객체(앱 캐시 경유)로만 실행 — 모듈 수준 regex 함수·캐시되는 compile 금지."""
    from pathlib import Path

    src = Path(validation.__file__).read_text(encoding="utf-8")
    for fn in ("match", "search", "fullmatch", "sub", "subn", "split", "findall", "finditer"):
        assert f"user_re.{fn}(" not in src, fn
    assert src.count("user_re.compile(") == 1 and "user_re.compile(pattern, cache_pattern=False)" in src
    root = Path(validation.__file__).resolve().parents[2] / "apps"
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if path.name != "validation.py":
            assert "import regex" not in text and "from regex" not in text, path


# ── 고유 패턴 단위 1회 컴파일·상한 ───────────────────────────
def test_same_pattern_compiled_once_per_definition(compile_spy):
    p = r"^dup\d{3}$"
    fields = [{"fieldId": f"k{i}", "fieldName": f"f{i}", "displayName": "x", "dataType": "TEXT" if i % 2 else "TEXTAREA",
               "constraints": {"pattern": p}} for i in range(100)]
    meta, cleaned, errs = validation.clean_definition(simple_payload(fields=fields), ctx=validation.CheckContext())
    assert errs == [] and all(f["constraints"]["pattern"] == p for f in cleaned)
    assert [c for c, _ in compile_spy].count(p) == 1


def test_timed_out_pattern_not_recompiled_for_other_length(compile_spy):
    """TEXTAREA 에서 시간 초과된 패턴을 TEXT 필드에서 다시 판정할 때도 컴파일은 한 번."""
    p = r"\w+\s\w+"
    ctx = validation.CheckContext()
    assert "시간 초과" in validation.regex_problem(p, ctx, max_len=validation.MAX_TEXT_DEFAULT["TEXTAREA"])["ko"]
    assert validation.regex_problem(p, ctx, max_len=validation.MAX_TEXT_DEFAULT["TEXT"]) is None
    assert [c for c, _ in compile_spy].count(p) == 1


def test_unique_pattern_cap(compile_spy):
    n = validation.MAX_DEFINITION_PATTERNS + 10
    fields = [{"fieldId": f"k{i}", "fieldName": f"f{i}", "displayName": "x", "dataType": "TEXT",
               "constraints": {"pattern": rf"^cap{i}\d+$"}} for i in range(n)]
    _, _, errs = validation.clean_definition(simple_payload(fields=fields), ctx=validation.CheckContext())
    cap = [e for e in errs if f"최대 {validation.MAX_DEFINITION_PATTERNS}개" in e["ko"]]
    assert len(cap) == 10 and all("distinct regular expressions" in e["en"] for e in cap)
    assert len({c for c, _ in compile_spy}) == validation.MAX_DEFINITION_PATTERNS
    # 정적 거부 패턴은 컴파일하지 않으므로 상한에 세지 않는다
    ctx = validation.CheckContext()
    for i in range(80):
        assert "지원하지 않는" in validation.regex_problem(f"(?i)x{i}", ctx)["ko"]
    assert validation.regex_problem(r"^after\d$", ctx) is None


# ── 예산 소진 후 컴파일 0회 ─────────────────────────────────
def test_budget_exhausted_rejects_without_compiling(compile_spy):
    ctx = validation.CheckContext(budget=0)
    for i in range(5):
        problem = validation.regex_problem(rf"^fresh{i}\d$", ctx)
        assert "검사 시간 초과" in problem["ko"] and "timed out" in problem["en"]
    assert compile_spy == []


def test_budget_spent_by_probes_then_no_more_compiles(compile_spy, settings):
    settings.SCHEMA_REGEX_BUDGET_SECONDS = 0.25
    ctx = validation.CheckContext()
    results = [validation.regex_problem(rf"^(z|zz)+z{{{i}}}$", ctx) for i in range(1, 8)]
    assert ctx.remaining <= 0 and all(r is not None for r in results)
    compiled = len(compile_spy)
    assert compiled < 7
    for i in range(10):
        assert "검사 시간 초과" in validation.regex_problem(rf"^late{i}\d$", ctx)["ko"]
    assert len(compile_spy) == compiled


# ── 2. 탐침 길이 = 타입별 최대 입력 길이 ────────────────────
COMMON_UNANCHORED = [r"\w+$", r"a.*b", r"[A-Za-z]+\d+", r"\w+\s\w+"]
TEXTAREA_DANGEROUS = [r"[A-Za-z]+\d+", r"\w+\s\w+"]  # 10,000자 입력에서 0.3초 이상(호출별 timeout 0.1초)


def test_probes_fit_max_input_length():
    for dtype, n in validation.MAX_TEXT_DEFAULT.items():
        probes = validation.regex_probes(r"^Rv\d{4}$", n)
        assert max(map(len, probes)) == n, dtype
    assert max(map(len, validation.regex_probes("abc"))) == validation.MAX_TEXT_DEFAULT["TEXTAREA"]


@pytest.mark.parametrize("pattern", COMMON_UNANCHORED)
def test_common_unanchored_patterns_pass_for_text(pattern):
    validation.compile_cache_clear()
    field = {"fieldId": "k", "fieldName": "code", "displayName": "코드", "dataType": "TEXT", "constraints": {"pattern": pattern}}
    _, fields, errs = validation.clean_definition(simple_payload(fields=[field]))
    assert errs == [] and fields[0]["constraints"]["pattern"] == pattern


@pytest.mark.parametrize("pattern", TEXTAREA_DANGEROUS)
def test_dangerous_for_textarea_still_rejected_with_hint(pattern):
    validation.compile_cache_clear()
    field = {"fieldId": "k", "fieldName": "memo", "displayName": "메모", "dataType": "TEXTAREA",
             "constraints": {"pattern": pattern}}
    _, _, errs = validation.clean_definition(simple_payload(fields=[field]))
    assert len(errs) == 1
    assert "검사 시간 초과" in errs[0]["ko"] and "^…$ 로 감싸거나 반복을 줄이세요" in errs[0]["ko"]
    assert "^…$" in errs[0]["en"] and "reduce repetition" in errs[0]["en"]
    assert pattern not in validation.compile_cache_patterns()


def test_runtime_timeout_still_applies_to_text_inputs():
    """정의 탐침이 짧아져도 실행 단계 timeout·예산은 그대로 — 위험 패턴은 실행에서도 오류."""
    f = {"data_type": "TEXT", "constraints": {"pattern": r"^(z|zz)+$"}, "display_name": "", "display_name_en": ""}
    _, err = validation.coerce_value(f, "z" * 60 + "!")
    assert err and "정규식 검사 시간 초과" in err["ko"]


# ── 3. 실행 시 regex.error → fail-closed ───────────────────
def test_runtime_regex_error_is_fail_closed(monkeypatch):
    validation.compile_cache_clear()

    def boom(*a, **kw):
        raise regex.error("broken")

    monkeypatch.setattr(regex, "compile", boom)
    f = {"data_type": "TEXT", "constraints": {"pattern": r"^legacy\d$"}, "display_name": "", "display_name_en": ""}
    val, err = validation.coerce_value(f, "anything")
    assert val is None and err and "정규식 패턴 오류" in err["ko"] and "pattern error" in err["en"]
    validation.compile_cache_clear()
