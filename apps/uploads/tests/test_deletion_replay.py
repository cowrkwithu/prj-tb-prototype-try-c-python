"""3c 3차 리뷰 재현 — 삭제 = 환자×도메인 재적용(replay).

1. 한 행에 파일 여러 개 + 업로드 간 부분 공유(HTTP 경로, 리뷰어 재현 그대로) — W1(전부 승인 후 삭제) == W2(남은 것만 승인).
2. 예전 건(recompute 표지 없음)과 새 건이 섞인 환자 — 다시 적용하지 않고 (환자, 도메인) 별로 안내.
3. 잠금 순서(환자 먼저, 한 문장) · 예외 처리(DatabaseError 만 안내, LookupError 는 드러냄) · 동시성.
4. approval_seq 데이터 마이그레이션 왕복.
"""
from __future__ import annotations

import datetime
import gzip
import threading

import pytest
from django.contrib.messages.storage.cookie import CookieStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.query import QuerySet
from django.test import RequestFactory
from django.utils import timezone

from apps.cases.models import ImagingStudy, Patient, SequenceRun
from apps.uploads import admin_views, deletion_views
from apps.uploads.models import DeletedSubmission, DerivedBaseline, Submission
from apps.uploads.services import apply, deletion, dicom, workflow

from .conftest import csv_upload, upload_id_from
from .test_deletion_invariant import P2, World, gen, img, lab, reg, snapshot
from .test_flow import _approve, _submit

P = P2  # KR-2024-0002 (시드: CXR 5장)


# ─── HTTP 업로드 도우미 ───
def _fq(key: str) -> bytes:
    return gzip.compress(f"@{key}\nACGTACGT\n+\nIIIIIIII\n".encode(), mtime=0)  # 내용이 같으면 SHA-256 도 같다


def http_genomic(client, users, files, rows):
    """files=[(파일명, 내용 키)], rows=[(대상자, 검체, SRA, 계통)] — 파일은 요청마다 1개(리뷰 결정)."""
    client.force_login(users["researcher"])
    uid = None
    for k, (name, key) in enumerate(files):
        post = {"mode": "batch", "files": [SimpleUploadedFile(name, _fq(key), content_type="application/gzip")]}
        if uid:
            post["id"] = uid
        if k == len(files) - 1:
            post["action"] = "next"
        r = client.post("/upload/genomic/files", post)
        uid = uid or upload_id_from(r)
    meta = "condition_id,specimen_id,platform,method,coverage,sra_accession,lineage\n" + "".join(
        f"{c},{s},Illumina,WGS,100,{a},{lin}\n" for c, s, a, lin in rows)
    client.post("/upload/genomic/csv", {"id": uid, "file": csv_upload("meta.csv", meta.encode())})
    client.post("/upload/genomic/proceed", {"id": uid, "from": "data"})
    _submit(client, "genomic", uid)
    assert Submission.objects.get(upload_id=uid).status == "pending"
    return uid


def http_imaging(client, users, keys, findings=(), cid=P):
    client.force_login(users["researcher"])
    uid = None
    for k, key in enumerate(keys):
        data = dicom.build_minimal_dicom({(0x0008, 0x0080): "SNUBH", (0x0020, 0x000D): f"1.2.410.{key}"})
        post = {"mode": "CXR", "condition_id": cid, "study_date": "2025-05-01",
                "files": [SimpleUploadedFile(f"{key}.dcm", data, content_type="application/dicom")]}
        if uid:
            post["id"] = uid
        if k == len(keys) - 1:
            post["action"] = "next"
        r = client.post("/upload/imaging/files", post)
        uid = uid or upload_id_from(r)
    client.post("/upload/imaging/proceed", {"id": uid, "from": "dicom"})
    n = len(findings)
    client.post("/upload/imaging/data", {"id": uid, "study_description": "x", "tb_activity": "Active",
                                         "ann_finding": list(findings), "ann_location": ["Right"] * n,
                                         "ann_severity": ["Mild"] * n, "ann_description": [""] * n,
                                         "impression": "TB", "action": "next"})
    _submit(client, "imaging", uid)
    assert Submission.objects.get(upload_id=uid).status == "pending"
    return uid


def _http_world(client, users, builders, approve, delete):
    """업로드를 모두 만든 뒤(ID 가 두 세계에서 같게) approve 순서로 승인, delete 순서로 HTTP 삭제 승인."""
    uids = [b() for b in builders]
    for i in approve:
        client.force_login(users["admin"])
        _approve(client, uids[i])
        assert Submission.objects.get(upload_id=uids[i]).status == "approved"
    for i in delete:
        client.force_login(users["researcher"])
        client.post(f"/upload/mine/{uids[i]}/delete")
        client.force_login(users["admin"])
        r = client.post(f"/admin/deletions/{uids[i]}/approve")
        assert r.status_code == 302 and r["Location"] == "/admin/deletions"
        assert not Submission.objects.filter(upload_id=uids[i]).exists()
    return uids, snapshot([P])


def _w1_equals_w2(client, users, builders, delete):
    n = len(builders)
    sid = transaction.savepoint()
    _, expected = _http_world(client, users, builders, [i for i in range(n) if i not in delete], [])
    transaction.savepoint_rollback(sid)
    uids, actual = _http_world(client, users, builders, list(range(n)), delete)
    assert actual == expected, f"\nactual={actual}\nexpected={expected}"
    for i in delete:  # 지운 업로드의 파일을 참조하는 행은 없다
        assert not SequenceRun.objects.filter(files__submission__upload_id=uids[i]).exists()
        assert not ImagingStudy.objects.filter(files__submission__upload_id=uids[i]).exists()
    return uids, actual[P]


@pytest.fixture
def users(demo_users, seeded_patients):
    return demo_users


# ─── 1. 리뷰어 재현(HTTP) ───
@pytest.mark.django_db
def test_http_cxr_partial_overlap(client, users):
    """A{f1,f2}, B{f1}, C{f2} 승인 후 A 삭제 → 검사 2건(B, C), cxr_count 7, C 의 섬유화 유지."""
    b = [lambda: http_imaging(client, users, ["f1", "f2"], ["Cavity"]),
         lambda: http_imaging(client, users, ["f1"], ["Nodule"]),
         lambda: http_imaging(client, users, ["f2"], ["Fibrosis"])]
    uids, s = _w1_equals_w2(client, users, b, [0])
    assert [x[0] for x in s["studies"]] == [uids[1], uids[2]]
    assert s["cxr_count"] == 7 and "섬유화" in s["cxr_findings"]


@pytest.mark.django_db
def test_http_genomic_partial_overlap(client, users):
    """A{x1,x2} SRR1, B{x1} SRR2, C{x2} SRR3 승인 후 A 삭제 → 런 2건, SRA 는 SRR3."""
    b = [lambda: http_genomic(client, users, [("TB_x1.fastq.gz", "x1"), ("TB_x2.fastq.gz", "x2")],
                              [(P, "SP-1", "SRR100001", "Beijing")]),
         lambda: http_genomic(client, users, [("TB_x1.fastq.gz", "x1")], [(P, "SP-2", "SRR100002", "")]),
         lambda: http_genomic(client, users, [("TB_x2.fastq.gz", "x2")], [(P, "SP-3", "SRR100003", "")])]
    uids, s = _w1_equals_w2(client, users, b, [0])
    assert [r[0] for r in s["runs"]] == [uids[1], uids[2]] and s["sra_id"] == "SRR100003"


@pytest.mark.django_db
def test_http_imaging_superset_two_deletions(client, users):
    """A{f1}, B{f2}, C{f1,f2} 승인 후 A·B 삭제 → C 소유 검사 1건, cxr_count 6."""
    b = [lambda: http_imaging(client, users, ["f1"]), lambda: http_imaging(client, users, ["f2"]),
         lambda: http_imaging(client, users, ["f1", "f2"], ["Fibrosis"])]
    uids, s = _w1_equals_w2(client, users, b, [0, 1])
    assert [x[0] for x in s["studies"]] == [uids[2]] and s["cxr_count"] == 6


@pytest.mark.django_db
def test_http_batch_two_rows_skip_one_run(client, users):
    """B(일괄)의 두 행이 A 의 런 하나를 함께 중복 건너뜀 → A 삭제 후 B 의 런 2건(W2)."""
    b = [lambda: http_genomic(client, users, [("TB_x1.fastq.gz", "x1"), ("TB_x2.fastq.gz", "x2")],
                              [(P, "SP-1", "SRR100001", "Beijing")]),
         lambda: http_genomic(client, users, [("TB_SPA.fastq.gz", "x1"), ("TB_SPB.fastq.gz", "x2")],
                              [(P, "SPA", "SRR100002", ""), (P, "SPB", "SRR100003", "LAM")])]
    uids, s = _w1_equals_w2(client, users, b, [0])
    assert [(r[0], r[1]) for r in s["runs"]] == [(uids[1], "SPA"), (uids[1], "SPB")]
    assert (s["sra_id"], s["lineage"]) == ("SRR100003", "LAM")


@pytest.mark.django_db
def test_plan_page_shows_per_case_mode(client, users):
    a = http_imaging(client, users, ["f1", "f2"])
    b = http_imaging(client, users, ["f1"])
    client.force_login(users["admin"])
    _approve(client, a)
    _approve(client, b)
    client.force_login(users["researcher"])
    client.post(f"/upload/mine/{a}/delete")
    client.force_login(users["admin"])
    page = client.get(f"/admin/deletions/{a}").content.decode()
    assert 'data-mode="replay"' in page and f"{P} · 영상 — 다시 적용" in page and b in page
    client.cookies["lang"] = "en"
    session = client.session
    session["lang"] = "en"
    session.save()
    page = client.get(f"/admin/deletions/{a}").content.decode()
    assert "Re-applied" in page


@pytest.mark.django_db
def test_approval_seq_assigned_in_order(client, users):
    a = http_imaging(client, users, ["f1"])
    b = http_imaging(client, users, ["f2"])
    client.force_login(users["admin"])
    _approve(client, b)
    _approve(client, a)
    sa, sb = Submission.objects.get(upload_id=a), Submission.objects.get(upload_id=b)
    assert sb.approval_seq == 1 and sa.approval_seq == 2
    assert Submission.objects.filter(status="pending").filter(approval_seq__isnull=False).count() == 0


# ─── 2. 예전 건(recompute 표지 없음) + 새 건 혼재 ───
def _make_legacy(sub_uid: str, domain_key: str):
    """3c 이전 승인 건 흉내: recompute 표지·공유 기록을 지우고, 기준 스냅샷도 없앤다(0005 적용 직후 상태)."""
    s = Submission.objects.get(upload_id=sub_uid)
    legacy = {k: v for k, v in s.apply_summary.items() if k not in ("recompute", domain_key)}
    Submission.objects.filter(pk=s.pk).update(apply_summary=legacy)
    DerivedBaseline.objects.all().delete()


@pytest.mark.django_db
def test_legacy_mixed_genomic_not_replayed(client, users):
    w = World(users)
    p = Patient.objects.get(condition_id=P)
    old = w.build(gen(P, "x1", "SRR1", "Beijing"), 0)
    w.approve(old)
    _make_legacy(old.upload_id, "shared_sequence_ids")
    new = w.build(gen(P, "x2", "SRR2", "LAM"), 1)
    w.approve(new)
    base = DerivedBaseline.objects.get(patient=p, domain="genomic")
    assert base.legacy_mixed  # 예전 건 효과(SRR1)가 든 값을 기준으로 잡았다
    for uid in (old.upload_id, new.upload_id):
        Submission.objects.filter(upload_id=uid).update(deletion_requested_at=timezone.now())
        plan = deletion.plan(Submission.objects.get(upload_id=uid))
        assert [x["mode"] for x in plan["pairs"]] == ["legacy"] and not plan["notes"]
        assert any(P in x["ko"] and "유전체 요약" in x["ko"] and "genomic summary" in x["en"]
                   for x in plan["irreversible"])
    client.force_login(users["admin"])
    page = client.get(f"/admin/deletions/{new.upload_id}").content.decode()
    assert 'data-mode="legacy"' in page and "예전 건 — 되돌리지 않음" in page
    w.delete(old.upload_id)  # 예전 건 삭제: 행만 지우고 요약은 그대로
    p.refresh_from_db()
    assert (p.sra_id, p.lineage) == ("SRR2", "LAM")
    assert [r.submission.upload_id for r in SequenceRun.objects.filter(patient=p)] == [new.upload_id]
    # 예전 건이 지워진 뒤에도 기준 스냅샷이 예전 건 효과를 품고 있으므로 새 건 삭제도 다시 적용하지 않는다
    plan = deletion.plan(Submission.objects.get(upload_id=new.upload_id))
    assert [x["mode"] for x in plan["pairs"]] == ["legacy"]
    w.delete(new.upload_id)
    p.refresh_from_db()
    assert not SequenceRun.objects.filter(patient=p).exists()
    assert (p.sra_id, p.lineage) == ("SRR2", "LAM")  # L 기준(SRR1)으로 '복원'하지 않는다
    log = DeletedSubmission.objects.get(upload_id=new.upload_id)
    assert log.summary["pairs"] == [{"condition_id": P, "mode": "legacy"}] and log.summary["irreversible"]


@pytest.mark.django_db
def test_legacy_mixed_imaging_counts_only(users):
    w = World(users)
    p = Patient.objects.get(condition_id=P)
    old = w.build(img(P, "f1", findings=["Fibrosis"]), 0)
    w.approve(old)
    _make_legacy(old.upload_id, "shared_imaging_ids")
    new = w.build(img(P, "f2", findings=["Cavity"]), 1)
    w.approve(new)
    p.refresh_from_db()
    assert p.cxr_count == 7
    w.delete(new.upload_id)
    p.refresh_from_db()
    assert p.cxr_count == 6 and "공동" in p.cxr_findings and "섬유화" in p.cxr_findings  # 수만 줄임
    assert [s.submission.upload_id for s in ImagingStudy.objects.filter(patient=p)] == [old.upload_id]


@pytest.mark.django_db
def test_legacy_only_on_affected_case(users):
    """일괄 업로드가 두 환자를 건드렸고 한 환자에만 예전 건이 있다 → 그 환자만 legacy, 다른 환자는 다시 적용."""
    from .test_deletion_invariant import P3

    w = World(users)
    old = w.build(gen(P3, "z9", "SRR9", "T"), 0)
    w.approve(old)
    _make_legacy(old.upload_id, "shared_sequence_ids")
    a = w.build(gen(None, None, mode="batch", rows=[(P, "SPA", "x1", "SRR1", ""), (P3, "SPB", "x2", "SRR2", "")]), 1)
    b = w.build(gen(P, "x1", "SRR5", "LAM"), 2)
    w.approve(a)
    w.approve(b)
    Submission.objects.filter(pk=a.pk).update(deletion_requested_at=timezone.now())
    plan = deletion.plan(Submission.objects.get(pk=a.pk))
    assert {x["condition_id"]: x["mode"] for x in plan["pairs"]} == {P: "replay", P3: "legacy"}
    assert len(plan["irreversible"]) == 1 and P3 in plan["irreversible"][0]["ko"]
    w.delete(a.upload_id)
    runs = list(SequenceRun.objects.filter(patient__condition_id=P).select_related("submission"))
    assert [(r.submission.upload_id, r.sra_accession) for r in runs] == [(b.upload_id, "SRR5")]


# ─── 3. 잠금 순서 · 예외 처리 · 동시성 ───
def _spy_select_for_update(monkeypatch):
    log = []
    real = QuerySet.select_for_update

    def spy(self, *a, **kw):
        log.append(self.model.__name__)
        return real(self, *a, **kw)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)
    return log


@pytest.mark.django_db
def test_lock_order_patients_first(users, monkeypatch):
    from .test_deletion_invariant import P3

    w = World(users)
    a = w.build(gen(None, None, mode="batch", rows=[(P3, "SPB", "x2", "", ""), (P, "SPA", "x1", "", "")]), 0)
    log = _spy_select_for_update(monkeypatch)
    w.approve(a)
    assert log[:2] == ["Patient", "Submission"] and log.count("Patient") == 1  # 환자 한 문장 → 업로드
    assert apply.lock_patients(apply.patient_ids_for(a)) == sorted(apply.patient_ids_for(a))
    log.clear()
    w.delete(a.upload_id)
    assert log[:2] == ["Patient", "Submission"] and log.count("Patient") == 1


@pytest.mark.django_db
def test_review_database_error_becomes_message(client, users, monkeypatch):
    uid = http_imaging(client, users, ["f1"])

    def boom(*a, **kw):
        raise OperationalError("database is locked")

    monkeypatch.setattr(workflow, "approve", boom)
    client.force_login(users["admin"])
    r = client.post(f"/admin/review/{uid}/approve", {"check": ["0", "1", "2", "3", "4"]}, follow=True)
    assert r.status_code == 200 and "다시 시도" in r.content.decode()
    assert Submission.objects.get(upload_id=uid).status == "pending"


@pytest.mark.django_db
def test_lookup_error_is_not_hidden(client, users, monkeypatch):
    uid = http_imaging(client, users, ["f1"])
    client.force_login(users["admin"])
    _approve(client, uid)
    client.force_login(users["researcher"])
    client.post(f"/upload/mine/{uid}/delete")

    def bug(*a, **kw):
        raise KeyError("bug")

    monkeypatch.setattr(deletion, "approve_deletion", bug)
    monkeypatch.setattr(workflow, "reject", bug)
    client.force_login(users["admin"])
    with pytest.raises(KeyError):
        client.post(f"/admin/deletions/{uid}/approve")
    uid2 = http_imaging(client, users, ["f9"])
    client.force_login(users["admin"])
    with pytest.raises(KeyError):
        client.post(f"/admin/review/{uid2}/reject", {"reason": "x"})


@pytest.mark.django_db
def test_replay_leaving_deleted_files_raises(users, monkeypatch):
    """재적용 뒤 지운 업로드의 파일을 참조하는 행이 남으면 예외(롤백) — 조용히 넘어가지 않는다."""
    w = World(users)
    a = w.build(img(P, ("f1", "f2")), 0)
    b = w.build(img(P, "f1"), 1)
    w.approve(a)
    w.approve(b)
    real = deletion._replay

    def bad_replay(s, pairs):
        out = real(s, pairs)
        ImagingStudy.objects.filter(submission=b).first().files.add(*s.files.all())
        return out

    monkeypatch.setattr(deletion, "_replay", bad_replay)
    with pytest.raises(RuntimeError):
        w.delete(a.upload_id)
    assert Submission.objects.filter(pk=a.pk).exists()  # 롤백


def _call(view, uid, user, results, errors, barrier, path, data=None):
    try:
        req = RequestFactory().post(path, data or {})
        req.user = user
        req.session = {}
        req._messages = CookieStorage(req)
        barrier.wait(timeout=10)
        results[uid] = view(req, uid).status_code
    except Exception as exc:  # 테스트가 실패 원인을 보이도록 모은다
        errors.append(repr(exc))
    finally:
        connection.close()


@pytest.mark.django_db(transaction=True)
def test_concurrent_approve_and_delete_consistent(demo_users, seeded_patients):
    """삭제 승인(A)과 같은 파일을 가진 B 의 승인이 동시에 — 500 없이, 결과가 직렬 실행 중 하나와 같다."""
    w = World(demo_users)
    a = w.build(img(P, ("f1", "f2"), findings=["Cavity"]), 0)
    b = w.build(img(P, "f1", findings=["Nodule"]), 1)
    w.approve(a)
    Submission.objects.filter(pk=a.pk).update(deletion_requested_at=timezone.now())
    results, errors = {}, []
    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=_call, args=(deletion_views.deletion_approve, a.upload_id, demo_users["dmanager"],
                                             results, errors, barrier, f"/admin/deletions/{a.upload_id}/approve")),
        threading.Thread(target=_call, args=(admin_views.review_approve, b.upload_id, demo_users["admin"], results,
                                             errors, barrier, f"/admin/review/{b.upload_id}/approve",
                                             {"check": ["0", "1", "2", "3", "4"]})),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert set(results.values()) == {302}
    a_gone = not Submission.objects.filter(pk=a.pk).exists()
    b_ok = Submission.objects.get(pk=b.pk).status == "approved"
    studies = [(s.submission.upload_id, sorted(f.submission_id for f in s.files.all()))
               for s in ImagingStudy.objects.filter(patient__condition_id=P).select_related("submission")]
    p = Patient.objects.get(condition_id=P)
    if a_gone and b_ok:  # 어느 순서든 직렬 결과: B 만 승인된 상태
        assert studies == [(b.upload_id, [b.pk])] and p.cxr_count == 6
    elif a_gone:
        assert studies == [] and p.cxr_count == 5
    elif b_ok:  # 삭제가 겹쳐 실패(다시 시도 안내) — A 승인 후 B 는 중복으로 건너뜀
        assert studies == [(a.upload_id, [a.pk, a.pk])] and p.cxr_count == 6
    else:
        pytest.fail("neither operation succeeded")


# ─── 4. 데이터 마이그레이션 왕복 ───
@pytest.mark.django_db(transaction=True)
def test_approval_seq_migration_roundtrip():
    old_targets = [("uploads", "0005_derived_baseline"), ("cases", "0003_upload_domain_tables")]
    executor = MigrationExecutor(connection)
    leaf = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(old_targets)
        executor.loader.build_graph()
        apps_ = executor.loader.project_state(old_targets).apps
        User = apps_.get_model("accounts", "User")
        Pat = apps_.get_model("cases", "Patient")
        Sub = apps_.get_model("uploads", "Submission")
        Rec = apps_.get_model("uploads", "SubmissionRecord")
        Bio = apps_.get_model("cases", "BiochemResult")
        Run = apps_.get_model("cases", "SequenceRun")
        Base = apps_.get_model("uploads", "DerivedBaseline")
        u = User.objects.create(username="mig", password="x")
        today = datetime.date(2025, 1, 1)
        p = Pat.objects.create(condition_id="MIG-1", age=30, sex="M", country="한국", site="x", diagnosis_type="DS-TB",
                               treatment_outcome="Cured", treatment_duration=1, registration_date=today,
                               last_update_date=today)
        t0 = timezone.now()
        mk = lambda uid, at, **kw: Sub.objects.create(upload_id=uid, submitter=u, domain=kw.pop("domain", "lab"),
                                                      method="csv", mode="csv", status=kw.pop("status", "approved"),
                                                      reviewed_at=at, **kw)
        s3 = mk("M-3", t0 + datetime.timedelta(seconds=5), apply_summary={"recompute": 1})
        s1 = mk("M-1", t0, apply_summary={"recompute": 1})
        s2 = mk("M-2", t0, apply_summary={"recompute": 1})  # 같은 시각 → id 순
        mk("M-P", None, status="pending")
        legacy = mk("M-L", t0 - datetime.timedelta(days=1), domain="genomic", apply_summary={"created": 1})
        Rec.objects.create(submission=legacy, row_no=1, payload={"condition_id": "MIG-1"})
        b1 = Bio.objects.create(patient=p, test="ALT", value="1")
        b2 = Bio.objects.create(patient=p, test="AST", value="2")
        Sub.objects.filter(pk=s1.pk).update(apply_summary={"recompute": 1, "created_biochem_ids": [b1.pk]})
        Sub.objects.filter(pk=s2.pk).update(apply_summary={"recompute": 1, "created_biochem_ids": [b1.pk, b2.pk]})
        Base.objects.create(patient=p, domain="genomic", values={})
        run = Run.objects.create(patient=p, submission=s3, specimen_id="x", platform="x", method="WGS")
        p2 = Pat.objects.create(condition_id="MIG-2", age=30, sex="M", country="한국", site="x", diagnosis_type="DS-TB",
                                treatment_outcome="Cured", treatment_duration=1, registration_date=today,
                                last_update_date=today)
        Base.objects.create(patient=p2, domain="imaging", values={}, preexisting_ids=[])
        Base.objects.create(patient=p2, domain="genomic", values={}, preexisting_ids=[run.pk])

        def check_forward():
            executor.loader.build_graph()
            new = executor.loader.project_state(leaf).apps
            S = new.get_model("uploads", "Submission")
            seqs = dict(S.objects.values_list("upload_id", "approval_seq"))
            assert seqs == {"M-L": 1, "M-1": 2, "M-2": 3, "M-3": 4, "M-P": None}
            B = new.get_model("cases", "BiochemResult")
            assert dict(B.objects.values_list("test", "submission__upload_id")) == {"ALT": "M-1", "AST": "M-2"}
            D = new.get_model("uploads", "DerivedBaseline")
            flags = {(b.patient.condition_id, b.domain): b.legacy_mixed for b in D.objects.select_related("patient")}
            assert flags == {("MIG-1", "genomic"): True, ("MIG-2", "imaging"): False, ("MIG-2", "genomic"): True}

        executor.migrate(leaf)
        check_forward()
        executor.loader.build_graph()
        executor.migrate(old_targets)  # 되돌리기(필드 제거)
        executor.loader.build_graph()
        executor.migrate(leaf)  # 다시 적용 — 같은 결과
        check_forward()
    finally:
        executor.loader.build_graph()
        executor.migrate(leaf)


# ─── 5. 최초 승인 == 재적용(같은 코드 경로) ───
@pytest.mark.django_db
@pytest.mark.parametrize("make", [
    lambda: [gen(None, None, mode="batch", rows=[(P, "SPA", ("x1", "x2"), "SRR1", "LAM"), (P, "SPB", "x1", "", "T")]),
             gen(P, ("x2", "x3"), "SRR2", "")],
    lambda: [img(P, ("f1", "f2"), findings=["Cavity"]), img(P, "f2", "CT"), img(P, ("f2", "f3"), findings=["Fibrosis"])],
    lambda: [reg(None, rows=[(P, ["Isoniazid"]), ("UNKNOWN-9", ["Rifampicin"])]), reg(P, "Isoniazid")],
    lambda: [lab(None, rows=[(P, [("ALT", "80"), ("AST", "50")], "2025-02-01"), (P, [("ALT", "80")], "2025-02-01")]),
             lab(P, ("ALT", "80"))],
], ids=["genomic", "imaging", "regimen", "lab"])
def test_replay_reproduces_initial_approval(users, make):
    """승인 직후 상태를 기억 → 그 환자의 업로드 소유 행을 지우고 기준으로 되돌린 뒤 모든 승인 업로드를 replay=True 로
    다시 적용 → 상태와 (pk 무관) 요약이 최초 승인과 같다. 최초 승인과 재적용이 같은 함수라는 것의 확인."""
    w = World(users)
    subs = [w.build(spec, k) for k, spec in enumerate(make())]
    for s in subs:
        w.approve(s)
    before = snapshot([P])
    originals = [Submission.objects.get(pk=s.pk) for s in subs]
    pid = Patient.objects.get(condition_id=P).pk
    domain = subs[0].domain
    with transaction.atomic():
        deletion.ROW_MODEL[domain].objects.filter(patient_id=pid, submission__isnull=False).delete()
        deletion._reset_to_baseline(domain, [pid])
        replayed = [apply.apply_submission(s, only={pid}, replay=True) for s in originals]
    assert snapshot([P]) == before
    keys = ("created", "duplicates", "dropped", "rows", "unmatched_files")
    for orig, again in zip(originals, replayed):
        assert {k: orig.apply_summary.get(k) for k in keys} == {k: again.get(k) for k in keys}
        if domain == "regimen":
            # 미등록 대상자 행은 다시 적용하지 않는다. 5단계부터 재적용은 대상 환자(only) 행만 읽으므로
            # 대상 밖(미등록 UNKNOWN-9) 행은 요약에도 나오지 않는다 — 승인 당시 요약에는 남아 있다.
            assert {x["condition_id"] for x in orig.apply_summary["skipped"]} <= {"UNKNOWN-9"}
            assert again["skipped"] == []
    if domain == "regimen":
        assert [x["condition_id"] for x in originals[0].apply_summary["skipped"]] == ["UNKNOWN-9"]
    assert all(Submission.objects.get(pk=s.pk).apply_summary == s.apply_summary for s in originals)  # 이력 보존


@pytest.mark.django_db
def test_clinical_is_not_replayable(users):
    s = Submission(domain="clinical")
    with pytest.raises(ValueError, match="not replayed"):
        apply.apply_submission(s, replay=True)
