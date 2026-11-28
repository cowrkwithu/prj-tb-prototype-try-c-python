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


# ─── DB 잠금(쓰기 경합) → 503 (5단계) ───
_BUSY_MARKERS = (
    "database is locked", "database table is locked", "database schema is locked", "database is busy",  # SQLite
    "lock timeout", "could not obtain lock", "deadlock detected", "could not serialize access",  # PostgreSQL
)
# 55P03 lock_not_available(lock_timeout·NOWAIT), 40P01 deadlock_detected, 40001 serialization_failure
_PG_BUSY_SQLSTATES = frozenset({"55P03", "40P01", "40001"})
RETRY_AFTER_SECONDS = 5


def is_db_busy(exc) -> bool:
    """DB 잠금 대기 초과·교착·직렬화 실패인가(OperationalError 중 잠금 계열만 — 다른 DB 오류는 결함으로 드러나야 한다)."""
    from django.db import OperationalError

    if not isinstance(exc, OperationalError):
        return False
    # PostgreSQL: 서버 메시지 언어(lc_messages)와 무관하게 SQLSTATE 로 판별한다(psycopg 3: sqlstate, psycopg2: pgcode).
    cause = exc.__cause__
    code = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    if code in _PG_BUSY_SQLSTATES:
        return True
    text = str(exc).lower()
    return any(m in text for m in _BUSY_MARKERS)


def _wants_json(request) -> bool:
    accept = request.headers.get("Accept", "")
    return (request.path.startswith("/api/") or "application/json" in accept
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or (request.content_type or "").startswith("application/json"))


class DatabaseBusyMiddleware:
    """뷰에서 처리하지 못한 DB 잠금 예외(SQLite busy_timeout 초과 등)를 500 대신 503 '잠시 후 다시 시도' 로 바꾼다.

    - 일반 요청: 안내 화면(core/db_busy.html), HTMX 요청: 토스트(HX-Trigger)만 띄우고 화면은 바꾸지 않음(HX-Reswap: none),
      JSON(API·fetch·XHR 업로드) 요청: {"error", "message", "status": "db_busy"}.
    - 모든 응답에 Retry-After. 트랜잭션은 예외로 이미 롤백됐다(atomic 블록 밖으로 전파된 예외만 여기 온다).
    MIDDLEWARE 의 맨 끝에 둔다(process_exception 은 역순으로 불리므로 가장 먼저 처리)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not is_db_busy(exception):
            return None
        import json
        import logging

        from django.http import HttpResponse, JsonResponse

        from .i18n import t

        logging.getLogger("apps.core.db_busy").warning("DB busy on %s %s: %s", request.method, request.path, exception)
        msg = t(request, "다른 작업이 데이터베이스를 쓰는 중이라 처리하지 못했습니다. 잠시 후 다시 시도해주세요. "
                         "(저장되지 않았습니다)",
                "The database is busy with another operation, so this request was not processed. "
                "Please try again shortly. (Nothing was saved)")
        if _wants_json(request):
            resp = JsonResponse({"error": msg, "message": msg, "status": "db_busy"}, status=503,
                                json_dumps_params={"ensure_ascii": False})
            resp["Cache-Control"] = "no-store"
        elif request.headers.get("HX-Request") == "true":
            resp = HttpResponse(msg, status=503, content_type="text/plain; charset=utf-8")
            resp["HX-Trigger"] = json.dumps({"showToast": {"type": "error", "message": msg}})  # ASCII(\\u) — 헤더 안전
            resp["HX-Reswap"] = "none"
        else:
            try:
                from django.shortcuts import render

                resp = render(request, "core/db_busy.html", {"message": msg}, status=503)
            except Exception:  # 안내 화면 렌더도 DB 를 못 쓰면 텍스트로
                resp = HttpResponse(msg, status=503, content_type="text/plain; charset=utf-8")
        resp["Retry-After"] = str(RETRY_AFTER_SECONDS)
        return resp
