"""사용자 매뉴얼 서비스 — 시드 적재(멱등), 검색(원본 ManualPage 의 matchesQuery 규칙), 화면용 직렬화.

검색 규칙(원본과 같음): 현재 언어로
  - 대주제 제목·요약에 검색어가 있으면 그 대주제의 소주제 전부,
  - 아니면 제목 또는 본문(블록 문구)에 검색어가 있는 소주제만,
  - 일치하는 것이 없으면 대주제를 숨긴다. 대소문자 무시, 부분 일치.
검색은 DB 에서 한다(소주제의 평탄화 본문 search_text / search_text_en).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch, Q

from apps.core.safejson import has_bad_chars
from apps.core.seeding import is_seeded, mark_seeded

from .models import ManualBlock, ManualSection, ManualTopic

DATASET = "manual"
MAX_QUERY = 100
STATIC_DIR = Path(settings.BASE_DIR) / "static"
KINDS = {k for k, _ in ManualBlock.Kind.choices}


# ═══════════════════════════════════════════════════════════
#  문구 쌍·블록 검사 (시드와 관리 사이트 편집 모두 이 규칙으로 읽는다 — 형식이 어긋난 블록은 화면에서 건너뛴다)
# ═══════════════════════════════════════════════════════════
def _is_l(v) -> bool:
    return isinstance(v, dict) and isinstance(v.get("ko"), str) and isinstance(v.get("en"), str)


def _pick(v, lang: str) -> str:
    if not _is_l(v):
        return ""
    return (v["en"] or v["ko"]) if lang == "en" else v["ko"]


def _l_list(v) -> bool:
    return isinstance(v, list) and all(_is_l(x) for x in v)


def block_ok(kind: str, c) -> bool:
    if kind not in KINDS or not isinstance(c, dict):
        return False
    if kind in ("paragraph", "note"):
        return _is_l(c.get("text"))
    if kind in ("steps", "list"):
        return _l_list(c.get("items"))
    if kind == "table":
        rows = c.get("rows")
        return _l_list(c.get("headers")) and isinstance(rows, list) and all(_l_list(r) for r in rows)
    if kind == "path":
        return _is_l(c.get("label")) and _is_l(c.get("value"))
    src = c.get("src")
    return (isinstance(src, str) and src.startswith("manual/") and ".." not in src and "\\" not in src
            and _is_l(c.get("caption")))


def block_text(kind: str, c, lang: str) -> str:
    """검색용 평탄화(원본 blockText)."""
    if not block_ok(kind, c):
        return ""
    if kind in ("paragraph", "note"):
        return _pick(c["text"], lang)
    if kind in ("steps", "list"):
        return " ".join(_pick(i, lang) for i in c["items"])
    if kind == "table":
        return " ".join(_pick(x, lang) for x in [*c["headers"], *[x for r in c["rows"] for x in r]])
    if kind == "path":
        return f"{_pick(c['label'], lang)} {_pick(c['value'], lang)}"
    return _pick(c["caption"], lang)


def refresh_search_text(topic: ManualTopic) -> None:
    blocks = list(topic.blocks.all())
    topic.search_text = "\n".join(block_text(b.kind, b.content, "ko") for b in blocks)
    topic.search_text_en = "\n".join(block_text(b.kind, b.content, "en") for b in blocks)
    topic.save(update_fields=["search_text", "search_text_en"])


# ═══════════════════════════════════════════════════════════
#  시드
# ═══════════════════════════════════════════════════════════
def _content(b: dict) -> dict:
    c = {k: v for k, v in b.items() if k != "kind"}
    if b.get("kind") == "image":
        c["src"] = c["src"].lstrip("/")  # 원본 '/manual/x.png'(public) → static 기준 'manual/x.png'
    return c


def load_manual(path: Path | None = None, force: bool = False) -> dict:
    """data/seed/manual.json 을 1회 적재(SeedLog). force: 키 기준으로 문구를 시드와 같게 맞추고 시드 소주제의 블록을 다시 만든다
    (관리 사이트에서 고친 내용은 시드 값으로 덮어쓴다). 시드에 없는 대주제·소주제(관리 사이트에서 추가)는 건드리지 않는다."""
    if is_seeded(DATASET) and not force:
        return {"sections": 0, "topics": 0, "blocks": 0, "skipped": True}
    data = json.loads(Path(path or settings.SEED_DIR / "manual.json").read_text(encoding="utf-8"))
    n_s = n_t = n_b = 0
    with transaction.atomic():
        for si, s in enumerate(data["sections"]):
            sec, created = ManualSection.objects.get_or_create(key=s["id"], defaults={
                "no": s["no"], "order": si, "icon": s.get("icon", ""), "title": s["title"]["ko"],
                "title_en": s["title"]["en"], "summary": s["summary"]["ko"], "summary_en": s["summary"]["en"]})
            if not created and force:
                ManualSection.objects.filter(pk=sec.pk).update(
                    no=s["no"], order=si, icon=s.get("icon", ""), title=s["title"]["ko"], title_en=s["title"]["en"],
                    summary=s["summary"]["ko"], summary_en=s["summary"]["en"])
            n_s += created
            for ti, t in enumerate(s["topics"]):
                topic, t_created = ManualTopic.objects.get_or_create(key=t["id"], defaults={
                    "section": sec, "order": ti, "title": t["title"]["ko"], "title_en": t["title"]["en"]})
                if not t_created and not force:
                    continue  # 이미 있는 소주제는 force 일 때만 시드와 맞춘다
                if not t_created:
                    topic.section, topic.order = sec, ti
                    topic.title, topic.title_en = t["title"]["ko"], t["title"]["en"]
                    topic.save(update_fields=["section", "order", "title", "title_en"])
                    topic.blocks.all().delete()
                n_t += t_created
                blocks = [ManualBlock(topic=topic, order=bi, kind=b["kind"], content=_content(b))
                          for bi, b in enumerate(t["blocks"]) if block_ok(b.get("kind"), _content(b))]
                ManualBlock.objects.bulk_create(blocks)
                n_b += len(blocks)
                refresh_search_text(topic)
        mark_seeded(DATASET, {"sections": len(data["sections"]),
                              "topics": sum(len(s["topics"]) for s in data["sections"])})
    return {"sections": n_s, "topics": n_t, "blocks": n_b, "skipped": False}


# ═══════════════════════════════════════════════════════════
#  화면
# ═══════════════════════════════════════════════════════════
def clean_query(raw) -> str:
    q = raw.strip() if isinstance(raw, str) else ""
    return q[:MAX_QUERY]


@lru_cache(maxsize=512)
def _static_exists(rel: str) -> bool:
    return (STATIC_DIR / rel).is_file()


def image_src(src: str, lang: str) -> str:
    """영어 모드는 static/manual/en/ 의 영어 캡처를 쓰고, 없으면 한국어 캡처(원본 ManualImage 의 대체 규칙을 서버에서)."""
    if lang == "en":
        en = src.replace("manual/", "manual/en/", 1)
        if _static_exists(en):
            return en
    return src


def localize_block(b: ManualBlock, lang: str) -> dict | None:
    c = b.content
    if not block_ok(b.kind, c):
        return None
    out = {"kind": b.kind}
    if b.kind in ("paragraph", "note"):
        out["text"] = _pick(c["text"], lang)
    elif b.kind in ("steps", "list"):
        out["items"] = [_pick(i, lang) for i in c["items"]]
    elif b.kind == "table":
        out["headers"] = [_pick(h, lang) for h in c["headers"]]
        out["rows"] = [[_pick(x, lang) for x in r] for r in c["rows"]]
    elif b.kind == "path":
        out["label"], out["value"] = _pick(c["label"], lang), _pick(c["value"], lang)
    else:
        out["src"], out["caption"] = image_src(c["src"], lang), _pick(c["caption"], lang)
    return out


def manual_sections(lang: str, q: str = "") -> list[dict]:
    """화면용 대주제 목록(검색어가 있으면 원본 규칙으로 거른 결과). 블록은 한 번에 미리 읽는다."""
    en = lang == "en"
    title, summary = ("title_en", "summary_en") if en else ("title", "summary")
    t_title, t_body = ("title_en", "search_text_en") if en else ("title", "search_text")
    sections = ManualSection.objects.all()
    topic_qs = ManualTopic.objects.prefetch_related(Prefetch("blocks", queryset=ManualBlock.objects.order_by("order", "id")))
    if q and has_bad_chars(q):  # NUL·서로게이트가 든 검색어는 어떤 값과도 일치하지 않는다(PostgreSQL 은 비교 자체가 DataError)
        topic_qs = topic_qs.none()
    elif q:
        sec_hit = set(ManualSection.objects.filter(Q(**{f"{title}__icontains": q}) | Q(**{f"{summary}__icontains": q}))
                      .values_list("pk", flat=True))
        topic_qs = topic_qs.filter(Q(section_id__in=sec_hit) | Q(**{f"{t_title}__icontains": q})
                                   | Q(**{f"{t_body}__icontains": q}))
    sections = sections.prefetch_related(Prefetch("topics", queryset=topic_qs.order_by("order", "id")))
    out = []
    for s in sections:
        topics = []
        for t in s.topics.all():
            blocks = [x for x in (localize_block(b, lang) for b in t.blocks.all()) if x]
            topics.append({"key": t.key, "title": (t.title_en or t.title) if en else t.title, "blocks": blocks})
        if q and not topics:
            continue
        out.append({"key": s.key, "no": s.no, "icon": ICONS.get(s.icon, "book-open"),
                    "title": (s.title_en or s.title) if en else s.title,
                    "summary": (s.summary_en or s.summary) if en else s.summary, "topics": topics,
                    "title_ko": s.title, "title_en": s.title_en or s.title})
    return out


# 원본 lucide-react 아이콘 이름 → static/icons 파일 이름
ICONS = {"Compass": "compass", "BookOpen": "book-open", "Database": "database", "BarChart3": "chart-column",
         "LayoutGrid": "layout-grid"}
