"""커스텀 데이터 구조 화면 상수 — 원본 types/schema.ts(SCHEMA_CATEGORIES, DATA_TYPE_LIST), SchemaCard·SchemaManager 라벨."""
from __future__ import annotations

from apps.dictionary.viewer import DATA_TYPES

# 원본 SCHEMA_CATEGORIES — (id, ko, en, 배지 톤)
CATEGORIES = [
    ("clinical_ext", "임상 확장", "Clinical Extension", "navy"),
    ("genomic_ext", "유전체 확장", "Genomic Extension", "purple"),
    ("imaging_ext", "영상 확장", "Imaging Extension", "rose"),
    ("survey", "설문조사", "Survey", "amber"),
    ("lab_ext", "검사실 확장", "Lab Extension", "emerald"),
    ("other", "기타", "Other", "gray"),
]
CATEGORY = {c[0]: {"id": c[0], "ko": c[1], "en": c[2], "tone": c[3]} for c in CATEGORIES}

STATUS = {
    "published": {"ko": "발행됨", "en": "Published", "variant": "success"},
    "draft": {"ko": "초안", "en": "Draft", "variant": "warning"},
    "archived": {"ko": "보관됨", "en": "Archived", "variant": "neutral"},
}
# 관리자 화면(SchemaManager)은 초안=neutral, 보관=warning 으로 칠한다
ADMIN_STATUS_VARIANT = {"draft": "neutral", "published": "success", "archived": "warning"}

# 카드·상세(SchemaCard) 라벨과 빌더(SchemaBuilder) 라벨, 관리자(SchemaManager) 라벨이 원본에서 조금씩 다르다
VISIBILITY = {
    "private": {"ko": "비공개", "en": "Private", "card_ko": "비공개", "card_en": "Private",
                "admin_ko": "비공개", "admin_en": "Private"},
    "shared": {"ko": "기관내 공유", "en": "Shared within Institution", "card_ko": "기관내공유", "card_en": "Institution Only",
               "admin_ko": "기관 공유", "admin_en": "Shared"},
    "public": {"ko": "전체 공개", "en": "Public", "card_ko": "공개", "card_en": "Public",
               "admin_ko": "전체 공개", "admin_en": "Public"},
}

# 빌더의 연결 키 필드 선택지
LINK_FIELDS = [
    ("condition_id", "condition_id (환자 케이스)", "condition_id (patient case)", "케이스 ID", "Case ID",
     "환자 케이스 연결 키", "Key linking to the patient case"),
    ("specimen_id", "specimen_id (검체)", "specimen_id (specimen)", "검체 ID", "Specimen ID",
     "검체 연결 키", "Key linking to the specimen"),
    ("series_id", "series_id (영상 시리즈)", "series_id (image series)", "영상 시리즈 ID", "Image Series ID",
     "영상 시리즈 연결 키", "Key linking to the image series"),
    ("custom", "custom (직접 정의)", "custom (user-defined)", "연결 키", "Link Key",
     "직접 정의한 연결 키", "User-defined link key"),
]
LINK_FIELD = {x[0]: x for x in LINK_FIELDS}

# 원본 FieldConstraintEditor 의 코드 체계 선택지
CODE_SYSTEMS = ["ICD-10", "SNOMED-CT", "LOINC", "ATC", "CTCAE v5.0", "MedDRA"]

# 원본 DATA_TYPE_LIST 설명·has* 플래그 (라벨·아이콘·색은 dictionary.viewer.DATA_TYPES 와 공유)
TYPE_DESC = {
    "TEXT": ("단일 행 문자열", "Single-line string"),
    "TEXTAREA": ("여러 줄 문자열", "Multi-line string"),
    "INTEGER": ("정수형 숫자", "Whole number"),
    "DECIMAL": ("소수점 숫자", "Decimal number"),
    "BOOLEAN": ("true/false 값", "true/false value"),
    "DATE": ("YYYY-MM-DD 형식", "YYYY-MM-DD format"),
    "DATETIME": ("YYYY-MM-DD HH:mm 형식", "YYYY-MM-DD HH:mm format"),
    "SELECT": ("드롭다운 선택", "Dropdown selection"),
    "MULTISELECT": ("체크박스 그룹", "Checkbox group"),
    "NUMERIC_RANGE": ("min~max 범위 입력", "min–max range input"),
    "FILE": ("파일 업로드", "File upload"),
    "REFERENCE": ("표준 데이터사전 참조", "References the standard data dictionary"),
    "CALCULATED": ("자동 계산 값", "Automatically calculated value"),
    "CODED": ("표준 코드체계 연동", "Linked to a standard code system"),
}
RANGE_TYPES = {"INTEGER", "DECIMAL", "DATE", "DATETIME", "NUMERIC_RANGE"}  # 원본 hasRange

# 타입별로 허용하는 제약조건 키 (원본 FieldConstraintEditor 가 타입마다 보여 주는 입력란)
CONSTRAINT_KEYS = {
    "TEXT": {"minLength", "maxLength", "pattern", "patternMessage", "patternMessageEn"},
    "TEXTAREA": {"minLength", "maxLength", "pattern", "patternMessage", "patternMessageEn"},
    "INTEGER": {"min", "max", "unit"},
    "DECIMAL": {"min", "max", "precision", "unit"},
    "NUMERIC_RANGE": {"min", "max", "precision", "unit"},
    "BOOLEAN": set(),
    "DATE": {"minDate", "maxDate"},
    "DATETIME": {"minDate", "maxDate"},
    "SELECT": {"options"},
    "MULTISELECT": {"options", "maxSelections"},
    "FILE": {"allowedFileTypes", "maxFileSizeMB"},
    "REFERENCE": {"refDictionary", "refField"},
    "CALCULATED": {"formula", "precision", "unit"},
    "CODED": {"codeSystem", "codeSystemEn"},
}

TYPE_ORDER = list(DATA_TYPES)


def pick(lang: str, ko: str, en: str | None) -> str:
    """원본 schemaLabels.pick — 영어 화면이면 영어(없으면 한국어)."""
    return (en or ko) if lang == "en" else ko


def type_label(t: str, lang: str) -> str:
    if not isinstance(t, str):  # 가져온 JSON 의 값이 리스트·객체일 수 있다(해시 불가)
        return ""
    info = DATA_TYPES.get(t)
    if not info:
        return t
    return info["label_en"] if lang == "en" else info["label"]


def type_palette(lang: str) -> list[dict]:
    out = []
    for t, info in DATA_TYPES.items():
        ko, en = TYPE_DESC[t]
        out.append({"type": t, "label": type_label(t, lang), "description": en if lang == "en" else ko,
                    "icon": info["icon"], "color": info["color"]})
    return out


def category_label(cid: str, lang: str) -> str:
    if not isinstance(cid, str):
        return ""
    c = CATEGORY.get(cid)
    return (c["en"] if lang == "en" else c["ko"]) if c else cid


def status_info(status: str, lang: str, admin: bool = False) -> dict:
    s = STATUS.get(status, {"ko": status, "en": status, "variant": "neutral"})
    return {"key": status, "label": s["en"] if lang == "en" else s["ko"],
            "variant": ADMIN_STATUS_VARIANT.get(status, "neutral") if admin else s["variant"]}


def visibility_label(v: str, lang: str, style: str = "") -> str:
    if not isinstance(v, str):
        return ""
    d = VISIBILITY.get(v)
    if not d:
        return v
    prefix = f"{style}_" if style else ""
    return d[f"{prefix}en"] if lang == "en" else d[f"{prefix}ko"]


def option_label(opt: dict, lang: str) -> str:
    label = opt.get("label") or opt.get("value") or ""
    return (opt.get("labelEn") or label) if lang == "en" else label


def name_has_unit(name: str, unit: str) -> bool:
    """원본 nameHasUnit — 표시명에 단위가 낱말로 들어 있으면 단위를 다시 붙이지 않는다."""
    import re

    return bool(re.search(r"(^|[\s(\[])" + re.escape(unit) + r"($|[\s)\]])", name or ""))
