"""데이터 공유 화면의 고정 정의 — 원본 pages/DataSharing/{DatasetsTab,DarTab,ApiTab,datasetDictionaryMap}.ts.

건수·기여·최종 업데이트 같은 수치는 여기 두지 않는다(services.dataset_cards 가 DB 에서 집계).
"""
from __future__ import annotations

import re

# ── DAR 데이터 유형 (원본 DarGateContext: clinical / genomics / imaging) ──
DATA_TYPES = ["clinical", "genomics", "imaging"]
DATA_TYPE_OPTIONS = [
    {"id": "clinical", "label": ("임상 데이터", "Clinical Data"), "desc": "Patient Cases, Regimens, DST"},
    {"id": "genomics", "label": ("유전체 데이터", "Genomic Data"), "desc": "WGS Variants, Lineage"},
    {"id": "imaging", "label": ("영상 데이터", "Imaging Data"), "desc": "CXR, CT, PET-CT DICOM"},
]
# 신청 현황 표의 짧은 라벨(원본 VALUE_LABEL 임상/유전체/영상)
DATA_TYPE_SHORT = {"clinical": ("임상", "Clinical"), "genomics": ("유전체", "Genomic"), "imaging": ("영상", "Imaging")}
# 원본 statusStyle — pending 은 원본 '검토중'(노랑)에 대응한다
DAR_STATUS = {
    "pending": {"label": ("검토중", "Under Review"), "cls": "dar-pending"},
    "approved": {"label": ("승인", "Approved"), "cls": "dar-approved"},
    "rejected": {"label": ("반려", "Rejected"), "cls": "dar-rejected"},
}


def dar_type_of_schema_category(category: str) -> str:
    """원본 darTypeOfCategory — 커스텀 스키마 카테고리 → DAR 유형. 확장 카테고리는 원 도메인, 그 밖은 임상."""
    category = category or ""
    if category.startswith("genomic"):
        return "genomics"
    if category.startswith("imaging"):
        return "imaging"
    return "clinical"


# ── 정식 데이터셋 7종 (원본 DatasetsTab datasets) ──
DATASETS = [
    {
        "slug": "patient-cases", "title": "Patient Cases", "subtitle": ("임상 데이터", "Clinical Data"), "icon": "database",
        "description": ("인구통계, 진단, 치료 결과, 동반질환 등 환자 케이스 데이터. 8개국 협력기관에서 수집된 익명화 데이터.",
                        "Patient case data including demographics, diagnosis, treatment outcomes and comorbidities. "
                        "Anonymized data collected from partner institutions in 8 countries."),
        "unit": ("건", "records"), "format": "CSV", "upload_path": "/upload/clinical", "dar_type": "clinical",
    },
    {
        "slug": "regimens", "title": "Regimens", "subtitle": ("치료 레지멘", "Treatment Regimens"), "icon": "pill",
        "description": ("환자별 약제 처방 이력, 투약 기간, 용량 정보. 1차/2차/3차 약제 구분.",
                        "Per-patient drug prescription history, treatment duration and dosage. "
                        "Classified into first-, second- and third-line drugs."),
        "unit": ("건", "records"), "format": "CSV", "upload_path": "/upload/regimen", "dar_type": "clinical",
    },
    {
        "slug": "dst-results", "title": "DST Results", "subtitle": ("약제감수성 검사", "Drug Susceptibility Testing"),
        "icon": "flask-conical",
        "description": ("1차/2차 약제에 대한 감수성(S), 내성(R), 중간(I) 결과. 표현형 DST 및 분자 DST.",
                        "Susceptible (S), resistant (R) and intermediate (I) results for first- and second-line drugs. "
                        "Phenotypic and molecular DST."),
        "unit": ("건", "records"), "format": "CSV", "upload_path": "/upload/lab", "dar_type": "clinical",
    },
    {
        "slug": "genomics", "title": "Genomics", "subtitle": ("유전체 변이", "Genomic Variants"), "icon": "dna",
        "description": ("전장 유전체 시퀀싱(WGS) 기반 변이 호출 결과. VCF 포맷 및 주석 정보 포함.",
                        "Variant calls from whole-genome sequencing (WGS). Includes VCF format and annotations."),
        "unit": ("건", "records"), "format": "VCF/CSV", "upload_path": "/upload/genomic", "dar_type": "genomics",
    },
    {
        "slug": "cxr-images", "title": "CXR Images", "subtitle": ("흉부X선 영상", "Chest X-ray Images"), "icon": "scan-line",
        "description": ("디지털 흉부X선 영상. DICOM 원본 및 JPG 변환본, AI 어노테이션 메타데이터 포함.",
                        "Digital chest X-ray images. Includes original DICOM, converted JPG and AI annotation metadata."),
        "unit": ("장", "images"), "format": "DICOM/JPG", "upload_path": "/upload/imaging", "dar_type": "imaging",
        "modality": "CXR",
    },
    {
        "slug": "ct-studies", "title": "CT Studies", "subtitle": ("CT 영상", "CT Images"), "icon": "layers",
        "description": ("흉부 CT 영상 스터디. 슬라이스 단위 DICOM, 3D 재구성 데이터 포함.",
                        "Chest CT imaging studies. Includes per-slice DICOM and 3D reconstruction data."),
        "unit": (" 스터디", "studies"), "format": "DICOM", "upload_path": "/upload/imaging", "dar_type": "imaging",
        "modality": "CT",
    },
    {
        "slug": "pet-ct-studies", "title": "PET-CT Studies", "subtitle": ("PET-CT 영상", "PET-CT Images"), "icon": "radiation",
        "description": ("18F-FDG PET-CT 융합 영상 스터디. PET·CT DICOM 시리즈와 병변별 SUVmax 정량값 포함. 치료 반응·활동성 병변 평가용.",
                        "18F-FDG PET-CT fusion imaging studies. Includes PET and CT DICOM series with per-lesion SUVmax values, "
                        "for assessing treatment response and active disease."),
        "unit": (" 스터디", "studies"), "format": "DICOM", "upload_path": "/upload/imaging", "dar_type": "imaging",
        "modality": "PET-CT",
    },
]
DATASET_BY_SLUG = {d["slug"]: d for d in DATASETS}


# ═══════════════════════════════════════════════════════════
#  데이터셋 ↔ 표준 데이터사전 대응 (원본 datasetDictionaryMap.ts)
# ═══════════════════════════════════════════════════════════
# ⑨ 치료 및 결과 중 처방·투약(레지멘) 항목 — 나머지(최종 결과·사망·재발·전출 등)는 임상 데이터로
_REGIMEN_ITEM = re.compile(r"치료 요법|Regimen|결핵 치료 시작|치료 경과 개월수|항결핵제 투여|투여 용량|기타 약제|병용/선행 약물|약물명|약제 영구적 중단")
# 흉부 CT / 흉부 X선 전용 항목 — 이름에 검사법이 드러나지 않는 항목은 ID 로 명시(원본과 동일)
_CT_ONLY_IDS = {"TB.U.IMG.035"}
_CXR_ONLY_IDS = {"TB.U.IMG.026"}
_AI_ALSO_CT_IDS = {"TB.U.AI.001"}
_CT_NAME = re.compile(r"흉부 ?CT")
_CXR_NAME = re.compile(r"흉부 ?X선")


def _is_regimen_item(e) -> bool:
    return bool(_REGIMEN_ITEM.search(e.display_name or ""))


def _is_molecular_dst_item(e) -> bool:
    return "Xpert/mWRD" in (e.display_name or "")


def _is_ct_only(e) -> bool:
    return e.dictionary_id in _CT_ONLY_IDS or bool(_CT_NAME.search(e.display_name or ""))


def _is_cxr_only(e) -> bool:
    return e.dictionary_id in _CXR_ONLY_IDS or bool(_CXR_NAME.search(e.display_name or ""))


DATASET_DICTIONARY_MAP = {
    # 인구통계·동반질환·증상·진단 + 치료 결과(처방·투약 항목은 Regimens 로)
    "patient-cases": {"categories": ["pat", "soc", "scr", "dx", "tx"],
                      "include": lambda e, cat: cat != "tx" or not _is_regimen_item(e)},
    # 처방 이력·투약 기간·용량 + 복약순응도/중단
    "regimens": {"categories": ["tx", "adh"], "include": lambda e, cat: cat != "tx" or _is_regimen_item(e)},
    # 표현형·유전형 DST + 분자신속검사(Xpert/mWRD) 약제내성
    "dst-results": {"categories": ["dst", "lab"], "include": lambda e, cat: cat != "lab" or _is_molecular_dst_item(e)},
    "genomics": {"categories": ["gen"], "include": None},
    # 흉부 CT 전용 항목을 뺀 영상 진단 항목 + AI 판독 메타데이터
    "cxr-images": {"categories": ["img", "ai"], "include": lambda e, cat: cat == "ai" or not _is_ct_only(e)},
    # 흉부 X선 전용 항목을 뺀 영상 진단 항목 + CT 판독을 포함하는 AI 항목
    "ct-studies": {"categories": ["img", "ai"],
                   "include": lambda e, cat: (e.dictionary_id in _AI_ALSO_CT_IDS) if cat == "ai" else not _is_cxr_only(e)},
    # 데이터사전 v3.2.0 에는 PET-CT·SUV·FDG 항목이 없다 → 빈 상태로 안내
    "pet-ct-studies": {"categories": [], "include": None},
}


def dictionary_entries_for_dataset(slug: str, entries) -> list:
    """데이터셋에 해당하는 사전 항목을 카테고리 순서대로 돌려준다(entries 는 category 가 select_related 된 목록)."""
    source = DATASET_DICTIONARY_MAP.get(slug)
    if not source:
        return []
    include = source["include"]
    out = []
    for cat in source["categories"]:
        out += [e for e in entries if e.category.code == cat and (include is None or include(e, cat))]
    return out


# ═══════════════════════════════════════════════════════════
#  API 문서 (원본 ApiTab endpoints — 화면 문구·예시 응답 그대로)
# ═══════════════════════════════════════════════════════════
API_ENDPOINTS = [
    {
        "method": "GET", "path": "/api/v1/cases", "description": ("전체 환자 케이스 목록 조회", "List all patient cases"),
        "auth": True,
        "params": [
            ("page", "number", False, ("페이지 번호 (기본값: 1)", "Page number (default: 1)")),
            ("limit", "number", False, ("페이지당 건수 (기본값: 20, 최대: 100)", "Items per page (default: 20, max: 100)")),
            ("country", "string", False, ("국적 필터", "Nationality filter")),
            ("diagnosisType", "string", False, ("진단 유형 필터 (DS-TB, MDR-TB, Pre-XDR-TB, XDR-TB)",
                                                "Diagnosis type filter (DS-TB, MDR-TB, Pre-XDR-TB, XDR-TB)")),
        ],
        "response": """{
  "data": [
    {
      "id": 1,
      "conditionId": "KR-2024-0001",
      "demographics": { "age": 45, "sex": "M", "country": "한국" },
      "clinical": { "diagnosisType": "DS-TB", "treatmentOutcome": "Cured" }
    }
  ],
  "pagination": { "page": 1, "limit": 20, "total": 2847 }
}""",
    },
    {
        "method": "GET", "path": "/api/v1/cases/{id}", "description": ("특정 환자 케이스 상세 조회", "Get patient case details"),
        "auth": True,
        "params": [
            ("id", "number", True, ("환자 ID", "Patient ID")),
            ("include", "string", False, ("포함할 데이터 (genomics, imaging, lab)", "Data to include (genomics, imaging, lab)")),
        ],
        "response": """{
  "id": 1,
  "conditionId": "KR-2024-0001",
  "demographics": { "age": 45, "sex": "M", "country": "한국", "site": "서울대병원" },
  "clinical": { "diagnosisType": "DS-TB", "treatmentOutcome": "Cured", "treatmentDuration": 180 },
  "genomics": { "hasSequence": true, "sraId": "SRR180001", "lineage": "L2.2.1" },
  "imaging": { "hasCxr": true, "hasCt": false, "hasPetCt": false, "cxrCount": 3 }
}""",
    },
    {
        "method": "GET", "path": "/api/v1/genomics/variants", "description": ("유전체 변이 목록 조회", "List genomic variants"),
        "auth": True,
        "params": [
            ("gene", "string", False, ("유전자 필터 (예: rpoB, katG)", "Gene filter (e.g., rpoB, katG)")),
            ("drug", "string", False, ("관련 약제 필터", "Related drug filter")),
            ("minFrequency", "number", False, ("최소 빈도 (0~1)", "Minimum frequency (0–1)")),
        ],
        "response": """{
  "data": [
    {
      "id": 1,
      "gene": "rpoB",
      "position": 761155,
      "refAllele": "C",
      "altAllele": "T",
      "aminoAcidChange": "S450L",
      "drugResistance": "Rifampicin",
      "frequency": 0.72,
      "sampleCount": 614
    }
  ],
  "total": 342
}""",
    },
    {
        "method": "GET", "path": "/api/v1/images/{id}", "description": ("영상 데이터 조회 및 다운로드", "Get and download imaging data"),
        "auth": True,
        "params": [
            ("id", "number", True, ("영상 ID", "Image ID")),
            ("format", "string", False, ("응답 포맷 (metadata, dicom, jpg)", "Response format (metadata, dicom, jpg)")),
        ],
        "response": """{
  "id": 101,
  "patientId": 1,
  "type": "CXR",
  "date": "2024-03-15",
  "findings": ["침윤", "결절"],
  "annotations": [
    { "type": "침윤", "bbox": [120, 85, 200, 180], "confidence": 0.92 }
  ],
  "downloadUrl": "/api/v1/images/101/download"
}""",
    },
    {
        "method": "POST", "path": "/api/v1/cohorts", "description": ("새 데이터셋 생성", "Create a new dataset"), "auth": True,
        "params": [
            ("name", "string", True, ("데이터셋 이름", "Dataset name")),
            ("conditions", "array", True, ("필터 조건 배열", "Array of filter conditions")),
            ("description", "string", False, ("데이터셋 설명", "Dataset description")),
        ],
        "response": """{
  "id": 4,
  "name": "신규 데이터셋",
  "patientIds": [2, 4, 8, 11, 13],
  "patientCount": 5,
  "createdDate": "2024-11-20"
}""",
    },
]
METHOD_COLORS = {"GET": "m-get", "POST": "m-post", "PUT": "m-put", "DELETE": "m-delete"}

# ── 화면 탭 (원본 index.tsx tabs — URL 상태 ?tab=) ──
TABS = [
    {"key": "datasets", "label": ("데이터셋", "Datasets"), "icon": "database",
     "help": ("공개된 데이터셋 목록을 보고 내려받거나 접근을 신청합니다.", "Browse published datasets to download them or request access.")},
    {"key": "api", "label": ("API", "API"), "icon": "code",
     "help": ("API 키 발급과 엔드포인트 사용법을 확인합니다.", "Issue API keys and see how to use the endpoints.")},
    {"key": "dar", "label": ("데이터 접근 신청", "Data Access Request"), "icon": "file-text",
     "help": ("제한 데이터 이용을 위한 접근 신청서를 작성하고 진행 상황을 봅니다.",
              "Fill in an access request for restricted data and track its status.")},
    {"key": "uploads", "label": ("내 업로드 관리", "My Uploads"), "icon": "upload",
     "help": ("내가 업로드한 데이터의 검증 상태와 이력을 관리합니다.", "Manage the review status and history of data you uploaded.")},
]
TAB_KEYS = [t["key"] for t in TABS]
