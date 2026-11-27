"""세션 기반 한/영 전환. Django gettext 대신 t(request, ko, en) 로 문구를 고른다."""
from __future__ import annotations

SESSION_KEY = "lang"
LANGS = ("ko", "en")
DEFAULT_LANG = "ko"


def get_lang(request) -> str:
    if request is None or not hasattr(request, "session"):
        return DEFAULT_LANG
    lang = request.session.get(SESSION_KEY, DEFAULT_LANG)
    return lang if lang in LANGS else DEFAULT_LANG


def set_lang(request, lang: str) -> str:
    if lang not in LANGS:
        lang = DEFAULT_LANG
    request.session[SESSION_KEY] = lang
    return lang


def t(request, ko: str, en: str) -> str:
    """현재 언어에 맞는 문구: t(request, '저장', 'Save')"""
    return en if get_lang(request) == "en" else ko


def tr_pair(lang: str, pair) -> str:
    """{'ko':…, 'en':…} 또는 (ko, en) 쌍에서 현재 언어 문구."""
    if isinstance(pair, dict):
        return pair.get(lang) or pair.get("ko", "")
    ko, en = pair
    return en if lang == "en" else ko


def localized(obj, field: str, lang: str):
    """모델의 *_en 병기 컬럼: 영어일 때 값이 있으면 field_en, 아니면 field."""
    if lang == "en":
        val = getattr(obj, f"{field}_en", None)
        if val:
            return val
    return getattr(obj, field, "")
