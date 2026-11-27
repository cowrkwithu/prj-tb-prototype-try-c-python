"""서버측 검증 규칙 · CSV 인코딩 · 개인정보 자동 탐지 · DICOM 헤더 판독."""
import io

import pytest

from apps.uploads.services import csvio, deidentify, dicom, schemas, validators

from .conftest import sample_bytes

pytestmark = pytest.mark.django_db


def _row(domain, **kw):
    base = {
        "clinical": {"condition_id": "TB-X-1", "age": "45", "sex": "Male", "country": "Korea", "site": "SNUBH",
                     "diagnosis_type": "MDR-TB", "diagnosis_date": "2025-08-15", "registration_date": "2025-08-20",
                     "site_of_disease": "Pulmonary", "treatment_outcome": "Ongoing"},
        "regimen": {"condition_id": "KR-2024-0001", "drug_name": "Isoniazid", "drug_code": "H", "start_day": "0", "end_day": "180"},
        "lab": {"condition_id": "KR-2024-0001", "specimen_id": "SP-1", "specimen_date": "2025-01-01", "specimen_type": "Sputum"},
        "genomic": {"condition_id": "KR-2024-0001", "specimen_id": "SP-1", "platform": "Illumina", "method": "WGS"},
        "imaging": {"condition_id": "KR-2024-0001", "modality": "CXR", "study_date": "2025-01-01"},
    }[domain]
    return {**base, **kw}


def _sev(msgs, field):
    return [m["severity"] for m in msgs if m["field"] == field]


# ─── 원본 validators.ts / uploadSchemas.ts 규칙 대표 케이스 ───
def test_clinical_rules():
    assert validators.validate_row("clinical", _row("clinical"), 1) == []
    msgs = validators.validate_row("clinical", _row("clinical", age="150", sex="M", country="Japan",
                                                    diagnosis_type="MDR", registration_date="2025/08/20"), 1)
    for f in ("age", "sex", "country", "diagnosis_type", "registration_date"):
        assert _sev(msgs, f) == ["error"], f  # 필수 컬럼의 값 오류 → error
    msgs = validators.validate_row("clinical", _row("clinical", outcome_date="2026-13-01"), 1)
    assert _sev(msgs, "outcome_date") == ["warning"]  # 선택 컬럼의 값 오류 → warning (원본 규칙)
    msgs = validators.validate_row("clinical", _row("clinical", site=""), 1)
    assert msgs[0]["ko"] == "필수 항목 누락: site" and msgs[0]["en"] == "Missing required field: site"
    # 원본 폼 규칙: 진단일은 등록일 이전
    msgs = validators.validate_row("clinical", _row("clinical", diagnosis_date="2025-09-01"), 1)
    assert any(m["ko"] == "진단일은 등록일 이전이어야 합니다" for m in msgs)
    # age 경계
    assert validators.validate_row("clinical", _row("clinical", age="0"), 1) == []
    assert _sev(validators.validate_row("clinical", _row("clinical", age="12.5"), 1), "age") == ["error"]


def test_regimen_rules():
    assert validators.validate_row("regimen", _row("regimen"), 1) == []
    msgs = validators.validate_row("regimen", _row("regimen", drug_code="RIF", end_day="0", route="Oral2"), 1)
    assert "error" in _sev(msgs, "drug_code") and _sev(msgs, "end_day") == ["error"]
    assert _sev(msgs, "route") == ["warning"]
    msgs = validators.validate_row("regimen", _row("regimen", start_day="100", end_day="60"), 1)
    assert any(m["field"] == "end_day" and m["severity"] == "error" for m in msgs)


def test_lab_and_genomic_and_imaging_rules():
    msgs = validators.validate_row("lab", _row("lab", microscopy_result="4+", isoniazid="RES", specimen_type="Blood"), 1)
    assert _sev(msgs, "microscopy_result") == ["warning"] and _sev(msgs, "isoniazid") == ["warning"]
    assert _sev(msgs, "specimen_type") == ["error"]
    msgs = validators.validate_row("genomic", _row("genomic", platform="illumina", sra_accession="ABC1", coverage="-3"), 1)
    assert _sev(msgs, "platform") == ["error"] and _sev(msgs, "sra_accession") == ["warning"] and _sev(msgs, "coverage") == ["warning"]
    # 폼은 폼 선택지(Illumina MiSeq …)로 판정한다
    assert validators.validate_row("genomic", _row("genomic", platform="Illumina MiSeq"), 1, form=True) == []
    msgs = validators.validate_row("imaging", _row("imaging", modality="XR", findings=[{"finding": "Tumor"}]), 1, form=True)
    assert _sev(msgs, "modality") == ["error"] and any(m["field"] == "findings[1]" for m in msgs)


def test_cross_row_rules(seeded_patients):
    rows = [_row("clinical", condition_id="KR-2024-0001"), _row("clinical", condition_id="NEW-1"), _row("clinical", condition_id="NEW-1")]
    res = validators.validate_rows("clinical", rows)
    assert res[0]["status"] == "warning"  # 기존 케이스 갱신 안내
    assert res[1]["status"] == res[2]["status"] == "error"  # 파일 안 중복
    res = validators.validate_rows("lab", [_row("lab", condition_id="NOPE")])
    assert res[0]["status"] == "warning" and "등록되지 않은" in res[0]["messages"][0]["ko"]


def test_classify_dst_matches_original():
    assert validators.classify_dst({"Isoniazid": "R", "Rifampicin": "R"})[1] == "MDR-TB"
    assert validators.classify_dst({"Isoniazid": "R", "Rifampicin": "R", "Moxifloxacin": "R"})[1] == "Pre-XDR-TB"
    assert validators.classify_dst({"Isoniazid": "R", "Rifampicin": "R", "Ofloxacin": "R", "Amikacin": "R"})[1] == "XDR-TB"
    assert validators.classify_dst({"Rifampicin": "R"})[0] == "단약제 내성"
    assert validators.classify_dst({})[1] == "Susceptible (DS-TB)"


# ─── CSV 인코딩 3종 ───
CSV_TEXT = "condition_id,age,sex,country,site,diagnosis_type,diagnosis_date,registration_date,site_of_disease,treatment_outcome,comorbidities\n" \
           "TB-ENC-1,45,Male,Korea,서울대병원,MDR-TB,2024-01-10,2024-01-15,Pulmonary,Cured,당뇨\n"


@pytest.mark.parametrize("encoding,label", [("utf-8", "utf-8"), ("utf-8-sig", "utf-8-sig"), ("cp949", "cp949")])
def test_csv_encodings(encoding, label):
    parsed = csvio.parse(CSV_TEXT.encode(encoding))
    assert parsed["encoding"] == label
    assert parsed["headers"][0] == "condition_id"  # BOM 이 헤더에 남지 않는다
    assert parsed["rows"][0]["site"] == "서울대병원" and parsed["rows"][0]["comorbidities"] == "당뇨"


def test_csv_parse_trims_and_skips_empty_lines():
    parsed = csvio.parse(b" condition_id , age \n  A1 , 3 \n,\n\nA2,4\n")
    assert parsed["headers"] == ["condition_id", "age"]
    assert parsed["rows"] == [{"condition_id": "A1", "age": "3"}, {"condition_id": "A2", "age": "4"}]
    with pytest.raises(csvio.CsvError):
        csvio.parse(b"")
    with pytest.raises(csvio.CsvError):
        csvio.parse(b"a,b\x00c")


def test_template_csv_roundtrip():
    text = csvio.template_csv("clinical")
    assert text.startswith("﻿")
    parsed = csvio.parse(text.encode("utf-8"))
    assert parsed["headers"] == schemas.template("clinical")["required"] + schemas.template("clinical")["optional"]
    assert len(parsed["rows"]) == 3 and parsed["rows"][2]["comorbidities"] == "HIV,Hepatitis B"


def test_report_csv_neutralizes_formulas():
    out = csvio.report_csv([{"row": 1, "field": "x", "value": "=HYPERLINK(1)", "severity": "error", "ko": "k", "en": "e"}], "ko")
    assert "'=HYPERLINK(1)" in out


# ─── 개인정보 자동 탐지 (static/samples) ───
@pytest.mark.parametrize("domain,name", [("clinical", "clinical_with_pii.csv"), ("clinical", "beta_clinical_identified.csv"),
                                         ("lab", "lab_with_pii.csv"), ("genomic", "genomic_with_pii.csv")])
def test_pii_samples_detected(domain, name):
    rows = csvio.parse(sample_bytes(name))["rows"]
    rep = deidentify.scan_rows(rows, schemas.safe_columns(domain))
    assert rep["blocking"] and rep["high"] > 0
    for t in ("rrn", "phone", "email", "name", "address"):
        assert rep["count_by_type"][t] > 0, t
    # 원문은 남기지 않는다(마스킹)
    flat = repr(rep)
    for raw in ("900101-1234567", "010-1234-5678", "hong@hospital.kr", "홍길동"):
        assert raw not in flat


@pytest.mark.parametrize("domain,name", [("clinical", "clinical_deidentified.csv"), ("clinical", "beta_clinical_deidentified.csv"),
                                         ("lab", "lab_deidentified.csv"), ("genomic", "genomic_deidentified.csv")])
def test_pii_deidentified_samples_pass(domain, name):
    rows = csvio.parse(sample_bytes(name))["rows"]
    rep = deidentify.scan_rows(rows, schemas.safe_columns(domain))
    assert rep["clean"] and not rep["blocking"], rep["detections"]
    assert "개인정보 0건" in deidentify.summary_text(rep, "ko")


def test_pii_medium_in_free_text_and_masking():
    rep = deidentify.scan_rows([{"notes": "보호자 김민수 확인 바람", "site": "서울대병원"}], ["site"])
    assert not rep["blocking"] and rep["medium"] == 1 and rep["detections"][0]["masked"] == "김*수"
    assert deidentify.mask_value("hong@hospital.kr") == "ho********kr"
    # 주민번호와 겹치는 전화번호 오탐 억제
    assert deidentify.find_phones("900101-1234567") == []


# ─── DICOM 헤더 판독 ───
@pytest.mark.parametrize("explicit", [True, False])
def test_dicom_header_inspection(explicit):
    data = dicom.build_minimal_dicom({(0x0010, 0x0010): "Hong^Gildong", (0x0008, 0x0080): "SNUBH"}, explicit=explicit)
    info = dicom.inspect(io.BytesIO(data))
    assert info["parsed"]
    tags = {t["name"]: t for t in info["tags"]}
    assert tags["Patient Name"]["risky"] and tags["Patient Name"]["value"] == "Hong Gildong"
    assert not tags["Institution Name"]["risky"] and not tags["Patient ID"]["value"]
    clean = dicom.inspect(io.BytesIO(dicom.build_minimal_dicom({(0x0008, 0x0080): "SNUBH"}, explicit=explicit)))
    assert not any(t["risky"] for t in clean["tags"])
    assert dicom.inspect(io.BytesIO(b"\x00" * 200))["parsed"] is False
