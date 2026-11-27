"""3c(2단계 이월분) — 삭제 요청의 관리자 처리(트랜잭션·파일은 커밋 후 삭제·apply 와 대칭), 같은 업로드 내 SHA-256 중복 파일 거부."""
import os

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.cases.models import BiochemResult, ImagingStudy, Patient, Regimen, SequenceRun
from apps.uploads.models import DeletedSubmission, Submission, SubmissionEvent, SubmissionFile
from apps.uploads.services import deletion, dicom, workflow

from .conftest import fastq_gz, make_fastq_gz, upload_id_from
from .test_flow import CLINICAL_FORM, _approve, _csv_to_pending, _submit

pytestmark = pytest.mark.django_db


def _request_delete(client, user, uid):
    client.force_login(user)
    client.post(f"/upload/mine/{uid}/delete")
    assert Submission.objects.get(upload_id=uid).deletion_pending


def _imaging_approved(client, demo_users, cid="KR-2024-0003", modality="CXR"):
    client.force_login(demo_users["researcher"])
    data = dicom.build_minimal_dicom({(0x0008, 0x0080): "SNUBH", (0x0020, 0x000D): cid + modality})
    r = client.post("/upload/imaging/files", {"mode": modality, "condition_id": cid, "study_date": "2025-05-01",
                                              "files": [SimpleUploadedFile("s.dcm", data, content_type="application/dicom")],
                                              "action": "next"})
    uid = upload_id_from(r)
    client.post("/upload/imaging/proceed", {"id": uid, "from": "dicom"})
    client.post("/upload/imaging/data", {"id": uid, "study_description": "x", "tb_activity": "Active",
                                         "ann_finding": ["Cavity"], "ann_location": ["Right"], "ann_severity": ["Severe"],
                                         "ann_description": [""], "impression": "TB", "action": "next"})
    _submit(client, "imaging", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    assert Submission.objects.get(upload_id=uid).status == "approved"
    return uid


def test_imaging_deletion_reverts_counts_and_removes_files_after_commit(client, demo_users, seeded_patients,
                                                                        django_capture_on_commit_callbacks):
    p0 = Patient.objects.get(condition_id="KR-2024-0003")
    uid = _imaging_approved(client, demo_users)
    sub = Submission.objects.get(upload_id=uid)
    path = sub.files.get().file.path
    p1 = Patient.objects.get(pk=p0.pk)
    assert p1.cxr_count == p0.cxr_count + 1 and os.path.exists(path)
    _request_delete(client, demo_users["researcher"], uid)
    client.force_login(demo_users["admin"])
    body = client.get("/admin/deletions").content.decode()
    assert uid in body and "처리 대기 삭제 요청 1건" in body
    page = client.get(f"/admin/deletions/{uid}").content.decode()
    assert "영상 검사 1건" in page
    assert "업로드 삭제 요청 대기 1건" in client.get("/admin").content.decode()
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        r = client.post(f"/admin/deletions/{uid}/approve")
    assert r.status_code == 302 and r["Location"] == "/admin/deletions"
    # 커밋 전에는 파일이 남아 있다 → 콜백(커밋 후) 실행 시 삭제
    assert os.path.exists(path) and len(callbacks) == 1
    callbacks[0]()
    assert not os.path.exists(path)
    assert not Submission.objects.filter(upload_id=uid).exists()
    assert not ImagingStudy.objects.filter(patient=p0).exists() and not SubmissionFile.objects.exists()
    p2 = Patient.objects.get(pk=p0.pk)
    assert p2.cxr_count == p0.cxr_count and p2.has_cxr == (p0.cxr_count > 0)
    log = DeletedSubmission.objects.get(upload_id=uid)
    assert log.reviewer == demo_users["admin"] and log.submitter == demo_users["researcher"]
    assert log.summary["removed"]["imaging_studies"] == 1 and log.status_at_deletion == "approved"
    assert uid in client.get("/admin/deletions").content.decode()  # 최근 삭제 기록


def test_deletion_rolls_back_and_keeps_files_on_error(client, demo_users, seeded_patients, monkeypatch,
                                                     django_capture_on_commit_callbacks):
    uid = _imaging_approved(client, demo_users)
    path = Submission.objects.get(upload_id=uid).files.get().file.path
    _request_delete(client, demo_users["researcher"], uid)
    monkeypatch.setattr(DeletedSubmission.objects, "create", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError):
            deletion.approve_deletion(Submission.objects.get(upload_id=uid), demo_users["admin"])
    assert not callbacks and os.path.exists(path)
    assert Submission.objects.filter(upload_id=uid).exists() and ImagingStudy.objects.filter(submission__upload_id=uid).exists()


def test_regimen_and_genomic_deletion(client, demo_users, seeded_patients, django_capture_on_commit_callbacks):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/regimen/data", {"mode": "form", "condition_id": "KR-2024-0001", "action": "next",
                                             "drug_name": ["Bedaquiline"], "start_day": ["0"], "end_day": ["168"],
                                             "dosage": ["400mg"], "route": ["Oral"]})
    reg_uid = upload_id_from(r)
    _submit(client, "regimen", reg_uid)
    r = client.post("/upload/genomic/files", {"mode": "batch", "files": [fastq_gz()], "action": "next"})
    gen_uid = upload_id_from(r)
    meta = b"condition_id,specimen_id,platform,method,coverage,sra_accession,lineage\nKR-2024-0002,SP-101A,Illumina,WGS,120,SRR99,Beijing\n"
    from .conftest import csv_upload

    client.post("/upload/genomic/csv", {"id": gen_uid, "file": csv_upload("meta.csv", meta)})
    client.post("/upload/genomic/proceed", {"id": gen_uid, "from": "data"})
    _submit(client, "genomic", gen_uid)
    seed = Patient.objects.get(condition_id="KR-2024-0002")
    before = (seed.has_sequence, seed.sra_id, seed.lineage)
    client.force_login(demo_users["admin"])
    _approve(client, reg_uid)
    _approve(client, gen_uid)
    assert Regimen.objects.filter(submission__upload_id=reg_uid).count() == 1
    p2 = Patient.objects.get(condition_id="KR-2024-0002")
    assert p2.sra_id == "SRR99" and SequenceRun.objects.filter(patient=p2).count() == 1
    for uid in (reg_uid, gen_uid):
        _request_delete(client, demo_users["researcher"], uid)
    client.force_login(demo_users["dmanager"])
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/admin/deletions/{reg_uid}/approve")
        client.post(f"/admin/deletions/{gen_uid}/approve")
    assert not Regimen.objects.exists()
    p2 = Patient.objects.get(pk=p2.pk)
    # 업로드가 덮어쓴 유전체 요약은 승인 직전 값(시드)으로 복원된다(지워지지 않는다)
    assert not SequenceRun.objects.exists() and (p2.has_sequence, p2.sra_id, p2.lineage) == before
    assert DeletedSubmission.objects.count() == 2


def test_clinical_and_lab_deletion_symmetry(client, demo_users, seeded_patients, django_capture_on_commit_callbacks):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/clinical/data", CLINICAL_FORM)
    uid = upload_id_from(r)
    _submit(client, "clinical", uid)
    r = client.post("/upload/lab/data", {
        "mode": "form", "condition_id": "KR-2024-0004", "specimen_id": "SP-77", "specimen_date": "2025-02-01",
        "specimen_type": "Sputum", "show_biochem": "1", "bc_test": ["ALT"], "bc_value": ["80"], "bc_unit": ["U/L"],
        "bc_date": ["2025-02-01"], "action": "next"})
    lab_uid = upload_id_from(r)
    _submit(client, "lab", lab_uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    _approve(client, lab_uid)
    assert Patient.objects.filter(condition_id="TB-NEW-001").exists()
    assert Submission.objects.get(upload_id=uid).apply_summary["created_ids"] == ["TB-NEW-001"]
    assert BiochemResult.objects.filter(patient__condition_id="KR-2024-0004").count() == 1
    for u in (uid, lab_uid):
        _request_delete(client, demo_users["researcher"], u)
    client.force_login(demo_users["admin"])
    page = client.get(f"/admin/deletions/{uid}").content.decode()
    assert "TB-NEW-001" in page
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/admin/deletions/{uid}/approve")
        client.post(f"/admin/deletions/{lab_uid}/approve")
    assert not Patient.objects.filter(condition_id="TB-NEW-001").exists()
    assert not BiochemResult.objects.filter(patient__condition_id="KR-2024-0004").exists()


def test_clinical_created_case_with_other_data_is_kept(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    uid = upload_id_from(client.post("/upload/clinical/data", CLINICAL_FORM))
    _submit(client, "clinical", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    p = Patient.objects.get(condition_id="TB-NEW-001")
    Regimen.objects.create(patient=p)  # 다른 데이터가 연결됨
    _request_delete(client, demo_users["researcher"], uid)
    plan = deletion.plan(Submission.objects.get(upload_id=uid))
    assert plan["keep_patients"] == ["TB-NEW-001"] and plan["irreversible"]
    deletion.approve_deletion(Submission.objects.get(upload_id=uid), demo_users["admin"])
    assert Patient.objects.filter(pk=p.pk).exists()


def test_reject_deletion_requires_reason_and_allows_rerequest(client, demo_users):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    _request_delete(client, demo_users["researcher"], uid)
    client.force_login(demo_users["admin"])
    client.post(f"/admin/deletions/{uid}/reject", {"reason": "  "})
    assert Submission.objects.get(upload_id=uid).deletion_pending
    client.post(f"/admin/deletions/{uid}/reject", {"reason": "감사 기간 중 보존 필요"})
    sub = Submission.objects.get(upload_id=uid)
    assert not sub.deletion_pending and sub.deletion_reject_reason == "감사 기간 중 보존 필요"
    assert sub.deletion_reviewer == demo_users["admin"]
    assert SubmissionEvent.objects.filter(submission=sub, action="deletion_rejected").exists()
    assert "삭제 요청 반려" in client.get("/admin").content.decode()  # 활동 로그
    # 제출자 화면에 반려 사유, 다시 요청 가능
    client.force_login(demo_users["researcher"])
    body = client.get("/data?tab=uploads").content.decode()
    assert "감사 기간 중 보존 필요" in body and 'data-testid="deletion-rejected"' in body
    client.post(f"/upload/mine/{uid}/delete", {"next": "/data?tab=uploads"})
    sub.refresh_from_db()
    assert sub.deletion_pending and not sub.deletion_reject_reason
    # 이미 처리된 요청 재처리 방지
    deletion.approve_deletion(sub, demo_users["admin"])
    with pytest.raises(workflow.WorkflowError):
        deletion.reject_deletion(sub, demo_users["admin"], "x")


def test_deletion_separation_of_duties_and_permissions(client, demo_users, other_researcher):
    # 관리자 본인이 올린 업로드의 삭제 요청은 본인이 처리할 수 없다
    client.force_login(demo_users["admin"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    client.post(f"/upload/mine/{uid}/delete")
    page = client.get(f"/admin/deletions/{uid}").content.decode()
    assert 'data-testid="own-upload"' in page
    client.post(f"/admin/deletions/{uid}/approve")
    assert Submission.objects.filter(upload_id=uid).exists()
    # 연구원은 관리자 화면 접근 불가, 남의 업로드에 삭제 요청 불가(404)
    client.force_login(other_researcher)
    assert client.get("/admin/deletions").status_code == 302
    assert client.post(f"/admin/deletions/{uid}/approve").status_code == 302
    assert client.post(f"/upload/mine/{uid}/delete").status_code == 404
    assert Submission.objects.filter(upload_id=uid).exists()
    # 다른 관리자는 처리 가능
    client.force_login(demo_users["dmanager"])
    assert client.get("/admin/deletions/UPL-20000101-001").status_code == 404
    client.post(f"/admin/deletions/{uid}/approve")
    assert not Submission.objects.filter(upload_id=uid).exists()


def test_my_uploads_tab_in_data_page(client, demo_users):
    client.force_login(demo_users["researcher"])
    uid = _csv_to_pending(client, "clinical", "clinical_deidentified.csv")
    _submit(client, "clinical", uid)
    body = client.get("/data?tab=uploads&status=pending").content.decode()
    assert uid in body and 'name="tab" value="uploads"' in body and 'name="next" value="/data?tab=uploads"' in body
    r = client.post(f"/upload/mine/{uid}/delete", {"next": "/data?tab=uploads"})
    assert r["Location"] == "/data?tab=uploads"
    r = client.post(f"/upload/mine/{uid}/delete", {"next": "https://evil.example/"})
    assert r["Location"] == "/upload/mine"  # 오픈 리다이렉트 방지
    assert "삭제 요청됨" in client.get("/data?tab=uploads").content.decode()


def test_duplicate_sha256_rejected_within_submission(client, demo_users):
    client.force_login(demo_users["researcher"])
    data = make_fastq_gz("@dup\nACGT\n+\nIIII\n")
    r = client.post("/upload/genomic/files", {"mode": "individual",
                                              "files": [SimpleUploadedFile("a.fq.gz", data, content_type="application/gzip")]})
    uid = upload_id_from(r)
    r = client.post("/upload/genomic/files", {"id": uid, "files": [SimpleUploadedFile("b.fq.gz", data,
                                                                                      content_type="application/gzip")]},
                    follow=True)
    sub = Submission.objects.get(upload_id=uid)
    assert sub.files.count() == 1 and "같은 내용의 파일이 이미 이 업로드에 있습니다" in r.content.decode()
    stored = [f for f in os.listdir(sub.files.get().file.path.rsplit(os.sep, 1)[0])]
    assert len(stored) == 1  # 거부된 사본은 디스크에도 남지 않는다
    # XHR 경로는 JSON 메시지(영문)
    client.post("/lang", {"lang": "en"})
    r = client.post("/upload/genomic/files", {"id": uid, "files": [SimpleUploadedFile("c.fq.gz", data,
                                                                                      content_type="application/gzip")]},
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert r.status_code == 400 and "identical content" in r.json()["message"]
    # 다른 업로드에는 같은 파일을 올릴 수 있다(교차 중복은 승인 시 apply 가 처리)
    client.post("/lang", {"lang": "ko"})
    r = client.post("/upload/genomic/files", {"mode": "individual",
                                              "files": [SimpleUploadedFile("a.fq.gz", data, content_type="application/gzip")]})
    assert Submission.objects.get(upload_id=upload_id_from(r)).files.count() == 1
