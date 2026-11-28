"""2단계 리뷰(CONFIRMED 1~7, PLAUSIBLE 8~11, NIT) 재현 테스트."""
import io
import os
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from apps.cases.models import BiochemResult, ImagingStudy, Patient, Regimen, SequenceRun
from apps.uploads.handlers import endpoint_limits
from apps.uploads.models import OrphanFile, Submission
from apps.uploads.services import csvio, deidentify, dicom, workflow

from .conftest import csv_upload, fastq_gz, make_fastq_gz, sample_bytes, upload_id_from

pytestmark = pytest.mark.django_db
DEID_ALL = [str(i) for i in range(7)]
REVIEW_ALL = [str(i) for i in range(5)]


def _csv_pending(client, domain, data, name="x.csv", mode=None):
    r = client.post(f"/upload/{domain}/csv", {"mode": mode or ("batch" if domain == "genomic" else "csv"),
                                              "file": csv_upload(name, data)})
    uid = upload_id_from(r)
    client.post(f"/upload/{domain}/proceed", {"id": uid, "from": "data"})
    client.post(f"/upload/{domain}/submit", {"id": uid, "deid": DEID_ALL})
    assert Submission.objects.get(upload_id=uid).status == "pending", Submission.objects.get(upload_id=uid).file_errors
    return uid


def _approve(client, uid):
    return client.post(f"/admin/review/{uid}/approve", {"check": REVIEW_ALL})


# ─── 1. 경고 행의 규칙 위반 값은 반영하지 않고 '버린 필드'로 남긴다 ───
def test_warning_values_dropped_on_apply(client, demo_users, seeded_patients):
    p0 = Patient.objects.get(condition_id="KR-2024-0001")
    client.force_login(demo_users["researcher"])
    lab = ("condition_id,specimen_id,specimen_date,specimen_type,microscopy_result,culture_result,isoniazid\n"
           "KR-2024-0001,SP-1,2025-01-01,Sputum,GARBAGE<b>,Positive???,R\n").encode()
    uid = _csv_pending(client, "lab", lab)
    reg = ("condition_id,drug_name,drug_code,start_day,end_day,dosage,route\n"
           "KR-2024-0001,Isoniazid,H,0,180,300mg,Teleport\n").encode()
    uid2 = _csv_pending(client, "regimen", reg)
    r = client.post("/upload/genomic/files", {"mode": "batch", "files": [fastq_gz()], "action": "next"})
    guid = upload_id_from(r)
    meta = b"condition_id,specimen_id,platform,method,coverage,sra_accession,lineage\nKR-2024-0001,SP-101A,Illumina,WGS,-5,NOT-AN-SRA,Martian\n"
    client.post("/upload/genomic/csv", {"id": guid, "file": csv_upload("meta.csv", meta)})
    sub = Submission.objects.get(upload_id=guid)
    assert sub.warning_records == 1 and sub.error_records == 0  # lineage 도 CSV 에서 규칙 검사(경고)
    client.post("/upload/genomic/proceed", {"id": guid, "from": "data"})
    client.post("/upload/genomic/submit", {"id": guid, "deid": DEID_ALL})
    client.force_login(demo_users["admin"])
    for u in (uid, uid2, guid):
        _approve(client, u)
    p = Patient.objects.get(pk=p0.pk)
    assert (p.microscopy, p.culture) == (p0.microscopy, p0.culture)
    assert p.dst_results.get(drug="INH").result == "R"  # 규칙을 통과한 값은 반영
    dropped = {d["field"] for d in Submission.objects.get(upload_id=uid).apply_summary["dropped"]}
    assert dropped == {"microscopy_result", "culture_result"}
    assert Regimen.objects.get(patient=p).drugs.get().route == ""
    run = SequenceRun.objects.get(patient=p)
    assert run.coverage is None and run.sra_accession == "" and run.lineage == ""
    assert p.sra_id == p0.sra_id and p.lineage == p0.lineage
    body = client.get(f"/admin/review/{uid}").content.decode()
    assert 'data-testid="dropped-fields"' in body and "GARBAGE&lt;b&gt;" in body and "GARBAGE<b>" not in body


# ─── 2. 임상 갱신: 헤더에 있는 컬럼의 값 있는 칸만, 빈 칸은 유지 ───
def test_clinical_update_keeps_missing_and_blank_columns(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0001")
    p.comorbidities, p.treatment_duration = ["고혈압"], 180
    p.save()
    client.force_login(demo_users["researcher"])
    head = "condition_id,age,sex,country,site,diagnosis_type,diagnosis_date,registration_date,site_of_disease,treatment_outcome"
    uid = _csv_pending(client, "clinical", (head + "\nKR-2024-0001,46,Male,Korea,서울대병원,DS-TB,2024-01-10,2024-01-15,Pulmonary,Cured\n").encode())
    uid2 = _csv_pending(client, "clinical", (head + ",comorbidities,outcome_date\n"
                                             "KR-2024-0001,47,Male,Korea,서울대병원,DS-TB,2024-01-10,2024-01-15,Pulmonary,Cured,,\n").encode())
    client.force_login(demo_users["admin"])
    detail = client.get(f"/admin/review/{uid}").content.decode()
    assert 'data-testid="clinical-blank-note"' in detail and "빈 칸은 기존 값을 그대로 유지" in detail
    _approve(client, uid)
    p.refresh_from_db()
    assert p.age == 46 and p.comorbidities == ["고혈압"] and p.treatment_duration == 180 and p.outcome_date is None
    _approve(client, uid2)
    p.refresh_from_db()
    assert p.age == 47 and p.comorbidities == ["고혈압"]  # 빈 칸은 지우지 않는다
    assert Submission.objects.get(upload_id=uid2).apply_summary["updated"] == 1


# ─── 3. DICOM: 큰 요소 뒤의 식별 태그까지 판독, 판독 불가는 차단 ───
def test_dicom_large_element_and_unreadable_blocked(client, demo_users, seeded_patients):
    big = dicom.build_minimal_dicom({(0x0010, 0x0010): "Lee^Younghee"}, private_blob=5 * 1024 * 1024)
    info = dicom.inspect(io.BytesIO(big))
    assert info["parsed"] and info["tags"][0]["risky"] and info["tags"][0]["value"] == "Lee Younghee"
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/imaging/files", {"mode": "CT", "condition_id": "KR-2024-0001", "study_date": "2025-01-01",
                                              "files": [SimpleUploadedFile("big.dcm", big, content_type="application/dicom")]})
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    assert sub.pii_blocking
    # 잘린 파일: 식별정보 부재를 확인할 수 없으므로 high(차단) + 사유 표시
    cut = big[:3 * 1024 * 1024]
    r = client.post("/upload/imaging/files", {"mode": "CT", "condition_id": "KR-2024-0001", "study_date": "2025-01-01",
                                              "files": [SimpleUploadedFile("cut.dcm", cut, content_type="application/dicom")], "action": "next"})
    sub2 = Submission.objects.get(upload_id=upload_id_from(r))
    assert sub2.pii_blocking and sub2.pii_report["detections"][0]["confidence"] == "high"
    page = client.get(f"/upload/imaging?id={sub2.upload_id}").content.decode()
    assert 'data-testid="dicom-unreadable"' in page and "잘린 파일" in page
    client.post("/upload/imaging/proceed", {"id": sub2.upload_id, "from": "dicom"})
    client.post("/upload/imaging/data", {"id": sub2.upload_id, "action": "next"})
    page = client.get(f"/upload/imaging?id={sub2.upload_id}").content.decode()
    assert 'data-testid="pii-reason"' in page and "식별정보 부재를 확인할 수 없습니다" in page
    client.post("/upload/imaging/submit", {"id": sub2.upload_id, "deid": DEID_ALL})
    assert Submission.objects.get(pk=sub2.pk).status == "draft"


# ─── 4. CSV 수식 주입 ───
@pytest.mark.parametrize("value,expected", [
    ("-1", "-1"), ("3.5", "3.5"), ("-12.25", "-12.25"), ("=1+1", "'=1+1"), ("+1", "'+1"), ("-1+2", "'-1+2"),
    ("-cmd", "'-cmd"), ("@SUM(A1)", "'@SUM(A1)"), ("\tx", "'\tx"), ("\rx", "'\rx"), ("1e5", "1e5"), ("abc", "abc"),
    ("--5", "'--5"), ("- 3", "'- 3"),
])
def test_safe_cell(value, expected):
    assert csvio.safe_cell(value) == expected


def test_report_download_neutralizes_formulas(client, demo_users):
    client.force_login(demo_users["researcher"])
    data = ("condition_id,age,sex,country,site,diagnosis_type,diagnosis_date,registration_date,site_of_disease,treatment_outcome\n"
            "TB-F1,-1+2,Male,Korea,S,DS-TB,2024-01-01,2024-01-02,Pulmonary,Cured\n").encode()
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("f.csv", data)})
    body = client.get(f"/upload/report/{upload_id_from(r)}.csv").content.decode("utf-8-sig")
    assert "'-1+2" in body and ",-1+2," not in body


# ─── 5. PII 정규식 우회 ───
@pytest.mark.parametrize("text", [
    "010 1234 5678", "010.1234.5678", "010 - 1234 - 5678", "010–1234—5678", "01012345678",
    "+82 10-1234-5678", "+82-10-1234-5678", "+821012345678", "82 10 1234 5678", "+82 (0)10 1234 5678",
    "+82 2 2072 1234", "031.123.4567", "연락처010-1234-5678",
])
def test_phone_variants_detected(text):
    rep = deidentify.scan_rows([{"memo_col": text}])
    assert rep["count_by_type"]["phone"] == 1, text


@pytest.mark.parametrize("text", ["900101 1234567", "900101 - 1234567", "900101\u20141234567", "주민 900101-1234567.", "9001011234567"])
def test_rrn_variants_detected(text):
    rep = deidentify.scan_rows([{"x": text}])
    assert rep["count_by_type"]["rrn"] == 1 and rep["count_by_type"]["phone"] == 0, text


@pytest.mark.parametrize("name", ["clinical_deidentified.csv", "beta_clinical_deidentified.csv", "lab_deidentified.csv",
                                  "genomic_deidentified.csv"])
def test_no_false_positive_on_deidentified_samples(name):
    from apps.uploads.services import schemas

    domain = name.split("_")[0] if not name.startswith("beta") else "clinical"
    rep = deidentify.scan_rows(csvio.parse(sample_bytes(name))["rows"], schemas.safe_columns(domain))
    assert rep["detections"] == []


@pytest.mark.parametrize("text", ["123456.1234567", "900101.1234567", "0.9001011234567", "1900101-1234567.5"])
def test_rrn_decimal_not_detected(text):
    """주민번호 패턴은 마침표 구분자를 쓰지 않는다(소수값 오탐 방지)."""
    assert deidentify.scan_rows([{"x": text}])["count_by_type"]["rrn"] == 0, text


# ─── 6. 재제출·재승인 중복 생성 방지 + 사전 점검 표시 ───
def test_reapproval_does_not_duplicate(client, demo_users, seeded_patients):
    p0 = Patient.objects.get(condition_id="KR-2024-0004")

    def lab_form():
        r = client.post("/upload/lab/data", {
            "mode": "form", "condition_id": "KR-2024-0004", "specimen_id": "SP-77", "specimen_date": "2025-02-01",
            "specimen_type": "Sputum", "show_biochem": "1", "bc_test": ["ALT"], "bc_value": ["80"], "bc_unit": ["U/L"],
            "bc_date": ["2025-02-01"], "action": "next"})
        uid = upload_id_from(r)
        client.post("/upload/lab/submit", {"id": uid, "deid": DEID_ALL})
        return uid

    def seq():
        r = client.post("/upload/genomic/files", {"mode": "individual", "files": [fastq_gz()], "action": "next"})
        uid = upload_id_from(r)
        client.post("/upload/genomic/data", {"id": uid, "condition_id": "KR-2024-0004", "specimen_id": "SP-1",
                                             "platform": "Illumina MiSeq", "method": "WGS", "action": "next"})
        client.post("/upload/genomic/submit", {"id": uid, "deid": DEID_ALL})
        return uid

    def img():
        f = SimpleUploadedFile("same.dcm", dicom.build_minimal_dicom({(0x0008, 0x0080): "H"}), content_type="application/dicom")
        r = client.post("/upload/imaging/files", {"mode": "CXR", "condition_id": "KR-2024-0004", "study_date": "2025-01-01",
                                                  "files": [f], "action": "next"})
        uid = upload_id_from(r)
        client.post("/upload/imaging/proceed", {"id": uid, "from": "dicom"})
        client.post("/upload/imaging/data", {"id": uid, "action": "next"})
        client.post("/upload/imaging/submit", {"id": uid, "deid": DEID_ALL})
        return uid

    def regimen():
        r = client.post("/upload/regimen/data", {"mode": "form", "condition_id": "KR-2024-0004", "action": "next",
                                                 "drug_name": ["Bedaquiline"], "start_day": ["0"], "end_day": ["168"],
                                                 "dosage": [""], "route": ["Oral"]})
        uid = upload_id_from(r)
        client.post("/upload/regimen/submit", {"id": uid, "deid": DEID_ALL})
        return uid

    for make in (lab_form, seq, img, regimen):
        client.force_login(demo_users["researcher"])
        first, second = make(), make()
        client.force_login(demo_users["admin"])
        _approve(client, first)
        detail = client.get(f"/admin/review/{second}").content.decode()
        assert 'data-testid="dup-preview"' in detail and "기존 데이터와 겹치는 항목이 없습니다" not in detail, make.__name__
        _approve(client, second)
        s2 = Submission.objects.get(upload_id=second)
        assert s2.status == "approved" and s2.apply_summary["duplicates"], make.__name__
        assert 'data-testid="dup-skipped"' in client.get(f"/admin/review/{second}").content.decode()
    p = Patient.objects.get(pk=p0.pk)
    assert BiochemResult.objects.filter(patient=p, test="ALT").count() == 1
    assert SequenceRun.objects.filter(patient=p).count() == 1
    assert ImagingStudy.objects.filter(patient=p).count() == 1 and p.cxr_count == p0.cxr_count + 1
    assert Regimen.objects.filter(patient=p).count() == 1


# ─── 7. upload_id 1000 번째 이후 ───
def test_upload_id_beyond_999(demo_users):
    from django.utils import timezone

    prefix = f"UPL-{timezone.localdate():%Y%m%d}-"
    u = demo_users["researcher"]
    Submission.objects.create(upload_id=prefix + "999", submitter=u, domain="clinical", method="form", mode="form")
    a = workflow.create_draft(u, "clinical", "form")
    b = workflow.create_draft(u, "clinical", "form")
    assert (a.upload_id, b.upload_id) == (prefix + "1000", prefix + "1001")
    assert workflow.create_draft(u, "lab", "csv").upload_id == prefix + "1002"


# ─── 8. 편집·제출 경합 ───
def test_stale_edit_after_submit_rejected(client, demo_users):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("clinical_deidentified.csv")})
    uid = upload_id_from(r)
    stale = Submission.objects.get(upload_id=uid)  # 편집 요청이 읽어 둔 draft
    client.post("/upload/clinical/proceed", {"id": uid, "from": "data"})
    client.post("/upload/clinical/submit", {"id": uid, "deid": DEID_ALL})
    for call in (lambda: workflow.handle_csv(stale, csv_upload("clinical_with_pii.csv")),
                 lambda: workflow.save_form(stale, {}), lambda: workflow.set_step(stale, 1),
                 lambda: workflow.change_mode(stale, "form"), lambda: workflow.add_files(stale, [])):
        with pytest.raises(workflow.WorkflowError):
            call()
    sub = Submission.objects.get(upload_id=uid)
    assert sub.status == "pending" and sub.total_records == 3 and not sub.pii_blocking and sub.files.count() == 1
    # 화면 경로도 그대로
    client.post("/upload/clinical/csv", {"id": uid, "file": csv_upload("clinical_with_pii.csv")})
    assert Submission.objects.get(upload_id=uid).total_records == 3


# ─── 9. 요청 합계·엔드포인트별 상한, 임시 파일 정리 ───
def test_endpoint_limits(settings):
    total, per = endpoint_limits("/upload/clinical/csv")
    assert per == settings.UPLOAD_LIMITS["csv"]["max_bytes"] and total < 60 * 1024 * 1024
    total, per = endpoint_limits("/upload/genomic/files")
    assert per == settings.UPLOAD_LIMITS["sequence"]["max_bytes"] and total <= settings.UPLOAD_MAX_REQUEST_BYTES
    assert endpoint_limits("/upload/clinical/data")[1] <= 10 * 1024 * 1024


def test_aborted_upload_cleans_temp_files(client, demo_users, settings, tmp_path):
    client.force_login(demo_users["researcher"])
    tmpdir = tmp_path / "uptmp"
    tmpdir.mkdir()
    settings.FILE_UPLOAD_TEMP_DIR = str(tmpdir)
    settings.FILE_UPLOAD_MAX_MEMORY_SIZE = 10  # 디스크 임시 파일 경로를 타게
    limits = {k: dict(v) for k, v in settings.UPLOAD_LIMITS.items()}
    limits["csv"]["max_bytes"] = 2000
    settings.UPLOAD_LIMITS = limits
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": SimpleUploadedFile("a.csv", b"a,b\n" + b"1,2\n" * 2000)},
                    follow=True)
    assert "파일이 너무 큽니다" in r.content.decode() and not Submission.objects.exists()
    assert list(tmpdir.iterdir()) == []
    # 다른 화면(파일을 받지 않는 엔드포인트)으로 큰 파일을 보내도 받지 않는다
    r = client.post("/upload/clinical/data", {"mode": "form", "file": SimpleUploadedFile("x.bin", b"0" * (11 * 1024 * 1024))})
    assert list(tmpdir.iterdir()) == []


# ─── 10. 길이·형식 ───
def test_length_and_format_validation(client, demo_users):
    client.force_login(demo_users["researcher"])
    head = "condition_id,age,sex,country,site,diagnosis_type,diagnosis_date,registration_date,site_of_disease,treatment_outcome\n"
    rows = (f"{'A' * 41},40,Male,Korea,S,DS-TB,2024-01-01,2024-01-02,Pulmonary,Cured\n"
            "TB<script>,40,Male,Korea,S,DS-TB,2024-01-01,2024-01-02,Pulmonary,Cured\n"
            f"TB-OK,40,Male,Korea,{'S' * 101},DS-TB,2024-01-01,2024-01-02,Pulmonary,Cured\n")
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("l.csv", (head + rows).encode())})
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    assert sub.error_records == 3
    msgs = [m["ko"] for rec in sub.records.all() for m in rec.messages]
    assert "최대 40자까지 입력할 수 있습니다" in msgs and "최대 100자까지 입력할 수 있습니다" in msgs
    assert any("영문·숫자" in m for m in msgs)
    r = client.post("/upload/lab/data", {"mode": "form", "condition_id": "KR-1", "specimen_id": "SP 1", "specimen_date": "2025-01-01",
                                         "specimen_type": "Sputum", "show_biochem": "1", "bc_test": ["ALT"], "bc_value": ["1"],
                                         "bc_unit": ["U" * 25], "bc_date": [""], "action": "next"})
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    fields = {m["field"] for m in sub.records.get().messages if m["severity"] == "error"}
    assert {"specimen_id", "biochemistry[1].unit"} <= fields and sub.step == 1


# ─── 11. 삭제 실패 파일 기록과 cleanup_media ───
def test_orphan_file_recorded_and_cleaned(client, demo_users, monkeypatch, _media_tmp, django_capture_on_commit_callbacks):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/genomic/files", {"mode": "individual",
                                              "files": [SimpleUploadedFile("a.fq.gz", make_fastq_gz(), content_type="application/gzip")]})
    uid = upload_id_from(r)
    path = Path(Submission.objects.get(upload_id=uid).files.get().file.path)
    real_unlink = Path.unlink

    def locked(self, *a, **k):
        raise PermissionError("[WinError 32] 다른 프로세스가 파일을 사용 중")

    monkeypatch.setattr(Path, "unlink", locked)
    with django_capture_on_commit_callbacks(execute=True):  # 파일 삭제는 커밋 뒤
        client.post(f"/upload/mine/{uid}/delete")
    assert not Submission.objects.filter(upload_id=uid).exists() and path.exists()
    o = OrphanFile.objects.get()
    assert "WinError 32" in o.reason
    monkeypatch.setattr(Path, "unlink", real_unlink)
    stray = Path(_media_tmp) / "uploads" / "2020" / "01" / "stray.bin"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    call_command("cleanup_media", "--min-age-minutes", "0")
    assert not path.exists() and not stray.exists() and not OrphanFile.objects.exists()
    # 참조 중인 파일은 지우지 않는다
    r = client.post("/upload/genomic/files", {"mode": "individual",
                                              "files": [SimpleUploadedFile("b.fq.gz", make_fastq_gz(), content_type="application/gzip")]})
    kept = Path(Submission.objects.get(upload_id=upload_id_from(r)).files.get().file.path)
    call_command("cleanup_media", "--min-age-minutes", "0")
    assert kept.exists()


# ─── NIT ───
def test_aborted_back_redirect_is_validated(client, demo_users, settings):
    client.force_login(demo_users["researcher"])
    settings.UPLOAD_MAX_FILE_BYTES = 100
    big = SimpleUploadedFile("a.csv", b"a,b\n" + b"1,2\n" * 100)
    r = client.post("/upload/clinical/csv", {"back": "//evil.example.com/x", "file": big})
    assert r["Location"] == "/upload/clinical"
    r = client.post("/upload/clinical/csv", {"back": "https://evil.example.com/", "file": SimpleUploadedFile("a.csv", b"a,b\n" + b"1,2\n" * 100)})
    assert "evil" not in r["Location"]
    r = client.post("/upload/clinical/csv", {"back": "/upload/mine", "file": SimpleUploadedFile("a.csv", b"a,b\n" + b"1,2\n" * 100)})
    assert r["Location"] == "/upload/mine"


def test_zero_width_reason_rejected(client, demo_users):
    client.force_login(demo_users["researcher"])
    uid = _csv_pending(client, "clinical", sample_bytes("clinical_deidentified.csv"))
    client.force_login(demo_users["admin"])
    for reason in ("​‌‍", "﻿ ​ ", " 　⁠"):
        client.post(f"/admin/review/{uid}/reject", {"reason": reason})
        client.post(f"/admin/review/{uid}/revise", {"reason": reason})
    assert Submission.objects.get(upload_id=uid).status == "pending"


def test_duplicate_csv_headers_error(client, demo_users):
    client.force_login(demo_users["researcher"])
    data = b"condition_id,age,age,sex,country,site,diagnosis_type,diagnosis_date,registration_date,site_of_disease,treatment_outcome\n" \
           b"TB-1,40,99,Male,Korea,S,DS-TB,2024-01-01,2024-01-02,Pulmonary,Cured\n"
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("d.csv", data)})
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    assert "중복된 컬럼" in sub.file_errors[0]["ko"] and "age" in sub.file_errors[0]["ko"] and sub.total_records == 0


def test_updated_counts_patients(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    data = (b"condition_id,specimen_id,specimen_date,specimen_type,microscopy_result\n"
            b"KR-2024-0001,SP-1,2025-01-01,Sputum,1+\nKR-2024-0001,SP-2,2025-02-01,Sputum,2+\nKR-2024-0002,SP-3,2025-02-01,Sputum,2+\n")
    uid = _csv_pending(client, "lab", data)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    assert Submission.objects.get(upload_id=uid).apply_summary["updated"] == 2


def test_admin_cannot_review_own_upload(client, demo_users):
    client.force_login(demo_users["admin"])
    uid = _csv_pending(client, "clinical", sample_bytes("clinical_deidentified.csv"))
    body = client.get(f"/admin/review/{uid}").content.decode()
    assert 'data-testid="own-upload-note"' in body and 'data-testid="approve-open"' not in body
    _approve(client, uid)
    client.post(f"/admin/review/{uid}/reject", {"reason": "x"})
    client.post(f"/admin/review/{uid}/revise", {"reason": "x"})
    assert Submission.objects.get(upload_id=uid).status == "pending" and not Patient.objects.exists()
    with pytest.raises(workflow.WorkflowError):
        workflow.approve(Submission.objects.get(upload_id=uid), demo_users["admin"], REVIEW_ALL)
    client.force_login(demo_users["dmanager"])
    _approve(client, uid)
    assert Submission.objects.get(upload_id=uid).status == "approved"
