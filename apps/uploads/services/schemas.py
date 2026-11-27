"""업로드 도메인 정의 — 원본 data/uploadSchemas.ts(컬럼·샘플: data/seed/uploadSchemas.json),
utils/validators.ts(값 규칙), pages/Upload/*.tsx(단계·선택지·표시 라벨)를 옮긴 것.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from django.conf import settings

DOMAINS = ["clinical", "regimen", "lab", "genomic", "imaging"]

# ─── 값 규칙 (utils/validators.ts) ───
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_date(v: str) -> bool:
    """원본 isValidDate 는 형식(YYYY-MM-DD)만 본다. 서버는 실제 달력 날짜인지도 확인한다."""
    if not DATE_RE.match(v):
        return False
    from datetime import date

    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _js_number(v: str):
    """JS Number(v) 와 비슷하게: 공백 제거 후 숫자 문자열만 float, 아니면 None."""
    s = v.strip()
    if s == "":
        return 0.0
    if "_" in s or s.lower().lstrip("+-") in ("nan", "inf", "infinity"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return f


def is_int_value(v: str) -> bool:
    n = _js_number(v)
    return n is not None and n == int(n)


def is_valid_age(v: str) -> bool:
    n = _js_number(v)
    return n is not None and n == int(n) and 0 <= n <= 120


ALLOWED_COUNTRIES = ["Korea", "Mongolia", "Vietnam", "India", "South Africa",
                     "Philippines", "Indonesia", "Myanmar", "Bangladesh", "Other"]
DIAGNOSIS_TYPES = ["DS-TB", "MDR-TB", "Pre-XDR-TB", "XDR-TB"]
OUTCOMES = ["Cured", "Completed", "Failed", "Died", "LTFU", "Ongoing"]
DST_VALUES = ["S", "R", "I"]
MICROSCOPY = ["Negative", "Scanty", "1+", "2+", "3+"]
DRUG_CODES = ["H", "R", "E", "Z", "S", "Lfx", "Mfx", "Ofx", "Am", "Km", "Cm", "Bdq", "Dlm", "Pa", "Lzd", "Cfz", "Cs", "PAS"]
ROUTES = ["Oral", "Injectable", "IV"]
SPECIMEN_TYPES = ["Sputum", "BAL", "Tissue", "Other"]
CULTURE_METHODS = ["LJ", "MGIT", "Both"]
CULTURE_RESULTS = ["Negative", "Positive", "Contaminated"]
GENEXPERT_MTB = ["Detected", "Not detected", "Invalid"]
GENEXPERT_RIF = ["Detected", "Not detected", "Indeterminate"]
DST_METHODS = ["Phenotypic", "LPA-FirstLine", "LPA-SecondLine"]
SEQ_PLATFORMS_CSV = ["Illumina", "ONT", "PacBio", "Other"]
SEQ_PLATFORMS_FORM = ["Illumina MiSeq", "Illumina NextSeq", "Illumina HiSeq", "ONT MinION", "ONT GridION", "PacBio", "Other"]
SEQ_METHODS = ["WGS", "Targeted"]
LINEAGES = ["Beijing", "LAM", "T", "H", "X", "EAI", "CAS", "Other", "Unknown"]
MODALITIES = ["CXR", "CT", "PET-CT"]
LOCATIONS = ["Left", "Right", "Bilateral"]
SEVERITIES = ["Mild", "Moderate", "Severe"]
TB_ACTIVITY = ["Active", "Inactive", "Indeterminate"]
SEXES = ["Male", "Female"]
SITES_OF_DISEASE = ["Pulmonary", "Extrapulmonary", "Both"]
COMORBIDITIES = ["HIV", "Diabetes", "Hepatitis B", "Hepatitis C", "Renal Disease", "COPD", "Cancer", "None"]
FORM_COUNTRIES = ["Korea", "Mongolia", "Vietnam", "India", "South Africa", "Other"]

# 레지멘 약물 목록·표준 템플릿 (RegimenUpload.tsx)
DRUG_GROUPS = [
    (("1차 약제", "First-line Drugs"), [("Isoniazid", "H"), ("Rifampicin", "R"), ("Ethambutol", "E"), ("Pyrazinamide", "Z"), ("Streptomycin", "S")]),
    (("플루오로퀴놀론", "Fluoroquinolones"), [("Levofloxacin", "Lfx"), ("Moxifloxacin", "Mfx")]),
    (("2차 주사제", "Second-line Injectables"), [("Amikacin", "Am"), ("Kanamycin", "Km"), ("Capreomycin", "Cm")]),
    (("신약", "New Drugs"), [("Bedaquiline", "Bdq"), ("Delamanid", "Dlm"), ("Pretomanid", "Pa"), ("Linezolid", "Lzd")]),
    (("기타", "Other"), [("Clofazimine", "Cfz"), ("Cycloserine", "Cs"), ("PAS", "PAS")]),
]
DRUG_CODE_BY_NAME = {name: code for _, drugs in DRUG_GROUPS for name, code in drugs}
REGIMEN_TEMPLATES = [
    (("2HRZE/4HR (DS-TB 표준)", "2HRZE/4HR (DS-TB standard)"),
     [("Isoniazid", "H", 0, 180), ("Rifampicin", "R", 0, 180), ("Ethambutol", "E", 0, 60), ("Pyrazinamide", "Z", 0, 60)]),
    (("Short MDR-TB (9-11개월)", "Short MDR-TB (9-11 months)"),
     [("Bedaquiline", "Bdq", 0, 168), ("Levofloxacin", "Lfx", 0, 280), ("Linezolid", "Lzd", 0, 280),
      ("Ethambutol", "E", 0, 280), ("Pyrazinamide", "Z", 0, 120)]),
    (("BPaL regimen", "BPaL regimen"),
     [("Bedaquiline", "Bdq", 0, 182), ("Pretomanid", "Pa", 0, 182), ("Linezolid", "Lzd", 0, 182)]),
    (("Longer MDR-TB regimen (18-20개월)", "Longer MDR-TB regimen (18-20 months)"),
     [("Bedaquiline", "Bdq", 0, 168), ("Levofloxacin", "Lfx", 0, 560), ("Linezolid", "Lzd", 0, 560),
      ("Clofazimine", "Cfz", 0, 560), ("Cycloserine", "Cs", 0, 560), ("Ethambutol", "E", 0, 560), ("Pyrazinamide", "Z", 0, 240)]),
]
DRUG_COLORS = {"H": "#60A5FA", "R": "#F87171", "E": "#FBBF24", "Z": "#4ADE80", "S": "#C084FC", "Lfx": "#22D3EE",
               "Mfx": "#2DD4BF", "Am": "#FB923C", "Km": "#FB7185", "Cm": "#F472B6", "Bdq": "#818CF8", "Dlm": "#A78BFA",
               "Pa": "#34D399", "Lzd": "#38BDF8", "Cfz": "#A3E635", "Cs": "#E879F9", "PAS": "#FACC15"}

# 검사실 DST 약제 (LabUpload.tsx DST_DRUGS) — 폼 키는 소문자(CSV 컬럼과 같은 이름)
DST_DRUG_GROUPS = [
    (("1차 약제", "First-line Drugs"), ["Isoniazid", "Rifampicin", "Ethambutol", "Pyrazinamide", "Streptomycin"]),
    (("플루오로퀴놀론", "Fluoroquinolones"), ["Levofloxacin", "Moxifloxacin", "Ofloxacin"]),
    (("2차 주사제", "Second-line Injectables"), ["Amikacin", "Kanamycin", "Capreomycin"]),
    (("신약", "New Drugs"), ["Bedaquiline", "Linezolid", "Clofazimine"]),
]
DST_FORM_DRUGS = [d for _, ds in DST_DRUG_GROUPS for d in ds]
BIOCHEM_TESTS = ["ALT", "AST", "Bilirubin", "Creatinine", "Albumin", "Hemoglobin", "WBC", "Platelet"]
BIOCHEM_UNITS = {"ALT": "U/L", "AST": "U/L", "Bilirubin": "mg/dL", "Creatinine": "mg/dL", "Albumin": "g/dL"}

# 영상 판독소견 (ImagingUpload.tsx FINDINGS)
FINDING_GROUPS = [
    (("침윤성", "Infiltrative"), ["Infiltrate", "Consolidation", "Ground-glass opacity"]),
    (("결절성", "Nodular"), ["Nodule", "Mass", "Miliary pattern"]),
    (("공동성", "Cavitary"), ["Cavity", "Cavitary lesion"]),
    (("섬유화", "Fibrotic"), ["Fibrosis", "Calcification", "Scarring"]),
    (("흉수/기타", "Pleural Effusion/Other"), ["Pleural effusion", "Pneumothorax", "Lymphadenopathy", "Atelectasis"]),
]
FINDINGS = [f for _, fs in FINDING_GROUPS for f in fs]

# 폼 선택지 화면 표시명 (ClinicalUpload VALUE_LABEL)
VALUE_LABEL = {
    "Male": ("남성", "Male"), "Female": ("여성", "Female"), "Korea": ("한국", "Korea"), "Mongolia": ("몽골", "Mongolia"),
    "Vietnam": ("베트남", "Vietnam"), "India": ("인도", "India"), "South Africa": ("남아공", "South Africa"),
    "Other": ("기타", "Other"), "Pulmonary": ("폐결핵", "Pulmonary"), "Extrapulmonary": ("폐외결핵", "Extrapulmonary"),
    "Both": ("모두", "Both"), "HIV": ("HIV", "HIV"), "Diabetes": ("당뇨", "Diabetes"), "Hepatitis B": ("B형 간염", "Hepatitis B"),
    "Hepatitis C": ("C형 간염", "Hepatitis C"), "Renal Disease": ("신장질환", "Renal Disease"), "COPD": ("COPD", "COPD"),
    "Cancer": ("암", "Cancer"), "None": ("없음", "None"), "Cured": ("완치", "Cured"), "Completed": ("치료완료", "Completed"),
    "Failed": ("실패", "Failed"), "Died": ("사망", "Died"), "LTFU": ("추적 소실(LTFU)", "LTFU"), "Ongoing": ("치료 중", "Ongoing"),
}

# 임상 폼 필드 라벨 (ClinicalUpload FIELD_LABEL) — 오류 목록에 항목명을 붙인다
CLINICAL_FIELD_LABEL = {
    "condition_id": ("대상자 ID", "Condition ID"), "registration_date": ("등록일", "Registration Date"),
    "age": ("나이", "Age"), "sex": ("성별", "Sex"), "country": ("국적", "Nationality"),
    "site": ("등록기관", "Registering Institution"), "diagnosis_type": ("진단 유형", "Diagnosis Type"),
    "diagnosis_date": ("진단일", "Diagnosis Date"), "site_of_disease": ("질병 부위", "Site of Disease"),
    "treatment_outcome": ("치료 결과", "Treatment Outcome"),
}


def _in(options):
    return lambda v: v in options


# ─── CSV 템플릿별 값 규칙 (uploadSchemas.ts validationRules) ───
RULES = {
    "clinical": {
        "age": is_valid_age, "sex": _in(SEXES), "country": _in(ALLOWED_COUNTRIES),
        "diagnosis_type": _in(DIAGNOSIS_TYPES), "diagnosis_date": is_valid_date, "registration_date": is_valid_date,
        "treatment_outcome": _in(OUTCOMES), "outcome_date": is_valid_date,
    },
    "regimen": {
        "drug_code": _in(DRUG_CODES),
        "start_day": lambda v: is_int_value(v) and _js_number(v) >= 0,
        "end_day": lambda v: is_int_value(v) and _js_number(v) > 0,
        "route": _in(ROUTES),
    },
    "lab": {
        "specimen_date": is_valid_date, "specimen_type": _in(SPECIMEN_TYPES), "microscopy_result": _in(MICROSCOPY),
        "culture_method": _in(CULTURE_METHODS), "culture_result": _in(CULTURE_RESULTS),
        "genexpert_mtb": _in(GENEXPERT_MTB), "genexpert_rif": _in(GENEXPERT_RIF), "dst_method": _in(DST_METHODS),
        **{d: _in(DST_VALUES) for d in ["isoniazid", "rifampicin", "ethambutol", "pyrazinamide", "streptomycin",
                                        "levofloxacin", "moxifloxacin", "amikacin", "kanamycin", "capreomycin",
                                        "bedaquiline", "linezolid"]},
    },
    "genomic": {
        "platform": _in(SEQ_PLATFORMS_CSV), "method": _in(SEQ_METHODS),
        "coverage": lambda v: _js_number(v) is not None and _js_number(v) > 0,
        "sra_accession": lambda v: bool(re.match(r"^(SRR|ERR)\d+$", v)),
        "lineage": _in(LINEAGES),  # 원본 폼 선택지와 같은 규칙을 CSV 에도 적용(리뷰 결정)
    },
    "imaging": {
        "modality": _in(MODALITIES), "study_date": is_valid_date, "location": _in(LOCATIONS), "severity": _in(SEVERITIES),
    },
}
# 웹 폼 전용 추가 규칙(폼에만 있는 항목·선택지) — 폼 값도 서버가 최종 판정한다
FORM_RULES = {
    "clinical": {"site_of_disease": _in(SITES_OF_DISEASE), "previous_treatment_count": lambda v: is_int_value(v) and _js_number(v) >= 1},
    "regimen": {"drug_name": _in(list(DRUG_CODE_BY_NAME))},
    "lab": {"microscopy_date": is_valid_date, "culture_date": is_valid_date,
            "days_to_positivity": lambda v: is_int_value(v) and _js_number(v) >= 0,
            "ofloxacin": _in(DST_VALUES), "clofazimine": _in(DST_VALUES)},
    "genomic": {"platform": _in(SEQ_PLATFORMS_FORM)},
    "imaging": {"tb_activity": _in(TB_ACTIVITY)},
}

TEMPLATE_KEYS = {"clinical": "clinicalCsvTemplate", "regimen": "regimenCsvTemplate", "lab": "labCsvTemplate",
                 "genomic": "genomicMetaCsvTemplate", "imaging": "imagingMetaCsvTemplate"}


@lru_cache(maxsize=1)
def _templates() -> dict:
    return json.loads((settings.SEED_DIR / "uploadSchemas.json").read_text(encoding="utf-8"))


def template(domain: str) -> dict:
    t = _templates()[TEMPLATE_KEYS[domain]]
    return {"type": t["type"], "required": list(t["requiredColumns"]), "optional": list(t["optionalColumns"]),
            "samples": list(t["sampleData"])}


def safe_columns(domain: str) -> list[str]:
    """표준 서식 컬럼(코드값 항목)은 성명·주소 탐지에서 제외한다 (원본 safeColumns)."""
    t = template(domain)
    extra = {"clinical": ["previous_treatment_count"], "regimen": ["drug_name"],
             "lab": ["microscopy_date", "culture_date", "days_to_positivity", "ofloxacin", "clofazimine"],
             "genomic": [], "imaging": ["tb_activity"]}[domain]
    return t["required"] + t["optional"] + extra


# ─── 화면 정의 ───
DOMAIN_META = {
    "clinical": {"name": ("환자 임상 데이터", "Patient Clinical Data"), "short": ("임상", "Clinical"),
                 "title": ("환자 임상 데이터 업로드", "Patient Clinical Data Upload"),
                 "desc": ("인구통계, 진단정보, 동반질환, 치료결과 데이터", "Demographics, diagnosis, comorbidities and treatment outcome data"),
                 "subtitle": ("인구통계, 진단정보, 동반질환, 치료결과", "Demographics, diagnosis, comorbidities, treatment outcome"),
                 "icon": "clipboard-list", "color": "#1B3A5C", "formats": ["CSV", "web"]},
    "regimen": {"name": ("치료 레지멘", "Treatment Regimen"), "short": ("레지멘", "Regimen"),
                "title": ("치료 레지멘 업로드", "Treatment Regimen Upload"),
                "desc": ("약물별 투약 기간 및 용량 데이터", "Dosing period and dose data by drug"),
                "subtitle": ("약물별 투약 기간 및 용량 데이터", "Dosing period and dose data by drug"),
                "icon": "pill", "color": "#0D9488", "formats": ["CSV", "web"]},
    "lab": {"name": ("검사실 데이터", "Lab Data"), "short": ("검사실", "Lab"),
            "title": ("검사실 데이터 업로드", "Lab Data Upload"),
            "desc": ("도말, 배양, DST, GeneXpert, 생화학 검사 결과", "Smear, culture, DST, GeneXpert and biochemistry results"),
            "subtitle": ("도말, 배양, DST, GeneXpert, 생화학 검사 결과", "Smear, culture, DST, GeneXpert and biochemistry results"),
            "icon": "test-tube", "color": "#F59E0B", "formats": ["CSV", "web"]},
    "genomic": {"name": ("유전체 데이터", "Genomic Data"), "short": ("유전체", "Genomic"),
                "title": ("유전체 데이터 업로드", "Genomic Data Upload"),
                "desc": ("Mtb 전장유전체 시퀀스 및 메타데이터", "Mtb whole-genome sequences and metadata"),
                "subtitle": ("Mtb 전장유전체 시퀀스 및 메타데이터", "Mtb whole-genome sequences and metadata"),
                "icon": "dna", "color": "#9333EA", "formats": ["FASTA", "FASTQ", "CSV"]},
    "imaging": {"name": ("영상 데이터", "Imaging Data"), "short": ("영상", "Imaging"),
                "title": ("영상 데이터 업로드", "Imaging Data Upload"),
                "desc": ("흉부 X선(CXR), CT, PET-CT 영상 및 판독소견", "Chest X-ray (CXR), CT and PET-CT images and radiology reports"),
                "subtitle": ("흉부 X선(CXR), CT, PET-CT 영상 및 판독소견", "Chest X-ray (CXR), CT and PET-CT images and radiology reports"),
                "icon": "scan-line", "color": "#F43F5E", "formats": ["DICOM", "JPG", "CSV"]},
}

_STD_STEPS = [("method", ("업로드 방식 선택", "Upload Method")), ("data", ("데이터 입력/검증", "Data Entry/Validation")),
              ("deid", ("비식별화 확인", "De-identification")), ("done", ("제출 완료", "Submitted"))]
STEPS = {
    "clinical": _STD_STEPS, "regimen": _STD_STEPS, "lab": _STD_STEPS,
    "genomic": [("method", ("업로드 방식 선택", "Upload Method")), ("files", ("시퀀스 파일 업로드", "Sequence Files")),
                ("data", ("메타데이터 입력", "Metadata")), ("deid", ("비식별화 확인", "De-identification")),
                ("done", ("제출", "Submit"))],
    "imaging": [("method", ("업로드 방식 선택", "Upload Method")), ("files", ("영상 파일 업로드", "Image Files")),
                ("dicom", ("DICOM 비식별화 검증", "DICOM De-identification")), ("data", ("어노테이션 입력", "Annotation")),
                ("deid", ("비식별화 확인", "De-identification")), ("done", ("제출", "Submit"))],
}
MODES = {"clinical": ["form", "csv"], "regimen": ["form", "csv"], "lab": ["form", "csv"],
         "genomic": ["individual", "batch"], "imaging": MODALITIES}


def step_index(domain: str, key: str) -> int:
    return [k for k, _ in STEPS[domain]].index(key)


def step_key(domain: str, idx: int) -> str:
    steps = STEPS[domain]
    return steps[max(0, min(idx, len(steps) - 1))][0]


def method_for(domain: str, mode: str) -> str:
    if domain == "imaging":
        return "file"
    if domain == "genomic":
        return "csv" if mode == "batch" else "form"
    return mode


def uses_csv(domain: str, mode: str) -> bool:
    return method_for(domain, mode) == "csv"


# 업로드 화면 경로 (MyUploadsTab 의 수정 버튼 등)
def upload_path(domain: str) -> str:
    return f"/upload/{domain}"


# 모달리티별 업로드 제한 키 (settings.UPLOAD_LIMITS)
def image_limit_key(modality: str) -> str:
    return "image_cxr" if modality == "CXR" else "image_volume"


# DICOM 비식별화 검사 대상 태그 (ImagingUpload DICOM_TAGS) — (group, element, 이름, 식별정보 여부)
DICOM_TAGS = [
    (0x0010, 0x0010, "Patient Name", True),
    (0x0010, 0x0020, "Patient ID", True),
    (0x0010, 0x0030, "Patient Birth Date", True),
    (0x0010, 0x1000, "Other Patient IDs", True),
    (0x0008, 0x0080, "Institution Name", False),  # 원본도 기관명은 '안전'으로 본다
    (0x0008, 0x0090, "Referring Physician", True),
]

# 원본 DeidentificationChecker CHECK_ITEMS
DEID_CHECK_ITEMS = [
    ("환자 성명이 제거되었습니다", "Patient names have been removed"),
    ("주민등록번호/여권번호가 제거되었습니다", "Resident registration / passport numbers have been removed"),
    ("상세 주소(시/군/구 이하)가 제거되었습니다", "Detailed addresses (below city/county/district level) have been removed"),
    ("연락처(전화번호/이메일)가 제거되었습니다", "Contact details (phone numbers / email) have been removed"),
    ("의료기록번호가 익명화되었습니다", "Medical record numbers have been anonymized"),
    ("영상 DICOM 헤더의 환자 식별정보가 제거되었습니다", "Patient identifiers in imaging DICOM headers have been removed"),
    ("날짜는 등록일 기준 상대적 일수로 변환되었습니다", "Dates have been converted to relative days from the registration date"),
]

# 원본 ReviewDashboard checklist
REVIEW_CHECK_ITEMS = [
    ("데이터 형식이 표준을 준수합니다", "Data format complies with the standard"),
    ("필수 필드가 모두 입력되었습니다", "All required fields are filled in"),
    ("비식별화가 적절히 수행되었습니다", "De-identification has been properly performed"),
    ("데이터 값의 범위가 합리적입니다", "Data values are within reasonable ranges"),
    ("기존 데이터와 중복이 없습니다", "There are no duplicates with existing data"),
]

STATUS_LABEL = {
    "draft": ("임시저장", "Draft"), "revision": ("수정요청", "Revision Requested"), "pending": ("검증대기", "Pending"),
    "approved": ("승인", "Approved"), "rejected": ("반려", "Rejected"),
}
STATUS_VARIANT = {"draft": "neutral", "revision": "neutral", "pending": "warning", "approved": "success", "rejected": "danger"}
STATUS_ICON = {"draft": "pencil", "revision": "triangle-alert", "pending": "clock", "approved": "circle-check", "rejected": "circle-x"}
AUTO_LABEL = {"pass": ("통과", "Pass"), "warning": ("경고", "Warning"), "fail": ("실패", "Fail")}
AUTO_STYLE = {"pass": ("text-success", "circle-check"), "warning": ("text-warning", "triangle-alert"), "fail": ("text-danger", "circle-x")}
PRIORITY = {"fail": ("high", ("높음", "High")), "warning": ("medium", ("보통", "Medium")), "pass": ("low", ("낮음", "Low"))}


# ─── 길이·형식 (DB 컬럼 max_length 와 맞춘다 — 넘으면 error, 조용히 자르지 않는다) ───
MAX_LENGTH = {
    "condition_id": 40, "site": 100, "specimen_id": 60, "drug_name": 60, "drug_code": 10, "dosage": 40,
    "route": 20, "platform": 40, "method": 20, "sra_accession": 40, "lineage": 40, "study_description": 200,
    "tb_activity": 20, "impression": 2000, "comorbidities": 500, "site_of_disease": 20,
}
SUB_MAX_LENGTH = {  # 하위 표 항목
    "biochemistry": {"test": 60, "value": 30, "unit": 20},
    "findings": {"finding": 60, "location": 20, "severity": 20, "description": 200},
}
ID_FIELDS = ("condition_id", "specimen_id")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
