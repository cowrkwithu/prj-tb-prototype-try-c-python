"""개인정보(PII) 자동 탐지 — 원본 src/utils/deidentify.ts 이식.

탐지 전략(원본과 동일, 오탐 억제를 위한 2단계):
 1) 값 패턴 탐지(모든 컬럼, 확신도 high): 주민등록번호·전화번호·이메일
 2) 컬럼 기준 탐지: 성명/주소 계열 컬럼(high) 또는 자유서술 컬럼(medium)에서만 성명·상세주소를 찾는다.
    한국 성씨 접두 조건과 임상 용어 화이트리스트를 적용한다.
탐지값은 원문을 저장·노출하지 않고 mask_value() 결과만 남긴다.

JS 정규식과 같은 의미가 되도록 re.ASCII 를 쓴다(\\b·\\d·\\s 를 ASCII 기준으로). 한글 범위는 명시 범위라 영향 없다.
"""
from __future__ import annotations

import re

PII_TYPES = ["rrn", "phone", "email", "name", "address", "dicom"]
PII_TYPE_LABEL = {
    "rrn": ("주민등록번호/외국인등록번호", "Resident / Alien Registration Number"),
    "phone": ("연락처(전화번호)", "Contact (Phone Number)"),
    "email": ("연락처(이메일)", "Contact (Email)"),
    "name": ("성명", "Name"),
    "address": ("상세주소", "Detailed Address"),
    "dicom": ("DICOM 헤더 식별정보", "DICOM Header Identifiers"),
}

NAME_COLUMN_RE = re.compile(
    r"(^|[_\s-])(name|성명|이름|환자명|보호자|guardian|physician|doctor|의사|담당자|submitter)([_\s-]|$)", re.I | re.A)
ADDRESS_COLUMN_RE = re.compile(r"(addr|address|주소|거주지|소재지)", re.I | re.A)
FREETEXT_COLUMN_RE = re.compile(
    r"(note|notes|remark|memo|comment|desc|description|비고|메모|설명|기타|소견)", re.I | re.A)

CLINICAL_TERMS = set("""
결핵 검사 치료 환자 진단 배양 검체 약물 내성 감수성
양성 음성 객담 도말 흉수 조직 소변 혈액 균주 판정
완치 완료 실패 사망 중단 재발 전원 퇴록 등록 추적
소견 선별 문진 채취 천자 반응 육안 심전도 백혈구 방사선
균음전 재치료 초치료 내원 외래 입원 퇴원 처방 투약 용량
당뇨 간염 빈혈 공동 결절 침윤 섬유화 석회화 무기폐 경화
고혈압 간경변 폐암 흡연 음주 임신 수유 설사 함몰 기침
한국 몽골 남아공 인도 중국 일본 미국 영국 남성 여성
남자 여자 해당 없음 있음 미상 기타 국가 지역 성별
연령 주소 구분 최초 신환자 원인 명칭 표준 예방 위치
우측 좌측 편측 방문 서식 제출 금연 길이 모름 차수
현재 안정 송부 허가 유의 노출 하기도 어린이 기관 단계
""".split())

SURNAMES = set(
    "김 이 박 최 정 강 조 윤 장 임 한 오 서 신 권 황 안 송 류 전 홍 고 문 양 손 배 백 허 "
    "유 남 심 노 하 곽 성 차 주 우 구 나 민 진 지 엄 채 원 천 방 공 현 함 변 염 여 추 도 "
    "소 석 선 설 마 길 연 위 표 명 기 반 왕 금 옥 육 인 맹 제 모 탁 국 어 은 편 용 예 봉".split())

# 구분자: 하이픈·마침표·공백·en/em 대시를 여러 개 섞어 써도 잡는다(원본 [-\s]? 단일 구분자보다 넓힘 — 리뷰 결정)
SEP = r"[-.\s\u2013\u2014\u2212]*"
# 주민번호는 마침표 구분자를 빼고 본다(123456.1234567 같은 소수값 오탐 방지 — 리뷰 결정). 전화번호는 마침표 유지.
RRN_SEP = r"[-\s\u2013\u2014\u2212]*"
RRN_RE = re.compile(rf"(?<!\w)(?<!\d\.)\d{{6}}{RRN_SEP}[1-8]\d{{6}}(?!\d|\.\d)", re.A)
# 휴대전화/유선전화: 0 으로 시작하는 국내 형식 + 국가번호(+82 / 82, 뒤의 0 생략 또는 (0) 표기) 형식
PHONE_RE = re.compile(
    rf"(?<![\w+])(?:\+?82{SEP}(?:\(0\){SEP}|0)?|0)(?:1[016789]|[2-6]\d?){SEP}\d{{3,4}}{SEP}\d{{4}}(?!\d)", re.A)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", re.A)
KOREAN_WORD_RE = re.compile(r"[가-힣]{2,4}")
ADDRESS_DETAIL_RE = re.compile(r"[가-힣]{2,10}(?:로|길|동|가)\s?\d+(?:-\d+)?\b", re.A)

MSG = {
    "rrn": ("주민등록번호/외국인등록번호 형식의 값이 포함되어 있습니다", "Contains a value formatted as a resident/alien registration number"),
    "phone": ("전화번호 형식의 값이 포함되어 있습니다", "Contains a value formatted as a phone number"),
    "email": ("이메일 주소가 포함되어 있습니다", "Contains an email address"),
    "name_high": ("성명 항목에 실명으로 판단되는 값이 남아 있습니다", "The name field still contains what appears to be a real name"),
    "name_medium": ("서술 항목에 성명일 가능성이 있는 값이 포함되어 있습니다", "A free-text field contains a value that may be a name"),
    "address_high": ("주소 항목에 번지 단위 상세주소가 남아 있습니다", "The address field still contains a street-level detailed address"),
    "address_medium": ("서술 항목에 상세주소일 가능성이 있는 값이 포함되어 있습니다", "A free-text field contains a value that may be a detailed address"),
    "dicom": ("DICOM 헤더에 환자 식별정보가 남아 있습니다", "Patient identifiers remain in the DICOM header"),
}


def mask_value(value: str) -> str:
    v = value.strip()
    if len(v) <= 2:
        return (v[:1] or "") + "*"
    if len(v) <= 4:
        return v[0] + "*" * (len(v) - 2) + v[-1]
    return v[:2] + "*" * min(len(v) - 4, 8) + v[-2:]


def _all(text: str, rx) -> list[str]:
    return [m.group(0) for m in rx.finditer(text)]


def find_name_candidates(text: str) -> list[str]:
    return [w for w in _all(text, KOREAN_WORD_RE) if w[0] in SURNAMES and w not in CLINICAL_TERMS]


def find_rrns(text: str) -> list[str]:
    return _all(text, RRN_RE)


def _digits(v: str) -> str:
    return re.sub(r"\D", "", v)


def find_phones(text: str) -> list[str]:
    """전화번호. 주민등록번호와 겹치는 구간은 제외한다."""
    rrns = [_digits(r) for r in find_rrns(text)]
    return [p for p in _all(text, PHONE_RE) if not any(_digits(p) in r for r in rrns)]


def find_emails(text: str) -> list[str]:
    return _all(text, EMAIL_RE)


def find_address_details(text: str) -> list[str]:
    return _all(text, ADDRESS_DETAIL_RE)


def _detection(type_, confidence, field, row, value, msg_key):
    ko, en = MSG[msg_key]
    return {"type": type_, "confidence": confidence, "field": field, "row": row, "masked": mask_value(value),
            "ko": ko, "en": en}


def scan_rows(rows: list[dict], safe_columns: list[str] | None = None) -> dict:
    """rows(문자열 값 dict 목록)를 검사한다. 리스트·dict 값(폼의 하위 표)은 문자열 필드만 펼쳐 검사한다."""
    safe = {c.lower() for c in (safe_columns or [])}
    detections: list[dict] = []
    fields: set[str] = set()
    for idx, row in enumerate(rows):
        row_no = idx + 1
        for field, raw in _flatten(row):
            value = (raw or "").strip()
            if not value:
                continue
            fields.add(field)
            for hit in find_rrns(value):
                detections.append(_detection("rrn", "high", field, row_no, hit, "rrn"))
            for hit in find_phones(value):
                detections.append(_detection("phone", "high", field, row_no, hit, "phone"))
            for hit in find_emails(value):
                detections.append(_detection("email", "high", field, row_no, hit, "email"))
            if field.lower() in safe:
                continue
            is_name = bool(NAME_COLUMN_RE.search(field))
            is_addr = bool(ADDRESS_COLUMN_RE.search(field))
            is_free = bool(FREETEXT_COLUMN_RE.search(field))
            if is_name:
                for hit in find_name_candidates(value):
                    detections.append(_detection("name", "high", field, row_no, hit, "name_high"))
            elif is_free:
                for hit in find_name_candidates(value):
                    detections.append(_detection("name", "medium", field, row_no, hit, "name_medium"))
            if is_addr:
                for hit in find_address_details(value):
                    detections.append(_detection("address", "high", field, row_no, hit, "address_high"))
            elif is_free:
                for hit in find_address_details(value):
                    detections.append(_detection("address", "medium", field, row_no, hit, "address_medium"))
    return summarize(detections, len(rows), len(fields))


def _flatten(row: dict):
    for k, v in row.items():
        if isinstance(v, str):
            yield k, v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            yield k, str(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    for k2, v2 in item.items():
                        if isinstance(v2, str):
                            yield f"{k}.{k2}", v2


def summarize(detections: list[dict], scanned_rows: int, scanned_fields: int) -> dict:
    count = {t: 0 for t in PII_TYPES}
    for d in detections:
        count[d["type"]] += 1
    high = sum(1 for d in detections if d["confidence"] == "high")
    return {"detections": detections, "count_by_type": count, "high": high, "medium": len(detections) - high,
            "scanned_rows": scanned_rows, "scanned_fields": scanned_fields,
            "clean": not detections, "blocking": high > 0}


def merge_dicom(report: dict, dicom_detections: list[dict]) -> dict:
    """영상 업로드: 행 검사 결과에 DICOM 헤더 탐지 결과(확신도 high)를 더한다."""
    return summarize(list(report.get("detections", [])) + dicom_detections,
                     report.get("scanned_rows", 0), report.get("scanned_fields", 0))


def dicom_detection(file_name: str, tag_name: str, value: str) -> dict:
    d = _detection("dicom", "high", f"{file_name} · {tag_name}", 0, value, "dicom")
    return d


def summary_text(report: dict, lang: str) -> str:
    """원본 scanForPII 의 summary 문구."""
    rows, fields = report.get("scanned_rows", 0), report.get("scanned_fields", 0)
    high, medium = report.get("high", 0), report.get("medium", 0)
    en = lang == "en"
    if not report.get("detections"):
        return (f"Scanned {rows} rows · {fields} fields — no personal information found" if en
                else f"{rows}행 · {fields}개 항목을 검사하여 개인정보 0건을 확인했습니다")
    if high == 0:
        return (f"Scanned {rows} rows — {medium} item(s) need review (submission allowed)" if en
                else f"{rows}행 검사 — 확인이 필요한 항목 {medium}건이 있습니다 (제출 가능)")
    return (f"Scanned {rows} rows — {high} personal information item(s) detected. De-identify and resubmit" if en
            else f"{rows}행 검사 — 개인정보 {high}건이 탐지되었습니다. 비식별화 후 다시 제출하세요")
