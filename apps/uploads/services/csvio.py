"""CSV 입출력 — 원본 utils/csvParser.ts(PapaParse header:true, skipEmptyLines, trim) + uploadSchemas 템플릿 생성.

인코딩 자동 판별: UTF-8(BOM 포함) → 실패하면 CP949(Windows 엑셀 '쉼표로 분리' 저장 기본값).
"""
from __future__ import annotations

import csv
import io
import re

from django.conf import settings

ENCODINGS = ("utf-8-sig", "cp949")


class CsvError(ValueError):
    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en


def decode_bytes(data: bytes) -> tuple[str, str]:
    """(텍스트, 판별된 인코딩). utf-8-sig 는 BOM 이 없으면 일반 UTF-8 과 같다."""
    if b"\x00" in data[:8192]:
        raise CsvError("텍스트 CSV 파일이 아닙니다(이진 데이터 포함)", "Not a text CSV file (contains binary data)")
    for enc in ENCODINGS:
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        label = "utf-8-sig" if enc == "utf-8-sig" and data.startswith(b"\xef\xbb\xbf") else ("utf-8" if enc == "utf-8-sig" else enc)
        return text, label
    raise CsvError("문자 인코딩을 알 수 없습니다(UTF-8 또는 CP949 로 저장하세요)",
                   "Unknown character encoding (save as UTF-8 or CP949)")


def parse(data: bytes, delimiter: str = ",") -> dict:
    """헤더·행(dict, 값 trim)·인코딩. 모든 칸이 빈 행은 건너뛴다(skipEmptyLines)."""
    text, encoding = decode_bytes(data)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise CsvError("빈 파일입니다", "The file is empty") from None
    except csv.Error as exc:
        raise CsvError(f"CSV 파싱 오류: {exc}", f"CSV parsing error: {exc}") from None
    headers = [h.strip() for h in header]
    if not any(headers):
        raise CsvError("헤더 행이 비어 있습니다", "The header row is empty")
    seen, dups = set(), []
    for h in headers:
        if h and h in seen and h not in dups:
            dups.append(h)
        seen.add(h)
    if dups:  # 같은 이름의 컬럼이 둘이면 어느 값을 쓸지 알 수 없다 — 조용히 덮어쓰지 않고 오류로 보고
        cols = ", ".join(dups)
        raise CsvError(f"중복된 컬럼 이름이 있습니다: {cols}", f"Duplicate column names: {cols}")
    rows = []
    max_rows = settings.UPLOAD_CSV_MAX_ROWS
    try:
        for raw in reader:
            if not any(c.strip() for c in raw):
                continue
            if len(rows) >= max_rows:
                raise CsvError(f"행이 너무 많습니다(최대 {max_rows:,}행)", f"Too many rows (max {max_rows:,})")
            rows.append({h: (raw[i].strip() if i < len(raw) else "") for i, h in enumerate(headers) if h})
    except csv.Error as exc:
        raise CsvError(f"CSV 파싱 오류: {exc}", f"CSV parsing error: {exc}") from None
    return {"headers": [h for h in headers if h], "rows": rows, "encoding": encoding}


def _csv_line(values) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


def template_csv(domain: str) -> str:
    """원본 generateTemplateCsv — 필수+선택 컬럼 헤더와 샘플 행. BOM 을 붙여 엑셀이 UTF-8 로 연다."""
    from .schemas import template

    t = template(domain)
    cols = t["required"] + t["optional"]
    return "\ufeff" + _csv_line(cols) + "".join(_csv_line([safe_cell(r.get(c, "")) for c in cols]) for r in t["samples"])


_PURE_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
# 탭·LF·CR 은 CSV 셀 안에서 의미가 있는 문자라 남기고(탭·CR 로 시작하면 아래 접두 처리), 나머지 C0 제어문자와 DEL 은 지운다
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_cell(v) -> str:
    """스프레드시트 수식 주입 방지: C0 제어문자(탭·개행 제외)를 **먼저** 지운 뒤(그래야 '\x01=1+1' 이 '=1+1' 로
    새지 않는다), 순수 숫자(-12, 3.5)만 그대로, 그 밖에 = + - @ 탭 CR 로 시작하면 ' 를 붙인다."""
    s = "" if v is None else _CONTROL.sub("", str(v))
    if s and not _PURE_NUMBER.match(s) and s[0] in "=+-@\t\r":
        return "'" + s
    return s


_cell = safe_cell


def report_csv(issues: list[dict], lang: str) -> str:
    """원본 ValidationReport.downloadReport — 행,필드,입력값,메시지,심각도."""
    head = ["Row", "Field", "Value", "Message", "Severity"] if lang == "en" else ["행", "필드", "입력값", "메시지", "심각도"]
    out = "\ufeff" + _csv_line(head)
    for e in issues:
        out += _csv_line([e.get("row") or "", safe_cell(e["field"]), safe_cell(e.get("value", "")),
                          safe_cell(e["en"] if lang == "en" else e["ko"]), e["severity"]])
    return out
