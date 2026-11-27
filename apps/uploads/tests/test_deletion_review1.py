"""3c 1차 리뷰(삭제 승인) 재현 테스트 — 유전체 요약 복원, CXR 소견 되돌리기, 공유 행 소유 이전,
생화학 '갱신만' 복원, 수정 요청 시 삭제 요청 정리.

2차 리뷰 이후 설계: 직전 값 기록(genomic_prev 등) 대신 기준 스냅샷(DerivedBaseline) + 남은 행 재계산(fold).
같은 의미의 검증은 유지하고, '그 사이 다른 경로로 바뀐 값' 은 재계산 결과(기준 값 + 남은 업로드)로 맞춰진다."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.cases.models import BiochemResult, ImagingStudy, Patient, Regimen, SequenceRun
from apps.uploads.models import DeletedSubmission, DerivedBaseline, Submission, SubmissionEvent
from apps.uploads.services import deletion, workflow

from .conftest import csv_upload, fastq_gz, make_fastq_gz, upload_id_from
from .test_deletion import _imaging_approved, _request_delete
from .test_flow import _approve, _submit

pytestmark = pytest.mark.django_db


def _genomic(p):
    p = Patient.objects.get(pk=p.pk)
    return (p.has_sequence, p.sra_id, p.lineage)


def _delete(client, demo_users, uid):
    _request_delete(client, demo_users["researcher"], uid)
    deletion.approve_deletion(Submission.objects.get(upload_id=uid), demo_users["admin"])
    assert not Submission.objects.filter(upload_id=uid).exists()


def _genomic_upload(client, demo_users, cid="KR-2024-0002", sra="SRR99", lineage="Beijing", approve=True):
    client.force_login(demo_users["researcher"])
    uid = upload_id_from(client.post("/upload/genomic/files", {"mode": "batch", "files": [fastq_gz()], "action": "next"}))
    meta = ("condition_id,specimen_id,platform,method,coverage,sra_accession,lineage\n"
            f"{cid},SP-101A,Illumina,WGS,120,{sra},{lineage}\n").encode()
    client.post("/upload/genomic/csv", {"id": uid, "file": csv_upload("meta.csv", meta)})
    client.post("/upload/genomic/proceed", {"id": uid, "from": "data"})
    _submit(client, "genomic", uid)
    if approve:
        client.force_login(demo_users["admin"])
        _approve(client, uid)
        assert Submission.objects.get(upload_id=uid).status == "approved"
    return uid


def _regimen_upload(client, demo_users, cid="KR-2024-0001"):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/regimen/data", {"mode": "form", "condition_id": cid, "action": "next",
                                             "drug_name": ["Bedaquiline"], "start_day": ["0"], "end_day": ["168"],
                                             "dosage": ["400mg"], "route": ["Oral"]})
    uid = upload_id_from(r)
    _submit(client, "regimen", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    assert Submission.objects.get(upload_id=uid).status == "approved"
    return uid


def _lab_upload(client, demo_users, cid="KR-2024-0004", value="80"):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/lab/data", {
        "mode": "form", "condition_id": cid, "specimen_id": "SP-77", "specimen_date": "2025-02-01",
        "specimen_type": "Sputum", "show_biochem": "1", "bc_test": ["ALT"], "bc_value": [value], "bc_unit": ["U/L"],
        "bc_date": ["2025-02-01"], "action": "next"})
    uid = upload_id_from(r)
    _submit(client, "lab", uid)
    client.force_login(demo_users["admin"])
    _approve(client, uid)
    assert Submission.objects.get(upload_id=uid).status == "approved"
    return uid


# ─── 2. 유전체 요약(has_sequence/sra_id/lineage) 덮어쓰기 → 삭제 시 직전 값 복원 ───
def test_genomic_overwrite_restored_on_deletion(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0002")
    assert _genomic(p) == (True, "SRR180002", "L2.2.1")
    uid = _genomic_upload(client, demo_users)
    assert _genomic(p) == (True, "SRR99", "Beijing")
    base = DerivedBaseline.objects.get(patient=p, domain="genomic")
    assert base.values == {"has_sequence": True, "sra_id": "SRR180002", "lineage": "L2.2.1"}
    assert Submission.objects.get(upload_id=uid).apply_summary["recompute"] == 1
    _request_delete(client, demo_users["researcher"], uid)
    plan = deletion.plan(Submission.objects.get(upload_id=uid))
    assert plan["sequence_runs"] == 1 and plan["notes"] and not plan["irreversible"]
    deletion.approve_deletion(Submission.objects.get(upload_id=uid), demo_users["admin"])
    assert not SequenceRun.objects.filter(patient=p).exists()
    assert _genomic(p) == (True, "SRR180002", "L2.2.1")


def test_genomic_recomputed_from_baseline_not_from_current_value(client, demo_users, seeded_patients):
    """재계산은 현재 값이 아니라 기준 스냅샷 + 남은 업로드로 계산한다(업로드 밖에서 바뀐 파생 값은 덮어쓴다 —
    포털에는 이 필드를 직접 고치는 화면이 없다)."""
    p = Patient.objects.get(condition_id="KR-2024-0002")
    uid = _genomic_upload(client, demo_users)
    Patient.objects.filter(pk=p.pk).update(lineage="L9.9")
    _delete(client, demo_users, uid)
    assert _genomic(p) == (True, "SRR180002", "L2.2.1")


def test_genomic_legacy_approval_left_untouched_with_notice(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0002")
    uid = _genomic_upload(client, demo_users)
    sub = Submission.objects.get(upload_id=uid)
    legacy = {k: v for k, v in sub.apply_summary.items() if k not in ("recompute", "shared_sequence_ids")}
    Submission.objects.filter(pk=sub.pk).update(apply_summary=legacy)
    DerivedBaseline.objects.filter(patient=p).delete()  # 재계산 규칙 이전 승인 건 = 기준 스냅샷 없음
    _request_delete(client, demo_users["researcher"], uid)
    plan = deletion.plan(Submission.objects.get(upload_id=uid))
    assert any("유전체 요약" in x["ko"] and "genomic summary" in x["en"] for x in plan["irreversible"])
    client.force_login(demo_users["admin"])
    page = client.get(f"/admin/deletions/{uid}").content.decode()
    assert 'data-testid="irreversible"' in page and "유전체 요약" in page
    deletion.approve_deletion(Submission.objects.get(upload_id=uid), demo_users["admin"])
    assert _genomic(p) == (True, "SRR99", "Beijing")  # 기준 없음 → 건드리지 않음(예전처럼 지우지도 않음)


def test_genomic_chain_restores_to_seed_after_both_deleted(client, demo_users, seeded_patients):
    """A(SRR99) → B(SRR77, 다른 파일) 승인 후 A 삭제 → B 삭제: 이미 지워진 A 값이 아니라 시드 값으로 돌아간다."""
    p = Patient.objects.get(condition_id="KR-2024-0002")
    a = _genomic_upload(client, demo_users)
    client.force_login(demo_users["researcher"])
    other = make_fastq_gz("@other\nACGTACGT\n+\nIIIIIIII\n")
    b = upload_id_from(client.post("/upload/genomic/files", {"mode": "batch", "action": "next", "files": [
        SimpleUploadedFile("TB-KR-102_SP-102A.fastq.gz", other, content_type="application/gzip")]}))
    meta = b"condition_id,specimen_id,platform,method,coverage,sra_accession,lineage\nKR-2024-0002,SP-102A,Illumina,WGS,90,SRR77,LAM\n"
    client.post("/upload/genomic/csv", {"id": b, "file": csv_upload("meta.csv", meta)})
    client.post("/upload/genomic/proceed", {"id": b, "from": "data"})
    _submit(client, "genomic", b)
    client.force_login(demo_users["admin"])
    _approve(client, b)
    assert _genomic(p) == (True, "SRR77", "LAM")
    _delete(client, demo_users, a)
    assert _genomic(p) == (True, "SRR77", "LAM")  # 남은 시퀀스(B) 기준
    _delete(client, demo_users, b)
    assert _genomic(p) == (True, "SRR180002", "L2.2.1")


# ─── 3. CXR 소견 되돌리기 ───
def test_imaging_added_cxr_findings_reverted(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0003")
    assert p.cxr_findings == ["결절"]
    uid = _imaging_approved(client, demo_users)  # Cavity → 공동
    p.refresh_from_db()
    assert p.cxr_findings == ["결절", "공동"]
    assert DerivedBaseline.objects.get(patient=p, domain="imaging").values["cxr_findings"] == ["결절"]
    _delete(client, demo_users, uid)
    p.refresh_from_db()
    assert p.cxr_findings == ["결절"] and p.cxr_count == 2 and p.has_cxr


def test_imaging_finding_kept_if_another_cxr_study_has_it(client, demo_users, seeded_patients):
    """업로드 없이 생긴 CXR 검사(기준 스냅샷 뒤)도 재계산에 맨 앞으로 접힌다 — 그 소견은 남는다."""
    p = Patient.objects.get(condition_id="KR-2024-0003")
    uid = _imaging_approved(client, demo_users)
    ImagingStudy.objects.create(patient=p, modality="CXR", study_date="2025-01-01",
                                findings=[{"finding": "Cavity", "location": "Left"}])
    _delete(client, demo_users, uid)
    p.refresh_from_db()
    assert p.cxr_findings == ["결절", "공동"] and p.cxr_count == 3  # 기준 2 + 남은 검사 1


def test_imaging_flag_restored_to_previous_value(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0003")
    # 수는 없지만 보유로 기록된 CT, 원래 없던 PET-CT — 첫 업로드 전 상태가 기준 스냅샷이 된다
    Patient.objects.filter(pk=p.pk).update(has_ct=True, ct_count=0, has_pet_ct=False, pet_ct_count=0)
    uid = _imaging_approved(client, demo_users, modality="CT")
    p.refresh_from_db()
    assert p.has_ct and p.ct_count == 1
    _delete(client, demo_users, uid)
    p.refresh_from_db()
    assert p.has_ct and p.ct_count == 0
    # 원래 없던 케이스는 False 로
    uid = _imaging_approved(client, demo_users, modality="PET-CT")
    p.refresh_from_db()
    assert p.has_pet_ct and p.pet_ct_count == 1
    _delete(client, demo_users, uid)
    p.refresh_from_db()
    assert not p.has_pet_ct and p.pet_ct_count == 0


def test_imaging_legacy_findings_untouched_with_notice(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0003")
    uid = _imaging_approved(client, demo_users)
    sub = Submission.objects.get(upload_id=uid)
    legacy = {k: v for k, v in sub.apply_summary.items() if k not in ("recompute", "shared_imaging_ids")}
    Submission.objects.filter(pk=sub.pk).update(apply_summary=legacy)
    DerivedBaseline.objects.filter(patient=p).delete()
    _request_delete(client, demo_users["researcher"], uid)
    plan = deletion.plan(Submission.objects.get(upload_id=uid))
    assert any("CXR 소견" in x["ko"] for x in plan["irreversible"])
    deletion.approve_deletion(Submission.objects.get(upload_id=uid), demo_users["admin"])
    p.refresh_from_db()
    assert p.cxr_findings == ["결절", "공동"] and p.cxr_count == 2 and p.has_cxr  # 수만 줄이고 소견은 유지


# ─── 4. 같은 내용을 다시 승인한 업로드(B)가 중복으로 건너뛴 행 — A 삭제 시 B 를 다시 적용(replay)해 B 소유 행이 생긴다 ───
# (3c 3차 리뷰 이후: 공유 기록 소유 이전 대신 환자×도메인 재적용. 행 pk 는 바뀔 수 있고, 내용·소유·파일이 W2 와 같다.)
def test_shared_regimen_reapplied_for_other_upload(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0001")
    base = p.regimens.count()
    a = _regimen_upload(client, demo_users)
    b = _regimen_upload(client, demo_users)
    reg = Regimen.objects.get(submission__upload_id=a)
    assert Submission.objects.get(upload_id=b).apply_summary["shared_regimen_ids"] == [reg.pk]  # 이력용 기록
    _request_delete(client, demo_users["researcher"], a)
    plan = deletion.plan(Submission.objects.get(upload_id=a))
    assert plan["regimens"] == 1 and plan["pairs"] == [
        {"condition_id": "KR-2024-0001", "mode": "replay", "others": [b], "ko": "KR-2024-0001 · 레지멘 — 다시 적용",
         "en": "KR-2024-0001 · Regimen — Re-applied"}]
    client.force_login(demo_users["admin"])
    page = client.get(f"/admin/deletions/{a}").content.decode()
    assert 'data-testid="deletion-pairs"' in page and "다시 적용" in page and b in page
    deletion.approve_deletion(Submission.objects.get(upload_id=a), demo_users["admin"])
    sb = Submission.objects.get(upload_id=b)
    assert sb.status == "approved" and p.regimens.count() == base + 1
    assert Regimen.objects.get(patient=p, submission__isnull=False).submission_id == sb.pk
    assert sb.apply_summary["shared_regimen_ids"] == [reg.pk]  # 승인 당시 결과는 덮어쓰지 않는다
    log = DeletedSubmission.objects.get(upload_id=a)
    assert log.summary["replayed"] == [b] and log.summary["pairs"] == [{"condition_id": "KR-2024-0001", "mode": "replay"}]
    _delete(client, demo_users, b)
    assert p.regimens.count() == base


def test_shared_regimen_goes_to_earliest_approved(client, demo_users, seeded_patients):
    a = _regimen_upload(client, demo_users)
    b = _regimen_upload(client, demo_users)
    c = _regimen_upload(client, demo_users)
    _delete(client, demo_users, a)
    assert [r.submission.upload_id for r in Regimen.objects.filter(submission__isnull=False)] == [b]
    _delete(client, demo_users, b)
    assert [r.submission.upload_id for r in Regimen.objects.filter(submission__isnull=False)] == [c]
    _delete(client, demo_users, c)
    assert not Regimen.objects.filter(submission__isnull=False).exists()


def test_shared_sequence_run_reapplied_with_own_files(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0002")
    a = _genomic_upload(client, demo_users)
    b = _genomic_upload(client, demo_users)  # 같은 파일(SHA-256) → 건너뜀
    run = SequenceRun.objects.get(patient=p)
    sb = Submission.objects.get(upload_id=b)
    assert sb.apply_summary["shared_sequence_ids"] == [run.pk]
    _delete(client, demo_users, a)
    run = SequenceRun.objects.get(patient=p)
    assert run.submission_id == sb.pk
    assert [f.submission_id for f in run.files.all()] == [sb.pk]  # B 자신의 파일로 연결
    assert _genomic(p) == (True, "SRR99", "Beijing")
    _delete(client, demo_users, b)
    assert not SequenceRun.objects.filter(patient=p).exists()
    assert _genomic(p) == (True, "SRR180002", "L2.2.1")


def test_shared_imaging_study_reapplied(client, demo_users, seeded_patients):
    p = Patient.objects.get(condition_id="KR-2024-0003")
    a = _imaging_approved(client, demo_users)
    b = _imaging_approved(client, demo_users)  # 같은 DICOM → 건너뜀(영상 수 증가 없음)
    p.refresh_from_db()
    assert p.cxr_count == 3 and p.cxr_findings == ["결절", "공동"]
    _delete(client, demo_users, a)
    study = ImagingStudy.objects.get(patient=p)
    p.refresh_from_db()
    assert study.submission.upload_id == b and study.files.get().submission.upload_id == b
    assert p.cxr_count == 3 and p.cxr_findings == ["결절", "공동"]
    _delete(client, demo_users, b)
    p.refresh_from_db()
    assert not ImagingStudy.objects.filter(patient=p).exists()
    assert p.cxr_count == 2 and p.cxr_findings == ["결절"]


def test_shared_biochem_reapplied(client, demo_users, seeded_patients):
    a = _lab_upload(client, demo_users)
    b = _lab_upload(client, demo_users)
    row = BiochemResult.objects.get(patient__condition_id="KR-2024-0004")
    assert row.submission.upload_id == a
    assert Submission.objects.get(upload_id=b).apply_summary["shared_biochem_ids"] == [row.pk]
    _request_delete(client, demo_users["researcher"], a)
    assert deletion.plan(Submission.objects.get(upload_id=a))["biochem"] == 1
    deletion.approve_deletion(Submission.objects.get(upload_id=a), demo_users["admin"])
    row = BiochemResult.objects.get(patient__condition_id="KR-2024-0004")
    assert row.submission.upload_id == b and (row.reference_range, row.flag) == ("7-56", "H")
    _delete(client, demo_users, b)
    assert not BiochemResult.objects.filter(patient__condition_id="KR-2024-0004").exists()


def test_biochem_update_only_restored_from_baseline(client, demo_users, seeded_patients):
    import datetime

    p = Patient.objects.get(condition_id="KR-2024-0004")
    row = BiochemResult.objects.create(patient=p, test="ALT", value="80 U/L", measured_on=datetime.date(2025, 2, 1),
                                       reference_range="old", flag="")
    a = _lab_upload(client, demo_users)
    row.refresh_from_db()
    assert (row.reference_range, row.flag) == ("7-56", "H")
    assert DerivedBaseline.objects.get(patient=p, domain="biochem").values == {
        str(row.pk): {"reference_range": "old", "flag": ""}}
    assert Submission.objects.get(upload_id=a).apply_summary["biochem_values"] == {
        str(row.pk): {"reference_range": "7-56", "flag": "H"}}
    _request_delete(client, demo_users["researcher"], a)
    assert deletion.plan(Submission.objects.get(upload_id=a))["notes"]
    deletion.approve_deletion(Submission.objects.get(upload_id=a), demo_users["admin"])
    row.refresh_from_db()
    assert (row.reference_range, row.flag) == ("old", "")  # 시드 행은 남고 값은 기준 값으로
    # 업로드 밖에서 바뀐 값도 재계산(기준 값)으로 맞춘다
    b = _lab_upload(client, demo_users)
    BiochemResult.objects.filter(pk=row.pk).update(flag="L")
    _delete(client, demo_users, b)
    row.refresh_from_db()
    assert (row.reference_range, row.flag) == ("old", "")


def test_biochem_value_kept_while_another_upload_wrote_it(client, demo_users, seeded_patients):
    """2차 리뷰 PLAUSIBLE: A·B 가 같은 기존 행을 갱신 → A 삭제 후에도 B 가 쓴 값 유지, B 삭제 후 기준 값."""
    import datetime

    p = Patient.objects.get(condition_id="KR-2024-0004")
    row = BiochemResult.objects.create(patient=p, test="ALT", value="80 U/L", measured_on=datetime.date(2025, 2, 1),
                                       reference_range="old", flag="")
    a = _lab_upload(client, demo_users)
    b = _lab_upload(client, demo_users)
    _delete(client, demo_users, a)
    row.refresh_from_db()
    assert (row.reference_range, row.flag) == ("7-56", "H")
    _delete(client, demo_users, b)
    row.refresh_from_db()
    assert (row.reference_range, row.flag) == ("old", "")


# ─── 5. 수정 요청(→임시저장)·재제출 시 삭제 요청 정리 ───
def test_revision_clears_pending_deletion_request(client, demo_users, seeded_patients):
    uid = _genomic_upload(client, demo_users, approve=False)
    _request_delete(client, demo_users["researcher"], uid)
    assert deletion.pending_deletions().filter(upload_id=uid).exists()
    sub = Submission.objects.get(upload_id=uid)
    workflow.request_revision(sub, demo_users["admin"], "메타데이터 보완")
    sub.refresh_from_db()
    assert sub.status == "draft" and sub.deletion_requested_at is None and not sub.deletion_pending
    assert SubmissionEvent.objects.filter(submission=sub, action="deletion_cleared").exists()
    client.force_login(demo_users["researcher"])
    for step in ("files", "data"):  # 수정 요청은 1단계(파일)로 되돌린다
        client.post("/upload/genomic/proceed", {"id": uid, "from": step})
    _submit(client, "genomic", uid)
    sub.refresh_from_db()
    assert sub.status == "pending" and not deletion.pending_deletions().filter(pk=sub.pk).exists()
    assert sub.deletion_requested_at is None
    # 다시 요청하면 새로 접수된다(조용히 무시되지 않음)
    assert workflow.request_deletion(sub, demo_users["researcher"]) is True
    assert deletion.pending_deletions().filter(pk=sub.pk).exists()


def test_resubmit_clears_stale_deletion_fields(client, demo_users, seeded_patients):
    """예전 데이터처럼 임시저장에 삭제 요청 필드가 남아 있어도 재제출 시 정리하고 이력을 남긴다."""
    from django.utils import timezone

    uid = _genomic_upload(client, demo_users, approve=False)
    sub = Submission.objects.get(upload_id=uid)
    Submission.objects.filter(pk=sub.pk).update(status="draft", deletion_requested_at=timezone.now())
    client.force_login(demo_users["researcher"])
    _submit(client, "genomic", uid)
    sub.refresh_from_db()
    assert sub.status == "pending" and sub.deletion_requested_at is None
    assert not deletion.pending_deletions().filter(pk=sub.pk).exists()
    assert SubmissionEvent.objects.filter(submission=sub, action="deletion_cleared").exists()


def test_duplicate_deletion_request_shows_message(client, demo_users, seeded_patients):
    uid = _genomic_upload(client, demo_users, approve=False)
    _request_delete(client, demo_users["researcher"], uid)
    r = client.post(f"/upload/mine/{uid}/delete", follow=True)
    assert "이미 삭제 요청되어 관리자 처리를 기다리고 있습니다." in r.content.decode()
    assert SubmissionEvent.objects.filter(submission__upload_id=uid, action="deletion_requested").count() == 1
    client.post("/lang", {"lang": "en"})
    r = client.post(f"/upload/mine/{uid}/delete", follow=True)
    assert "already pending" in r.content.decode()


def test_activity_log_shows_cleared_event(client, demo_users, seeded_patients):
    uid = _genomic_upload(client, demo_users, approve=False)
    _request_delete(client, demo_users["researcher"], uid)
    workflow.request_revision(Submission.objects.get(upload_id=uid), demo_users["admin"], "보완")
    client.force_login(demo_users["admin"])
    r = client.get("/admin")
    assert r.status_code == 200 and "삭제 요청 해제" in r.content.decode()

