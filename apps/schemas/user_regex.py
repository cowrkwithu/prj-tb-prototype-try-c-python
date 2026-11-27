"""사용자 정의 정규식의 허용 문법(엄격한 부분집합) 파서와 정적 크기 계산.

regex 모듈의 전체 문법을 흉내 내지 않는다. 사용자가 쓸 수 있는 문법을 작고 명확한 부분집합으로 제한하고,
이 문법을 정확히 따르는 작은 재귀 하강 파서로 AST 를 만든 뒤 그 AST 에서 반복 수치·중첩 곱·펼친 크기를 계산한다.
파서가 받아들이지 않는 패턴은 컴파일하지 않는다(regex.compile 단계 메모리 고갈 방지).

허용 문법
    pattern  := alt
    alt      := seq ('|' seq)*
    seq      := (atom quant?)*
    atom     := 리터럴 | '.' | '^' | '$' | '(' alt ')' | '(?:' alt ')' | escape | class
    escape   := \\d \\D \\w \\W \\s \\S \\b \\B | \\p{이름} \\P{이름} (이름 [A-Za-z_]{1,30})
              | 메타문자 이스케이프 \\. \\\\ \\( \\) \\[ \\] \\{ \\} \\* \\+ \\? \\| \\^ \\$ \\- \\/
    class    := '[' '^'? item+ ']'     item := 리터럴 | 범위 a-z | 클래스 이스케이프(\\b·\\B 제외)
    quant    := ('*' | '+' | '?' | '{n}' | '{n,}' | '{n,m}') '?'?      (n, m 은 ASCII 숫자 1~4자리)

거부(해석이 regex 모듈과 달라질 여지가 있는 것 포함)
    (?: 외의 모든 (? 구문(인라인 플래그·이름 그룹·lookaround·조건부·원자 그룹·주석 등), (*VERB),
    역참조·\\g<..>·\\N{..}·\\x..·\\n 같은 그 밖의 이스케이프, 퍼지 {e<=1}, 정식 수량자가 아닌 {,
    이스케이프되지 않은 } · ] (클래스 밖), 수량자 뒤 수량자(a{2}{3}, a++), 반복할 대상이 없는 수량자(^*, (*a)),
    클래스 안의 이스케이프 없는 [ (중첩·POSIX [:..:]·집합 연산), 클래스 안 && || -- ~~, [:..:]/[=..=]/[...] 형태,
    애매한 - (범위가 아니면서 처음·끝이 아닌 -), 빈 클래스, 역순 범위, 제어 문자·서로게이트, 200자 초과.
"""
from __future__ import annotations

import unicodedata

MAX_PATTERN = 200
MAX_REPEAT = 1000              # {n}·{n,}·{n,m} 의 각 수치 상한
MAX_REPEAT_PRODUCT = 10_000    # 중첩 반복 횟수의 곱(고정 확장 크기) 상한
MAX_REPEAT_EXPANSION = 10_000  # 패턴 전체를 펼친 크기(반복 × 내용, 분기 합) 상한
MAX_GROUP_DEPTH = 30           # 괄호 중첩 깊이 상한(파서·컴파일러 재귀 깊이 보호)

META = set("\\.^$|()[]{}*+?")
META_ESCAPES = set(".\\()[]{}*+?|^$-/")
CLASS_ESCAPES = set("dDwWsS")
ANCHOR_ESCAPES = set("bB")
DIGITS = set("0123456789")
NAME_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")


class GrammarError(Exception):
    """허용 문법 밖 — 위치(0부터)와 한/영 사유."""

    def __init__(self, pos: int, ko: str, en: str):
        super().__init__(ko)
        self.pos, self.ko, self.en = pos, ko, en


class SizeError(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # "repeat" | "product" | "expansion"


# AST 노드(튜플)
#   ("lit", ch) ("any",) ("anchor", "^"|"$"|"b"|"B") ("esc", "d") ("prop", neg, name) ("class", neg, items)
#   ("group", capture, alt) ("alt", [seq, ...]) ("seq", [node, ...]) ("rep", node, lo, hi|None, lazy)


class _Parser:
    def __init__(self, pattern: str):
        self.p = pattern
        self.n = len(pattern)
        self.i = 0
        self.depth = 0

    def fail(self, ko: str, en: str, pos: int | None = None):
        raise GrammarError(self.i if pos is None else pos, ko, en)

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.p[j] if j < self.n else ""

    def parse(self):
        node = self.alt()
        if self.i < self.n:  # alt 는 ')' 에서만 멈춘다
            self.fail("짝이 맞지 않는 닫는 괄호 )", "unbalanced closing parenthesis )")
        return node

    def alt(self):
        branches = [self.seq()]
        while self.peek() == "|":
            self.i += 1
            branches.append(self.seq())
        return ("alt", branches)

    def seq(self):
        items = []
        while self.i < self.n and self.peek() not in ("|", ")"):
            atom = self.atom()
            if self.peek() in ("*", "+", "?", "{"):
                if atom[0] == "anchor":
                    self.fail("위치 지정자(^ $ \\b \\B)에는 반복을 붙일 수 없습니다",
                              "quantifier on an anchor (^ $ \\b \\B) is not supported")
                atom = self.quant(atom)
            items.append(atom)
        return ("seq", items)

    def quant(self, atom):
        start = self.i
        ch = self.peek()
        if ch == "*":
            lo, hi = 0, None
            self.i += 1
        elif ch == "+":
            lo, hi = 1, None
            self.i += 1
        elif ch == "?":
            lo, hi = 0, 1
            self.i += 1
        else:
            lo, hi = self.braces()
        lazy = False
        if self.peek() == "?":
            lazy = True
            self.i += 1
        if self.peek() in ("*", "+", "?", "{"):
            self.fail("수량자 뒤에 다시 수량자를 붙일 수 없습니다(a{2}{3}, a++ 등)",
                      "a quantifier cannot follow another quantifier (a{2}{3}, a++ …)")
        if hi is not None and lo > hi:
            self.fail("반복 범위의 최솟값이 최댓값보다 큽니다", "repeat minimum exceeds maximum", start)
        return ("rep", atom, lo, hi, lazy)

    def _number(self) -> int:
        j = self.i
        while self.i < self.n and self.p[self.i] in DIGITS:
            self.i += 1
        digits = self.p[j:self.i]
        if not digits or len(digits) > 4:
            self.fail("중괄호는 {n} {n,} {n,m} 형식의 반복(숫자 1~4자리)만 쓸 수 있습니다 — 글자 { 는 \\{ 로 쓰세요",
                      "braces are only for repeats {n} {n,} {n,m} (1–4 digits) — write a literal { as \\{", j)
        return int(digits)

    def braces(self):
        start = self.i
        self.i += 1  # {
        lo = self._number()
        hi = lo
        if self.peek() == ",":
            self.i += 1
            hi = self._number() if self.peek() in DIGITS else None
        if self.peek() != "}":
            self.fail("중괄호는 {n} {n,} {n,m} 형식의 반복만 쓸 수 있습니다 — 글자 { 는 \\{ 로 쓰세요",
                      "braces are only for repeats {n} {n,} {n,m} — write a literal { as \\{", start)
        self.i += 1
        if lo > MAX_REPEAT or (hi is not None and hi > MAX_REPEAT):
            raise SizeError("repeat")
        return lo, hi

    def atom(self):
        ch = self.peek()
        start = self.i
        if ch in ("*", "+", "?", "{"):
            self.fail("반복할 대상이 없는 수량자입니다", "quantifier with nothing to repeat")
        if ch == "(":
            self.i += 1
            capture = True
            if self.peek() == "?":
                if self.peek(1) != ":":
                    self.fail("(?: 외의 (? 구문(인라인 플래그·이름 그룹·전후방 탐색 등)은 지원하지 않습니다",
                              "(? constructs other than (?: (inline flags, named groups, lookaround …) are not supported")
                self.i += 2
                capture = False
            self.depth += 1
            if self.depth > MAX_GROUP_DEPTH:
                self.fail(f"괄호 중첩은 {MAX_GROUP_DEPTH}단계까지입니다", f"groups may nest at most {MAX_GROUP_DEPTH} deep")
            body = self.alt()
            if self.peek() != ")":
                self.fail("닫는 괄호 ) 가 없습니다", "missing closing parenthesis )", start)
            self.i += 1
            self.depth -= 1
            return ("group", capture, body)
        if ch == "[":
            return self.char_class()
        if ch == "\\":
            return self.escape(in_class=False)
        if ch == ".":
            self.i += 1
            return ("any",)
        if ch in ("^", "$"):
            self.i += 1
            return ("anchor", ch)
        if ch in ("]", "}"):
            self.fail(f"글자 {ch} 는 \\{ch} 로 이스케이프해서 쓰세요", f"write a literal {ch} as \\{ch}")
        self._literal_ok(ch)
        self.i += 1
        return ("lit", ch)

    def _literal_ok(self, ch: str):
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cs", "Cn", "Co"):
            self.fail("제어 문자·서로게이트·미지정 문자는 쓸 수 없습니다", "control/surrogate/unassigned characters are not allowed")

    def escape(self, *, in_class: bool):
        start = self.i
        nxt = self.peek(1)
        if not nxt:
            self.fail("패턴이 \\ 로 끝났습니다", "pattern ends with a backslash")
        if nxt in CLASS_ESCAPES:
            self.i += 2
            return ("esc", nxt)
        if nxt in ANCHOR_ESCAPES and not in_class:
            self.i += 2
            return ("anchor", nxt)
        if nxt in ("p", "P"):
            if self.peek(2) != "{":
                self.fail("\\p 는 \\p{이름} 형식만 쓸 수 있습니다", "\\p must be written as \\p{Name}")
            j = self.i + 3
            k = j
            while k < self.n and self.p[k] in NAME_CHARS and k - j < 31:
                k += 1
            name = self.p[j:k]
            if not (1 <= len(name) <= 30) or k >= self.n or self.p[k] != "}":
                self.fail("\\p{이름} 의 이름은 영문·밑줄 1~30자여야 합니다", "the name in \\p{Name} must be 1–30 letters/underscores",
                          start)
            self.i = k + 1
            return ("prop", nxt == "P", name)
        if nxt in META_ESCAPES:
            self.i += 2
            return ("lit", nxt)
        self.fail(f"지원하지 않는 이스케이프 \\{nxt} 입니다(역참조·\\n·\\x 등은 쓸 수 없음)",
                  f"unsupported escape \\{nxt} (backreferences, \\n, \\x … are not allowed)")

    def char_class(self):
        start = self.i
        self.i += 1  # [
        neg = False
        if self.peek() == "^":
            neg = True
            self.i += 1
        body_start = self.i
        close = self.p.find("]", body_start)
        # [:..:] [=..=] [.....] — POSIX 문자 클래스·조합 문자처럼 보이는 형태(해석 차이 여지)
        if self.peek() in (":", "=", ".") and close > body_start + 1 and self.p[close - 1] == self.peek() \
                and self.p[close - 2] != "\\":
            self.fail("[:...:] 같은 POSIX 형식 문자 클래스는 지원하지 않습니다", "POSIX-style classes like [:...:] are not supported")
        items = []      # ("char", ch) | ("esc", e) | ("prop", neg, name) | ("range", a, b)
        last_kind = None  # "char" | "other" | "range"
        while True:
            if self.i >= self.n:
                self.fail("닫는 대괄호 ] 가 없습니다", "missing closing bracket ]", start)
            ch = self.peek()
            if ch == "]":
                if not items:
                    self.fail("빈 문자 클래스 [] 는 쓸 수 없습니다 — 글자 ] 는 \\] 로 쓰세요",
                              "empty character class [] — write a literal ] as \\]")
                self.i += 1
                return ("class", neg, items)
            if ch == "[":
                self.fail("문자 클래스 안의 [ 는 \\[ 로 이스케이프해야 합니다(중첩·POSIX·집합 연산 미지원)",
                          "a [ inside a character class must be escaped as \\[ (nesting/POSIX/set operations unsupported)")
            if ch in "&|-~" and self.peek(1) == ch:
                self.fail(f"문자 클래스 안의 {ch}{ch} 는 쓸 수 없습니다(집합 연산 미지원)",
                          f"{ch}{ch} inside a character class is not supported (set operations)")
            if ch == "-":
                first = not items
                last = self.peek(1) == "]"
                if first or last:
                    self.i += 1
                    items.append(("char", "-"))
                    last_kind = "char"
                    continue
                # 범위: 바로 앞이 단일 문자(범위의 끝이 아님), 바로 뒤가 단일 문자
                if last_kind != "char":
                    self.fail("문자 클래스 안의 - 위치가 애매합니다 — 글자 - 는 \\- 로 쓰세요",
                              "ambiguous - inside a character class — write a literal - as \\-")
                self.i += 1
                hi = self._class_single()
                if hi is None:
                    self.fail("문자 범위의 끝은 글자 하나여야 합니다", "a range must end with a single character")
                lo = items.pop()[1]
                if ord(lo) > ord(hi):
                    self.fail("문자 범위가 거꾸로 되어 있습니다", "character range is reversed")
                items.append(("range", lo, hi))
                last_kind = "range"
                continue
            if ch == "\\":
                node = self.escape(in_class=True)
                if node[0] == "lit":
                    items.append(("char", node[1]))
                    last_kind = "char"
                else:
                    items.append(node)
                    last_kind = "other"
                continue
            self._literal_ok(ch)
            self.i += 1
            items.append(("char", ch))
            last_kind = "char"

    def _class_single(self):
        ch = self.peek()
        if not ch or ch in ("]", "["):
            return None
        if ch == "\\":
            node = self.escape(in_class=True)
            return node[1] if node[0] == "lit" else None
        if ch == "-":
            return None
        self._literal_ok(ch)
        self.i += 1
        return ch


def parse(pattern: str):
    """허용 문법 → AST. 벗어나면 GrammarError, 반복 수치 상한 초과면 SizeError("repeat")."""
    if len(pattern) > MAX_PATTERN:
        raise GrammarError(MAX_PATTERN, "too long", "too long")
    return _Parser(pattern).parse()


CAPTURE_WEIGHT = 2


def repeat_factor(lo: int, hi: int | None) -> int:
    """반복이 컴파일된 패턴 크기에 곱하는 배수(실측 기준).

    - {n,m}·{n}·? : 상한 m (n 개 필수 사본 + m-n 개 선택 사본)
    - * ({0,})    : 1
    - + · {n,} (n≥1): n + 1 — regex 는 X+ 를 'X 사본 + X*' 로 컴파일한다. 그래서 하한만 곱하면
      (?:(?:(?:a{1000})+)+)+ … 처럼 + 를 중첩할 때마다 크기가 두 배가 되는데도 통과한다(12단 중첩 시 RSS 2.2GB 실측).
    """
    if hi is not None:
        return max(1, hi)
    return lo + 1 if lo >= 1 else 1


def measure(node) -> tuple[int, int]:
    """(펼친 크기, 최대 중첩 반복 곱). 반복 배수는 repeat_factor(+·{n,} 는 n+1, * 는 1, {n,m} 은 m).
    문자 클래스는 구성원 수만큼, 캡처 그룹은 내용 + CAPTURE_WEIGHT, 분기(|)는 분기 수를 더하고, 나머지 원소는 1로 센다."""
    kind = node[0]
    if kind in ("lit", "any", "anchor", "esc", "prop"):
        return 1, 1
    if kind == "class":  # 컴파일된 크기가 구성원 수(글자·범위·이스케이프)에 비례 — 구성원마다 1로 센다(보수적)
        return max(1, len(node[2])), 1
    if kind == "group":  # 캡처 그룹은 반복될 때마다 그룹 표지(시작·끝)가 함께 복제된다 — 2를 더한다(실측)
        size, prod = measure(node[2])
        return max(1, size) + (CAPTURE_WEIGHT if node[1] else 0), prod
    if kind in ("alt", "seq"):
        # 분기가 둘 이상이면 분기마다 분기 표지가 복제된다 — 분기 수를 더한다(실측)
        size, prod = (len(node[1]) if kind == "alt" and len(node[1]) > 1 else 0), 1
        for child in node[1]:
            s, p = measure(child)
            size += s
            prod = max(prod, p)
            if size > MAX_REPEAT_EXPANSION:
                raise SizeError("expansion")
        return size, prod
    if kind == "rep":
        _, child, lo, hi, _ = node
        factor = repeat_factor(lo, hi)
        s, p = measure(child)
        if p * factor > MAX_REPEAT_PRODUCT:
            raise SizeError("product")
        if s * factor > MAX_REPEAT_EXPANSION:
            raise SizeError("expansion")
        return s * factor, p * factor
    raise AssertionError(kind)


def check(pattern: str) -> None:
    """허용 문법 + 크기 상한 검사. 통과하면 None, 아니면 GrammarError/SizeError."""
    tree = parse(pattern)
    measure(tree)
