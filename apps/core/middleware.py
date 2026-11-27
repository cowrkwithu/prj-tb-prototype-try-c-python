"""요청 입력 방어 미들웨어 (프로젝트 공통)."""
import re

from django.http import HttpResponseBadRequest

SURROGATE = re.compile("[\ud800-\udfff]")


def _bad_querydict(qd) -> bool:
    for key, values in qd.lists():
        if SURROGATE.search(key) or any(isinstance(v, str) and SURROGATE.search(v) for v in values):
            return True
    return False


class RejectSurrogatesMiddleware:
    """GET/POST 키·값(파일 제외)에 고립 서로게이트(U+D800–DFFF)가 있으면 뷰·렌더 전에 400.

    서로게이트는 UTF-8 로 인코딩할 수 없어 DB 바인딩·응답 렌더에서 500 이 된다.
    process_view 에서 검사하므로 CsrfViewMiddleware 뒤에 두면 본문 파싱 시점이 바뀌지 않는다
    (CSRF 검사가 이미 request.POST 를 읽는다). 본문을 읽을 수 없는 요청은 그대로 뷰에 넘긴다.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        try:
            bad = _bad_querydict(request.GET) or (
                request.method == "POST" and _bad_querydict(request.POST))
        except Exception:  # 본문 파싱 오류(크기 초과 등)는 기존처럼 뷰·Django 가 처리
            return None
        if bad:
            return HttpResponseBadRequest("Invalid characters in request.", content_type="text/plain; charset=utf-8")
        return None
