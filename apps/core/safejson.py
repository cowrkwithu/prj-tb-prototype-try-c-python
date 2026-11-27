"""사용자 입력 JSON 의 안전한 파싱 (프로젝트 공통).

json.loads 는 `"\\udfff"` 같은 이스케이프로 고립 서로게이트(U+D800–DFFF)와 NUL 을 문자열에 넣을 수 있다.
이런 값은 DB 바인딩(UTF-8 인코딩)·템플릿 출력에서 UnicodeEncodeError(500)를 일으키므로 파싱 단계에서 거부한다.
"""
import json
import re

BAD_CHARS = re.compile("[\ud800-\udfff\x00]")


def has_bad_chars(s: str) -> bool:
    return bool(BAD_CHARS.search(s))


def loads_user_json(text: str):
    """json.loads + 결과 전체(객체 키·문자열 값, 재귀)에 서로게이트·NUL 이 있으면 ValueError.

    깊은 중첩은 json.loads 가 RecursionError 를 낼 수 있다 — 호출자가 함께 처리한다. 검사는 반복(스택)으로 한다.
    """
    obj = json.loads(text)
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if has_bad_chars(cur):
                raise ValueError("JSON 문자열에 허용되지 않는 문자(서로게이트·NUL)가 있습니다.")
        elif isinstance(cur, dict):
            for k, v in cur.items():
                if has_bad_chars(k):
                    raise ValueError("JSON 키에 허용되지 않는 문자(서로게이트·NUL)가 있습니다.")
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return obj
