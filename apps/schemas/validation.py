"""서버측 검증 — ① 빌더가 보낸 구조 정의 ② 동적 폼/CSV 입력값(SchemaField 제약조건).

원본은 브라우저(DynamicForm.validateFields, SchemaData.runValidation)에서만 검사했다. 여기서는 모든 입력을
서버가 같은 규칙(필수·타입·길이·정규식·범위·소수 자릿수·날짜 범위·선택지·최대 선택 수·파일 형식/크기·고유값·
데이터 구조 검증 규칙)으로 다시 검사한다. 화면 검사는 편의일 뿐이다.
"""
from __future__ import annotations

import ast
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import regex as user_re  # 사용자 정의 정규식은 반드시 이 모듈 + timeout 으로 실행한다(표준 re 는 시간 제한이 없음)
from django.conf import settings

from apps.core.naming import field_name_problem
from apps.core.safejson import has_bad_chars

from . import user_regex
from .catalog import CODE_SYSTEMS, CONSTRAINT_KEYS, LINK_FIELD, CATEGORY, VISIBILITY, pick, option_label

LINK_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$")
MAX_FIELDS = 200
MAX_OPTIONS = 200
MAX_PATTERN = user_regex.MAX_PATTERN
MAX_TEXT_DEFAULT = {"TEXT": 1000, "TEXTAREA": 10000, "REFERENCE": 200, "CODED": 100}
MULTI_SEP = re.compile(r"\s*[|;]\s*")
INT_LIMIT = 2 ** 53          # INTEGER 입력 절댓값 상한(JS Number 안전 정수와 같음)
DECIMAL_LIMIT = 1e15         # DECIMAL·수치 범위 입력 절댓값 상한
EXPR_INT_BITS = 64           # 수식 계산 중간 결과 정수 비트 수 상한
# 정의 단계에서 패턴을 미리 돌려 보는 탐침 문자열(휴리스틱). 백트래킹 폭주 패턴 상당수를 저장 전에 거른다.
# 탐침 길이는 그 필드 타입의 실제 최대 입력 길이(MAX_TEXT_DEFAULT)에 맞춘다 — 입력은 이 길이를 넘으면 정규식 검사 전에
# 거부되므로, 그보다 긴 탐침은 \w+$ · a.*b 같은 흔한 비앵커 패턴을 과잉 거부할 뿐이다.
# ※ 탐침은 완전하지 않다 — 최종 방어선은 실행 단계의 호출별 timeout + 검사 1회 단위 시간 예산(CheckContext)이다.
PROBE_DEFAULT_LEN = max(MAX_TEXT_DEFAULT.values())  # 타입을 모를 때(가장 긴 입력 = TEXTAREA)
PROBE_LITERALS = 20  # 패턴에 나오는 글자별 탐침 수 상한
MAX_DEFINITION_PATTERNS = 50  # 정의 1건에서 컴파일·탐침하는 서로 다른 정규식 수 상한


def _fit(unit: str, n: int, tail: str = "") -> str:
    """unit 을 반복해 tail 포함 정확히 n 글자로(최소 tail 길이)."""
    body = max(0, n - len(tail))
    return (unit * (body // len(unit) + 1))[:body] + tail


def regex_probes(pattern: str, max_len: int | None = None) -> list[str]:
    """탐침 목록: 고정 모양 + 패턴에 등장하는 영숫자 글자별 '같은 글자 반복 + 불일치 꼬리'.
    ^(z|zz)+$ · ^(b+)+$ · (x+x+)+y 같은 교대·중첩 수량자는 짧은 반복(40자)에서도 지수적으로 폭주한다.
    모든 탐침은 max_len 글자 이하."""
    n = max(2, int(max_len or PROBE_DEFAULT_LEN))
    out = ["", _fit("a", n, "!"), _fit("0", n, "x"), _fit(" ", n, "!"), _fit("aA0_-. ", n, "\n!"),
           _fit("Rv0667", n, "!"), _fit("ab", n, "!"), _fit("x@y.", n, "!"),
           ("a" * 11 + "x" * 3000)[:n]]  # (.*a){12} 처럼 반복 그룹에 수량자가 붙은 다항 폭주(regex 모듈은 대개 최적화)
    chars = []
    for ch in pattern:
        if ch.isalnum() and ch not in chars:
            chars.append(ch)
        if len(chars) >= PROBE_LITERALS:
            break
    for ch in chars:
        out += [(ch * 40 + "!")[:n], (ch * 40)[:n], _fit(ch, n, "!")]
    return list(dict.fromkeys(out))


class RegexTimeout(Exception):
    """이 패턴의 실행이 호출별 시간 상한을 넘었다(같은 검사 안에서는 다시 실행하지 않는다)."""


class RegexBudgetExceeded(RegexTimeout):
    """검사 1회의 정규식 총 시간 예산을 다 썼다 — 남은 정규식 검사는 실행하지 않고 오류로 처리."""


class CheckContext:
    """검사 1회(폼 검사·CSV 검사·수정·제출 재검증·미리보기·정의 저장) 단위의 정규식 시간 예산.

    호출마다 새로 만들어 인자로 넘긴다(전역·스레드 로컬 상태 없음 → 스레드 안전).
    - 총 예산: settings.SCHEMA_REGEX_BUDGET_SECONDS. 다 쓰면 이후 정규식 검사는 실행하지 않고 즉시 오류.
    - 한 번 시간 초과된 패턴은 이 검사 안에서 다시 실행하지 않고 즉시 같은 오류.
    """

    def __init__(self, budget: float | None = None):
        self.remaining = settings.SCHEMA_REGEX_BUDGET_SECONDS if budget is None else budget
        self.timed_out: set[str] = set()
        # 정의 검사(regex_problem) 기록 — 같은 패턴은 정의 1건 안에서 한 번만 컴파일·판정한다
        self.def_stage: dict[str, dict | None] = {}       # 패턴 → 정적·컴파일 단계 문제(None=통과)
        self.def_results: dict[tuple[str, int], dict | None] = {}  # (패턴, 탐침 길이) → 최종 판정
        self.def_timeout_len: dict[str, int] = {}         # 패턴 → 탐침 시간 초과가 난 가장 짧은 길이
        self.def_compiled: dict[str, object] = {}         # 시간 초과 패턴의 컴파일 객체(더 짧은 탐침 재판정용, 개수는 예산이 제한)

    def search(self, pattern: str, text: str, compiled=None, remember: bool = True) -> bool:
        """compiled: 호출 측이 이미 컴파일한 객체(정의 검사 중 — 탐침을 다 통과하기 전에는 캐시에 넣지 않으므로).
        remember=False: 시간 초과를 이 검사의 '시간 초과 패턴'으로 기록하지 않는다(정의 검사는 탐침 길이별로 따로 판정)."""
        if pattern in self.timed_out:
            raise RegexTimeout()
        if self.remaining <= 0:
            raise RegexBudgetExceeded()
        limit = min(settings.SCHEMA_REGEX_TIMEOUT, self.remaining)
        t0 = time.monotonic()
        try:
            rx = compiled if compiled is not None else compile_user_regex(pattern)
            return rx.search(text, timeout=limit) is not None
        except TimeoutError:
            if remember:
                self.timed_out.add(pattern)
            _cache_discard(pattern)  # 시간 초과 패턴은 캐시에 두지 않는다
            if limit < settings.SCHEMA_REGEX_TIMEOUT:
                raise RegexBudgetExceeded() from None
            raise RegexTimeout() from None
        finally:
            self.remaining -= time.monotonic() - t0


MAX_REPEAT = user_regex.MAX_REPEAT                      # {n}·{n,}·{n,m} 의 각 수치 상한
MAX_REPEAT_PRODUCT = user_regex.MAX_REPEAT_PRODUCT      # 중첩 반복 횟수의 곱(고정 확장 크기) 상한
MAX_REPEAT_EXPANSION = user_regex.MAX_REPEAT_EXPANSION  # 패턴 전체를 펼친 크기(반복 × 내용, 분기 합) 상한
COMPILE_CACHE_MAX = 64


class UnsafePattern(Exception):
    """정규식을 컴파일하기 전 정적 검사(허용 문법·크기)에서 거부됐거나 컴파일 중 자원 오류(MemoryError 등)가 났다."""

    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en


def regex_static_problem(pattern: str) -> dict | None:
    """regex.compile 전에 패턴을 정적으로 검사한다(컴파일 단계 메모리 고갈 방지). 문제 없으면 None.

    허용 문법(user_regex 모듈 docstring)의 작은 부분집합만 받는다 — 그 밖의 문법은 "지원하지 않는 정규식 문법".
    받아들인 패턴은 AST 에서 반복 수치 ≤ MAX_REPEAT, 중첩 반복 곱 ≤ MAX_REPEAT_PRODUCT,
    펼친 크기 ≤ MAX_REPEAT_EXPANSION 을 확인한다(무한 반복은 하한 기준 — 백트래킹 폭주는 탐침·timeout 이 맡는다).
    """
    if not isinstance(pattern, str):
        return _msg("정규식 패턴 형식 오류", "Invalid regular expression pattern")
    if len(pattern) > MAX_PATTERN:
        return _msg(f"정규식은 {MAX_PATTERN}자 이하여야 합니다", f"Pattern must be {MAX_PATTERN} characters or fewer")
    try:
        user_regex.check(pattern)
    except user_regex.GrammarError as exc:
        return _msg(f"지원하지 않는 정규식 문법입니다 — {exc.pos + 1}번째 글자 근처: {exc.ko}",
                    f"Unsupported regular expression syntax — near character {exc.pos + 1}: {exc.en}")
    except user_regex.SizeError as exc:
        if exc.kind == "repeat":
            return _msg(f"정규식 반복 횟수는 {MAX_REPEAT} 이하여야 합니다", f"Repetition counts must be {MAX_REPEAT} or less")
        if exc.kind == "product":
            return _msg(f"정규식이 너무 큽니다(중첩 반복 횟수의 곱이 {MAX_REPEAT_PRODUCT:,} 초과)",
                        f"Regular expression too large (nested repetition product exceeds {MAX_REPEAT_PRODUCT:,})")
        return _msg(f"정규식이 너무 큽니다(펼친 크기가 {MAX_REPEAT_EXPANSION:,} 초과)",
                    f"Regular expression too large (expanded size exceeds {MAX_REPEAT_EXPANSION:,})")
    except RecursionError:
        return _msg("정규식이 너무 복잡합니다", "Regular expression too complex")
    return None


# 컴파일 캐시 — 정적 검사·컴파일·시간 판정을 모두 통과한 패턴만 명시적으로 넣는다(거부·시간 초과 패턴은 넣지 않음).
_compile_cache: OrderedDict[str, object] = OrderedDict()
_compile_cache_lock = threading.Lock()


def _cache_get(pattern: str):
    with _compile_cache_lock:
        compiled = _compile_cache.get(pattern)
        if compiled is not None:
            _compile_cache.move_to_end(pattern)
        return compiled


def _cache_put(pattern: str, compiled) -> None:
    with _compile_cache_lock:
        _compile_cache[pattern] = compiled
        _compile_cache.move_to_end(pattern)
        while len(_compile_cache) > COMPILE_CACHE_MAX:
            _compile_cache.popitem(last=False)


def _cache_discard(pattern: str) -> None:
    with _compile_cache_lock:
        _compile_cache.pop(pattern, None)


def compile_cache_patterns() -> list[str]:
    """(테스트·진단용) 현재 캐시에 든 패턴."""
    with _compile_cache_lock:
        return list(_compile_cache)


def compile_cache_clear() -> None:
    with _compile_cache_lock:
        _compile_cache.clear()


def _compile_uncached(pattern: str):
    """정적 검사 → 컴파일 → 컴파일 시간 판정(보조). 캐시는 건드리지 않는다.
    거부·자원 오류·시간 초과는 UnsafePattern, 문법 오류는 regex.error.

    regex 모듈 내부 캐시(regex.regex._cache, 최대 500)에 남기지 않도록 cache_pattern=False 로 컴파일하고,
    캐시를 끄더라도 쌓이는 로캘 판정 기록(_locale_sensitive)도 지운다 — 보관은 앱 LRU(통과한 패턴만)가 유일하다."""
    problem = regex_static_problem(pattern)
    if problem:
        raise UnsafePattern(problem["ko"], problem["en"])
    t0 = time.monotonic()
    try:
        compiled = user_re.compile(pattern, cache_pattern=False)
    except (MemoryError, OverflowError, RecursionError):
        raise UnsafePattern("정규식이 너무 복잡해 컴파일할 수 없습니다",
                            "Regular expression too complex to compile") from None
    finally:
        _forget_regex_internals(pattern)
    # 보조 판정: 허용 문법·크기 상한 안에서는 컴파일이 수 ms 이내라 정상 환경에서는 걸리지 않는다
    if time.monotonic() - t0 > settings.SCHEMA_REGEX_TIMEOUT * 10:
        raise UnsafePattern("정규식이 너무 복잡합니다(컴파일 시간 초과)",
                            "Regular expression too complex (compile time exceeded)")
    return compiled


def _forget_regex_internals(pattern: str) -> None:
    """regex 모듈이 cache_pattern=False 여도 남기는 패턴별 기록(로캘 판정)을 지운다(사용자 패턴이 무한히 쌓이지 않게).
    지워도 다음 컴파일에서 로캘을 다시 판정할 뿐 동작은 같다."""
    internal = getattr(getattr(user_re, "regex", None), "_locale_sensitive", None)
    if isinstance(internal, dict):
        internal.pop((type(pattern), pattern), None)


def compile_user_regex(pattern: str):
    """사용자 정규식 컴파일의 **유일한** 진입점(정의 저장·임시저장·미리보기·가져오기·실행 모두).

    캐시에 있으면 그대로, 없으면 정적 검사·컴파일·시간 판정을 거쳐 통과한 것만 캐시에 넣는다.
    거부·자원 오류는 UnsafePattern, 문법 오류는 regex.error 를 그대로 올린다."""
    if not isinstance(pattern, str):
        raise UnsafePattern("정규식 패턴 형식 오류", "Invalid regular expression pattern")
    compiled = _cache_get(pattern)
    if compiled is None:
        compiled = _compile_uncached(pattern)
        _cache_put(pattern, compiled)
    return compiled


def user_regex_search(pattern: str, text: str, ctx: CheckContext | None = None) -> bool:
    """사용자 정의 정규식 부분 일치(원본 RegExp.test 와 같음). 시간 초과면 RegexTimeout(예산 소진은 하위 클래스),
    정적 검사 거부면 UnsafePattern."""
    return (ctx or CheckContext()).search(pattern, text)


TIMEOUT_PROBLEM = {"ko": "정규식이 너무 복잡합니다(검사 시간 초과) — 패턴을 ^…$ 로 감싸거나 반복을 줄이세요",
                   "en": "Regular expression too complex (time limit exceeded) — wrap the pattern in ^…$ or reduce repetition"}
BUDGET_PROBLEM = {"ko": "정규식 검사 시간 초과(시간 예산 초과) — 검사를 중단했습니다. 정규식 수를 줄이거나 패턴을 ^…$ 로 감싸세요",
                  "en": "Regular expression check timed out (time budget exceeded) — check aborted. "
                        "Use fewer patterns or wrap them in ^…$"}


def regex_problem(pattern: str, ctx: CheckContext | None = None, max_len: int | None = None) -> dict | None:
    """정의 단계 검사: 허용 문법·크기 정적 검사 → 컴파일(시간 판정 보조) → 탐침 실행 시간. 문제 없으면 None.

    max_len: 이 패턴을 쓰는 필드의 최대 입력 길이(탐침 길이). 없으면 가장 긴 입력(TEXTAREA) 기준.
    모든 판정을 통과한 패턴만 컴파일 캐시에 넣는다 — 거부·시간 초과 패턴은 캐시에 남지 않고, 같은 패턴을 다른
    요청에서 다시 내도 매번 같은 검사를 다시 받는다. 같은 ctx(정의 1건) 안에서는 고유 패턴마다 한 번만 컴파일하고,
    고유 패턴 수는 MAX_DEFINITION_PATTERNS 로 제한하며, 시간 예산을 다 쓰면 남은 패턴은 컴파일하지 않고 거부한다."""
    ctx = ctx or CheckContext()
    if not isinstance(pattern, str):
        return _msg("정규식 패턴 형식 오류", "Invalid regular expression pattern")
    n = max(2, int(max_len or PROBE_DEFAULT_LEN))
    key = (pattern, n)
    if key not in ctx.def_results:
        ctx.def_results[key] = _regex_problem(pattern, ctx, n)
    problem = ctx.def_results[key]
    return dict(problem) if problem else None


def _regex_problem(pattern: str, ctx: CheckContext, n: int) -> dict | None:
    if pattern in ctx.def_stage:
        if ctx.def_stage[pattern] is not None:
            return ctx.def_stage[pattern]
    else:
        static = regex_static_problem(pattern)  # 컴파일 없음 — 상한·예산과 무관하게 항상 알려 준다
        if static:
            ctx.def_stage[pattern] = static
            return static
        if sum(1 for v in ctx.def_stage.values() if v is None) >= MAX_DEFINITION_PATTERNS:
            return _msg(f"한 데이터 구조에 쓸 수 있는 서로 다른 정규식은 최대 {MAX_DEFINITION_PATTERNS}개입니다",
                        f"A data schema can use at most {MAX_DEFINITION_PATTERNS} distinct regular expressions")
    timeout_len = ctx.def_timeout_len.get(pattern)
    if timeout_len is not None and timeout_len <= n:  # 더 짧은 탐침에서 이미 시간 초과
        return dict(TIMEOUT_PROBLEM)
    compiled = ctx.def_compiled.get(pattern) or _cache_get(pattern)
    if compiled is None:
        if ctx.remaining <= 0:  # 예산 소진 — 컴파일하지 않는다(다음 요청에서 다시 검사 가능하도록 기록도 남기지 않음)
            return dict(BUDGET_PROBLEM)
        t0 = time.monotonic()
        try:
            compiled = _compile_uncached(pattern)
        except UnsafePattern as exc:
            ctx.def_stage[pattern] = _msg(exc.ko, exc.en)
            return ctx.def_stage[pattern]
        except (user_re.error, ValueError, TypeError):
            ctx.def_stage[pattern] = _msg("정규식 문법이 올바르지 않습니다", "Invalid regular expression")
            return ctx.def_stage[pattern]
        finally:
            ctx.remaining -= time.monotonic() - t0
    ctx.def_stage[pattern] = None
    try:
        for probe in regex_probes(pattern, n):
            ctx.search(pattern, probe, compiled=compiled, remember=False)
    except RegexBudgetExceeded:
        _cache_discard(pattern)
        return dict(BUDGET_PROBLEM)
    except RegexTimeout:
        _cache_discard(pattern)
        ctx.def_timeout_len[pattern] = min(n, ctx.def_timeout_len.get(pattern, n))
        ctx.def_compiled[pattern] = compiled
        return dict(TIMEOUT_PROBLEM)
    _cache_put(pattern, compiled)
    return None


def check_user_regex(pattern: str, ctx: CheckContext | None = None, max_len: int | None = None) -> str | None:
    """regex_problem 의 한국어 사유만(문제 없으면 None)."""
    problem = regex_problem(pattern, ctx, max_len)
    return problem["ko"] if problem else None


class DefinitionError(ValueError):
    pass


def _msg(ko: str, en: str) -> dict:
    return {"ko": ko, "en": en}


# ═══════════════════════════════════════════════════════════
#  안전한 수식 평가기 (CALCULATED 수식, 데이터 구조 검증 규칙)
#  허용: 숫자·문자열·불리언 상수, 필드명, + - * / %, 비교, and/or/not, 괄호. 함수 호출·속성 접근 불가.
# ═══════════════════════════════════════════════════════════
class Missing(Exception):
    """수식이 참조하는 값이 비어 있음 — 계산·검증을 건너뛴다."""


_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Constant, ast.Name,
                  ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.USub, ast.UAdd, ast.Not,
                  ast.And, ast.Or, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


def parse_expression(expr: str) -> ast.Expression:
    s = (expr or "").strip()
    if not s or len(s) > 300:
        raise DefinitionError("수식이 비었거나 너무 깁니다(최대 300자)")
    s = _js_ops_to_python(s)
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError:
        raise DefinitionError("수식 문법 오류") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise DefinitionError(f"수식에 쓸 수 없는 요소: {type(node).__name__}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool)):
            raise DefinitionError("수식 상수 형식 오류")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            raise DefinitionError("거듭제곱은 쓸 수 없습니다")
    return tree


def _js_ops_to_python(s: str) -> str:
    """JS 식 표기(===, !==, &&, ||, !)를 파이썬 식으로. 문자열 리터럴 안은 바꾸지 않는다(작은 토크나이저)."""
    out, i, n = [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "'\"":
            j = i + 1
            while j < n and s[j] != ch:
                j += 2 if s[j] == "\\" else 1
            out.append(s[i:j + 1])
            i = j + 1
            continue
        for js, py in (("===", "=="), ("!==", "!="), ("&&", " and "), ("||", " or ")):
            if s.startswith(js, i):
                out.append(py)
                i += len(js)
                break
        else:
            if ch == "!" and not s.startswith("!=", i):
                out.append(" not ")
            else:
                out.append(ch)
            i += 1
    return "".join(out)


def _guard(v):
    """중간 결과 크기 제한 — 너무 큰 정수·무한대는 계산 불가(Missing)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v.bit_length() > EXPR_INT_BITS:
        raise Missing("overflow")
    if isinstance(v, float) and not math.isfinite(v):
        raise Missing("overflow")
    return v


def expression_names(tree: ast.Expression) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def evaluate(tree: ast.Expression, values: dict):
    try:
        return _evaluate(tree, values)
    except (OverflowError, ArithmeticError, ValueError, TypeError, RecursionError, MemoryError):
        raise Missing("arith") from None


def _evaluate(tree: ast.Expression, values: dict):
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return _guard(n.value) if isinstance(n.value, (int, float)) else n.value
        if isinstance(n, ast.Name):
            if n.id in ("true", "True"):
                return True
            if n.id in ("false", "False"):
                return False
            v = values.get(n.id)
            if v is None or v == "" or v == []:
                raise Missing(n.id)
            return v
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.Not):
                return not v
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise Missing("type")
            return _guard(-v if isinstance(n.op, ast.USub) else +v)
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                return all(ev(x) for x in n.values)
            return any(ev(x) for x in n.values)
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)):
                raise Missing("type")
            if isinstance(n.op, ast.Add):
                return _guard(a + b)
            if isinstance(n.op, ast.Sub):
                return _guard(a - b)
            if isinstance(n.op, ast.Mult):
                if isinstance(a, int) and isinstance(b, int) and a.bit_length() + b.bit_length() > EXPR_INT_BITS + 1:
                    raise Missing("overflow")
                return _guard(a * b)
            if b == 0:
                raise Missing("div0")
            return _guard(a / b if isinstance(n.op, ast.Div) else a % b)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, comp in zip(n.ops, n.comparators):
                right = ev(comp)
                try:
                    ok = {ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right, ast.Gt: lambda: left > right,
                          ast.GtE: lambda: left >= right, ast.Eq: lambda: left == right,
                          ast.NotEq: lambda: left != right}[type(op)]()
                except TypeError:
                    raise Missing("type") from None
                if not ok:
                    return False
                left = right
            return True
        raise Missing("node")

    return ev(tree)


# ═══════════════════════════════════════════════════════════
#  ① 구조 정의 검증 (빌더 저장·JSON 가져오기)
# ═══════════════════════════════════════════════════════════
NUM_ABS_LIMIT = DECIMAL_LIMIT  # 정의(제약조건) 수치의 절댓값 상한 — 관리자 데이터사전과 공유


def finite_number(v) -> float:
    """정의용 수치 → 유한한 float. bool·리스트·객체·NaN·±inf·거대 정수(10**400)·절댓값 상한 초과는 ValueError."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError
    try:
        f = float(v)
    except OverflowError:
        raise ValueError from None
    if not math.isfinite(f) or abs(f) > NUM_ABS_LIMIT:
        raise ValueError
    return f


def _num(v, *, integer=False):
    if v is None or v == "":
        return None
    f = finite_number(v)
    if integer:
        if f != int(f):
            raise ValueError
        return int(f)
    return int(f) if f == int(f) and abs(f) < 1e15 else f


def _str(v, limit: int) -> str:
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)):
        raise ValueError
    s = str(v).strip()
    if has_bad_chars(s):
        raise ValueError
    if len(s) > limit:
        raise ValueError
    return s


def _iso_date(v) -> str | None:
    if v in (None, ""):
        return None
    s = str(v)
    date.fromisoformat(s)  # ValueError
    if len(s) != 10:
        raise ValueError
    return s


def clean_constraints(dtype: str, raw: dict, *, strict: bool, errs: list, label: str, ctx: CheckContext | None = None) -> dict:
    """타입별 허용 키만 남기고 값 형식을 맞춘다. strict=False(임시저장)는 '아직 비어 있음'만 너그럽게 본다."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {}
    allowed = CONSTRAINT_KEYS.get(dtype, set())

    def err(ko, en):
        errs.append(_msg(f"{label}: {ko}", f"{label}: {en}"))

    try:
        if "minLength" in allowed:
            out["minLength"] = _num(raw.get("minLength"), integer=True)
            out["maxLength"] = _num(raw.get("maxLength"), integer=True)
            for k in ("minLength", "maxLength"):
                if out[k] is not None and out[k] < 0:
                    raise ValueError
            if out["minLength"] is not None and out["maxLength"] is not None and out["minLength"] > out["maxLength"]:
                err("최소 길이가 최대 길이보다 큽니다", "Min length exceeds max length")
    except (TypeError, ValueError):
        err("길이 제약은 0 이상의 정수여야 합니다", "Length constraints must be non-negative integers")
    if "pattern" in allowed and raw.get("pattern"):
        p = raw.get("pattern")
        if not isinstance(p, str):
            err("정규식은 문자열이어야 합니다", "Pattern must be a string")
        elif len(p) > MAX_PATTERN:
            err(f"정규식은 {MAX_PATTERN}자 이하여야 합니다", f"Pattern must be {MAX_PATTERN} characters or fewer")
        else:
            problem = regex_problem(p, ctx, max_len=MAX_TEXT_DEFAULT.get(dtype))
            if problem is None:
                out["pattern"] = p
            else:
                err(problem["ko"], problem["en"])
        for k in ("patternMessage", "patternMessageEn"):
            if raw.get(k):
                out[k] = str(raw[k])[:200]
    try:
        if "min" in allowed:
            out["min"] = _num(raw.get("min"))
            out["max"] = _num(raw.get("max"))
            if any(v is not None and abs(v) > DECIMAL_LIMIT for v in (out["min"], out["max"])):
                raise ValueError
            if out["min"] is not None and out["max"] is not None and out["min"] > out["max"]:
                err("최솟값이 최댓값보다 큽니다", "Min exceeds max")
    except (TypeError, ValueError):
        err("최솟값·최댓값은 숫자여야 합니다", "Min/max must be numbers")
    if "precision" in allowed:
        try:
            p = _num(raw.get("precision"), integer=True)
            if p is not None and not 0 <= p <= 6:
                raise ValueError
            out["precision"] = p
        except (TypeError, ValueError):
            err("소수 자릿수는 0~6 사이 정수여야 합니다", "Decimal places must be an integer 0–6")
    if "unit" in allowed and raw.get("unit"):
        out["unit"] = str(raw["unit"]).strip()[:30]
    if "minDate" in allowed:
        try:
            out["minDate"] = _iso_date(raw.get("minDate"))
            out["maxDate"] = _iso_date(raw.get("maxDate"))
            if out["minDate"] and out["maxDate"] and out["minDate"] > out["maxDate"]:
                err("최소 날짜가 최대 날짜보다 늦습니다", "Min date is after max date")
        except (TypeError, ValueError):
            err("날짜 제약은 YYYY-MM-DD 형식이어야 합니다", "Date constraints must be YYYY-MM-DD")
    if "options" in allowed:
        opts, seen = [], set()
        raw_opts = raw.get("options") if isinstance(raw.get("options"), list) else []
        for o in raw_opts[:MAX_OPTIONS + 1]:
            if isinstance(o, str):
                o = {"value": o}
            if not isinstance(o, dict):
                continue
            v = str(o.get("value") if o.get("value") is not None else "").strip()[:100]
            if not v:
                if strict:
                    err("선택지 값이 비어 있습니다", "An option value is empty")
                continue
            if v in seen:
                err(f"선택지 값이 중복됩니다: {v}", f"Duplicate option value: {v}")
                continue
            seen.add(v)
            opt = {"value": v, "label": str(o.get("label") or v).strip()[:200]}
            if o.get("labelEn"):
                opt["labelEn"] = str(o["labelEn"]).strip()[:200]
            if o.get("description"):
                opt["description"] = str(o["description"]).strip()[:200]
            opts.append(opt)
        if len(opts) > MAX_OPTIONS:
            err(f"선택지는 최대 {MAX_OPTIONS}개입니다", f"At most {MAX_OPTIONS} options")
        if not opts and strict:
            err("선택지를 1개 이상 입력하세요", "Add at least one option")
        out["options"] = opts
    if "maxSelections" in allowed:
        try:
            m = _num(raw.get("maxSelections"), integer=True)
            if m is not None and m < 1:
                raise ValueError
            out["maxSelections"] = m
        except (TypeError, ValueError):
            err("최대 선택 수는 1 이상의 정수여야 합니다", "Max selections must be a positive integer")
    if "allowedFileTypes" in allowed:
        types = raw.get("allowedFileTypes") or []
        if isinstance(types, str):
            types = types.split(",")
        clean = []
        for t in types if isinstance(types, list) else []:
            t = str(t).strip().lower()
            if not t:
                continue
            if not t.startswith("."):
                t = "." + t
            if not re.match(r"^\.[a-z0-9]{1,10}(\.[a-z0-9]{1,5})?$", t):
                err(f"허용 파일 유형 형식 오류: {t}", f"Invalid file type: {t}")
                continue
            clean.append(t)
        out["allowedFileTypes"] = clean or None
        try:
            mb = _num(raw.get("maxFileSizeMB"))
            cap = settings.SCHEMA_FILE_MAX_BYTES / 1024 / 1024
            if mb is not None and not 0 < mb <= cap:
                err(f"최대 파일 크기는 0 초과 {cap:g}MB 이하여야 합니다", f"Max file size must be within (0, {cap:g}] MB")
                mb = None
            out["maxFileSizeMB"] = mb
        except (TypeError, ValueError):
            err("최대 파일 크기는 숫자여야 합니다", "Max file size must be a number")
    if "refDictionary" in allowed:
        for k in ("refDictionary", "refField"):
            if raw.get(k):
                out[k] = str(raw[k]).strip()[:100]
    if "formula" in allowed:
        f = raw.get("formula") or ""
        f = f.strip() if isinstance(f, str) else None
        if f is None:
            err("수식은 문자열이어야 합니다", "Formula must be a string")
        elif f:
            out["formula"] = f[:300]
        elif strict:
            err("계산 필드에는 수식이 필요합니다", "A calculated field needs a formula")
    if "codeSystem" in allowed:
        for k in ("codeSystem", "codeSystemEn"):
            if raw.get(k):
                out[k] = str(raw[k]).strip()[:120]
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _contains_bad_chars(obj) -> bool:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if has_bad_chars(cur):
                return True
        elif isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(k, str) and has_bad_chars(k):
                    return True
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return False


def link_field_name_of(link_field):
    """JSON 의 linkField — 객체일 때만 fieldName 을 읽는다(문자열·리스트 등은 없음으로)."""
    return link_field.get("fieldName") if isinstance(link_field, dict) else None


def clean_definition(payload: dict, *, strict: bool = True, ctx: CheckContext | None = None,
                     legacy_names=frozenset()) -> tuple[dict, list[dict], list[dict]]:
    """빌더 JSON → (meta, fields, errors). errors 가 있으면 저장하지 않는다. ctx: 정규식 시간 예산(없으면 새로).
    legacy_names: 이미 저장돼 있던 필드명 — 연속·끝 밑줄 금지 규칙(새 이름 규칙)의 예외."""
    ctx = ctx or CheckContext()
    errs: list[dict] = []
    if not isinstance(payload, dict):
        return {}, [], [_msg("요청 형식이 올바르지 않습니다", "Malformed request")]
    if _contains_bad_chars(payload):  # 구조명·필드명·라벨·선택지 등 어떤 텍스트에도 서로게이트·NUL 금지
        return {}, [], [_msg("허용되지 않는 문자(서로게이트·NUL)가 있습니다", "Disallowed characters (surrogates/NUL)")]
    meta_raw = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
    meta = {}
    try:
        meta["name"] = _str(meta_raw.get("name"), 200)
        meta["name_en"] = _str(meta_raw.get("nameEn") or meta_raw.get("name_en"), 200)
        meta["description"] = _str(meta_raw.get("description"), 4000)
        meta["description_en"] = _str(meta_raw.get("descriptionEn") or meta_raw.get("description_en"), 4000)
    except ValueError:
        errs.append(_msg("이름은 200자, 설명은 4,000자 이하여야 합니다", "Name ≤ 200 and description ≤ 4,000 characters"))
    if not meta.get("name"):
        errs.append(_msg("데이터 구조명을 입력하세요", "Enter a data schema name"))
    # 코드값 비교 전에 형식부터 확인한다(리스트·객체는 해시 불가 → 집합/사전 조회에서 TypeError)
    cat = meta_raw.get("category") or "clinical_ext"
    if not isinstance(cat, str) or cat not in CATEGORY:
        errs.append(_msg("카테고리가 올바르지 않습니다", "Invalid category"))
        cat = "clinical_ext"
    meta["category"] = cat
    vis = meta_raw.get("visibility") or "private"
    if not isinstance(vis, str) or vis not in VISIBILITY:
        errs.append(_msg("공개 범위가 올바르지 않습니다", "Invalid visibility"))
        vis = "private"
    meta["visibility"] = vis
    link = meta_raw.get("linkFieldName") or link_field_name_of(meta_raw.get("linkField")) or "condition_id"
    if not isinstance(link, str) or link not in LINK_FIELD:
        errs.append(_msg("연결 키 필드가 올바르지 않습니다", "Invalid link key field"))
        link = "condition_id"
    meta["link_field_name"] = link
    tags = meta_raw.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    meta["tags"] = [str(t).strip()[:40] for t in tags if str(t).strip()][:20] if isinstance(tags, list) else []
    tags_en = meta_raw.get("tagsEn") or []
    meta["tags_en"] = [str(t).strip()[:40] for t in tags_en if str(t).strip()][:20] if isinstance(tags_en, list) else []
    groups = meta_raw.get("fieldGroups") if isinstance(meta_raw.get("fieldGroups"), list) else []
    meta["field_groups"] = [
        {k: (str(g.get(k))[:200] if k != "order" else g.get(k)) for k in
         ("groupName", "displayName", "displayNameEn", "description", "descriptionEn", "order") if g.get(k) is not None}
        for g in groups if isinstance(g, dict) and g.get("groupName")][:50]
    rules = meta_raw.get("validationRules") if isinstance(meta_raw.get("validationRules"), list) else []
    meta["validation_rules"] = []
    for r in rules[:30]:
        if not isinstance(r, dict) or not r.get("expression"):
            continue
        try:
            parse_expression(str(r["expression"]))
        except DefinitionError as exc:
            errs.append(_msg(f"검증 규칙 '{r.get('name', '')}': {exc}", f"Validation rule '{r.get('name', '')}': {exc}"))
            continue
        meta["validation_rules"].append({k: str(r[k])[:300] for k in
                                         ("ruleId", "name", "nameEn", "expression", "message", "messageEn") if r.get(k)})

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        errs.append(_msg("fields 배열이 필요합니다", "A fields array is required"))
        raw_fields = []
    if len(raw_fields) > MAX_FIELDS:
        errs.append(_msg(f"필드는 최대 {MAX_FIELDS}개입니다", f"At most {MAX_FIELDS} fields"))
        raw_fields = raw_fields[:MAX_FIELDS]
    if strict and not raw_fields:
        errs.append(_msg("필드를 1개 이상 추가하세요", "Add at least one field"))

    from apps.dictionary.models import DataFieldType

    types = set(DataFieldType.values)
    fields, names, keys = [], {}, set()
    for i, f in enumerate(raw_fields):
        if not isinstance(f, dict):
            errs.append(_msg(f"fields[{i}]: 객체가 아닙니다", f"fields[{i}]: not an object"))
            continue
        raw_name = f.get("fieldName")
        fname = raw_name.strip() if isinstance(raw_name, str) else ""
        label = fname or f"fields[{i}]"
        name_problem = field_name_problem(fname, legacy=legacy_names)  # 관리자 데이터사전과 같은 규칙(apps.core.naming)
        if name_problem:
            errs.append(_msg(f"{label}: {name_problem['ko']}", f"{label}: {name_problem['en']}"))
        elif fname.lower() in names:
            errs.append(_msg(f"{label}: 같은 데이터 구조 내에 중복 필드명이 있습니다",
                             f"{label}: duplicate field name in this data schema"))
        elif fname == link:
            errs.append(_msg(f"{label}: 연결 키 필드명과 같은 이름은 쓸 수 없습니다",
                             f"{label}: cannot reuse the link key field name"))
        names[fname.lower()] = fname
        dtype = f.get("dataType")
        if not isinstance(dtype, str) or dtype not in types:
            errs.append(_msg(f"{label}: 올바른 dataType 이 필요합니다", f"{label}: a valid dataType is required"))
            continue
        try:
            display = _str(f.get("displayName"), 200)
            display_en = _str(f.get("displayNameEn"), 200)
            desc = _str(f.get("description"), 2000)
            desc_en = _str(f.get("descriptionEn"), 2000)
            group = _str(f.get("group"), 60)
        except ValueError:
            errs.append(_msg(f"{label}: 표시명 200자·설명 2,000자·그룹 60자 이하", f"{label}: display ≤ 200, description ≤ 2,000, group ≤ 60"))
            continue
        if not display:
            errs.append(_msg(f"{label}: 표시명을 입력하세요", f"{label}: enter a display name"))
        key = str(f.get("fieldId") or "").strip()[:60]
        if not key or key in keys:
            import uuid

            key = uuid.uuid4().hex[:12]
        keys.add(key)
        cons = clean_constraints(dtype, f.get("constraints") or {}, strict=strict, errs=errs, label=label, ctx=ctx)
        src = f.get("sourceRef") if isinstance(f.get("sourceRef"), dict) else None
        default = f.get("defaultValue")
        if default in ("", None) or isinstance(default, (dict, list)):
            default = None
        elif not isinstance(default, (str, int, float, bool)):
            default = None
        fields.append({
            "field_key": key, "field_name": fname, "display_name": display or fname, "display_name_en": display_en,
            "description": desc, "description_en": desc_en, "data_type": dtype, "constraints": cons,
            "default_value": default, "is_required": bool(f.get("isRequired")), "is_unique": bool(f.get("isUnique")),
            "order": len(fields), "group": group,
            "tags": [str(t)[:40] for t in f.get("tags", []) if isinstance(t, (str, int))][:20] if isinstance(f.get("tags"), list) else [],
            "source_dictionary_id": str(src.get("dictionaryId") or "")[:60] if src else "",
            "source_field_name": str(src.get("fieldName") or "")[:100] if src else "",
            "source_modified": bool(src.get("isModified")) if src else False,
        })
    # 계산 필드 수식은 같은 데이터 구조의 숫자 필드만 참조할 수 있다
    numeric = {f["field_name"] for f in fields if f["data_type"] in ("INTEGER", "DECIMAL", "CALCULATED")}
    for f in fields:
        formula = f["constraints"].get("formula")
        if f["data_type"] == "CALCULATED" and formula:
            try:
                tree = parse_expression(formula)
                unknown = expression_names(tree) - numeric
                if unknown:
                    errs.append(_msg(f"{f['field_name']}: 수식이 숫자 필드가 아닌 이름을 참조합니다 ({', '.join(sorted(unknown))})",
                                     f"{f['field_name']}: formula references non-numeric names ({', '.join(sorted(unknown))})"))
                if f["field_name"] in expression_names(tree):
                    errs.append(_msg(f"{f['field_name']}: 수식이 자기 자신을 참조합니다", f"{f['field_name']}: formula references itself"))
            except DefinitionError as exc:
                errs.append(_msg(f"{f['field_name']}: {exc}", f"{f['field_name']}: invalid formula"))
        if f["default_value"] is not None and f["data_type"] not in ("CALCULATED", "FILE"):
            _, issue = coerce_value(f, f["default_value"], ctx=ctx)
            if issue:
                errs.append(_msg(f"{f['field_name']}: 기본값이 제약조건에 맞지 않습니다 ({issue['ko']})",
                                 f"{f['field_name']}: default value violates constraints ({issue['en']})"))
    return meta, fields, errs


# ═══════════════════════════════════════════════════════════
#  ② 입력값 검증 (웹 폼·CSV·기존 데이터 수정 — 모두 이 함수)
# ═══════════════════════════════════════════════════════════
def _f(field, key, default=None):
    return (field["constraints"] if isinstance(field, dict) else field.constraints).get(key, default)


def _attr(field, key):
    return field[key] if isinstance(field, dict) else getattr(field, key)


def _name(field, lang="ko"):
    return pick(lang, _attr(field, "display_name"), _attr(field, "display_name_en"))


def _fmt_num(v):
    return f"{v:g}" if isinstance(v, float) else str(v)


BOOL_TRUE = {"true", "1", "yes", "y", "예", "t"}
BOOL_FALSE = {"false", "0", "no", "n", "아니오", "f"}


def is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "") or v == [] or v == {}


def coerce_value(field, raw, lang_obj=None, ctx: CheckContext | None = None):
    """한 값의 형 변환 + 제약조건 검사 → (값, 오류 dict|None). 빈 값은 (None, None) — 필수 여부는 호출 측."""
    dtype = _attr(field, "data_type")
    c = field["constraints"] if isinstance(field, dict) else field.constraints
    if is_empty(raw):
        return None, None

    def bad(ko, en):
        return None, _msg(ko, en)

    if _contains_bad_chars(raw):
        return bad("허용되지 않는 문자(서로게이트·NUL)가 있습니다", "Disallowed characters (surrogates/NUL)")

    if dtype in ("TEXT", "TEXTAREA", "REFERENCE", "CODED"):
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            return bad("문자열이어야 합니다", "Must be text")
        s = str(raw).strip() if dtype != "TEXTAREA" else str(raw).replace("\r\n", "\n").strip()
        hard = MAX_TEXT_DEFAULT.get(dtype, 1000)
        if c.get("minLength") is not None and len(s) < c["minLength"]:
            return bad(f"최소 {c['minLength']}자 이상 입력하세요", f"Enter at least {c['minLength']} characters")
        if c.get("maxLength") is not None and len(s) > c["maxLength"]:
            return bad(f"최대 {c['maxLength']}자까지 입력 가능합니다", f"Up to {c['maxLength']} characters allowed")
        if len(s) > hard:
            return bad(f"최대 {hard}자까지 입력 가능합니다", f"Up to {hard} characters allowed")
        if c.get("pattern"):
            try:
                ok = user_regex_search(c["pattern"], s, ctx)  # 원본 RegExp.test 와 같이 부분 일치(regex + timeout)
            except RegexBudgetExceeded:
                return bad("정규식 검사 중단(시간 예산 초과) — 입력을 나누어 다시 시도하거나 관리자에게 문의하세요",
                           "Regular expression check aborted (time budget exceeded) — split the input or contact an administrator")
            except RegexTimeout:
                return bad("정규식 검사 시간 초과 — 입력값을 확인하거나 관리자에게 문의하세요",
                           "Regular expression check timed out — check the value or contact an administrator")
            except UnsafePattern as exc:  # 예전에 저장된 위험 패턴 — 컴파일하지 않고 패턴 오류로 처리
                return bad(f"정규식 패턴 오류 — 관리자에게 문의하세요 ({exc.ko})",
                           f"Regular expression pattern error — contact an administrator ({exc.en})")
            except (user_re.error, ValueError, TypeError):  # 예전에 저장된 잘못된 패턴 — 통과시키지 않는다(fail-closed)
                return bad("정규식 패턴 오류 — 관리자에게 문의하세요",
                           "Regular expression pattern error — contact an administrator")
            if not ok:
                return bad(c.get("patternMessage") or "입력 형식이 올바르지 않습니다",
                           c.get("patternMessageEn") or c.get("patternMessage") or "Invalid input format")
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in s):
            return bad("제어 문자는 입력할 수 없습니다", "Control characters are not allowed")
        return s, None

    if dtype in ("INTEGER", "DECIMAL"):
        if isinstance(raw, bool):
            return bad("숫자여야 합니다", "Must be a number")
        try:
            # 문자열은 float 를 거치지 않고 Decimal 로 정확히 읽는다(2^53 초과 정수가 반올림돼 통과하지 않게)
            if isinstance(raw, int):
                dec = Decimal(raw)
            elif isinstance(raw, float):
                if not math.isfinite(raw):
                    raise ValueError
                dec = Decimal(repr(raw))
            else:
                s = str(raw).strip().replace(",", "")
                if "_" in s or not re.match(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$", s) or len(s) > 400:
                    raise ValueError
                dec = Decimal(s)
            if not dec.is_finite():
                raise ValueError
            if dec != 0 and dec.adjusted() > 30:  # 아주 큰 지수 — int 변환 전에 범위 밖으로 처리
                dec = Decimal("1e31").copy_sign(dec)
            f = float(dec)
        except (ValueError, InvalidOperation):
            return bad("숫자를 입력하세요" if dtype == "DECIMAL" else "정수를 입력하세요",
                       "Enter a number" if dtype == "DECIMAL" else "Enter a whole number")
        if dtype == "INTEGER":
            if dec != dec.to_integral_value():
                return bad("정수를 입력하세요", "Enter a whole number")
            if abs(dec) > INT_LIMIT:
                return bad("값이 너무 큽니다(절댓값 2^53 이하)", "Value too large (|v| ≤ 2^53)")
            val = int(dec)
        else:
            if abs(dec) > Decimal(int(DECIMAL_LIMIT)):
                return bad("값이 너무 큽니다(절댓값 10^15 이하)", "Value too large (|v| ≤ 10^15)")
            val = f
            p = c.get("precision")
            if p is not None:
                if round(val, p) != val and abs(round(val, p) - val) > 1e-12 * max(1.0, abs(val)):
                    return bad(f"소수 {p}자리까지 입력할 수 있습니다", f"Up to {p} decimal places allowed")
        if c.get("min") is not None and val < c["min"]:
            return bad(f"{_fmt_num(c['min'])} 이상 입력하세요", f"Enter a value of {_fmt_num(c['min'])} or more")
        if c.get("max") is not None and val > c["max"]:
            return bad(f"{_fmt_num(c['max'])} 이하로 입력하세요", f"Enter a value of {_fmt_num(c['max'])} or less")
        return val, None

    if dtype == "BOOLEAN":
        if isinstance(raw, bool):
            return raw, None
        s = str(raw).strip().lower()
        if s in BOOL_TRUE:
            return True, None
        if s in BOOL_FALSE:
            return False, None
        return bad("예/아니오(true/false) 값이어야 합니다", "Must be yes/no (true/false)")

    if dtype == "DATE":
        s = str(raw).strip()
        try:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
                raise ValueError
            date.fromisoformat(s)
        except ValueError:
            return bad("YYYY-MM-DD 형식의 날짜를 입력하세요", "Enter a date as YYYY-MM-DD")
        if c.get("minDate") and s < c["minDate"]:
            return bad(f"{c['minDate']} 이후 날짜를 선택하세요", f"Select a date on or after {c['minDate']}")
        if c.get("maxDate") and s > c["maxDate"]:
            return bad(f"{c['maxDate']} 이전 날짜를 선택하세요", f"Select a date on or before {c['maxDate']}")
        return s, None

    if dtype == "DATETIME":
        s = str(raw).strip()
        m = DATETIME_RE.match(s)
        try:
            if not m:
                raise ValueError
            datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4) or '00'}")
        except ValueError:
            return bad("YYYY-MM-DD HH:mm 형식의 날짜·시간을 입력하세요", "Enter a date-time as YYYY-MM-DD HH:mm")
        norm = f"{m.group(1)}T{m.group(2)}:{m.group(3)}"
        d = m.group(1)
        if c.get("minDate") and d < c["minDate"]:
            return bad(f"{c['minDate']} 이후 날짜를 선택하세요", f"Select a date on or after {c['minDate']}")
        if c.get("maxDate") and d > c["maxDate"]:
            return bad(f"{c['maxDate']} 이전 날짜를 선택하세요", f"Select a date on or before {c['maxDate']}")
        return norm, None

    if dtype == "SELECT":
        s = str(raw).strip()
        values = [o["value"] for o in c.get("options", [])]
        if s not in values:
            # CSV 는 라벨로 적어도 받아 준다(한·영)
            hit = next((o["value"] for o in c.get("options", []) if s in (o.get("label"), o.get("labelEn"))), None)
            if hit is None:
                return bad("허용된 선택지가 아닙니다", "Not an allowed option")
            s = hit
        return s, None

    if dtype == "MULTISELECT":
        items = raw if isinstance(raw, list) else MULTI_SEP.split(str(raw).strip())
        items = [str(x).strip() for x in items if str(x).strip()]
        opts = c.get("options", [])
        values = {o["value"] for o in opts}
        out = []
        for it in items:
            if it not in values:
                hit = next((o["value"] for o in opts if it in (o.get("label"), o.get("labelEn"))), None)
                if hit is None:
                    return bad(f"허용된 선택지가 아닙니다: {it}", f"Not an allowed option: {it}")
                it = hit
            if it in out:
                return bad(f"같은 선택지를 두 번 골랐습니다: {it}", f"Option selected twice: {it}")
            out.append(it)
        if c.get("maxSelections") is not None and len(out) > c["maxSelections"]:
            return bad(f"최대 {c['maxSelections']}개까지 선택 가능합니다", f"Up to {c['maxSelections']} selections allowed")
        return (out or None), None

    if dtype == "NUMERIC_RANGE":
        if isinstance(raw, dict):
            lo, hi = raw.get("min"), raw.get("max")
        else:
            parts = re.split(r"\s*~\s*", str(raw).strip())
            if len(parts) != 2:
                return bad("최소~최대 형식으로 입력하세요(예: 1.5~3)", "Enter as min~max (e.g. 1.5~3)")
            lo, hi = parts
        num_field = {"data_type": "DECIMAL", "constraints": {k: c[k] for k in ("min", "max", "precision") if k in c},
                     "display_name": "", "display_name_en": ""}
        lo_v, e1 = coerce_value(num_field, lo, ctx=ctx)
        hi_v, e2 = coerce_value(num_field, hi, ctx=ctx)
        if e1 or e2:
            e = e1 or e2
            return bad(f"범위 값: {e['ko']}", f"Range value: {e['en']}")
        if lo_v is None and hi_v is None:
            return None, None
        if lo_v is not None and hi_v is not None and lo_v > hi_v:
            return bad("최솟값이 최댓값보다 큽니다", "Min is greater than max")
        out = {}
        if lo_v is not None:
            out["min"] = lo_v
        if hi_v is not None:
            out["max"] = hi_v
        return out, None

    if dtype == "FILE":
        # 파일 자체는 services.store_field_file 이 검사한다. 여기서는 이미 저장된 첨부 메타데이터만 받는다.
        if isinstance(raw, dict) and raw.get("file_id"):
            return {k: raw[k] for k in ("file_id", "name", "size") if k in raw}, None
        return bad("파일은 웹 폼에서 첨부해야 합니다(CSV 로는 입력할 수 없음)", "Attach files in the web form (not via CSV)")

    if dtype == "CALCULATED":
        return None, None  # 서버가 계산한다(입력값 무시)
    return bad("알 수 없는 데이터 타입", "Unknown data type")


def compute_calculated(fields, data: dict) -> None:
    for f in fields:
        if _attr(f, "data_type") != "CALCULATED":
            continue
        name = _attr(f, "field_name")
        data.pop(name, None)
        formula = _f(f, "formula")
        if not formula:
            continue
        try:
            v = evaluate(parse_expression(formula), data)
        except (Missing, DefinitionError):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        p = _f(f, "precision")
        data[name] = round(v, p if p is not None else 2)


def validate_entry(fields, rules, raw_values: dict, link_value, row_no=None, *, link_label=("케이스 ID", "Case ID"),
                   ctx: CheckContext | None = None):
    """한 행 → (정리된 data, condition_id, issues). issues 의 severity 가 error 면 저장 불가."""
    issues = []

    def add(field_name, value, m, severity="error"):
        issues.append({"row": row_no, "field": field_name, "value": "" if value is None else str(value)[:200],
                       "ko": m["ko"], "en": m["en"], "severity": severity})

    link = "" if link_value is None else str(link_value).strip()
    if not link:
        add("condition_id", link, _msg(f"{link_label[0]}은(는) 필수 입력입니다", f"{link_label[1]} is required"))
    elif not LINK_VALUE_RE.match(link):
        add("condition_id", link, _msg("영문·숫자·-·_·. 만 쓸 수 있습니다(최대 40자)",
                                       "Use letters, digits, '-', '_' and '.' only (max 40)"))
    data = {}
    for f in fields:
        name = _attr(f, "field_name")
        dtype = _attr(f, "data_type")
        if dtype == "CALCULATED":
            continue
        raw = raw_values.get(name)
        val, err = coerce_value(f, raw, ctx=ctx)
        if err:
            add(name, raw if not isinstance(raw, (list, dict)) else raw, err)
            continue
        if val is None:
            if _attr(f, "is_required"):
                add(name, "", _msg(f"{_name(f)}은(는) 필수 입력입니다", f"{_name(f, 'en')} is required"))
            continue
        data[name] = val
    compute_calculated(fields, data)
    for r in rules or []:
        try:
            ok = evaluate(parse_expression(r.get("expression", "")), data)
        except (Missing, DefinitionError):
            continue
        if not ok:
            add(r.get("name") or "rule", "", _msg(r.get("message") or "검증 규칙 위반",
                                                  r.get("messageEn") or r.get("message") or "Validation rule failed"))
    return data, link, issues


def display_value(field, val, lang: str) -> str:
    """표·CSV·상세에 보일 문자열(선택지는 라벨, 범위는 min~max)."""
    if val is None or val == "":
        return ""
    dtype = _attr(field, "data_type")
    c = field["constraints"] if isinstance(field, dict) else field.constraints
    if dtype == "BOOLEAN":
        return ("Yes" if val else "No") if lang == "en" else ("예" if val else "아니오")
    if dtype == "SELECT":
        opt = next((o for o in c.get("options", []) if o["value"] == val), None)
        return option_label(opt, lang) if opt else str(val)
    if dtype == "MULTISELECT" and isinstance(val, list):
        labels = []
        for v in val:
            opt = next((o for o in c.get("options", []) if o["value"] == v), None)
            labels.append(option_label(opt, lang) if opt else str(v))
        return ", ".join(labels)
    if dtype == "NUMERIC_RANGE" and isinstance(val, dict):
        return f"{_fmt_num(val.get('min', ''))}~{_fmt_num(val.get('max', ''))}"
    if dtype == "FILE" and isinstance(val, dict):
        return str(val.get("name", ""))
    if dtype in ("DECIMAL", "CALCULATED") and isinstance(val, (int, float)) and not isinstance(val, bool):
        p = c.get("precision")
        return f"{val:.{p if p is not None else 2}f}"
    return str(val)


def csv_value(field, val) -> str:
    """CSV 내보내기 값 — 다시 가져올 수 있는 형식(선택지는 값, 다중은 |, 범위는 min~max)."""
    if val is None:
        return ""
    dtype = _attr(field, "data_type")
    if dtype == "BOOLEAN":
        return "true" if val else "false"
    if dtype == "MULTISELECT" and isinstance(val, list):
        return "|".join(str(v) for v in val)
    if dtype == "NUMERIC_RANGE" and isinstance(val, dict):
        return f"{_fmt_num(val.get('min', ''))}~{_fmt_num(val.get('max', ''))}"
    if dtype == "FILE" and isinstance(val, dict):
        return str(val.get("name", ""))
    if isinstance(val, float):
        return _fmt_num(val)
    return str(val)


__all__ = ["clean_definition", "validate_entry", "coerce_value", "display_value", "csv_value", "CODE_SYSTEMS"]
