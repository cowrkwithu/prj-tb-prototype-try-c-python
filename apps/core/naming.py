"""필드명 규칙 — 커스텀 데이터 구조(빌더)와 관리자 데이터사전이 공유하는 한 곳의 정의.

- 영문으로 시작, 영문·숫자·밑줄만, 최대 64자
- 연속 밑줄(__)·끝 밑줄 금지 — ORM 조회식 구분자(`__`)와 혼동되지 않게(데이터사전 항목을 빌더로 가져와도 저장 가능하게)
- legacy: 이 규칙이 생기기 전에 이미 저장된 이름 — 이름을 바꾸지 않는 한 길이·밑줄 규칙의 예외
"""
from __future__ import annotations

import re

FIELD_NAME_MAX = 64
_FIELD_NAME_CHARS = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def field_name_problem(name, *, legacy=frozenset()) -> dict | None:
    """문제 없으면 None, 있으면 {"code", "ko", "en"} (code: "format" | "underscore")."""
    if not isinstance(name, str) or not _FIELD_NAME_CHARS.match(name) or (
            len(name) > FIELD_NAME_MAX and name not in legacy):
        return {"code": "format",
                "ko": f"필드명은 영문으로 시작하고 영문·숫자·밑줄만 쓸 수 있습니다(최대 {FIELD_NAME_MAX}자)",
                "en": f"field name must start with a letter and use letters, digits and underscores (max {FIELD_NAME_MAX})"}
    if name in legacy:
        return None
    if "__" in name or name.endswith("_"):
        return {"code": "underscore",
                "ko": "필드명에는 연속 밑줄(__)이나 끝 밑줄을 쓸 수 없습니다",
                "en": "field names cannot contain double underscores (__) or end with an underscore"}
    return None
