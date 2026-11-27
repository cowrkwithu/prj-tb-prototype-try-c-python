"""3a 4차 리뷰: ① 정규식 허용 문법(엄격한 부분집합)·컴파일 캐시 ② 발행 구조의 임시저장 우회·보관 복원 strict 검증
③(데이터사전 — apps/dictionary/tests/test_review_fixes4.py) ④ 가져오기 연결 키 보존 ⑤ 고유값 숫자 정규화."""
import json
import random
import re
import time
import tracemalloc

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.schemas import services, user_regex, validation
from apps.schemas.models import PendingEntry, Schema, SchemaField, SchemaRecord

from .conftest import simple_payload
from .test_review_fixes3 import SAFE as SAFE3
from .test_review_fixes3 import UNSAFE as UNSAFE3

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_compile(monkeypatch):
    """여기 넣은 패턴이 regex.compile 에 닿으면 실패(실제로 컴파일하면 테스트 기계가 위험하다)."""
    import regex

    real = regex.compile
    blocked = set()

    def spy(pattern, *a, **kw):
        if pattern in blocked:
            raise AssertionError(f"compiled unsafe pattern: {pattern!r}")
        return real(pattern, *a, **kw)

    monkeypatch.setattr(regex, "compile", spy)
    validation.compile_cache_clear()
    yield blocked
    validation.compile_cache_clear()


# ── 1. 리뷰어 우회 4종(+ 최대 RSS 2.9GB 사례) ─────────────────
REVIEWER_BYPASS = [
    "(?V1x)(a{1000}b) {1000}",           # (a) 숫자 섞인 인라인 플래그로 verbose 감지 실패
    "[[:](a{1000}b){1000}:]",            # (b) 클래스 안 [: 를 :] 까지 건너뜀
    "x{(a{1000}b){1000}}",               # (c) 숫자가 아닌 { 를 } 까지 건너뜀
    "(?x)(#)\n(a{1000}b)c){1000}",       # (d) verbose # 주석 미해석
    "x{((a{1000}b){1000}c){10}}",        # 최대 RSS 2.9GB
]


@pytest.mark.parametrize("pattern", REVIEWER_BYPASS)
def test_reviewer_bypasses_rejected_without_compiling(no_compile, pattern):
    no_compile.add(pattern)
    problem = validation.regex_problem(pattern)
    assert problem and "지원하지 않는 정규식 문법" in problem["ko"] and "Unsupported" in problem["en"]
    with pytest.raises(validation.UnsafePattern):
        validation.compile_user_regex(pattern)
    assert pattern not in validation.compile_cache_patterns()


def test_reviewer_bypasses_rejected_on_endpoint_every_time(researcher_client, no_compile):
    """같은 패턴을 다시 내도(캐시 우회) 매번 같은 판정 — 2회째도 400."""
    for p in REVIEWER_BYPASS:
        no_compile.add(p)
        fields = [{"fieldId": "k1", "fieldName": "code", "displayName": "코드", "dataType": "TEXT",
                   "constraints": {"pattern": p}}]
        payload = json.dumps(simple_payload(fields=fields))
        for _ in range(2):
            r = researcher_client.post("/schemas/create", {"payload": payload, "action": "publish"})
            assert r.status_code == 400 and "지원하지 않는 정규식 문법" in r.content.decode()
    assert Schema.objects.count() == 0
    assert validation.compile_cache_patterns() == []


# ── 캐시: 모든 판정을 통과한 패턴만, 거부·시간 초과는 넣지 않고 재제출도 같은 판정 ──
def test_compile_time_rejection_is_not_cached_and_repeats(monkeypatch, settings):
    import regex

    real = regex.compile
    slow = r"^slow\d+$"

    def slow_compile(pattern, *a, **kw):
        if pattern == slow:
            time.sleep(0.05)
        return real(pattern, *a, **kw)

    validation.compile_cache_clear()
    monkeypatch.setattr(regex, "compile", slow_compile)
    settings.SCHEMA_REGEX_TIMEOUT = 0.001  # 컴파일 시간 판정 상한 = 0.01초
    for _ in range(3):
        problem = validation.regex_problem(slow)
        assert problem and "컴파일 시간 초과" in problem["ko"]
        with pytest.raises(validation.UnsafePattern):
            validation.compile_user_regex(slow)
    assert slow not in validation.compile_cache_patterns()


def test_probe_timeout_pattern_is_not_cached_and_repeats(researcher_client):
    validation.compile_cache_clear()
    evil = r"^(z|zz)+$"
    for _ in range(2):
        assert "시간 초과" in validation.regex_problem(evil)["ko"]
        assert evil not in validation.compile_cache_patterns()
    fields = [{"fieldId": "k1", "fieldName": "code", "displayName": "코드", "dataType": "TEXT",
               "constraints": {"pattern": evil}}]
    payload = json.dumps(simple_payload(fields=fields))
    for _ in range(2):
        r = researcher_client.post("/schemas/create", {"payload": payload, "action": "publish"})
        assert r.status_code == 400
    assert Schema.objects.count() == 0 and evil not in validation.compile_cache_patterns()


def test_runtime_timeout_evicts_pattern_from_cache():
    validation.compile_cache_clear()
    evil = r"^(z|zz)+$"
    validation.compile_user_regex(evil)  # 실행 경로(DB 에 이미 저장된 패턴): 정적·컴파일 통과 → 캐시
    assert evil in validation.compile_cache_patterns()
    with pytest.raises(validation.RegexTimeout):
        validation.CheckContext().search(evil, "z" * 40 + "!")
    assert evil not in validation.compile_cache_patterns()


def test_cache_is_bounded_lru_and_holds_only_passed():
    validation.compile_cache_clear()
    for i in range(validation.COMPILE_CACHE_MAX + 20):
        assert validation.regex_problem(rf"^p{i}\d+$") is None
    pats = validation.compile_cache_patterns()
    assert len(pats) == validation.COMPILE_CACHE_MAX
    assert pats[-1] == rf"^p{validation.COMPILE_CACHE_MAX + 19}\d+$" and r"^p0\d+$" not in pats
    validation.compile_cache_clear()


def test_compile_function_is_not_lru_cache_decorated():
    assert not hasattr(validation.compile_user_regex, "cache_info")
    assert not hasattr(validation, "_compile_checked")


# ── 3차 UNSAFE 목록 + 허용 문법 밖 구문 ─────────────────────
GRAMMAR_REJECT = [
    "(?i)abc", "(?x)a b", "(?V1)a", "(?s).", "(?m)^a", "(?i:a)", "(?P<n>a)", "(?<n>a)", "(?'n'a)", "(?=a)", "(?!a)",
    "(?<=a)b", "(?<!a)b", "(?>a+)", "(?|a|b)", "(?(1)a|b)", "(?#c)a", "(?R)", "(*FAIL)", "(*SKIP)a",
    r"(a)\1", r"\g<1>", r"\k<n>", r"\N{LATIN SMALL LETTER A}", r"\x41", "\\" + "u0041", r"\n", r"\t", r"\0", r"\K", r"\G",
    r"\X", r"\L<x>", r"\A", r"\Z", r"\pL", r"\p{^L}", r"\p{Script=Hangul}", r"\p{" + "A" * 31 + "}",
    "a{e<=1}", "(?:abc){e<=1}", "a{1,2}{3}", "a{2}{3}", "a++", "a*+", "a?+", "a{2}+", "a???", "a**", "a{,5}",
    "a{ 2}", "a{2 }", "a{1,2,3}", "a{x}", "a{", "a{1", "a}", "a]", "{2}", "*a", "+a", "?a", "(*a)", "(|*)",
    "^*", "$+", r"\b{2}", "a|*b", "x{(a{1000}b){1000}}", "a{١}",
    "[[a]]", "[a[b]", "[[:alpha:]]", "[:alpha:]", "[=a=]", "[.a.]", "[a&&b]", "[a||b]", "[a--b]", "[a~~b]",
    "[]", "[^]", "[]a]", "[a-c-e]", r"[\d-z]", r"[a-\d]", "[z-a]", r"[\b]", r"[\1]", r"[\n]", "[a", "(a", "a)", "(?",
    "\\", "a\x00b", "a\x07", "a\ud800",
    "a" * 201,
    "(" * 31 + "a" + ")" * 31,
]


@pytest.mark.parametrize("pattern", UNSAFE3 + GRAMMAR_REJECT)
def test_unsafe_and_unsupported_patterns_rejected(no_compile, pattern):
    no_compile.add(pattern)
    assert validation.regex_static_problem(pattern) is not None, pattern
    assert validation.regex_problem(pattern) is not None, pattern
    with pytest.raises(validation.UnsafePattern):
        validation.compile_user_regex(pattern)
    assert validation.compile_cache_patterns() == []


SAFE4 = SAFE3 + [
    r"^[\w.+-]+@[\w-]+\.[A-Za-z]{2,}$",          # 이메일
    r"^\d{2,3}-\d{3,4}-\d{4}$",                 # 전화
    r"^\d{4}-\d{2}-\d{2}$",                     # 날짜
    r"^(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
    r"^\d{6}-[1-4]\d{6}$",                      # 주민번호 형식
    r"^\d{6}-?[1-4]\d{6}$",
    r"\p{Hangul}", r"^\p{Hangul}{2,10}$", r"[\p{Hangul}\s]+", r"\P{Hangul}",
    r"^(\d{1,3}\.){3}\d{1,3}$",                 # IPv4
    r"\d{1000}",
    r"^[A-Z][a-z]*(?: [A-Z][a-z]*)*$", r"^(?:ICD-10|SNOMED):?\w+$", r"^\$\d+(?:\.\d{2})?$", r"^a/b\/c$",
    r"^[-+]?\d+$", r"^[a-z-]+$", r"^[\-\]\[\\]$", r"^[^-]$", r"a{0}", r"a{0,0}", r"(?:)", r"()", r"a|", r"|",
    r"^.*?x$", r"a{2,}?", r"\bword\b", r"\Bx", r"^[가-힣]{2,4}$", r"^한글\s*이름$", r"# 주석 아님", r"a b",
]


# a{10000} 꼴: 9,999자 'a' + 꼬리 입력에서 위치마다 10,000자 비교 — 1,000자 TEXT 에서는 즉시 실패(길이 부족)
TEXTAREA_SLOW = {r"(?:a{100}){100}"}


@pytest.mark.parametrize("pattern", SAFE4)
def test_common_patterns_pass(pattern):
    assert validation.regex_static_problem(pattern) is None, pattern
    assert validation.check_user_regex(pattern, max_len=validation.MAX_TEXT_DEFAULT["TEXT"]) is None, pattern
    if pattern in TEXTAREA_SLOW:  # 5차 리뷰: 탐침 = 실제 최대 입력 길이 — TEXTAREA(10,000자) 입력에서는 실제로 느리다
        problem = validation.check_user_regex(pattern)  # 기계 속도에 따라 0.1초 timeout 을 넘거나 못 넘는다
        assert problem is None or "시간 초과" in problem, pattern
    else:
        assert validation.check_user_regex(pattern) is None, pattern


def test_all_seed_patterns_within_grammar(seeded_schemas):
    pats = [f.constraints["pattern"] for f in SchemaField.objects.all() if f.constraints.get("pattern")]
    assert pats
    for p in pats:
        assert validation.regex_static_problem(p) is None, p
        assert validation.check_user_regex(p) is None, p


def test_class_members_count_toward_expansion():
    assert validation.regex_static_problem(r"(?:[a-z]{100}){100}") is None
    problem = validation.regex_static_problem(r"(?:[a-zA-Z]{100}){100}")  # 구성원 2 × 10,000
    assert problem and "펼친 크기" in problem["ko"]
    assert validation.regex_static_problem(r"^[A-Za-z0-9]{1,1000}$") is None


def test_nested_plus_tower_counts_doubling():
    """regex 는 X+ 를 'X + X*' 로 컴파일한다 — + 를 중첩하면 단계마다 두 배(12단: RSS 2.2GB 실측). 하한만 곱하면 통과했다."""
    tower = "(?:" * 12 + "a{1000}" + ")+" * 12
    problem = validation.regex_static_problem(tower)
    assert problem and "너무 큽니다" in problem["ko"]
    assert validation.regex_static_problem("(?:" * 3 + "a{1000}" + ")+" * 3) is None  # 8,000
    assert validation.regex_static_problem("(?:a{1000}){9,}") is None                 # 10,000
    assert validation.regex_static_problem("(?:a{1000}){10,}") is not None            # 11,000
    assert user_regex.repeat_factor(0, None) == 1 and user_regex.repeat_factor(1, None) == 2
    assert user_regex.repeat_factor(0, 1) == 1 and user_regex.repeat_factor(2, 7) == 7


def test_capture_groups_and_branches_count():
    assert validation.regex_static_problem(r"(?:(?:a){100}){100}") is None
    assert validation.regex_static_problem(r"(?:(a){100}){100}") is not None      # 캡처 표지 × 10,000
    assert validation.regex_static_problem(r"(?:(a){100}){30}") is None
    assert validation.regex_static_problem(r"(?:x|y){1000}") is None              # (2+2) × 1,000
    assert validation.regex_static_problem(r"(?:x|y|z){2000}") is not None


def test_legacy_pattern_outside_grammar_in_db_is_pattern_error(researcher_client, demo_users, no_compile):
    bad = REVIEWER_BYPASS[0]
    no_compile.add(bad)
    s = services.save_definition(demo_users["researcher"], simple_payload(), action="publish")
    SchemaField.objects.filter(schema=s, field_name="note").update(constraints={"pattern": bad})
    for _ in range(2):
        r = researcher_client.post(f"/schemas/{s.schema_id}/data/check",
                                   {"mode": "form", "link_value": "A1", "f__score": "3", "f__note": "aaa"})
        assert r.status_code == 302
        errs = PendingEntry.objects.order_by("-pk").first().errors
        assert any(e["field"] == "note" and "정규식 패턴 오류" in e["ko"] for e in errs)
    r = researcher_client.post(f"/schemas/{s.schema_id}/data/check",
                               {"mode": "csv", "file": SimpleUploadedFile("d.csv", b"condition_id,score,note\nA1,3,aaa\n",
                                                                          "text/csv")})
    assert r.status_code == 302
    assert SchemaRecord.objects.count() == 0 and bad not in validation.compile_cache_patterns()


# ── 무작위 패턴 퍼징 ─────────────────────────────────────────
_TOKENS = ["a", "b", "z", "0", "가", ".", "^", "$", "|", "(", ")", "(?:", "(?", "[", "]", "[^", "-", "{", "}", ",",
           "*", "+", "?", "\\", "\\d", "\\w", "\\s", "\\b", "\\D", "\\p{Hangul}", "\\P{L}", "\\.", "\\-", "\\1", "\\n",
           ":", "=", "#", " ", "\n", "&&", "--", "e<=1", "1", "2", "9", "10", "100", "999", "1000", "1001", "{2}",
           "{2,}", "{0,3}", "{100}", "{1000}", "{10,100}", "{1,1000}", "a-z", "A-Z", "\\]", "\\[", "\\{", "\\}"]


def _random_pattern(rng: random.Random) -> str:
    mode = rng.random()
    if mode < 0.55:  # 토큰 무작위 나열
        return "".join(rng.choice(_TOKENS) for _ in range(rng.randint(1, 25)))[:220]
    if mode < 0.6:  # + 중첩 탑(단계마다 컴파일 크기 두 배)
        k = rng.randint(1, 14)
        return "(?:" * k + rng.choice(["a", "a{10}", "a{100}", "a{1000}", "(a){50}"]) + rng.choice([")+", "){1,}", "){2,}"]) * k
    # 구조적 생성 — 상한 근처의 반복 중첩을 많이 만든다
    def gen(depth):
        r = rng.random()
        if depth > 3 or r < 0.3:
            return rng.choice(["a", "\\d", "[a-z]", "[A-Za-z0-9_]", "\\p{Hangul}", ".", "x|y", "[^@\\s]"])
        inner = "".join(gen(depth + 1) for _ in range(rng.randint(1, 3)))
        q = rng.choice(["", "*", "+", "+", "?", "{%d}" % rng.choice([1, 2, 10, 50, 100, 1000]),
                        "{%d,%d}" % (rng.choice([0, 1, 10]), rng.choice([10, 100, 1000])), "{%d,}" % rng.choice([0, 5, 500]),
                        "*?", "{2}?"])
        return rng.choice(["(?:%s)", "(%s)"]) % inner + q
    return gen(0)[:200]


# 받아들인 패턴의 컴파일 메모리 상한(tracemalloc 최고치). 정적 상한(펼친 크기 10,000) 안에서 실측 최대 약 8MB.
ACCEPTED_COMPILE_PEAK_LIMIT = 32 * 1024 * 1024
_DIFF_TEXTS = ["", "a", "ab", "aaa", "b0", "abc가", "가나다", "a-b", "a.b", "x y", "ICD-10", "zz9", "0123456789", "a\nb",
               "[a]", "{2}", "#", "a b c", "AAA", "010-1234-5678"]


def test_random_pattern_fuzz_accepted_patterns_are_bounded_and_match_re():
    import regex

    rng = random.Random(20261127)
    accepted = rejected = 0
    peak_max = 0
    tracemalloc.start()
    try:
        for _ in range(4000):
            p = _random_pattern(rng)
            problem = validation.regex_static_problem(p)  # 예외 0건이어야 한다
            if problem is not None:
                rejected += 1
                assert problem["ko"] and problem["en"]
                continue
            accepted += 1
            tracemalloc.reset_peak()
            before = tracemalloc.get_traced_memory()[0]
            try:
                compiled = regex.compile(p, cache_pattern=False)
            except regex.error:
                continue  # 문법상 허용되지만 regex 가 거부(예: 반복 대상이 그룹뿐인 경우) — 문법 오류로 처리됨
            peak = tracemalloc.get_traced_memory()[1] - before
            peak_max = max(peak_max, peak)
            assert peak < ACCEPTED_COMPILE_PEAK_LIMIT, (p, peak)
            # 허용 문법은 표준 re 와 regex 가 같은 의미로 읽는 부분집합이어야 한다(\p 는 re 미지원이라 제외)
            if "\\p" in p or "\\P" in p:
                continue
            std = re.compile(p)
            for text in _DIFF_TEXTS:
                got = compiled.search(text, timeout=1.0)
                exp = std.search(text)
                assert (got and got.span()) == (exp and exp.span()), (p, text)
    finally:
        tracemalloc.stop()
    assert accepted > 500 and rejected > 500, (accepted, rejected)
    print(f"fuzz accepted={accepted} rejected={rejected} max_compile_peak={peak_max / 1024 / 1024:.2f}MB")


def test_worst_case_accepted_patterns_compile_memory():
    import regex

    worst = [r"(?:a{100}){100}", r"(?:a{1000}){10}", r"(?:a{10}){1000}", r"(?:(?:a{10}){10}){100}", r"\d{1000}" * 10,
             r"(?:\p{Hangul}{100}){100}", r"(?:[a-z]{100}){100}", r"(?:[\d\w]{50}){100}", r"(?:x{100}|y{100}){49}",
             r"(?:[\p{Hangul}\p{Latin}\p{Han}\p{Greek}\d\w\s\D\W\S]{10}){100}",
             "(?:" * 3 + "a{1000}" + ")+" * 3, r"(?:a{1000}){9,}", r"(?:(a){100}){30}", r"(?:x|y){1000}",
             "(?:" * 13 + "a" + ")+" * 13]
    tracemalloc.start()
    try:
        for p in worst:
            assert validation.regex_static_problem(p) is None, p
            tracemalloc.reset_peak()
            before = tracemalloc.get_traced_memory()[0]
            t = time.monotonic()
            regex.compile(p)
            elapsed = time.monotonic() - t
            peak = tracemalloc.get_traced_memory()[1] - before
            assert peak < ACCEPTED_COMPILE_PEAK_LIMIT, (p, peak)
            assert elapsed < 1.0, (p, elapsed)  # 컴파일 시간 판정(보조, 1초)에 한참 못 미친다
    finally:
        tracemalloc.stop()


def test_parser_does_not_raise_unexpected_exceptions():
    rng = random.Random(7)
    alphabet = "ab()[]{}|*+?^$.\\-,:=#!<>&~0123456789dDwWsSbBpP가 \n"
    for _ in range(3000):
        p = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        try:
            user_regex.check(p)
        except (user_regex.GrammarError, user_regex.SizeError):
            pass


# ── 2. 발행된 구조에 action=draft — strict 검증, 상태 유지 ─────
BAD_FIELDS = [
    {"fieldId": "k1", "fieldName": "pick", "displayName": "선택", "dataType": "SELECT", "isRequired": True,
     "constraints": {"options": []}},
    {"fieldId": "k3", "fieldName": "calc", "displayName": "계산", "dataType": "CALCULATED", "constraints": {}},
]


@pytest.mark.parametrize("fields", [BAD_FIELDS, []])
def test_draft_action_on_published_schema_is_strict(researcher_client, demo_users, fields):
    s = services.save_definition(demo_users["researcher"], simple_payload(), action="publish")
    before = sorted(s.fields.values_list("field_name", flat=True))
    payload = json.dumps(simple_payload(fields=fields))
    r = researcher_client.post(f"/schemas/{s.schema_id}/edit", {"payload": payload, "action": "draft", "rev": s.rev})
    assert r.status_code == 400
    s.refresh_from_db()
    assert s.status == Schema.Status.PUBLISHED
    assert sorted(s.fields.values_list("field_name", flat=True)) == before
    with pytest.raises(services.SchemaError):
        services.save_definition(demo_users["admin"], simple_payload(fields=fields), schema=s, action="draft")
    s.refresh_from_db()
    assert s.status == Schema.Status.PUBLISHED and s.fields.count() == len(before)


def test_draft_action_on_published_schema_with_valid_fields_saves_and_stays_published(researcher_client, demo_users):
    s = services.save_definition(demo_users["researcher"], simple_payload(), action="publish")
    payload = simple_payload()
    payload["meta"]["description"] = "고침"
    r = researcher_client.post(f"/schemas/{s.schema_id}/edit",
                               {"payload": json.dumps(payload), "action": "draft", "rev": s.rev})
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.status == Schema.Status.PUBLISHED and s.description == "고침"
    assert any("저장되었습니다" in m.message for m in r.wsgi_request._messages)


def test_draft_action_still_lenient_for_new_and_draft(researcher_client, demo_users):
    payload = json.dumps(simple_payload(fields=BAD_FIELDS))
    r = researcher_client.post("/schemas/create", {"payload": payload, "action": "draft"})
    assert r.status_code == 302
    s = Schema.objects.get()
    assert s.status == Schema.Status.DRAFT
    r = researcher_client.post(f"/schemas/{s.schema_id}/edit", {"payload": payload, "action": "draft", "rev": s.rev})
    assert r.status_code == 302


def test_builder_hides_draft_button_unless_draft(researcher_client, demo_users):
    r = researcher_client.get("/schemas/create")
    assert 'data-testid="btn-draft"' in r.content.decode()
    d = services.save_definition(demo_users["researcher"], simple_payload(), action="draft")
    assert 'data-testid="btn-draft"' in researcher_client.get(f"/schemas/{d.schema_id}/edit").content.decode()
    p = services.save_definition(demo_users["researcher"], simple_payload(), action="publish")
    assert 'data-testid="btn-draft"' not in researcher_client.get(f"/schemas/{p.schema_id}/edit").content.decode()


def _archived_with_bad_definition(user):
    s = services.save_definition(user, simple_payload(), action="publish")
    services.transition(s, "archive", user)
    # 예전 우회(또는 직접 DB 수정)로 저장된 미완성 정의 — 선택지 없는 필수 SELECT
    SchemaField.objects.filter(schema=s, field_name="note").update(data_type="SELECT", constraints={"options": []},
                                                                   is_required=True)
    s.refresh_from_db()
    return s


def test_restore_runs_strict_validation_owner_admin_and_service(client, demo_users):
    s = _archived_with_bad_definition(demo_users["researcher"])
    client.force_login(demo_users["researcher"])
    r = client.post(f"/schemas/{s.schema_id}/status", {"action": "restore"})
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.status == Schema.Status.ARCHIVED
    assert any("복원할 수 없습니다" in m.message for m in r.wsgi_request._messages)
    client.force_login(demo_users["admin"])
    r = client.post(f"/admin/schemas/{s.schema_id}/action", {"action": "restore"})
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.status == Schema.Status.ARCHIVED
    with pytest.raises(services.SchemaError) as ei:
        services.transition(s, "restore", demo_users["admin"], by_admin=True)
    assert "Cannot restore" in ei.value.en and ei.value.errors


def test_restore_without_fields_rejected(demo_users):
    u = demo_users["researcher"]
    s = services.save_definition(u, simple_payload(), action="publish")
    services.transition(s, "archive", u)
    s.fields.all().delete()
    with pytest.raises(services.SchemaError):
        services.transition(s, "restore", u)
    s.refresh_from_db()
    assert s.status == Schema.Status.ARCHIVED


def test_restore_valid_archived_schema_still_works(researcher_client, demo_users):
    u = demo_users["researcher"]
    s = services.save_definition(u, simple_payload(), action="publish")
    services.transition(s, "archive", u)
    r = researcher_client.post(f"/schemas/{s.schema_id}/status", {"action": "restore"})
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.status == Schema.Status.PUBLISHED


def test_every_transition_to_published_is_checked():
    """전이표에서 결과 상태가 published 인 전이는 모두 strict 검증 대상(publish·restore)."""
    to_pub = {a for a, (_, dst) in services.TRANSITIONS.items() if dst == Schema.Status.PUBLISHED}
    assert to_pub == {"publish", "restore"}


def test_seed_published_schemas_pass_strict(seeded_schemas):
    for s in Schema.objects.filter(status__in=[Schema.Status.PUBLISHED, Schema.Status.ARCHIVED]):
        assert services.publish_problems(s) == [], s.schema_id


# ── 4. 가져오기: 빌더 형식 meta.linkFieldName 보존 ───────────────
def test_import_builder_format_keeps_link_field_name(demo_users):
    obj = simple_payload(linkFieldName="specimen_id")
    obj["name"] = "빌더 형식"
    s = services.import_definition(demo_users["researcher"], obj)
    assert s.link_field_name == "specimen_id" and s.visibility == Schema.Visibility.PRIVATE


def test_import_export_format_keeps_link_field_object(demo_users):
    src = services.save_definition(demo_users["researcher"], simple_payload(linkFieldName="series_id"), action="draft")
    exported = services.schema_to_json(src)
    assert "linkFieldName" not in exported and exported["linkField"]["fieldName"] == "series_id"
    s = services.import_definition(demo_users["researcher"], exported)
    assert s.link_field_name == "series_id"


def test_import_builder_format_via_endpoint(researcher_client):
    obj = simple_payload(linkFieldName="specimen_id")
    obj["name"] = "빌더 형식"
    r = researcher_client.post("/schemas/import", {"confirm": "1", "json": json.dumps(obj)})
    assert r.status_code in (200, 204, 302)
    assert Schema.objects.get().link_field_name == "specimen_id"


# ── 5. 고유값: 숫자는 Decimal 정규화(2 == 2.0), 불리언은 구분 ───
def _unique_numeric(user, dtype):
    fields = [{"fieldId": "u1", "fieldName": "val", "displayName": "값", "dataType": dtype, "isUnique": True}]
    return services.save_definition(user, simple_payload(fields=fields), action="publish")


def _rec(s, data, rid):
    return SchemaRecord.objects.create(schema=s, record_id=rid, condition_id="X1", data=data, author_label="x",
                                       created_at=s.created_at, updated_at=s.created_at)


@pytest.mark.parametrize("dtype", ["DECIMAL", "INTEGER"])
def test_unique_stored_int_matches_input_float(demo_users, dtype):
    s = _unique_numeric(demo_users["researcher"], dtype)
    _rec(s, {"val": 2}, "REC-NUM-0001")
    fields = list(s.fields.all())
    issues = []
    services._check_unique(s, fields, [{"data": {"val": 2.0}, "_row": 1}], issues)
    assert len(issues) == 1 and "이미 등록된 값" in issues[0]["ko"]
    issues = []
    services._check_unique(s, fields, [{"data": {"val": 2.5}, "_row": 1}], issues)
    assert issues == []


def test_unique_stored_float_matches_input_int_and_batch(demo_users):
    s = _unique_numeric(demo_users["researcher"], "DECIMAL")
    _rec(s, {"val": 3.0}, "REC-NUM-0002")
    fields = list(s.fields.all())
    issues = []
    services._check_unique(s, fields, [{"data": {"val": 3}, "_row": 1}], issues)
    assert len(issues) == 1
    issues = []  # 같은 제출 안에서 2 와 2.0
    services._check_unique(s, fields, [{"data": {"val": 2}, "_row": 1}, {"data": {"val": 2.0}, "_row": 2}], issues)
    assert [i["row"] for i in issues] == [2]


def test_unique_form_decimal_input_against_stored_int(researcher_client, demo_users):
    s = _unique_numeric(demo_users["researcher"], "DECIMAL")
    _rec(s, {"val": 2}, "REC-NUM-0003")
    r = researcher_client.post(f"/schemas/{s.schema_id}/data/check",
                               {"mode": "form", "link_value": "A1", "f__val": "2.0"})
    assert r.status_code == 302
    errs = PendingEntry.objects.get().errors
    assert any(e["field"] == "val" and "고유값" in e["ko"] for e in errs)


def test_unique_boolean_true_distinct_from_number_one(demo_users):
    u = demo_users["researcher"]
    s = _unique_numeric(u, "INTEGER")
    _rec(s, {"val": True}, "REC-NUM-0004")  # 같은 키에 불리언이 저장돼 있어도 숫자 1 과는 다르다
    issues = []
    services._check_unique(s, list(s.fields.all()), [{"data": {"val": 1}, "_row": 1}], issues)
    assert issues == []
    assert services.unique_key(True) != services.unique_key(1)
    assert services.unique_key(2) == services.unique_key(2.0)
    assert services.unique_key("2") != services.unique_key(2)
