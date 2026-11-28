"""업로드 → 검증 → 비식별화 → 제출 → 관리자 검토(승인/반려/수정요청) → 케이스·홈 반영 (test client E2E)."""
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.cases.models import BiochemResult, DSTResult, ImagingStudy, Patient, Regimen, SequenceRun
from apps.core import dashboard
from apps.uploads.models import Submission, SubmissionEvent
from apps.uploads.services import dicom

from .conftest import csv_upload, fastq_gz, upload_id_from

pytestmark = pytest.mark.django_db
DEID_ALL = [str(i) for i in range(7)]
REVIEW_ALL = [str(i) for i in range(5)]


def _submit(client, domain, uid, **extra):
    return client.post(f"/upload/{domain}/submit", {"id": uid, "deid": DEID_ALL, **extra})


def _approve(client, uid, **extra):
    return client.post(f"/admin/review/{uid}/approve", {"check": REVIEW_ALL, **extra})


def _csv_to_pending(client, domain, filename, data=None, ignore_errors=False):
    r = client.post(f"/upload/{domain}/csv", {"mode": "csv" if domain != "genomic" else "batch",
                                              "file": csv_upload(filename, data)})
    assert r.status_code == 302
    uid = upload_id_from(r)
    r = client.post(f"/upload/{domain}/proceed", {"id": uid, "from": "data", **({"ignore_errors": "1"} if ignore_errors else {})})
    assert r.status_code == 302
    return uid


CLINICAL_FORM = {
    "mode": "form", "condition_id": "TB-NEW-001", "registration_date": "2025-08-20", "age": "45", "sex": "Male",
    "country": "Mongolia", "site": "울란바토르 국립병원", "diagnosis_type": "MDR-TB", "diagnosis_date": "2025-08-15",
    "site_of_disease": "Pulmonary", "previous_treatment": "1", "previous_treatment_count": "2",
    "comorbidities": ["Diabetes", "HIV"], "treatment_outcome": "Cured", "outcome_date": "2026-02-28", "action": "next",
}


def test_clinical_csv_e2e_reflected_in_case_editor_and_home(client, demo_users, seeded_patients):
    researcher, admin = demo_users["researcher"], demo_users["admin"]
    client.force_login(researcher)
    before_total = dashboard.stat_cards()["total_cases"]
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    sub = Submission.objects.get(upload_id=uid)
    assert sub.status == "draft" and sub.total_records == 3 and sub.error_records == 0 and sub.step == 2
    page = client.get(f"/upload/clinical?id={uid}").content.decode()
    assert 'data-pii-tone="clean"' in page and "개인정보 0건" in page
    # 체크리스트 일부만 → 제출 거부 (서버 강제)
    r = client.post("/upload/clinical/submit", {"id": uid, "deid": ["0", "1"]})
    assert Submission.objects.get(pk=sub.pk).status == "draft"
    r = _submit(client, "clinical", uid, signer="담당자", confirm_date="2026-01-02")
    sub.refresh_from_db()
    assert sub.status == "pending" and sub.submitted_at and sub.deid_signer == "담당자"
    assert "관리자 검증 대기 중" in client.get(r["Location"]).content.decode()

    client.force_login(admin)
    queue = client.get("/admin/review").content.decode()
    assert uid in queue
    detail = client.get(f"/admin/review/{uid}").content.decode()
    assert "TB-KR-101" in detail and "자동 검증 리포트" in detail
    r = _approve(client, uid, comment="확인 완료")
    assert r.status_code == 302 and r["Location"] == "/admin/review"
    sub.refresh_from_db()
    assert sub.status == "approved" and sub.reviewer == admin and sub.apply_summary["created"] == 3

    p = Patient.objects.get(condition_id="TB-KR-101")
    assert (p.age, p.sex, p.country, p.diagnosis_type, p.treatment_outcome) == (45, "M", "한국", "MDR-TB", "Cured")
    assert p.comorbidities == ["당뇨"] and p.diagnosis_date == date(2024, 1, 10) and p.previous_treatment is False
    assert p.treatment_duration == (date(2024, 8, 20) - date(2024, 1, 10)).days
    # 케이스 에디터·홈에 즉시 반영
    client.force_login(researcher)
    body = client.get(f"/cases?id={p.pk}").content.decode()
    assert 'data-testid="detail-id">TB-KR-101<' in body and "폐결핵" in body and "2024-01-10" in body
    assert "TB-KR-101" in client.get("/cases?q=TB-KR-10").content.decode()
    assert dashboard.stat_cards()["total_cases"] == before_total + 3
    assert SubmissionEvent.objects.filter(submission=sub, action="approved").exists()


def test_clinical_form_with_draft_and_resume(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    # 1) 일부만 입력하고 임시저장 → 서버 draft
    r = client.post("/upload/clinical/data", {"mode": "form", "condition_id": "TB-DRAFT-1", "age": "30", "action": "draft"})
    uid = upload_id_from(r)
    sub = Submission.objects.get(upload_id=uid)
    assert sub.status == "draft" and sub.form_data["condition_id"] == "TB-DRAFT-1" and sub.error_records == 1
    # 2) 다시 열면 입력값이 채워져 있다(이어쓰기) + 내 업로드 목록에 '이어서 작성'
    page = client.get(f"/upload/clinical?id={uid}").content.decode()
    assert "TB-DRAFT-1" in page and 'data-step="data"' in page
    mine = client.get("/upload/mine").content.decode()
    assert f'data-testid="continue-{uid}"' in mine
    # 3) 오류가 있으면 다음 단계로 못 간다
    r = client.post("/upload/clinical/data", {"id": uid, "condition_id": "TB-DRAFT-1", "action": "next"})
    assert Submission.objects.get(pk=sub.pk).step == 1
    assert "입력 오류" in client.get(r["Location"]).content.decode()
    # 4) 모두 채우면 비식별화 단계 → 제출
    form = {**CLINICAL_FORM, "id": uid, "condition_id": "TB-DRAFT-1"}
    form.pop("mode")
    r = client.post("/upload/clinical/data", form)
    sub.refresh_from_db()
    assert sub.step == 2 and sub.error_records == 0 and sub.form_data["comorbidities"] == "Diabetes,HIV"
    _submit(client, "clinical", uid)
    sub.refresh_from_db()
    assert sub.status == "pending"
    # 제출 후에는 수정 불가
    r = client.post("/upload/clinical/data", {**form, "action": "draft"})
    sub.refresh_from_db()
    assert sub.status == "pending"


def test_conditionid_prefill_goes_to_form(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    body = client.get("/upload/lab?conditionId=KR-2024-0004").content.decode()
    assert 'data-step="data"' in body and 'value="KR-2024-0004"' in body
    body = client.get("/upload/imaging?conditionId=KR-2024-0004").content.decode()
    assert 'data-step="method"' in body and 'name="conditionId" value="KR-2024-0004"' in body


def test_pii_csv_blocks_submission(client, demo_users):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_with_pii.csv")
    sub = Submission.objects.get(upload_id=uid)
    assert sub.pii_blocking and sub.pii_report["high"] > 0
    page = client.get(f"/upload/clinical?id={uid}").content.decode()
    assert 'data-pii-tone="block"' in page and "900101-1234567" not in page
    r = _submit(client, "clinical", uid)
    sub.refresh_from_db()
    assert sub.status == "draft"
    assert "개인정보가 탐지되어 제출할 수 없습니다" in client.get(r["Location"]).content.decode()


def test_csv_missing_columns_and_errors(client, demo_users):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("x.csv", b"condition_id,age\nA,1\n")})
    uid = upload_id_from(r)
    sub = Submission.objects.get(upload_id=uid)
    assert sub.file_errors and "필수 컬럼 누락" in sub.file_errors[0]["ko"] and sub.total_records == 0
    client.post("/upload/clinical/proceed", {"id": uid, "from": "data"})
    assert Submission.objects.get(pk=sub.pk).step == 1
    # 값 오류 행: '오류 무시하고 진행' 체크 없이는 못 넘어간다. 체크하면 진행(오류 행은 반영 안 됨)
    bad = ("condition_id,age,sex,country,site,diagnosis_type,diagnosis_date,registration_date,site_of_disease,treatment_outcome\n"
           "TB-OK-1,40,Female,Korea,S,DS-TB,2024-01-01,2024-01-02,Pulmonary,Cured\n"
           "TB-BAD-1,150,Female,Korea,S,DS-TB,2024-01-01,2024-01-02,Pulmonary,Cured\n").encode()
    r = client.post("/upload/clinical/csv", {"id": uid, "file": csv_upload("bad.csv", bad)})
    sub.refresh_from_db()
    assert (sub.total_records, sub.error_records) == (2, 1)
    page = client.get(f"/upload/clinical?id={uid}").content.decode()
    assert 'data-testid="validation-report"' in page and "유효하지 않은 값: 150" in page
    report = client.get(f"/upload/report/{uid}.csv")
    assert report.status_code == 200 and "유효하지 않은 값: 150" in report.content.decode("utf-8-sig")
    client.post("/upload/clinical/proceed", {"id": uid, "from": "data"})
    assert Submission.objects.get(pk=sub.pk).step == 1
    client.post("/upload/clinical/proceed", {"id": uid, "from": "data", "ignore_errors": "1"})
    assert Submission.objects.get(pk=sub.pk).step == 2
    _submit(client, "clinical", uid)
    sub.refresh_from_db()
    assert sub.status == "pending" and sub.auto_result == "fail"
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    assert Patient.objects.filter(condition_id="TB-OK-1").exists()
    assert not Patient.objects.filter(condition_id="TB-BAD-1").exists()


def test_cp949_csv_upload(client, demo_users):
    client.force_login(demo_users["researcher"])
    data = ("condition_id,age,sex,country,site,diagnosis_type,diagnosis_date,registration_date,site_of_disease,treatment_outcome,comorbidities\n"
            "TB-CP-1,40,Female,Korea,서울대병원,DS-TB,2024-01-01,2024-01-02,Pulmonary,Cured,당뇨\n").encode("cp949")
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("excel.csv", data)})
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    assert sub.form_data["encoding"] == "cp949" and sub.records.get().payload["site"] == "서울대병원"


def test_regimen_form_e2e_updates_case_chart(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/regimen/data", {
        "mode": "form", "condition_id": "KR-2024-0001", "action": "next",
        "drug_name": ["Bedaquiline", "Linezolid"], "start_day": ["0", "14"], "end_day": ["168", "180"],
        "dosage": ["400mg", "600mg"], "route": ["Oral", "Oral"]})
    uid = upload_id_from(r)
    sub = Submission.objects.get(upload_id=uid)
    assert sub.step == 2 and sub.total_records == 2
    assert [rec.payload["drug_code"] for rec in sub.records.all()] == ["Bdq", "Lzd"]
    _submit(client, "regimen", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    reg = Regimen.objects.get(patient__condition_id="KR-2024-0001")
    assert [(d.drug_code, d.start_day, d.end_day) for d in reg.drugs.all()] == [("Bdq", 0, 168), ("Lzd", 14, 180)]
    body = client.get("/cases?id=1").content.decode()
    assert 'data-testid="regimen-source"' in body
    assert '"drug": "Lzd", "name": "Linezolid", "start": 14, "duration": 166' in body


def test_lab_csv_e2e_updates_dst_and_smear(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    data = ("condition_id,specimen_id,specimen_date,specimen_type,microscopy_result,culture_result,isoniazid,rifampicin,levofloxacin,moxifloxacin\n"
            "KR-2024-0001,SP-1,2025-01-01,Sputum,1+,Positive,R,R,S,R\n"
            "KR-2024-0001,SP-2,2025-03-01,Sputum,Scanty,Negative,,,,\n"
            "UNKNOWN-1,SP-9,2025-03-01,Sputum,1+,Positive,S,S,,\n").encode()
    uid = _csv_to_pending(client, "lab", "lab.csv", data)
    sub = Submission.objects.get(upload_id=uid)
    assert sub.warning_records == 1  # 등록되지 않은 대상자
    _submit(client, "lab", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    p = Patient.objects.get(condition_id="KR-2024-0001")
    assert (p.microscopy, p.culture) == ("Scanty", "Negative")  # 검체일 최신 값
    dst = {d.drug: d.result for d in p.dst_results.all()}
    assert dst["INH"] == "R" and dst["RIF"] == "R" and dst["FQ"] == "R"  # 플루오로퀴놀론은 가장 나쁜 결과
    assert p.diagnosis_type == "DS-TB"  # 원본처럼 내성 분류는 표시만 하고 진단 유형은 바꾸지 않는다
    sub.refresh_from_db()
    assert sub.apply_summary["skipped"][0]["condition_id"] == "UNKNOWN-1"
    assert sub.apply_summary["dst_profiles"][0]["en"] == "Pre-XDR-TB"
    assert "Pre-XDR-TB" in client.get(f"/admin/review/{uid}").content.decode()


def test_lab_form_biochem(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/lab/data", {
        "mode": "form", "condition_id": "KR-2024-0004", "specimen_id": "SP-77", "specimen_date": "2025-02-01",
        "specimen_type": "Sputum", "show_dst": "1", "dst_method": "Phenotypic", "dst_isoniazid": "R", "dst_ofloxacin": "R",
        "show_biochem": "1", "bc_test": ["ALT", "Albumin"], "bc_value": ["80", "3.0"], "bc_unit": ["U/L", "g/dL"],
        "bc_date": ["2025-02-01", ""], "action": "next"})
    uid = upload_id_from(r)
    _submit(client, "lab", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    bc = {b.test: b for b in BiochemResult.objects.filter(patient__condition_id="KR-2024-0004")}
    assert bc["ALT"].flag == "H" and bc["ALT"].value == "80 U/L" and bc["Albumin"].flag == "L"
    assert DSTResult.objects.get(patient__condition_id="KR-2024-0004", drug="FQ").result == "R"
    body = client.get("/cases?id=4&tab=4").content.decode()
    assert "80 U/L" in body and 'data-testid="biochem-empty"' not in body


def test_genomic_batch_e2e(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/genomic/files", {"mode": "batch", "files": [fastq_gz()], "action": "next"})
    uid = upload_id_from(r)
    sub = Submission.objects.get(upload_id=uid)
    assert sub.files.count() == 1 and sub.step == 2
    f = sub.files.get()
    assert len(f.sha256) == 64 and f.original_name == "TB-KR-101_SP-101A.fastq.gz"
    meta = b"condition_id,specimen_id,platform,method,coverage,sra_accession,lineage\nKR-2024-0002,SP-101A,Illumina,WGS,120,SRR20101011,Beijing\n"
    client.post("/upload/genomic/csv", {"id": uid, "file": csv_upload("meta.csv", meta)})
    client.post("/upload/genomic/proceed", {"id": uid, "from": "data"})
    _submit(client, "genomic", uid)
    assert Submission.objects.get(pk=sub.pk).status == "pending"
    client.force_login(demo_users["admin"])
    detail = client.get(f"/admin/review/{uid}").content.decode()
    assert f"/upload/file/{f.pk}" in detail
    dl = client.get(f"/upload/file/{f.pk}")
    assert dl.status_code == 200 and b"".join(dl.streaming_content)[:2] == b"\x1f\x8b"
    _approve(client, uid)
    p = Patient.objects.get(condition_id="KR-2024-0002")
    assert p.has_sequence and p.sra_id == "SRR20101011" and p.lineage == "Beijing"
    run = SequenceRun.objects.get(patient=p)
    assert list(run.files.values_list("original_name", flat=True)) == ["TB-KR-101_SP-101A.fastq.gz"]


def _dicom_file(name="study.dcm"):
    data = dicom.build_minimal_dicom({(0x0008, 0x0080): "SNUBH"})
    return SimpleUploadedFile(name, data, content_type="application/dicom")


def test_imaging_e2e_dicom_check_and_counts(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    p0 = Patient.objects.get(condition_id="KR-2024-0003")
    # 식별정보가 남은 DICOM → 제출 차단
    r = client.post("/upload/imaging/files", {"mode": "CXR", "condition_id": "KR-2024-0003", "study_date": "2025-05-01",
                                              "files": [SimpleUploadedFile(
                                                  "risky.dcm", dicom.build_minimal_dicom({(0x0010, 0x0010): "Kim^Chulsoo"}),
                                                  content_type="application/dicom")], "action": "next"})
    uid = upload_id_from(r)
    sub = Submission.objects.get(upload_id=uid)
    assert sub.pii_blocking and sub.step == 2
    page = client.get(f"/upload/imaging?id={uid}").content.decode()
    assert 'data-testid="dicom-risky"' in page and "Kim Chulsoo" not in page
    # 위험 파일 제거 후 깨끗한 파일로 교체
    client.post(f"/upload/imaging/files/{sub.files.get().pk}/remove", {"id": uid})
    client.post("/upload/imaging/files", {"id": uid, "condition_id": "KR-2024-0003", "study_date": "2025-05-01",
                                          "files": [_dicom_file()], "action": "next"})
    sub.refresh_from_db()
    assert not sub.pii_blocking
    client.post("/upload/imaging/proceed", {"id": uid, "from": "dicom"})
    client.post("/upload/imaging/data", {"id": uid, "study_description": "Initial CXR", "tb_activity": "Active",
                                         "ann_finding": ["Cavity"], "ann_location": ["Right"], "ann_severity": ["Severe"],
                                         "ann_description": [""], "impression": "cavitary TB", "action": "next"})
    sub.refresh_from_db()
    assert sub.step == 4
    _submit(client, "imaging", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    p = Patient.objects.get(pk=p0.pk)
    assert p.cxr_count == p0.cxr_count + 1 and p.has_cxr and "공동" in p.cxr_findings
    st = ImagingStudy.objects.get(patient=p)
    assert st.modality == "CXR" and st.findings[0]["finding"] == "Cavity" and st.files.count() == 1
    body = client.get(f"/cases?id={p.pk}&tab=1").content.decode()
    # 5단계: 영상 탭은 DB 검사 목록(RAP 과 같음) — 승인된 업로드 검사가 썸네일로, 설명은 상세 모달에
    assert f'data-image="{st.pk}"' in body and 'data-testid="thumb-upload"' in body
    assert "Initial CXR" in client.get(f"/cases/{p.pk}/images/{st.pk}").content.decode()
    assert dashboard.contribution_stats()[3]["count"] == 1  # 홈 '영상 업로드' 기여(최근 30일)


def test_reject_requires_reason_and_is_final(client, demo_users):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    client.force_login(demo_users["admin"])
    client.post(f"/admin/review/{uid}/reject", {"reason": "   "})
    assert Submission.objects.get(upload_id=uid).status == "pending"
    client.post(f"/admin/review/{uid}/reject", {"reason": "기관 코드가 비표준입니다"})
    sub = Submission.objects.get(upload_id=uid)
    assert sub.status == "rejected" and sub.review_comment == "기관 코드가 비표준입니다"
    # 반려 후 승인 시도 → 그대로 반려, 환자 생성 없음
    _approve(client, uid)
    assert Submission.objects.get(pk=sub.pk).status == "rejected" and not Patient.objects.filter(condition_id="TB-KR-101").exists()
    # 제출자: 반려 사유 확인 → 수정 후 재제출(새 draft 로 행 복사)
    client.force_login(demo_users["researcher"])
    assert "기관 코드가 비표준입니다" in client.get("/upload/mine").content.decode()
    r = client.post(f"/upload/mine/{uid}/resubmit")
    new = Submission.objects.exclude(pk=sub.pk).get()
    assert new.status == "draft" and new.resubmitted_from == sub and new.total_records == 3
    assert r["Location"] == f"/upload/clinical?id={new.upload_id}"


def test_double_approval_prevented(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    count = Patient.objects.count()
    r = _approve(client, uid)
    assert r["Location"] == f"/admin/review/{uid}"
    assert Patient.objects.count() == count
    assert SubmissionEvent.objects.filter(submission__upload_id=uid, action="approved").count() == 1
    # 서비스 수준에서도 조건부 UPDATE 로 막힌다
    from apps.uploads.services import workflow

    with pytest.raises(workflow.WorkflowError):
        workflow.approve(Submission.objects.get(upload_id=uid), demo_users["admin"], REVIEW_ALL)


def test_approve_requires_full_checklist(client, demo_users):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    client.force_login(demo_users["admin"])
    client.post(f"/admin/review/{uid}/approve", {"check": ["0", "1"]})
    assert Submission.objects.get(upload_id=uid).status == "pending"


def test_revision_request_round_trip(client, demo_users):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    client.force_login(demo_users["admin"])
    client.post(f"/admin/review/{uid}/revise", {"reason": ""})
    assert Submission.objects.get(upload_id=uid).status == "pending"
    client.post(f"/admin/review/{uid}/revise", {"reason": "site 값을 기관 코드로 바꿔 주세요"})
    sub = Submission.objects.get(upload_id=uid)
    assert sub.status == "draft" and sub.revision_requested and sub.display_status == "revision"
    client.force_login(demo_users["researcher"])
    page = client.get(f"/upload/clinical?id={uid}").content.decode()
    assert 'data-testid="revision-note"' in page and "site 값을 기관 코드로" in page
    client.post("/upload/clinical/proceed", {"id": uid, "from": "data"})
    _submit(client, "clinical", uid)
    sub.refresh_from_db()
    assert sub.status == "pending" and not sub.revision_requested


def test_permissions(client, demo_users, other_researcher):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    sub = Submission.objects.get(upload_id=uid)
    f = sub.files.get()
    # 다른 연구원: 본인 것이 아니면 404 (조회·이어쓰기·파일·리포트·삭제·제출)
    client.force_login(other_researcher)
    assert client.get(f"/upload/clinical?id={uid}").status_code == 404
    assert client.get(f"/upload/file/{f.pk}").status_code == 404
    assert client.get(f"/upload/report/{uid}.csv").status_code == 404
    assert client.post(f"/upload/mine/{uid}/delete").status_code == 404
    assert client.post("/upload/clinical/submit", {"id": uid, "deid": DEID_ALL}).status_code == 404
    assert uid not in client.get("/upload/mine").content.decode()
    # 연구원은 승인·반려·검토 화면 불가 (관리자 로그인으로 보낸다)
    for url in (f"/admin/review/{uid}/approve", f"/admin/review/{uid}/reject", f"/admin/review/{uid}/revise"):
        r = client.post(url, {"check": REVIEW_ALL, "reason": "x"})
        assert r.status_code == 302 and r["Location"].startswith("/admin/login"), url
    assert client.get(f"/admin/review/{uid}").status_code == 302
    assert Submission.objects.get(pk=sub.pk).status == "pending"
    # 비로그인
    client.logout()
    assert client.get("/upload").status_code == 302
    assert client.post("/upload/clinical/csv", {}).status_code == 302
    # CSRF: 토큰 없는 POST 는 거부
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    strict.force_login(demo_users["admin"])
    assert strict.post(f"/admin/review/{uid}/reject", {"reason": "x"}).status_code == 403
    assert Submission.objects.get(pk=sub.pk).status == "pending"


def test_my_uploads_delete_and_deletion_request(client, demo_users, django_capture_on_commit_callbacks):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("clinical_deidentified.csv")})
    draft_uid = upload_id_from(r)
    stored = Submission.objects.get(upload_id=draft_uid).files.get().file.path
    with django_capture_on_commit_callbacks(execute=True):  # 파일은 행 삭제가 커밋된 뒤에 지운다
        client.post(f"/upload/mine/{draft_uid}/delete")
    assert not Submission.objects.filter(upload_id=draft_uid).exists()
    import os

    assert not os.path.exists(stored)
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    client.post(f"/upload/mine/{uid}/delete")
    sub = Submission.objects.get(upload_id=uid)
    assert sub.status == "pending" and sub.deletion_requested_at  # 제출 건은 삭제 '요청'만
    body = client.get("/upload/mine?status=pending").content.decode()
    assert uid in body and "삭제 요청됨" in body
    assert uid not in client.get("/upload/mine?status=approved").content.decode()


def test_hub_and_admin_dashboard_counts(client, demo_users):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    hub = client.get("/upload").content.decode()
    assert uid in hub and 'data-testid="hub-card-imaging"' in hub and timezone.localdate().isoformat() in hub
    client.force_login(demo_users["admin"])
    dash = client.get("/admin").content.decode()
    assert "검증 대기 1건" in dash
    rows = client.get("/admin/review?status=pending&type=clinical").content.decode()
    assert uid in rows
    assert uid not in client.get("/admin/review?type=lab").content.decode()
    _approve(client, uid)
    dash = client.get("/admin").content.decode()
    assert "검증 대기 0건" in dash and "데이터 승인" in dash
    # 홈 최근 업로드 활동
    home = client.get("/").content.decode()
    assert 'data-testid="activity-empty"' not in home and "분당서울대병원" in home
