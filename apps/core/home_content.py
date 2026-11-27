"""홈 화면의 편집 콘텐츠 (원본 src/pages/Home/index.tsx 의 상수).

- 분석 도구 바로가기(TOOLS)는 화면 구성 요소라 상수로 둔다.
- 뉴스·게시판·공지·연구사업·관련 사이트는 DB 모델(apps.core.models)에 두고 /django-admin 에서 편집한다.
  아래 NEWS_HIGHLIGHTS·BOARD_TABS·NOTICES·RESEARCH_PROGRAMS·RELATED_SITES 는 그 **최초 시드 원본**이다
  (seed_home_content — SeedLog 'home_content' 로 1회만 적재).
"""

TOOLS = [
    {"to": "/cases", "icon": "clipboard-list", "name": ("대상자 등록조회", "Cases"),
     "desc": ("환자별 임상·영상·유전체 데이터 통합 조회", "Integrated view of clinical, imaging, and genomic data per patient"),
     "color": "tone-navy"},
    {"to": "/depot", "icon": "database", "name": ("데이터 탐색", "Data Explorer"),
     "desc": ("데이터셋 구성 및 다차원 통계 비교 분석", "Build datasets and compare multi-dimensional statistics"),
     "color": "tone-teal"},
    {"to": "/gap", "icon": "dna", "name": ("유전자 분석", "Genomic Analysis"),
     "desc": ("유전체 브라우저, GWAS, 약제내성 예측", "Genome Browser, GWAS, Drug Resistance Prediction"),
     "color": "tone-info"},
    {"to": "/rap", "icon": "scan-line", "name": ("영상 분석", "Imaging Analysis"),
     "desc": ("CXR/CT/PET-CT 영상 뷰어 및 AI 어노테이션 분석", "CXR/CT/PET-CT image viewer and AI annotation analysis"),
     "color": "tone-success"},
    {"to": "/upload", "icon": "cloud-upload", "name": ("데이터 업로드 센터", "Data Upload Center"),
     "desc": ("협력기관 데이터를 업로드하고 관리합니다", "Upload and manage partner institution data"),
     "color": "tone-purple"},
    {"to": "/schemas", "icon": "blocks", "name": ("데이터 정의", "Data Definition"),
     "desc": ("커스텀 데이터 구조 생성 및 데이터 관리", "Create custom data schemas and manage data"),
     "color": "tone-indigo"},
]

NOTICES = [
    {"date": "2026-02-28", "title": ("TBRS Platform v2.1 업데이트: GWAS 분석 모듈 추가", "TBRS Platform v2.1 Update: GWAS Analysis Module Added")},
    {"date": "2026-02-15", "title": ("몽골 국립결핵연구소와 데이터 협약 체결", "Data Agreement Signed with Mongolia National TB Research Institute")},
    {"date": "2026-01-30", "title": ("2025년 연간 데이터 리포트 발간 (총 2,847건 등록)", "2025 Annual Data Report Published (2,847 records registered)")},
]

NEWS_HIGHLIGHTS = [
    {"tag": "뉴스", "date": "2026-02-25", "color": "bg-teal",
     "title": ("AI 기반 결핵 흉부 X선 판독 정확도 97% 달성", "AI-Based TB Chest X-ray Reading Reaches 97% Accuracy"),
     "desc": ("국립보건연구원과 공동 개발한 딥러닝 모델이 전문의 수준의 판독 성능을 입증하였습니다.",
              "A deep learning model co-developed with the Korea National Institute of Health demonstrated specialist-level reading performance.")},
    {"tag": "성과", "date": "2026-02-18", "color": "bg-navy",
     "title": ("MDR-TB 신규 바이오마커 3종 발견", "Three New MDR-TB Biomarkers Discovered"),
     "desc": ("다제내성 결핵의 조기 진단을 위한 혈청 바이오마커가 Nature Microbiology에 게재되었습니다.",
              "Serum biomarkers for early diagnosis of multidrug-resistant TB were published in Nature Microbiology.")},
    {"tag": "뉴스", "date": "2026-02-10", "color": "bg-info",
     "title": ("제3차 국제 결핵 데이터 컨소시엄 회의 개최", "3rd International TB Data Consortium Meeting Held"),
     "desc": ("8개국 12개 기관이 참여한 데이터 표준화 회의가 서울에서 개최되었습니다.",
              "A data standardization meeting with 12 institutions from 8 countries was held in Seoul.")},
    {"tag": "성과", "date": "2026-01-28", "color": "bg-success",
     "title": ("결핵 유전체 변이 DB 10,000건 돌파", "TB Genomic Variant DB Surpasses 10,000 Records"),
     "desc": ("유전자 분석 모듈을 통해 축적된 약제내성 관련 유전체 변이 데이터가 1만 건을 넘었습니다.",
              "Drug resistance-related genomic variant data accumulated through the Genomic Analysis module exceeded 10,000 records.")},
]

TAG_LABEL = {
    "뉴스": ("뉴스", "News"), "성과": ("성과", "Achievement"), "공지": ("공지", "Notice"),
    "공고": ("공고", "Announcement"), "채용": ("채용", "Careers"), "보도": ("보도", "Press"),
}

_CONTEST = ("2026년 상반기 결핵연구 데이터 활용 공모전 안내", "2026 H1 TB Research Data Utilization Contest")
_HIRING = ("결핵연구데이터센터 연구원(계약직) 채용 공고", "Hiring: TB Research Data Center Researcher (Contract)")
_PRESS_UPGRADE = ("국립보건연구원, 결핵 데이터 플랫폼 고도화 사업 착수", "KNIH Launches TB Data Platform Enhancement Project")
_MAINT = ("TBRS Platform 시스템 정기 점검 안내 (3/5)", "TBRS Platform Scheduled Maintenance (3/5)")
_PRESS_WHO = ("결핵 AI 진단 기술, WHO 사전적격성 평가 통과", "TB AI Diagnostic Technology Passes WHO Prequalification")

BOARD_TABS = [
    {"key": "all", "label": ("전체", "All"), "icon": None,
     "help": ("모든 분류의 게시글을 함께 봅니다.", "Show posts from every category."),
     "items": [("2026-02-28", "공지", _CONTEST), ("2026-02-26", "채용", _HIRING), ("2026-02-24", "보도", _PRESS_UPGRADE),
               ("2026-02-20", "공지", _MAINT), ("2026-02-18", "보도", _PRESS_WHO)]},
    {"key": "notice", "label": ("공지·공고", "Notices"), "icon": "megaphone",
     "help": ("'공지·공고' 분류의 게시글만 봅니다.", "Show only Notices posts."),
     "items": [("2026-02-28", "공지", _CONTEST), ("2026-02-20", "공지", _MAINT),
               ("2026-02-12", "공고", ("2026년 결핵 연구용역과제 신규과제 공모", "Call for New 2026 TB Research Service Projects")),
               ("2026-02-05", "공지", ("데이터 접근 신청 양식 개정 안내", "Revised Data Access Request Form")),
               ("2026-01-30", "공고", ("결핵 유전체 분석 장비 도입 입찰 공고", "Bid Notice: TB Genomic Analysis Equipment"))]},
    {"key": "careers", "label": ("채용", "Careers"), "icon": "briefcase",
     "help": ("'채용' 분류의 게시글만 봅니다.", "Show only Careers posts."),
     "items": [("2026-02-26", "채용", _HIRING),
               ("2026-02-15", "채용", ("바이오인포매틱스 분석 전문인력 채용", "Hiring: Bioinformatics Analyst")),
               ("2026-02-01", "채용", ("박사후연수생(결핵 역학) 모집 공고", "Recruiting: Postdoctoral Fellow (TB Epidemiology)")),
               ("2026-01-20", "채용", ("AI/ML 엔지니어 채용 (영상의학 데이터)", "Hiring: AI/ML Engineer (Radiology Data)")),
               ("2026-01-10", "채용", ("데이터베이스 관리자(DBA) 경력직 채용", "Hiring: Experienced Database Administrator (DBA)"))]},
    {"key": "press", "label": ("보도자료", "Press Releases"), "icon": "newspaper",
     "help": ("'보도자료' 분류의 게시글만 봅니다.", "Show only Press Releases posts."),
     "items": [("2026-02-24", "보도", _PRESS_UPGRADE), ("2026-02-18", "보도", _PRESS_WHO),
               ("2026-02-08", "보도", ("한-몽 결핵 공동연구 성과 보고회 개최", "Korea–Mongolia Joint TB Research Results Briefing Held")),
               ("2026-01-25", "보도", ("2025 결핵 연구 백서 발간", "2025 TB Research White Paper Published")),
               ("2026-01-15", "보도", ("결핵 데이터 플랫폼 이용자 1,000명 돌파", "TB Data Platform Surpasses 1,000 Users"))]},
]

RESEARCH_PROGRAMS = [
    {"icon": "flask-conical", "color": "tone-teal", "title": ("결핵 진단기술 개발", "TB Diagnostic Technology"),
     "desc": ("AI 기반 영상 판독 및 분자진단 기술 연구", "Research on AI-based image reading and molecular diagnostics")},
    {"icon": "microscope", "color": "tone-navy", "title": ("약제내성 결핵 연구", "Drug-Resistant TB Research"),
     "desc": ("MDR/XDR-TB 유전체 분석 및 신규 치료 표적 발굴", "MDR/XDR-TB genomic analysis and discovery of new therapeutic targets")},
    {"icon": "heart-pulse", "color": "tone-info", "title": ("결핵 역학 감시", "TB Epidemiological Surveillance"),
     "desc": ("다국가 데이터셋 기반 결핵 발생 및 치료결과 모니터링", "Monitoring TB incidence and treatment outcomes with multinational datasets")},
    {"icon": "shield-check", "color": "tone-success", "title": ("결핵 백신·예방 연구", "TB Vaccine & Prevention Research"),
     "desc": ("BCG 대체 후보 백신 임상시험 데이터 관리", "Clinical trial data management for BCG replacement vaccine candidates")},
]

RELATED_SITES = [
    {"icon": "landmark", "name": ("국립보건연구원", "Korea National Institute of Health"), "desc": ("질병관리청 산하 국가 보건연구기관", "National health research institute under KDCA")},
    {"icon": "shield-check", "name": ("KDCA 질병관리청", "KDCA"), "desc": ("감염병 예방 및 관리 총괄", "Infectious disease prevention and control")},
    {"icon": "globe", "name": ("WHO Global TB Programme", "WHO Global TB Programme"), "desc": ("세계보건기구 결핵 프로그램", "World Health Organization TB programme")},
    {"icon": "database", "name": ("NIAID TB Portals", "NIAID TB Portals"), "desc": ("미국 NIH 결핵 데이터 포털", "US NIH TB data portal")},
    {"icon": "book-open", "name": ("의과학지식센터", "Medical Science Knowledge Center"), "desc": ("보건의료 연구 정보 허브", "Health and medical research information hub")},
    {"icon": "graduation-cap", "name": ("CRIS 임상연구정보서비스", "CRIS Clinical Research Information Service"), "desc": ("국내 임상시험 등록 시스템", "Korean clinical trial registry")},
]


def localize(obj, lang):
    """(ko, en) 튜플 값을 현재 언어 문자열로 바꾼 사본(중첩 dict/list 지원)."""
    if isinstance(obj, tuple) and len(obj) == 2 and all(isinstance(x, str) for x in obj):
        return obj[1] if lang == "en" else obj[0]
    if isinstance(obj, tuple):
        return tuple(localize(v, lang) for v in obj)
    if isinstance(obj, dict):
        return {k: localize(v, lang) for k, v in obj.items()}
    if isinstance(obj, list):
        return [localize(v, lang) for v in obj]
    return obj


# ═══════════════════════════════════════════════════════════
#  DB 시드 · 홈 화면 조회
# ═══════════════════════════════════════════════════════════
from datetime import date as _date  # noqa: E402

SEED_DATASET = "home_content"
BOARD_TAB_META = [  # (key, 라벨, 아이콘, 도움말) — '전체' 탭은 모든 분류 최신순
    ("all", ("전체", "All"), None, ("모든 분류의 게시글을 함께 봅니다.", "Show posts from every category.")),
    ("notice", ("공지·공고", "Notices"), "megaphone", ("'공지·공고' 분류의 게시글만 봅니다.", "Show only Notices posts.")),
    ("careers", ("채용", "Careers"), "briefcase", ("'채용' 분류의 게시글만 봅니다.", "Show only Careers posts.")),
    ("press", ("보도자료", "Press Releases"), "newspaper", ("'보도자료' 분류의 게시글만 봅니다.", "Show only Press Releases posts.")),
]
BOARD_PER_TAB = 5


def seed_home_content(force: bool = False) -> dict:
    """홈 편집 콘텐츠를 1회 적재한다(SeedLog). force=True 면 비어 있는 모델만 다시 채운다."""
    from django.db import transaction

    from .models import BoardPost, NewsItem, Notice, RelatedSite, ResearchProgram
    from .seeding import is_seeded, mark_seeded

    if is_seeded(SEED_DATASET) and not force:
        return {"skipped": True}
    d = _date.fromisoformat
    result = {"skipped": False}
    with transaction.atomic():
        if not NewsItem.objects.exists():
            NewsItem.objects.bulk_create([
                NewsItem(tag=n["tag"], date=d(n["date"]), color=n["color"], title=n["title"][0], title_en=n["title"][1],
                         desc=n["desc"][0], desc_en=n["desc"][1], order=i) for i, n in enumerate(NEWS_HIGHLIGHTS)])
        if not BoardPost.objects.exists():
            posts, seen = [], set()
            for tab in BOARD_TABS:
                if tab["key"] == "all":
                    continue
                for i, (dt, tag, title) in enumerate(tab["items"]):
                    if (dt, title) in seen:
                        continue
                    seen.add((dt, title))
                    posts.append(BoardPost(category=tab["key"], tag=tag, date=d(dt), title=title[0], title_en=title[1], order=i))
            BoardPost.objects.bulk_create(posts)
        if not Notice.objects.exists():
            Notice.objects.bulk_create([Notice(date=d(n["date"]), title=n["title"][0], title_en=n["title"][1], order=i)
                                        for i, n in enumerate(NOTICES)])
        if not ResearchProgram.objects.exists():
            ResearchProgram.objects.bulk_create([
                ResearchProgram(icon=r["icon"], color=r["color"], title=r["title"][0], title_en=r["title"][1],
                                desc=r["desc"][0], desc_en=r["desc"][1], order=i) for i, r in enumerate(RESEARCH_PROGRAMS)])
        if not RelatedSite.objects.exists():
            RelatedSite.objects.bulk_create([
                RelatedSite(icon=r["icon"], name=r["name"][0], name_en=r["name"][1], desc=r["desc"][0], desc_en=r["desc"][1],
                            order=i) for i, r in enumerate(RELATED_SITES)])
        result.update(news=NewsItem.objects.count(), board=BoardPost.objects.count(), notices=Notice.objects.count(),
                      programs=ResearchProgram.objects.count(), sites=RelatedSite.objects.count())
        mark_seeded(SEED_DATASET, {k: v for k, v in result.items() if k != "skipped"})
    return result


def _loc(obj, field, lang):
    if lang == "en":
        return getattr(obj, f"{field}_en", "") or getattr(obj, field)
    return getattr(obj, field)


def home_sections(lang: str) -> dict:
    """홈 편집 콘텐츠를 DB 에서 읽는다 — 모델당 쿼리 1회(총 5회)."""
    from .models import BoardPost, NewsItem, Notice, RelatedSite, ResearchProgram

    news = [{"tag": n.tag, "tag_label": localize(TAG_LABEL.get(n.tag, (n.tag, n.tag)), lang), "date": n.date.isoformat(),
             "color": n.color, "title": _loc(n, "title", lang), "desc": _loc(n, "desc", lang)}
            for n in NewsItem.objects.published()[:4]]
    posts = list(BoardPost.objects.published().order_by("-date", "order", "id")[:200])
    board = []
    for key, label, icon, help_ in BOARD_TAB_META:
        items = [p for p in posts if key == "all" or p.category == key][:BOARD_PER_TAB]
        board.append({"key": key, "label": localize(label, lang), "icon": icon, "help": localize(help_, lang),
                      "items": [{"date": p.date.isoformat(), "tag": p.tag,
                                 "tag_label": localize(TAG_LABEL.get(p.tag, (p.tag, p.tag)), lang),
                                 "title": _loc(p, "title", lang)} for p in items]})
    notices = [{"date": n.date.isoformat(), "title": _loc(n, "title", lang)} for n in Notice.objects.published()[:3]]
    programs = [{"icon": r.icon, "color": r.color, "title": _loc(r, "title", lang), "desc": _loc(r, "desc", lang)}
                for r in ResearchProgram.objects.published()]
    sites = [{"icon": r.icon, "name": _loc(r, "name", lang), "desc": _loc(r, "desc", lang), "url": r.url}
             for r in RelatedSite.objects.published()]
    return {"news": news, "board": board, "notices": notices, "programs": programs, "sites": sites}
