"""포털 공통 템플릿 태그 (settings.TEMPLATES builtins 로 등록 — {% load %} 불필요).

  {% t "한국어" "English" %}           현재 언어 문구 (as 변수 지원)
  {% icon "microscope" "w-5 h-5" %}    static/icons/ 의 lucide SVG 인라인
  {% locf obj "name" %}               영어일 때 obj.name_en (값이 있으면)
"""
import re
from functools import lru_cache

from django import template
from django.conf import settings
from django.utils.html import escape
from django.utils.safestring import mark_safe

from apps.core.i18n import get_lang, localized

register = template.Library()

ICON_DIR = settings.BASE_DIR / "static" / "icons"
_ICON_NAME = re.compile(r"^[a-z0-9-]+$")


def _ctx_lang(context) -> str:
    lang = context.get("lang")
    if lang in ("ko", "en"):
        return lang
    return get_lang(context.get("request"))


@register.simple_tag(takes_context=True)
def t(context, ko, en):
    return en if _ctx_lang(context) == "en" else ko


@lru_cache(maxsize=256)
def _icon_body(name: str) -> str:
    if not _ICON_NAME.match(name):
        return ""
    path = ICON_DIR / f"{name}.svg"
    if not path.exists():
        return ""
    svg = path.read_text(encoding="utf-8")
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S).strip()
    # 크기·클래스는 CSS 로 지정하므로 <svg> 여는 태그에서만 class/width/height 를 뗀다
    # (내부 <rect width=…> 등은 도형 정의라 유지해야 한다)
    m = re.match(r"<svg\b[^>]*>", svg)
    if not m:
        return ""
    open_tag = re.sub(r'\s(class|width|height)="[^"]*"', "", m.group(0))
    svg = open_tag + svg[m.end():]
    svg = re.sub(r"\s+", " ", svg)
    return svg


@register.simple_tag
def icon(name, css_class="icon"):
    body = _icon_body(str(name))
    if not body:
        return ""
    return mark_safe(body.replace("<svg", f'<svg class="icon {escape(css_class)}" aria-hidden="true"', 1))


@register.simple_tag(takes_context=True)
def locf(context, obj, field):
    """{% locf category "name" %} — 현재 언어로 *_en 병기 컬럼 선택."""
    return localized(obj, field, _ctx_lang(context))
