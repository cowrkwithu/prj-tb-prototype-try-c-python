"""상단 메뉴·사이드바 정의 (원본 components/layout/Header.tsx, App.tsx 와 동일한 항목·순서·라벨)."""

NAV_ITEMS = [
    {
        "to": "/", "label": ("홈", "Home"),
        "help": ("플랫폼 전체 현황 대시보드입니다. 핵심 지표와 분석 도구·연구사업 바로가기, 뉴스·공지사항을 한눈에 봅니다.",
                 "Platform dashboard with key metrics, shortcuts to analysis tools and research programs, plus news and notices."),
    },
    {
        "to": "/cases", "label": ("대상자 등록조회", "Cases"),
        "help": ("결핵 연구 대상자(환자 케이스)를 검색하고, 임상정보·영상·유전체·약제감수성·치료 타임라인을 상세 탭에서 확인합니다.",
                 "Search TB patient cases and review clinical, imaging, genomic, drug-susceptibility and treatment-timeline details."),
    },
    {
        "to": "/depot", "label": ("데이터 탐색", "Data Explorer"),
        "help": ("인구통계·임상·유전체·영상 조건을 조합해 연구 데이터셋을 만들고 요약·비교·통계 결과를 분석합니다.",
                 "Combine demographic, clinical, genomic and imaging criteria to build a research dataset and analyse its results."),
    },
    {
        "to": "/gap", "label": ("유전자 분석", "Genomics"),
        "help": ("유전체 브라우저·약제내성 예측·GWAS·유사도 분석·변이 검색으로 결핵균 유전체 변이를 분석합니다.",
                 "Analyse M. tuberculosis variants with the genome browser, resistance prediction, GWAS, similarity and variant search."),
    },
    {
        "to": "/rap", "label": ("영상 분석", "Imaging"),
        "help": ("흉부 X선·CT·PET-CT 영상을 판독하고 AI 어노테이션, 전문가 소견 입력, 시계열 변화 비교를 제공합니다.",
                 "Read chest X-ray, CT and PET-CT images with AI annotations, expert findings and time-series comparison."),
    },
    {
        "to": "/data", "label": ("데이터신청", "Data Request"),
        "help": ("공유 데이터셋과 API를 살펴보고 데이터 접근(DAR)을 신청하며, 내 신청·업로드 현황을 관리합니다.",
                 "Browse shared datasets and APIs, submit data access requests (DAR) and manage your requests and uploads."),
    },
    {
        "to": "/schemas", "label": ("데이터 정의", "Data Definition"), "icon": "database",
        "help": ("연구 맞춤형 데이터 구조를 탐색·생성하고, 표준 데이터사전에서 항목 정의를 확인합니다.",
                 "Browse and create custom research data schemas, and look up field definitions in the standard data dictionary."),
        "submenu": [
            {"to": "/schemas", "label": ("데이터 구조 탐색", "Browse Data Schemas"),
             "help": ("연구용 커스텀 데이터 구조를 검색·필터링하고 필드 구성과 사용 현황, 입력된 데이터를 확인합니다.",
                      "Search and filter custom research data schemas and review their fields, usage and records.")},
            {"to": "/schemas/create", "label": ("새 데이터 구조 만들기", "Create Data Schema"),
             "help": ("데이터 타입 필드와 표준 데이터사전 항목을 끌어다 놓아 연구 맞춤형 데이터 구조(DB)를 만듭니다.",
                      "Build a custom research data schema (DB) by dragging in typed fields and standard dictionary items.")},
            {"to": "/dictionary", "label": ("표준 데이터사전", "Standard Data Dictionary"),
             "help": ("플랫폼 표준 데이터 항목의 정의·형식·허용값을 카테고리별로 찾아봅니다.",
                      "Look up definitions, formats and allowed values of the platform's standard data fields by category.")},
        ],
    },
    {
        "to": "/upload", "label": ("데이터 업로드", "Upload"), "icon": "upload", "badge": "lock",
        "help": ("임상·치료 레지멘·검사실·유전체·영상·커스텀 데이터 파일을 올리고 검증·처리 상태를 확인합니다.",
                 "Upload clinical, regimen, lab, genomic, imaging and custom data files and track their validation status."),
    },
]

ADMIN_NAV_ITEM = {
    "to": "/admin", "label": ("관리", "Admin"), "icon": "shield-check",
    "help": ("관리자 전용 화면입니다. 업로드 데이터 검증·승인, 사용자와 데이터사전 관리, 시스템 상태를 확인합니다.",
             "Administrators only: review and approve uploaded data, manage users and the data dictionary, and check system status."),
}

MANUAL_NAV_ITEM = {
    "to": "/manual", "label": ("사용자 매뉴얼", "Manual"), "icon": "book-text",
    "help": ("플랫폼 사용법을 화면 캡처와 함께 단계별로 안내하는 사용자 매뉴얼입니다.",
             "Step-by-step user guide to the platform, illustrated with screenshots."),
}

# [베타 임시] 로그인 전 화면의 업로드 테스트용 샘플 CSV (원본 betaSampleDownloads)
BETA_SAMPLE_DOWNLOADS = [
    {"href": "samples/beta_clinical_identified.csv", "file": "임상데이터_식별.csv", "label": ("식별데이터", "Identified Data")},
    {"href": "samples/beta_clinical_deidentified.csv", "file": "임상데이터_비식별.csv", "label": ("비식별데이터", "De-identified Data")},
]

SIDEBARS = {
    "gap": {
        "title": ("유전자 분석", "Genomic Analysis"),
        "items": [
            {"to": "/gap", "label": ("유전체 브라우저", "Genome Browser"), "icon": "dna"},
            {"to": "/gap/resistance", "label": ("약제내성 예측", "Drug Resistance Prediction"), "icon": "shield"},
            {"to": "/gap/gwas", "label": ("GWAS 분석", "GWAS Analysis"), "icon": "chart-column"},
            {"to": "/gap/similarity", "label": ("유사도 분석", "Similarity Analysis"), "icon": "git-branch"},
            {"to": "/gap/search", "label": ("변이 검색", "Variant Search"), "icon": "search"},
        ],
    },
    "rap": {
        "title": ("영상 분석", "Imaging Analysis"),
        "items": [
            {"to": "/rap", "label": ("영상 뷰어", "Image Viewer"), "icon": "scan-line"},
            {"to": "/rap/timeseries", "label": ("타임시리즈", "Time Series"), "icon": "clock"},
            {"to": "/rap/annotation", "label": ("어노테이션", "Annotation"), "icon": "layers"},
        ],
    },
    "admin": {
        "title": ("관리", "Admin"),
        "items": [
            {"to": "/admin", "label": ("대시보드", "Dashboard"), "icon": "layout-dashboard"},
            {"to": "/admin/review", "label": ("데이터 검증", "Data Review"), "icon": "clipboard-check"},
            {"to": "/admin/dictionary", "label": ("데이터사전 관리", "Data Dictionary Management"), "icon": "book-open"},
            {"to": "/admin/schemas", "label": ("데이터 구조 관리", "Data Schema Management"), "icon": "database"},
            {"to": "/admin/dar", "label": ("데이터 접근 신청", "Data Access Requests"), "icon": "file-text"},
            {"to": "/admin/deletions", "label": ("삭제 요청", "Deletion Requests"), "icon": "trash-2"},
        ],
    },
}


def _is_active(path: str, to: str, exact: bool) -> bool:
    if exact or to == "/":
        return path.rstrip("/") == to.rstrip("/") or (to == "/" and path == "/")
    return path == to or path.startswith(to.rstrip("/") + "/")


def _loc(lang, pair):
    return pair[1] if lang == "en" else pair[0]


def build_nav(path: str, lang: str, user):
    """현재 사용자·경로·언어에 맞춘 메뉴 데이터를 만든다 (원본 Header 의 allNavItems 규칙)."""
    logged_in = bool(user and user.is_authenticated)
    items = []
    if logged_in:
        source = list(NAV_ITEMS)
        if getattr(user, "is_portal_admin", False):
            source.append(ADMIN_NAV_ITEM)
        for it in source:
            entry = {
                "to": it["to"], "label": _loc(lang, it["label"]), "help": _loc(lang, it["help"]),
                "icon": it.get("icon"), "badge": it.get("badge"),
            }
            if "submenu" in it:
                entry["submenu"] = [
                    {"to": s["to"], "label": _loc(lang, s["label"]), "help": _loc(lang, s["help"])}
                    for s in it["submenu"]
                ]
                # 원본: pathname.startsWith('/schemas') || pathname === '/dictionary'
                entry["active"] = _is_active(path, it["to"], False) or path.rstrip("/") == "/dictionary"
            else:
                entry["active"] = _is_active(path, it["to"], False)
            items.append(entry)
    manual = None
    if logged_in:
        manual = {
            "to": MANUAL_NAV_ITEM["to"], "label": _loc(lang, MANUAL_NAV_ITEM["label"]),
            "help": _loc(lang, MANUAL_NAV_ITEM["help"]), "icon": MANUAL_NAV_ITEM["icon"],
            "active": _is_active(path, "/manual", False),
        }
    return {"items": items, "manual": manual}


def build_sidebar(key: str | None, path: str, lang: str):
    if not key or key not in SIDEBARS:
        return None
    sb = SIDEBARS[key]
    return {
        "title": _loc(lang, sb["title"]),
        "items": [
            {"to": i["to"], "label": _loc(lang, i["label"]), "icon": i["icon"],
             "active": path.rstrip("/") == i["to"]}  # 원본 NavLink end
            for i in sb["items"]
        ],
    }
