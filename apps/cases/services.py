"""환자 시드 적재 (data/seed/patients.json ← 원본 src/data/patients.ts, scripts/convert_mock_ts.py)."""
import json
from datetime import date
from pathlib import Path

from django.conf import settings
from django.db import transaction

from apps.core.seeding import is_seeded, mark_seeded, reset_sequences

from .models import DSTResult, Patient

DATASET = "patients"


def patient_fields_from_mock(p: dict) -> dict:
    d, c, g, im, lab = p["demographics"], p["clinical"], p["genomics"], p["imaging"], p["lab"]
    return {
        "age": d["age"], "sex": d["sex"], "country": d["country"], "site": d["site"],
        "diagnosis_type": c["diagnosisType"], "treatment_outcome": c["treatmentOutcome"],
        "treatment_duration": c["treatmentDuration"], "comorbidities": list(c["comorbidities"]),
        "has_sequence": g["hasSequence"], "sra_id": g["sraId"], "lineage": g["lineage"],
        "resistance_mutations": list(g["resistanceMutations"]),
        "has_cxr": im["hasCxr"], "has_ct": im["hasCt"], "has_pet_ct": im["hasPetCt"],
        "cxr_count": im["cxrCount"], "ct_count": im["ctCount"], "pet_ct_count": im["petCtCount"],
        "cxr_findings": list(im["cxrFindings"]),
        "microscopy": lab["microscopy"], "culture": lab["culture"],
        "registration_date": date.fromisoformat(p["registrationDate"]),
        "last_update_date": date.fromisoformat(p["lastUpdateDate"]),
    }


def load_patients(path: Path | None = None, force: bool = False) -> dict:
    """환자 시드를 1회 적재한다.

    - 이미 적재 기록(SeedLog)이 있으면 건너뛴다 → 사용자가 지운 환자가 되살아나지 않는다.
    - force=True 면 없는 condition_id 만 다시 만든다(기존 환자는 수정하지 않음).
    """
    if is_seeded(DATASET) and not force:
        return {"created": 0, "existing": 0, "skipped": True, "total": Patient.objects.count()}
    path = Path(path or settings.SEED_DIR / "patients.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    created = existing = 0
    with transaction.atomic():
        have = set(Patient.objects.values_list("condition_id", flat=True))
        used_ids = set(Patient.objects.values_list("id", flat=True))
        for p in data:
            if p["conditionId"] in have:
                existing += 1
                continue
            # 원본 id 를 유지한다(코호트 patientIds 가 이 id 를 참조). 이미 쓰인 id 면 자동 부여.
            kwargs = {"id": p["id"]} if p.get("id") not in used_ids else {}
            patient = Patient.objects.create(condition_id=p["conditionId"], **kwargs,
                                             **patient_fields_from_mock(p))
            used_ids.add(patient.id)
            DSTResult.objects.bulk_create([
                DSTResult(patient=patient, drug=e["drug"], result=e["result"], order=i)
                for i, e in enumerate(p["lab"]["dstResults"])
            ])
            created += 1
        reset_sequences(Patient, DSTResult)
        result = {"created": created, "existing": existing, "skipped": False, "total": Patient.objects.count()}
        mark_seeded(DATASET, {"created": created, "existing": existing})
    return result


# ═══════════════════════════════════════════════════════════
#  케이스 에디터 (원본 src/pages/CaseEditor/*)
# ═══════════════════════════════════════════════════════════
from datetime import timedelta  # noqa: E402

from django.db.models import Q  # noqa: E402

# 원본 CaseList 의 필터 선택지 그대로 (국적은 5개국만 노출)
FILTER_COUNTRIES = ["한국", "몽골", "베트남", "인도", "남아공"]
FILTER_DIAG_TYPES = ["DS-TB", "MDR-TB", "Pre-XDR-TB", "XDR-TB"]
FILTER_OUTCOMES = ["Cured", "Completed", "Failed", "Died", "LTFU"]
PER_PAGE = 10

FIRST_LINE_DRUGS = ["INH", "RIF", "EMB", "PZA"]
SECOND_LINE_DRUGS = ["FQ", "AMK", "KAN", "CAP", "ETH", "CS", "LZD", "BDQ", "DLM"]

DATA_VALUE_EN = {
    "서울대병원": "Seoul National University Hospital", "삼성서울병원": "Samsung Medical Center",
    "국립결핵병원": "National Tuberculosis Hospital", "분당서울대병원": "Seoul National University Bundang Hospital",
    "한국생명공학연구원": "Korea Research Institute of Bioscience and Biotechnology", "국립중앙의료원": "National Medical Center",
    "서울아산병원": "Asan Medical Center", "울란바토르 국립병원": "Ulaanbaatar National Hospital", "다르항 병원": "Darkhan Hospital",
    "하노이 국립결핵연구소": "Hanoi National TB Institute", "호치민 폐병원": "Ho Chi Minh City Lung Hospital",
    "뭄바이 의과대학": "Mumbai Medical College", "케이프타운 대학병원": "Cape Town University Hospital",
    "요하네스버그 병원": "Johannesburg Hospital", "자카르타 폐병원": "Jakarta Lung Hospital", "마닐라 폐센터": "Manila Lung Center",
    "양곤 종합병원": "Yangon General Hospital", "서울대학교병원": "Seoul National University Hospital",
    "국립보건연구원": "Korea National Institute of Health", "몽골국립의료원": "National Medical Center of Mongolia",
    "베트남국립결핵병원": "Vietnam National Lung Hospital", "인도 NIRT": "NIRT, India", "델리 AIIMS": "AIIMS Delhi",
    "서울대학교": "Seoul National University", "연세대학교": "Yonsei University",
    "결절": "Nodule", "침윤": "Infiltration", "공동": "Cavity", "흉수": "Pleural Effusion", "섬유화": "Fibrosis",
    "석회화": "Calcification", "무기폐": "Atelectasis", "기관지확장": "Bronchiectasis", "결핵 병변": "TB lesion",
    "고섭취 병소": "FDG-avid focus", "좌폐": "Left Lung", "우폐": "Right Lung", "양측": "Bilateral",
    "경미": "Mild", "중등도": "Moderate", "중증": "Severe",
    "당뇨": "Diabetes", "고혈압": "Hypertension", "폐암": "Lung Cancer", "간경변": "Liver Cirrhosis",
    "알코올 의존": "Alcohol Dependence", "만성신부전": "Chronic Kidney Disease",
    "B형 간염": "Hepatitis B", "C형 간염": "Hepatitis C", "신장질환": "Renal Disease", "암": "Cancer",
    "폐결핵": "Pulmonary", "폐외결핵": "Extrapulmonary", "모두": "Both",
    **Patient.COUNTRY_EN,
}


def data_label(value, lang: str) -> str:
    """원본 i18n/dataLabels.ts dataLabel — 한국어로 저장된 값을 현재 언어로 표시."""
    if value is None:
        return ""
    return DATA_VALUE_EN.get(value, value) if lang == "en" else value


def filter_patients(params: dict, lang: str):
    """원본 CaseList 의 filtered (검색어·국적·약제내성·치료결과·영상 보유)."""
    qs = Patient.objects.all().order_by("id")
    q = (params.get("q") or "").strip()
    if q:
        ql = q.lower()
        cond = (Q(condition_id__icontains=ql) | Q(country__contains=ql) | Q(diagnosis_type__icontains=ql))
        if lang == "en":  # dataLabel(country, 'en').toLowerCase().includes(q)
            matched = [k for k, v in Patient.COUNTRY_EN.items() if ql in v.lower()]
            if matched:
                cond |= Q(country__in=matched)
        qs = qs.filter(cond)
    countries = [c for c in params.get("country", []) if c in FILTER_COUNTRIES]
    types = [c for c in params.get("type", []) if c in FILTER_DIAG_TYPES]
    outcomes = [c for c in params.get("outcome", []) if c in FILTER_OUTCOMES]
    if countries:
        qs = qs.filter(country__in=countries)
    if types:
        qs = qs.filter(diagnosis_type__in=types)
    if outcomes:
        qs = qs.filter(treatment_outcome__in=outcomes)
    if params.get("imaging"):
        qs = qs.filter(Q(has_cxr=True) | Q(has_ct=True) | Q(has_pet_ct=True))
    return qs


def paginate(qs, page: int):
    total = qs.count()
    pages = max(1, -(-total // PER_PAGE))
    page = min(max(page, 0), pages - 1)
    return list(qs[page * PER_PAGE:(page + 1) * PER_PAGE]), page, pages, total


def outcome_variant(outcome: str) -> str:
    if outcome in ("Cured", "Completed"):
        return "success"
    if outcome in ("Failed", "Died"):
        return "danger"
    return "neutral"


RESISTANCE_VARIANT = {"DS-TB": "success", "MDR-TB": "warning", "Pre-XDR-TB": "danger", "XDR-TB": "danger"}


def regimen_data(p: Patient) -> list[dict]:
    """치료 레지멘 차트 데이터.

    승인된 레지멘 업로드(Regimen)가 있으면 가장 최근 것을 쓴다(시작일·종료일 그대로).
    없으면 원본 ClinicalTab regimenData 처럼 진단 유형·치료 기간에서 파생한다.
    """
    reg = p.regimens.prefetch_related("drugs").order_by("-created_at", "-id").first()
    if reg is not None:
        rows = [{"drug": d.drug_code or d.drug_name, "name": d.drug_name, "start": d.start_day,
                 "duration": max(0, d.end_day - d.start_day)} for d in reg.drugs.all()]
        if rows:
            return rows
    d = p.treatment_duration
    rows = [{"drug": "INH", "start": 0, "duration": d}, {"drug": "RIF", "start": 0, "duration": d},
            {"drug": "EMB", "start": 0, "duration": min(60, d)}, {"drug": "PZA", "start": 0, "duration": min(60, d)}]
    if p.diagnosis_type != "DS-TB":
        rows.append({"drug": "FQ", "start": 0, "duration": d})
    return rows


def related_drug(mutation: str) -> str:
    if mutation.startswith("rpoB"):
        return "Rifampicin"
    if mutation.startswith("katG") or mutation.startswith("inhA"):
        return "Isoniazid"
    if mutation.startswith("gyrA"):
        return "Fluoroquinolones"
    if mutation.startswith("rrs"):
        return "Amikacin"
    return "Unknown"


def mutations(p: Patient) -> list[dict]:
    out = []
    for m in p.resistance_mutations or []:
        parts = m.split(" ")
        out.append({"mutation": m, "gene": parts[0], "change": parts[1] if len(parts) > 1 and parts[1] else "-",
                    "drug": related_drug(m)})
    return out


def dst_rows(p: Patient) -> dict:
    res = {d.drug: d.result for d in p.dst_results.all()}
    label = {"S": "Susceptible", "R": "Resistant", "I": "Intermediate"}
    mk = lambda drugs: [{"drug": d, "result": res.get(d), "label": label.get(res.get(d))} for d in drugs]  # noqa: E731
    return {"first": mk(FIRST_LINE_DRUGS), "second": mk(SECOND_LINE_DRUGS)}


def timeline(p: Patient, lang: str) -> list[dict]:
    """원본 TimelineTab events — 등록일 기준 상대 일자."""
    en = lang == "en"
    base = p.registration_date

    def d(n):
        return (base + timedelta(days=n)).isoformat()

    ev = [
        {"date": base.isoformat(), "label": "Patient Registered" if en else "환자 등록",
         "desc": f"Registered at {data_label(p.site, 'en')}" if en else f"{p.site}에서 등록", "color": "bg-navy"},
        {"date": d(3), "label": "Smear Microscopy" if en else "도말 검사",
         "desc": f"Result: {p.microscopy}" if en else f"결과: {p.microscopy}", "color": "bg-teal"},
        {"date": d(7), "label": "Culture" if en else "배양 검사",
         "desc": f"Result: {p.culture}" if en else f"결과: {p.culture}", "color": "bg-teal"},
        {"date": d(14), "label": "Treatment Started" if en else "치료 시작",
         "desc": f"{p.diagnosis_type} treatment regimen initiated" if en else f"{p.diagnosis_type} 치료 레지멘 개시",
         "color": "bg-info"},
    ]
    if p.has_cxr:
        if en:
            desc = "Findings: " + ", ".join(data_label(f, "en") for f in p.cxr_findings)
        else:
            desc = "소견: " + ", ".join(p.cxr_findings)
        ev.append({"date": d(30), "label": "CXR Taken" if en else "CXR 촬영", "desc": desc, "color": "bg-warning"})
    if p.has_sequence:
        ev.append({"date": d(45), "label": "Genome Sequencing" if en else "유전체 시퀀싱",
                   "desc": f"Lineage: {p.lineage}", "color": "bg-success"})
    ev.append({"date": p.last_update_date.isoformat(), "label": "Treatment Ended" if en else "치료 종료",
               "desc": (f"Result: {p.treatment_outcome} ({p.treatment_duration} days)" if en
                        else f"결과: {p.treatment_outcome} ({p.treatment_duration}일)"), "color": "bg-danger"})
    return ev
