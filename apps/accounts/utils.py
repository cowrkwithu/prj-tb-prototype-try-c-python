from urllib.parse import quote

from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, candidate, default="/"):
    """같은 호스트의 상대 경로만 허용한다(오픈 리다이렉트 방지)."""
    if not candidate:
        return default
    candidate = str(candidate).strip()
    # 상대 경로만: '/x' 허용, '//evil', '/\\evil', 'http://…' 차단
    if not candidate.startswith("/") or candidate.startswith("//") or candidate.startswith("/\\"):
        return default
    if any(ch in candidate for ch in ("\r", "\n", "\x00")):
        return default
    if not url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        return default
    return candidate


def login_url_with_next(base: str, next_path: str) -> str:
    return f"{base}?next={quote(next_path, safe='/')}"
