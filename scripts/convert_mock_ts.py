#!/usr/bin/env python3
r"""원본(React) Mock 데이터(.ts/.json)를 data/seed/*.json 으로 변환하는 일회성 스크립트.

Node.js 없이 TypeScript 객체 리터럴을 파싱한다. 지원 범위는 원본 data/*.ts 가 쓰는 문법:
주석(// · /* */), 따옴표 없는 키, '…'/"…"/`…`(보간 없는) 문자열, 숫자, true/false/null/undefined,
후행 쉼표, 숫자(1_000·0x1F·.5·+5·- 1), 줄 이음(\+CRLF/LF).
리터럴이 아닌 값(함수 참조·화살표 함수)은 허용 목록 키(EXPR_ALLOWED_PARENTS = validationRules) 아래에서만
{"$expr": "<원문>"} 으로 보존하고, 그 밖에서는 TsParseError 로 중단한다.

사용법:
    python scripts/convert_mock_ts.py [--src <원본 저장소 루트>]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT.parent / "prj-tb-prototype-try-b"
OUT_DIR = ROOT / "data" / "seed"


class TsParseError(ValueError):
    pass


# 리터럴이 아닌 값(함수 참조·화살표 함수)을 {"$expr": 원문} 으로 보존해도 되는 부모 키.
# 원본에서는 uploadSchemas.ts 의 validationRules 에서만 쓰인다. 그 밖의 위치에서 리터럴이
# 아닌 값을 만나면 조용히 보존하지 않고 예외를 낸다(무증상 오변환 방지).
EXPR_ALLOWED_PARENTS = frozenset({"validationRules"})

_NUM = re.compile(
    r"0[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*"
    r"|0[bB][01](?:_?[01])*"
    r"|0[oO][0-7](?:_?[0-7])*"
    r"|(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|\.\d(?:_?\d)*)(?:[eE][+-]?\d(?:_?\d)*)?"
)
_IDENT_CHAR = re.compile(r"[\w$]")
_LINE_TERMINATORS = ("\r\n", "\n", "\r", " ", " ")


class TsLiteralParser:
    def __init__(self, text: str, pos: int = 0):
        self.s = text
        self.i = pos
        self.depth = 0  # 객체/배열 중첩 깊이 (0 = export 문 최상위)

    def error(self, msg):
        ctx = self.s[max(0, self.i - 40): self.i + 40].replace("\n", "⏎")
        return TsParseError(f"{msg} @ {self.i}: …{ctx}…")

    def at_end(self):
        return self.i >= len(self.s)

    # ── 공백·주석 ──
    def ws(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c.isspace() or c in "  ﻿":
                self.i += 1
            elif s.startswith("//", self.i):
                m = re.compile(r"[\r\n  ]").search(s, self.i)
                self.i = len(s) if m is None else m.end()
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                if end < 0:
                    raise self.error("닫히지 않은 주석")
                self.i = end + 2
            else:
                break

    def peek(self):
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch):
        if self.peek() != ch:
            raise self.error(f"{ch!r} 필요, {self.peek() or 'EOF'!r} 발견")
        self.i += 1

    # ── 값 ──
    def value(self, allow_expr=False):
        c = self.peek()
        if c == "":
            raise self.error("값이 필요하지만 입력이 끝남")
        if c == "{":
            return self.obj(allow_expr_children=False)
        if c == "[":
            return self.arr()
        if c in "'\"`":
            return self.string()
        start = self.i
        num = self.number()
        if num is not None:
            return num
        self.i = start
        for kw, val in (("true", True), ("false", False), ("null", None), ("undefined", None)):
            if self.s.startswith(kw, self.i):
                j = self.i + len(kw)
                if not _IDENT_CHAR.match(self.s[j:j + 1]):
                    self.i = j
                    if self._value_end_ok():
                        return val
                    self.i = start
        if allow_expr:
            expr = self.raw_expr()
            if not expr:
                raise self.error("빈 값")
            return {"$expr": expr}
        raise self.error("리터럴이 아닌 값(허용 목록 밖)")

    def number(self):
        """JS 숫자 리터럴: 부호(+/-, 공백 허용), 1_000, 0x1F, 0b, 0o, .5, 1e3. 아니면 None."""
        start = self.i
        sign = 1
        if self.s[self.i:self.i + 1] in ("+", "-"):
            sign = -1 if self.s[self.i] == "-" else 1
            self.i += 1
            self.ws()
        m = _NUM.match(self.s, self.i)
        if not m or m.end() == m.start():
            self.i = start
            return None
        tail = self.s[m.end():m.end() + 1]
        if _IDENT_CHAR.match(tail) or tail == ".":
            raise self.error(f"숫자 리터럴 형식 오류 {self.s[m.start():m.end() + 1]!r}")
        raw = m.group(0).replace("_", "")
        self.i = m.end()
        low = raw.lower()
        if low.startswith("0x"):
            val = int(raw[2:], 16)
        elif low.startswith("0b"):
            val = int(raw[2:], 2)
        elif low.startswith("0o"):
            val = int(raw[2:], 8)
        elif any(ch in raw for ch in ".eE"):
            val = float(raw)
        else:
            val = int(raw)
        # 뒤에 연산자가 이어지면 리터럴이 아니다(예: 1 + 2)
        if not self._value_end_ok():
            raise self.error("숫자 뒤에 예상치 못한 토큰")
        return sign * val

    def _value_end_ok(self) -> bool:
        """현재 위치(값 직후)가 값의 끝인지. 위치는 바꾸지 않는다.

        중첩 안: , } ] 또는 입력 끝.  최상위(export 문): 추가로 ';' 또는 줄바꿈 뒤의 다음 문장(ASI).
        같은 줄에서 다른 토큰이 이어지면(1 + 2, true as const) 값의 끝이 아니다.
        """
        save = self.i
        c = self.peek()
        gap = self.s[save:self.i]
        self.i = save
        if c in (",", "}", "]", ""):
            return True
        if self.depth == 0:
            if c == ";":
                return True
            if re.search(r"[\r\n\u2028\u2029]", gap) and not re.match(r"(as|satisfies)\b", self.s[save + len(gap):]):
                return True
        return False

    def _regex_allowed(self, start):
        j = self.i - 1
        while j >= start and self.s[j].isspace():
            j -= 1
        return j < start or self.s[j] in "(,=:[!&|?{};+-*%<>~^"

    def raw_expr(self):
        """다음 최상위 , } ] 까지의 원문(문자열·주석·정규식 리터럴·괄호 균형 고려)."""
        s, start, depth = self.s, self.i, 0
        while self.i < len(s):
            c = s[self.i]
            if c in "'\"`":
                self.string()
                continue
            if s.startswith("//", self.i) or s.startswith("/*", self.i):
                self.ws()
                continue
            if c == "/" and self._regex_allowed(start):
                self.regex_literal()
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            self.i += 1
        if depth != 0:
            raise self.error("괄호 균형이 맞지 않음")
        return s[start:self.i].strip()

    def regex_literal(self):
        self.i += 1
        in_class = False
        while True:
            if self.at_end() or self.s[self.i] in "\r\n":
                raise self.error("닫히지 않은 정규식 리터럴")
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                self.i += 1
                while self.i < len(self.s) and self.s[self.i].isalpha():
                    self.i += 1
                return
            self.i += 1

    def string(self):
        q = self.s[self.i]
        self.i += 1
        out = []
        esc = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
        s = self.s
        while True:
            if self.at_end():
                raise self.error("닫히지 않은 문자열")
            c = s[self.i]
            if c == "\\":
                if self.i + 1 >= len(s):
                    raise self.error("문자열 끝의 역슬래시")
                # 줄 이음: \ + (CRLF | LF | CR | LS | PS)
                cont = next((t for t in _LINE_TERMINATORS if s.startswith(t, self.i + 1)), None)
                if cont:
                    self.i += 1 + len(cont)
                    continue
                n = s[self.i + 1]
                try:
                    if n == "u":
                        if s[self.i + 2:self.i + 3] == "{":
                            end = s.index("}", self.i)
                            out.append(chr(int(s[self.i + 3:end], 16)))
                            self.i = end + 1
                        else:
                            out.append(chr(int(s[self.i + 2:self.i + 6], 16)))
                            self.i += 6
                        continue
                    if n == "x":
                        out.append(chr(int(s[self.i + 2:self.i + 4], 16)))
                        self.i += 4
                        continue
                except ValueError as e:
                    raise self.error(f"잘못된 이스케이프: {e}") from None
                out.append(esc.get(n, n))
                self.i += 2
                continue
            if c == q:
                self.i += 1
                break
            if q != "`" and c in "\r\n":
                raise self.error("따옴표 문자열 안의 줄바꿈")
            if q == "`" and s.startswith("${", self.i):
                raise self.error("템플릿 문자열 보간은 지원하지 않음")
            if q == "`" and s.startswith("\r\n", self.i):  # 템플릿 문자열 줄바꿈은 LF 로 정규화
                out.append("\n")
                self.i += 2
                continue
            out.append(c)
            self.i += 1
        return "".join(out)

    def key(self):
        c = self.peek()
        if c in "'\"":
            return self.string()
        m = re.compile(r"[A-Za-z_$][\w$]*|\d+").match(self.s, self.i)
        if not m:
            raise self.error("키 파싱 실패")
        self.i = m.end()
        return m.group(0)

    def _sep(self, closer):
        c = self.peek()
        if c == ",":
            self.i += 1
        elif c != closer:
            raise self.error(f"',' 또는 {closer!r} 필요")

    def obj(self, allow_expr_children=False):
        self.expect("{")
        self.depth += 1
        out = {}
        while True:
            if self.peek() == "}":
                self.i += 1
                self.depth -= 1
                return out
            if self.s.startswith("...", self.i):
                raise self.error("spread 는 지원하지 않음")
            k = self.key()
            if k in out:
                raise self.error(f"중복 키 {k!r}")
            if self.peek() in (",", "}"):  # 단축 프로퍼티 {a}
                if not allow_expr_children:
                    raise self.error(f"단축 프로퍼티 {k!r} (허용 목록 밖)")
                out[k] = {"$expr": k}
            else:
                self.expect(":")
                if k in EXPR_ALLOWED_PARENTS and self.peek() == "{":
                    out[k] = self.obj(allow_expr_children=True)
                else:
                    out[k] = self.value(allow_expr=allow_expr_children)
            self._sep("}")

    def arr(self):
        self.expect("[")
        self.depth += 1
        out = []
        while True:
            if self.peek() == "]":
                self.i += 1
                self.depth -= 1
                return out
            if self.peek() == ",":
                raise self.error("배열의 빈 요소(hole)는 지원하지 않음")
            out.append(self.value())
            self._sep("]")


def _skip_type_annotation(text: str, i: int) -> int:
    """`: Type` 다음의 초기화 `=` 위치를 찾는다. `=>`(함수 타입)·괄호·제네릭 깊이를 고려."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if text.startswith("=>", i):
            i += 2
            continue
        if text.startswith("//", i) or text.startswith("/*", i):
            p = TsLiteralParser(text, i)
            p.ws()
            i = p.i
            continue
        if c in "'\"`":
            p = TsLiteralParser(text, i)
            p.string()
            i = p.i
            continue
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
            if depth < 0:
                raise TsParseError(f"타입 주석 괄호 불일치 @ {i}")
        elif c == "=" and depth == 0:
            return i
        elif c == ";" and depth == 0:
            break
        i += 1
    raise TsParseError(f"초기화식(=)을 찾지 못함 @ {i}")


def extract_exports(text: str) -> dict:
    """`export const NAME(: Type)? = <literal>` 를 모두 찾아 {NAME: value}."""
    result = {}
    for m in re.finditer(r"export\s+const\s+([A-Za-z_$][\w$]*)\s*", text):
        i = m.end()
        if text[i:i + 1] == ":":
            i = _skip_type_annotation(text, i + 1)
        if text[i:i + 1] != "=":
            raise TsParseError(f"{m.group(1)}: '=' 필요 @ {i}")
        p = TsLiteralParser(text, i + 1)
        result[m.group(1)] = p.value()
        # 리터럴 뒤에는 ';', 입력 끝, 또는 줄바꿈 뒤의 다음 문장만 올 수 있다
        # (`as const`/`satisfies`·연산자 꼬리는 거부)
        end = p.i
        nxt = p.peek()
        gap = text[end:p.i]
        if re.match(r"(as|satisfies)\b", text[p.i:]):
            raise p.error(f"{m.group(1)}: 'as'/'satisfies' 는 지원하지 않음")
        if nxt not in ("", ";") and not (re.search(r"[\r\n\u2028\u2029]", gap) and _IDENT_CHAR.match(nxt)):
            raise p.error(f"{m.group(1)}: 리터럴 뒤 예상치 못한 토큰")
    return result


def write_json(name: str, data) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def count_ts_pattern(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="원본 저장소 루트")
    args = ap.parse_args(argv)
    data_dir = Path(args.src) / "src" / "data"
    if not data_dir.is_dir():
        sys.exit(f"원본 데이터 디렉터리를 찾을 수 없습니다: {data_dir}")

    def read(name):
        return (data_dir / name).read_text(encoding="utf-8")

    report = []
    errors = []

    def check(label, got, expected):
        ok = got == expected
        report.append(f"  {label}: {got} (원본 {expected}) {'OK' if ok else 'MISMATCH'}")
        if not ok:
            errors.append(label)

    # patients.ts
    text = read("patients.ts")
    patients = extract_exports(text)["patients"]
    write_json("patients.json", patients)
    check("patients", len(patients), count_ts_pattern(text, r"conditionId:"))

    # genomics.ts
    text = read("genomics.ts")
    g = extract_exports(text)
    write_json("genomics.json", g)
    check("genomics.resistanceGenes", len(g["resistanceGenes"]), count_ts_pattern(text, r"\{\s*gene:"))
    check("genomics.variants", len(g["variants"]), count_ts_pattern(text, r"\{\s*id:\s*\d+,\s*gene:"))

    # cohorts.ts
    text = read("cohorts.ts")
    c = extract_exports(text)["cohorts"]
    write_json("cohorts.json", c)
    check("cohorts", len(c), count_ts_pattern(text, r"\n\s*id:\s*\d+,"))

    # statistics.ts
    text = read("statistics.ts")
    st = extract_exports(text)["statistics"]
    write_json("statistics.json", st)
    check("statistics.monthlyTrends", len(st["monthlyTrends"]), count_ts_pattern(text, r"\{\s*month:"))
    check("statistics.countryDistribution", len(st["countryDistribution"]), count_ts_pattern(text, r"\{\s*country:"))

    # rapImaging.json (이미 JSON — 정규화 복사)
    rap = json.loads(read("rapImaging.json"))
    write_json("rapImaging.json", rap)
    for k, v in rap.items():
        if isinstance(v, list):
            report.append(f"  rapImaging.{k}: {len(v)}")

    # sampleSchemas.ts
    text = read("sampleSchemas.ts")
    ss = extract_exports(text)
    write_json("sampleSchemas.json", ss)
    check("sampleSchemas.SAMPLE_SCHEMAS", len(ss["SAMPLE_SCHEMAS"]), count_ts_pattern(text, r"schemaId:\s*'schema-\d+',\s*\n\s*name:"))
    check("sampleSchemas.SAMPLE_DYNAMIC_DATA", len(ss["SAMPLE_DYNAMIC_DATA"]), count_ts_pattern(text, r"rowId:"))
    check("sampleSchemas.fields", sum(len(s["fields"]) for s in ss["SAMPLE_SCHEMAS"]), count_ts_pattern(text, r"\{\s*fieldId:"))

    # uploadSchemas.ts (검증 함수는 $expr 로 원문 보존)
    text = read("uploadSchemas.ts")
    us = extract_exports(text)
    write_json("uploadSchemas.json", us)
    check("uploadSchemas.templates", len(us), count_ts_pattern(text, r"export const \w+CsvTemplate"))

    print("변환 결과 →", OUT_DIR)
    print("\n".join(report))
    if errors:
        sys.exit(f"건수 불일치: {', '.join(errors)}")


if __name__ == "__main__":
    main()
