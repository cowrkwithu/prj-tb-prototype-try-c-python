"""템플릿 전역 회귀 방지 — Alpine 속성 안의 템플릿 출력과 |safe 사용 (3a 리뷰 CONFIRMED 1).

규칙
1. 모든 템플릿에서 `|safe` 금지(허용 목록 없음).
2. Alpine 속성(x-*, @*, :*) 값 안의 `{{ … }}` 는 `|escapejs` 를 거치거나, 정수·불리언만 나오는 안전한 형태여야 한다.
   그 밖의 기존 사용(다른 단계 화면)은 아래 ALLOWLIST 에 근거와 함께 둔다 — 새로 추가하면 테스트가 실패한다.
3. Alpine 속성 안의 `{% t "…" "…" %}` 문구에는 작은따옴표를 쓰지 않는다(JS 문자열을 끊는다).
"""
import re
from pathlib import Path

from django.conf import settings

ROOT = Path(settings.BASE_DIR)
ATTR = re.compile(r'(?<![\w-])((?:x-[\w:.-]+|@[\w:.-]+|:[\w-]+))="([^"]*)"')
OUT = re.compile(r"\{\{\s*(.*?)\s*\}\}")
TTAG = re.compile(r"\{%\s*t\s+(\"[^\"]*\"|'[^']*')\s+(\"[^\"]*\"|'[^']*')\s*%\}")
# 정수·불리언만 나오는 출력(문자열이 될 수 없음)
SAFE_EXPR = re.compile(
    r"^(forloop\.counter0|forloop\.counter|[\w.]+\|yesno:'true,false'|tab\|default:0|active_filter_count|im\.id|"
    r"volume\.(peak|axial_count|slice_range\.\d)|max_files|max_bytes|[\w.]+\|add:-?\d+|patient\.pk|[\w.]+\.pk)$")
# 다른 단계 화면의 기존 사용 — 서버 상수(선택지 코드·도메인 키·메뉴 경로)만 들어가며 사용자 입력이 아님
ALLOWLIST = {
    ("apps/uploads/templates/uploads/steps/method.html", "mode|default:''"): "업로드 방식 코드(서버 상수)",
    ("apps/uploads/templates/uploads/steps/method_genomic.html", "mode|default:''"): "업로드 방식 코드(서버 상수)",
    ("apps/uploads/templates/uploads/steps/method_imaging.html", "mode|default:'CXR'"): "영상 방식 코드(서버 상수)",
    ("apps/uploads/templates/uploads/steps/method_imaging.html", "m"): "영상 방식 코드(서버 상수)",
    ("apps/uploads/templates/uploads/steps/form_imaging.html", "a"): "결핵 활동성 선택지(서버 상수)",
    ("apps/uploads/templates/uploads/steps/form_lab.html", "r"): "검사 결과 선택지(서버 상수)",
    ("apps/uploads/templates/uploads/steps/form_lab.html", "drug"): "약제 코드(서버 상수)",
    ("apps/uploads/templates/uploads/steps/form_lab.html", "val"): "DST 판정 R/S(서버 상수)",
    ("apps/uploads/templates/uploads/steps/files.html", "domain"): "업로드 도메인 키(URL 정규식으로 제한)",
    ("apps/uploads/templates/uploads/steps/files.html", "sub.upload_id|default:''"): "UPL-YYYYMMDD-NNN 서버 생성 ID",
    ("apps/uploads/templates/uploads/_my_uploads.html", "s.upload_id"): "UPL-YYYYMMDD-NNN 서버 생성 ID",
    ("apps/cases/templates/cases/_image_modal.html", "vdir"): "정적 영상 폴더 경로(서버 상수)",
    ("templates/partials/header.html", "item.to"): "메뉴 경로(navigation.py 상수)",
}


def _templates():
    for base in [ROOT / "templates", *sorted((ROOT / "apps").glob("*/templates"))]:
        yield from sorted(base.rglob("*.html"))


def test_no_safe_filter_anywhere():
    bad = [f"{p.relative_to(ROOT)}:{i}" for p in _templates()
           for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if re.search(r"\|\s*safe\b", line)]
    assert not bad, bad


def test_alpine_attribute_outputs_are_escaped():
    bad = []
    for p in _templates():
        rel = p.relative_to(ROOT).as_posix()
        for m in ATTR.finditer(p.read_text(encoding="utf-8")):
            for expr in OUT.findall(m.group(2)):
                if "|escapejs" in expr or SAFE_EXPR.match(expr) or (rel, expr) in ALLOWLIST:
                    continue
                bad.append(f"{rel}: {m.group(1)} → {{{{ {expr} }}}}")
    assert not bad, "\n".join(bad)


def test_translated_text_inside_alpine_has_no_single_quote():
    bad = []
    for p in _templates():
        for m in ATTR.finditer(p.read_text(encoding="utf-8")):
            for ko, en in TTAG.findall(m.group(2)):
                if "'" in ko[1:-1] or "'" in en[1:-1]:
                    bad.append(f"{p.relative_to(ROOT)}: {ko} / {en}")
    assert not bad, bad
