"""공통 입력 방어: safejson.loads_user_json, RejectSurrogatesMiddleware."""
import json

import pytest
from django.http import HttpResponse, QueryDict
from django.test import RequestFactory

from apps.core.middleware import RejectSurrogatesMiddleware
from apps.core.safejson import has_bad_chars, loads_user_json


@pytest.mark.parametrize("text", [
    '"a\\udfff"', '["ok", "\\ud800"]', '{"k\\udc00": 1}', '{"a": {"b": ["\\u0000"]}}', '{"\\u0000": 1}',
])
def test_loads_user_json_rejects_surrogates_and_nul(text):
    json.loads(text)  # 표준 json 은 통과시킨다
    with pytest.raises(ValueError):
        loads_user_json(text)


def test_loads_user_json_ok_and_deep():
    assert loads_user_json('{"a": ["당뇨", 1, null, {"b": "😀"}]}') == {"a": ["당뇨", 1, None, {"b": "😀"}]}
    assert loads_user_json('"\\ud83d\\ude00"') == "😀"  # 짝이 맞는 서로게이트 쌍은 정상 문자
    deep = "[" * 900 + "]" * 900
    loads_user_json(deep)  # 검사는 반복(스택) — 재귀 한도와 무관
    assert has_bad_chars("a\udfff") and has_bad_chars("\x00") and not has_bad_chars("가")


def _mw():
    return RejectSurrogatesMiddleware(lambda r: HttpResponse("ok"))


def _with(request, attr, pairs):
    qd = QueryDict(mutable=True)
    for k, v in pairs:
        qd.appendlist(k, v)
    setattr(request, attr, qd)
    return request


@pytest.mark.parametrize("attr,pairs", [
    ("GET", [("tree", "a\udfff")]), ("GET", [("k\ud800", "v")]), ("GET", [("x", "ok"), ("x", "\udc00")]),
    ("POST", [("name", "a\udfff")]), ("POST", [("q\udfff", "1")]),
])
def test_middleware_rejects_surrogates(attr, pairs):
    rf = RequestFactory()
    req = rf.post("/x") if attr == "POST" else rf.get("/x")
    _with(req, attr, pairs)
    resp = _mw().process_view(req, None, (), {})
    assert resp is not None and resp.status_code == 400


def test_middleware_passes_normal_input():
    rf = RequestFactory()
    req = _with(rf.get("/x"), "GET", [("country", "한국"), ("emoji", "😀"), ("nul", "a\x00b")])
    assert _mw().process_view(req, None, (), {}) is None
    req = rf.post("/x", {"name": "데이터셋"})
    assert _mw().process_view(req, None, (), {}) is None


def test_middleware_installed_after_csrf(settings):
    mw = settings.MIDDLEWARE
    assert mw.index("apps.core.middleware.RejectSurrogatesMiddleware") > mw.index("django.middleware.csrf.CsrfViewMiddleware")
