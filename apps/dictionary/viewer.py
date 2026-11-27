"""표준 데이터사전 뷰어 서비스 (원본 pages/Dictionary/DictionaryViewer.tsx, utils/dictionarySearch.ts,
components/dictionary/DictionaryAiAssistant.tsx, components/common/DataTable.tsx).

현행 버전(is_active)의 폐기되지 않은(is_deprecated=False) 항목만 다룬다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import DictionaryCategory, DictionaryField
from .services import active_version

# 원본 types/schema.ts DATA_TYPE_LIST (순서·라벨·아이콘)
DATA_TYPE_LIST = [
    ("TEXT", "텍스트", "Text", "type", "#3B82F6"),
    ("TEXTAREA", "장문 텍스트", "Long Text", "align-left", "#60A5FA"),
    ("INTEGER", "정수", "Integer", "hash", "#10B981"),
    ("DECIMAL", "실수", "Decimal", "divide", "#34D399"),
    ("BOOLEAN", "예/아니오", "Yes/No", "toggle-left", "#0EA5E9"),
    ("DATE", "날짜", "Date", "calendar", "#F97316"),
    ("DATETIME", "날짜시간", "Date-Time", "clock", "#FB923C"),
    ("SELECT", "단일 선택", "Single Select", "chevron-down", "#8B5CF6"),
    ("MULTISELECT", "다중 선택", "Multi Select", "square-check", "#A78BFA"),
    ("NUMERIC_RANGE", "수치 범위", "Numeric Range", "sliders-horizontal", "#14B8A6"),
    ("FILE", "파일 첨부", "File Attachment", "paperclip", "#F43F5E"),
    ("REFERENCE", "참조 필드", "Reference Field", "link", "#06B6D4"),
    ("CALCULATED", "계산 필드", "Calculated Field", "calculator", "#F59E0B"),
    ("CODED", "코드값", "Code Value", "code", "#64748B"),
]
DATA_TYPES = {t[0]: {"type": t[0], "label": t[1], "label_en": t[2], "icon": t[3], "color": t[4]} for t in DATA_TYPE_LIST}

# 원본 DictionaryViewer iconMap — lucide 이름(PascalCase) → static/icons 파일명
CATEGORY_ICON_FILES = {
    "Users": "users", "Cigarette": "cigarette", "ClipboardList": "clipboard-list", "Stethoscope": "stethoscope",
    "Microscope": "microscope", "TestTube": "test-tube", "ShieldAlert": "shield-alert", "Droplet": "droplet",
    "Pill": "pill", "ScanLine": "scan-line", "ShieldCheck": "shield-check", "Dna": "dna", "Brain": "brain",
    "Settings": "settings", "AlertTriangle": "triangle-alert", "CalendarCheck": "calendar-check",
    "Syringe": "syringe", "MessageSquare": "message-square", "HeartPulse": "heart-pulse",
    "FlaskConical": "flask-conical", "Folder": "folder", "BookOpen": "book-open", "Beaker": "beaker",
    "FileText": "file-text", "Activity": "activity", "Heart": "heart",
}

CARD_PAGE_SIZE = 24
TABLE_PAGE_SIZES = (10, 20, 50)


def current_fields():
    ver = active_version()
    if ver is None:
        return DictionaryField.objects.none()
    return (DictionaryField.objects.filter(version=ver, is_deprecated=False)
            .select_related("category").order_by("category__order", "order", "id"))


def current_categories():
    ver = active_version()
    if ver is None:
        return []
    from django.db.models import Count, Q

    cats = (DictionaryCategory.objects.filter(version=ver)
            .annotate(n=Count("fields", filter=Q(fields__is_deprecated=False))).order_by("order"))
    return list(cats)


def match_entries(entries: list, q: str) -> list:
    """원본 matchDictionaryEntries — 항목명(한·영)·필드명에서 먼저 찾고, 없을 때만 설명(한·영)까지.

    대소문자 무시 부분 문자열 일치, 순서는 입력 순서(카테고리 순 → UDE 순) 그대로.
    """
    query = (q or "").strip().lower()
    if not query:
        return []

    def has(s):
        return query in (s or "").lower()

    by_name = [e for e in entries if has(e.display_name) or has(e.display_name_en) or has(e.field_name)]
    if by_name:
        return by_name
    return [e for e in entries if has(e.description) or has(e.description_en)]


def filter_entries(category_code: str, q: str, types: list[str]) -> list:
    entries = list(current_fields())
    if q.strip():
        entries = match_entries(entries, q)  # 검색은 카테고리와 무관하게 전체에서
    else:
        entries = [e for e in entries if e.category.code == category_code]
    if types:
        entries = [e for e in entries if e.data_type in types]
    return entries


def entry_text(e, lang: str) -> dict:
    if lang == "en":
        return {"display_name": e.display_name_en or e.display_name, "description": e.description_en or e.description}
    return {"display_name": e.display_name, "description": e.description}


def category_text(c, lang: str) -> dict:
    if lang == "en":
        return {"name": c.name_en or c.name, "description": c.description_en or c.description}
    return {"name": c.name, "description": c.description}


def type_label(t: str, lang: str) -> str:
    info = DATA_TYPES.get(t)
    if not info:
        return t
    return info["label_en"] if lang == "en" else info["label"]


# ── 테이블 보기 (원본 DataTable: 행 값 문자열 검색, 열 정렬 asc→desc→해제, 10/20/50) ──
TABLE_COLUMNS = ["category", "fieldName", "displayName", "dataType", "description", "cdiscMapping"]


@dataclass
class TableResult:
    rows: list
    total: int
    page: int
    pages: int
    page_size: int


def table_rows(entries: list, lang: str) -> list[dict]:
    rows = []
    for e in entries:
        tx = entry_text(e, lang)
        rows.append({"entry": e, "category": e.category.code, "fieldName": e.field_name,
                     "displayName": tx["display_name"], "dataType": e.data_type, "description": tx["description"],
                     "cdiscMapping": e.cdisc_mapping or None})
    return rows


def table_view(entries: list, lang: str, search: str, sort: str | None, direction: str | None,
               page: int, page_size: int) -> TableResult:
    rows = table_rows(entries, lang)
    q = (search or "").strip().lower()
    if q:
        rows = [r for r in rows if any(r[c] is not None and q in str(r[c]).lower() for c in TABLE_COLUMNS)]
    if sort in TABLE_COLUMNS and direction in ("asc", "desc"):
        present = [r for r in rows if r[sort] is not None]
        missing = [r for r in rows if r[sort] is None]  # 원본: null 은 항상 뒤로
        present.sort(key=lambda r: str(r[sort]).casefold(), reverse=direction == "desc")
        rows = present + missing
    page_size = page_size if page_size in TABLE_PAGE_SIZES else 10
    total = len(rows)
    pages = max(1, -(-total // page_size))
    page = min(max(page, 0), pages - 1)
    return TableResult(rows[page * page_size:(page + 1) * page_size], total, page, pages, page_size)


# ── AI 검색 도우미 (원본 DictionaryAiAssistant — 외부 LLM 없이 규칙 기반 시뮬레이션) ──
SCENARIOS = [
    {"id": "dst", "prompt": ("약제 내성 관련 데이터 항목을 찾아줘", "Find data items related to drug resistance"),
     "query": ("내성", "resistance"),
     "reply": ("약제 내성과 관련된 항목을 찾았어요. 주로 ⑫ 유전체(내성 유전자/변이)와 ⑦ 약제감수성검사(DST) 카테고리에 분포합니다.",
               "I found items related to drug resistance. They are mostly in the ⑫ Genomics (resistance genes/mutations) and ⑦ Drug Susceptibility Testing (DST) categories.")},
    {"id": "img", "prompt": ("흉부 영상(X-ray/CT) 소견 항목이 어디 있나요?", "Where are the chest imaging (X-ray/CT) finding items?"),
     "query": ("흉부", "chest"),
     "reply": ("흉부 영상 소견 항목입니다. ⑩ 영상 진단과 ⑬ AI 예측 분석(영상 AI 판독) 카테고리에 주로 있어요.",
               "Here are the chest imaging finding items. They are mostly in the ⑩ Imaging and ⑬ AI Prediction (imaging AI reading) categories.")},
    {"id": "adh", "prompt": ("복약 순응도를 분석하려면 어떤 항목을 봐야 하나요?", "Which items should I look at to analyze medication adherence?"),
     "query": ("순응도", "adherence"),
     "reply": ("복약 순응도 분석에 쓰이는 항목이에요. ⑯ 복약순응도/중단 카테고리에서 확인할 수 있습니다.",
               "These items are used for medication adherence analysis. You can find them in the ⑯ Adherence / Discontinuation category.")},
    {"id": "gen", "prompt": ("유전체 내성 변이 데이터 항목 알려줘", "Show me genomic resistance mutation data items"),
     "query": ("변이", "mutation"),
     "reply": ("유전체 내성 변이 관련 항목입니다. ⑫ 유전체 카테고리의 염기서열·변이 데이터를 참고하세요.",
               "Here are items related to genomic resistance mutations. See the sequencing and mutation data in the ⑫ Genomics category.")},
    {"id": "pka", "prompt": ("약동학 분석용 혈중 약물농도 항목을 보여줘", "Show blood drug concentration items for pharmacokinetic analysis"),
     "query": ("약물농도", "drug concentration"),
     "reply": ("약동학(PK) 분석에 필요한 혈중 약물농도 항목이에요. ⑰ 약동학/약물농도 카테고리에 있습니다.",
               "These are the blood drug concentration items needed for pharmacokinetic (PK) analysis. They are in the ⑰ Pharmacokinetics / Drug Levels category.")},
]
HELP_PATTERNS = [
    "무엇을 질문", "무엇을 물어", "뭘 질문", "뭘 물어", "뭐 질문", "뭐 물어", "뭐라고",
    "어떤 질문", "어떤 걸", "질문할 수 있", "물어볼 수 있", "가능한 질문", "질문 예시",
    "예시 질문", "어떻게 검색", "어떻게 물어", "사용법", "도움말", "help", "뭐 할 수 있",
    "what can i ask", "what can you do", "how do i search", "how to search", "example question",
]
HELP_TEXT = ("현재 AI 검색 도우미는 프로토타입 단계로, 미리 준비된 몇 가지 질문과 그에 대한 답변만 제공할 수 있어요. 아래 예시 질문 중 하나를 눌러보시거나 비슷한 키워드로 물어봐 주세요.",
             "The AI Search Assistant is currently a prototype and can only answer a few prepared questions. Click one of the example questions below or ask using similar keywords.")
HELP_PROMPT = ("무엇을 질문할 수 있어?", "What can I ask?")
GREETING = ("안녕하세요! 데이터사전 검색 도우미입니다. 찾으시는 데이터 항목을 자연어로 물어보세요. 무엇을 물어볼 수 있는지 궁금하면 \"무엇을 질문할 수 있어?\"라고 입력하거나 아래 추천 질문을 눌러보세요.",
            "Hello! I am the Data Dictionary Search Assistant. Ask about the data items you need in natural language. To see what you can ask, type \"What can I ask?\" or click a suggested question below.")
MAX_SHOWN = 6


def is_help_intent(text: str) -> bool:
    t = text.lower()
    return any(p.lower() in t for p in HELP_PATTERNS)


def assistant_reply(*, text: str | None = None, scenario_id: str | None = None, help_request: bool = False,
                    lang: str = "ko") -> dict:
    """한 번의 질의에 대한 응답: {user, reply, results, extra, query, show_suggestions}."""
    en = lang == "en"
    pick = (lambda pair: pair[1] if en else pair[0])
    if help_request:
        return {"user": pick(HELP_PROMPT), "reply": pick(HELP_TEXT), "results": [], "extra": 0,
                "query": None, "show_suggestions": True}
    text = (text or "").strip()[:300]
    scenario = next((s for s in SCENARIOS if s["id"] == scenario_id), None)
    if scenario is None and text:
        if is_help_intent(text):
            return {"user": text, "reply": pick(HELP_TEXT), "results": [], "extra": 0,
                    "query": None, "show_suggestions": True}
        scenario = next((s for s in SCENARIOS if text in s["prompt"]), None)
    if scenario is not None:
        user, query, intro = pick(scenario["prompt"]), pick(scenario["query"]), pick(scenario["reply"])
    elif text:
        user, query = text, text
        intro = f"I searched for items related to '{query}'." if en else f"'{query}' 관련 항목을 찾아봤어요."
    else:
        return {"user": "", "reply": "", "results": [], "extra": 0, "query": None, "show_suggestions": False}

    results = match_entries(list(current_fields()), query)
    n = len(results)
    if n:
        reply = (f"{intro} Found {n} {'item' if n == 1 else 'items'}, shown in the list below." if en
                 else f"{intro} 총 {n}개 항목을 찾아 아래 목록에 표시했습니다.")
    else:
        reply = (f"{intro} Unfortunately, no matching items were found. Please try again with different keywords." if en
                 else f"{intro} 아쉽게도 일치하는 항목을 찾지 못했어요. 다른 키워드로 다시 물어봐 주세요.")
    return {"user": user, "reply": reply, "results": results[:MAX_SHOWN], "total": n,
            "extra": max(0, n - MAX_SHOWN), "query": query if n else None, "show_suggestions": False}
