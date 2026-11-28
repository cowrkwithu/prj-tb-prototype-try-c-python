"""관리자·사용자 자유 텍스트 입력(검토 의견·반려/수정 사유·서명 등)의 공통 정리·검사.

- clean_free_text: 제로폭 문자(U+200B~U+200D, U+2060, BOM, soft hyphen)를 지우고 앞뒤 공백을 다듬는다.
- free_text_problem: NUL·고립 서로게이트가 있거나 길이 상한을 넘으면 (한국어, 영어) 안내 문구, 문제 없으면 None.
  NUL 은 PostgreSQL text 에 저장할 수 없고(SQLite 는 저장되지만 출력·내보내기에서 문제) Django 폼 CharField 도 거부한다 —
  두 백엔드에서 같은 결과가 되도록 서비스 계층에서 거부한다. 호출자가 자기 오류 형식(DarError·WorkflowError 등)으로 올린다.
"""
from .safejson import has_bad_chars

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)


def clean_free_text(v) -> str:
    return v.translate(_INVISIBLE).strip() if isinstance(v, str) else ""


def free_text_problem(text: str, max_len: int):
    if has_bad_chars(text):
        return ("허용되지 않는 문자(NUL 등)가 포함되어 있습니다.", "The text contains characters that are not allowed (NUL, etc.).")
    if len(text) > max_len:
        return (f"최대 {max_len}자까지 입력할 수 있습니다", f"At most {max_len} characters allowed")
    return None
