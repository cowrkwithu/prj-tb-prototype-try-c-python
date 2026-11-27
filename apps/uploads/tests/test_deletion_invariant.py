"""삭제 승인 불변식 하네스 (3c 2차 리뷰, 3차 리뷰에서 확장 — 환자×도메인 재적용 replay).

불변식: 업로드 S 를 순서대로 승인한 뒤 일부를 (어떤 순서로든) 삭제한 상태(W1)
      == 남은 업로드만 같은 순서(approval_seq)로 승인한 상태(W2).

3차 확장: 행당 파일 1~3개 + 업로드 간 부분 겹침, 일괄(여러 환자·같은 환자 여러 행) 업로드, 4개 도메인,
승인 시각이 모두 같거나 거꾸로 가는 시계(정렬은 approval_seq 만 써야 한다), 시드 고정 무작위 240개.

각 시나리오는 savepoint 롤백으로 두 세계를 만들어 비교한다 — 삭제를 한 건 할 때마다(접두 단계마다) 비교한다.
비교 대상(환자별): 유전체·영상 파생 필드(cxr_findings 는 순서까지), 레지멘(승인 순서·소유 업로드·약물 구성),
SequenceRun 메타+파일(sha256·소유 업로드), ImagingStudy 메타+파일, BiochemResult(항목·값·검사일·참고범위·판정·소유).
제외: last_update_date('마지막 반영일'), 생화학 표시 순서(order — 만든 시점의 개수), 행 pk·created_at,
임상 덮어쓰기·DST·도말·배양(설계상 되돌리지 않음 — 그래서 시나리오에 넣지 않는다).
"""
from __future__ import annotations

import datetime
import hashlib
import random

import pytest
from django.db import transaction
from django.utils import timezone

from apps.cases.models import BiochemResult, ImagingStudy, Patient, Regimen, RegimenDrug, SequenceRun
from apps.uploads.models import Submission, SubmissionFile, SubmissionRecord
from apps.uploads.services import deletion, derived, schemas, workflow

pytestmark = pytest.mark.django_db

P2, P3 = "KR-2024-0002", "KR-2024-0003"
CHECKLIST = [str(i) for i in range(len(schemas.REVIEW_CHECK_ITEMS))]
DRUGS = {"Isoniazid": "H", "Rifampicin": "R", "Ethambutol": "E"}


# ─── 업로드 명세 → 업로드 ───
def _contents(x) -> list[str]:
    return [x] if isinstance(x, str) else list(x)


def gen(cid, content, sra="", lineage="", spec="SP-1", mode="individual", rows=None):
    """유전체 업로드 명세. content 는 파일 내용 키 하나 또는 여러 개(행당 파일 1~3개).
    rows=[(cid, spec, content, sra, lineage), ...] 면 일괄(같은 제출 내 여러 환자·같은 환자 여러 행)."""
    if rows is None:
        rows = [(cid, spec, content, sra, lineage)]
    files = []
    for _, s_, x, _, _ in rows:
        keys = _contents(x)
        for k, key in enumerate(keys):
            if mode == "batch":
                name = f"TB_{s_}.fastq.gz" if len(keys) == 1 else f"TB_{s_}_{k}.fastq.gz"
            else:
                name = "reads.fastq.gz" if len(keys) == 1 else f"reads{k}.fastq.gz"
            files.append((name, f"seq-{key}"))
    return {"domain": "genomic", "mode": mode, "rows": [
        {"condition_id": c, "specimen_id": s_, "platform": "Illumina" if mode == "batch" else "Illumina MiSeq",
         "method": "WGS", "coverage": "100", "sra_accession": a, "lineage": lin} for c, s_, _, a, lin in rows],
        "files": files}


def img(cid, content, modality="CXR", findings=(), date="2025-05-01", activity="Active", impression="TB"):
    keys = _contents(content)
    return {"domain": "imaging", "mode": modality, "rows": [
        {"condition_id": cid, "modality": modality, "study_date": date, "study_description": f"{modality} study",
         "tb_activity": activity, "impression": impression,
         "findings": [{"finding": f, "location": "Right", "severity": "Mild", "description": ""} for f in findings]}],
        "files": [(f"s{k}.dcm" if len(keys) > 1 else "s.dcm", f"img-{key}") for k, key in enumerate(keys)]}


def reg(cid, *names, rows=None):
    """레지멘 명세. rows=[(cid, [약물…]), …] 면 여러 환자."""
    rows = rows if rows is not None else [(cid, list(names))]
    return {"domain": "regimen", "mode": "csv", "rows": [
        {"condition_id": c, "drug_name": n, "drug_code": DRUGS[n], "start_day": "0", "end_day": "180",
         "dosage": "300mg", "route": "Oral"} for c, ns in rows for n in ns], "files": []}


def lab(cid, *tests, date="2025-02-01", rows=None):
    """검사실 명세. rows=[(cid, [(검사, 값)…], 날짜), …] 면 여러 환자·여러 행."""
    rows = rows if rows is not None else [(cid, list(tests), date)]
    return {"domain": "lab", "mode": "csv", "rows": [
        {"condition_id": c, "specimen_id": f"SP-{k}", "specimen_date": d, "specimen_type": "Sputum",
         "biochemistry": [{"test": t, "value": v, "unit": "U/L", "date": d} for t, v in ts]}
        for k, (c, ts, d) in enumerate(rows)], "files": []}


class World:
    def __init__(self, users):
        self.admin, self.researcher = users["admin"], users["researcher"]

    def build(self, spec: dict, tag: int) -> Submission:
        uid = f"UPL-29990101-{tag + 1:03d}"
        resub_from = None
        if spec.get("resubmit"):  # 재제출 체인: 반려된 원본 → 같은 내용의 재제출이 승인된다
            resub_from = self._create(spec, f"UPL-29990102-{tag + 1:03d}", None)
            workflow.reject(resub_from, self.admin, "보완 필요")
        return self._create(spec, uid, resub_from)

    def _create(self, spec, uid, resub_from):
        domain, mode = spec["domain"], spec["mode"]
        sub = Submission.objects.create(
            upload_id=uid, submitter=self.researcher, domain=domain, mode=mode,
            method=schemas.method_for(domain, mode), status=Submission.Status.PENDING, submitted_at=timezone.now(),
            csv_headers=sorted({k for r in spec["rows"] for k in r}), resubmitted_from=resub_from)
        for i, r in enumerate(spec["rows"], start=1):
            SubmissionRecord.objects.create(submission=sub, row_no=i, payload=r, status="valid")
        kind = SubmissionFile.Kind.SEQUENCE if domain == "genomic" else SubmissionFile.Kind.IMAGE
        for name, content in spec["files"]:
            data = content.encode()
            SubmissionFile.objects.create(submission=sub, kind=kind, file=f"test-invariant/{uid}/{name}",
                                          original_name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        return sub

    def approve(self, sub):
        workflow.approve(sub, self.admin, CHECKLIST)

    def delete(self, uid):
        Submission.objects.filter(upload_id=uid).update(deletion_requested_at=timezone.now())
        deletion.approve_deletion(Submission.objects.get(upload_id=uid), self.admin)


def _owner(row):
    return row.submission.upload_id if row.submission_id else None


def _files(row):
    return sorted((f.sha256[:12], f.submission.upload_id) for f in row.files.select_related("submission"))


def snapshot(cids) -> dict:
    out = {}
    for p in Patient.objects.filter(condition_id__in=cids).order_by("condition_id"):
        d = {f: getattr(p, f) for f in derived.GENOMIC_FIELDS + derived.IMAGING_FIELDS}
        d["regimens"] = [(_owner(r), derived.regimen_drugs(r)) for r in derived.regimen_sequence(p)]
        runs = sorted(p.sequence_runs.select_related("submission"), key=derived.owner_key)
        d["runs"] = [(_owner(r), r.specimen_id, r.platform, r.method, r.coverage, r.sra_accession, r.lineage, _files(r))
                     for r in runs]
        studies = sorted(p.imaging_studies.select_related("submission"), key=derived.owner_key)
        d["studies"] = [(_owner(s), s.modality, str(s.study_date), s.description, s.tb_activity, s.impression,
                         s.findings, _files(s)) for s in studies]
        d["biochem"] = sorted((b.test, b.value, str(b.measured_on), b.reference_range, b.flag, _owner(b))
                              for b in p.biochem_results.select_related("submission"))
        out[p.condition_id] = d
    return out


def _setup_preexisting(pre: dict | None):
    """업로드 전부터 있던 행(업로드 소유 아님) — 두 세계에 똑같이 만든다."""
    if not pre:
        return
    for cid, kinds in pre.items():
        p = Patient.objects.get(condition_id=cid)
        for k in kinds:
            if k == "regimen":
                r = Regimen.objects.create(patient=p)
                RegimenDrug.objects.create(regimen=r, drug_name="Isoniazid", drug_code="H", start_day=0, end_day=180,
                                           dosage="300mg", route="Oral")
            elif k == "biochem":
                import datetime
                BiochemResult.objects.create(patient=p, test="ALT", value="80 U/L", measured_on=datetime.date(2025, 2, 1),
                                             reference_range="old", flag="")
            elif k == "sequence":
                SequenceRun.objects.create(patient=p, specimen_id="SEED", platform="Illumina", method="WGS",
                                           sra_accession="SRR555", lineage="H")
            elif k == "imaging":
                ImagingStudy.objects.create(patient=p, modality="CXR", study_date="2024-01-01",
                                            findings=[{"finding": "Fibrosis"}])


def _world(users, specs, approve_tags, delete_order, pre, cids):
    """approve_tags 순서대로 승인 후 delete_order 를 하나씩 삭제 — 각 삭제 뒤 스냅샷 목록.
    W2 에서도 지운 업로드를 만들어 둔다(승인하지 않음) — 업로드 ID 가 두 세계에서 같아야 소유 비교가 된다."""
    w = World(users)
    _setup_preexisting(pre)
    subs = {t: w.build(specs[t], t) for t in range(len(specs))}
    for t in approve_tags:
        w.approve(subs[t])
    shots = [snapshot(cids)]
    for t in delete_order:
        w.delete(f"UPL-29990101-{t + 1:03d}")
        shots.append(snapshot(cids))
    return shots


def check(users, specs, delete_order, pre=None, cids=(P2, P3)):
    """불변식 검사. 반환: 마지막 단계의 실제 상태(추가 단언용)."""
    n = len(specs)
    for spec in specs:  # 같은 제출 안의 같은 SHA-256 은 업로드 단계에서 거부되므로 명세에도 두지 않는다
        contents = [c for _, c in spec["files"]]
        assert len(contents) == len(set(contents)), spec
    sid = transaction.savepoint()
    actual = _world(users, specs, list(range(n)), delete_order, pre, cids)
    transaction.savepoint_rollback(sid)
    for k in range(1, len(delete_order) + 1):
        gone = set(delete_order[:k])
        sid = transaction.savepoint()
        expected = _world(users, specs, [t for t in range(n) if t not in gone], [], pre, cids)[0]
        transaction.savepoint_rollback(sid)
        assert actual[k] == expected, (
            f"\nspecs={specs}\npre={pre}\ndeleted={delete_order[:k]}\nactual={actual[k]}\nexpected={expected}")
    return actual[-1]


@pytest.fixture
def users(demo_users, seeded_patients):
    return demo_users


# ─── 고정 시나리오 (2차 리뷰 재현 + 경계) ───
def test_A_deleted_upload_sra_does_not_survive(users):
    last = check(users, [gen(P2, "a", "SRR1", "Beijing"), gen(P2, "b", "", "LAM")], [0])
    assert (last[P2]["sra_id"], last[P2]["lineage"]) == ("SRR180002", "LAM")


def test_A_deleted_upload_lineage_does_not_survive(users):
    last = check(users, [gen(P2, "a", "SRR1", ""), gen(P2, "b", "SRR2", "LAM")], [1])
    assert (last[P2]["sra_id"], last[P2]["lineage"]) == ("SRR1", "L2.2.1")


def test_B_cxr_finding_of_shared_owner_removed(users):
    specs = [img(P3, "a", findings=["Atelectasis"]), img(P3, "b", findings=["Atelectasis"])]
    last = check(users, specs, [0, 1])
    assert last[P3]["cxr_findings"] == ["결절"] and last[P3]["cxr_count"] == 2


def test_C_handoff_rewrites_sequence_metadata(users):
    specs = [gen(P2, "a", "SRR1", "Beijing"), gen(P2, "b", "SRR2", "LAM"), gen(P2, "a", "SRR3", "T")]
    last = check(users, specs, [0])
    assert (last[P2]["sra_id"], last[P2]["lineage"]) == ("SRR3", "T")
    assert [r[0] for r in last[P2]["runs"]] == ["UPL-29990101-002", "UPL-29990101-003"]
    assert last[P2]["runs"][1][5:7] == ("SRR3", "T")


def test_C_handoff_rewrites_modality_cxr_to_ct(users):
    last = check(users, [img(P3, "a", "CXR", ["Cavity"]), img(P3, "a", "CT")], [0])
    s = last[P3]
    assert [x[1] for x in s["studies"]] == ["CT"] and s["ct_count"] == 1 and s["has_ct"]
    assert s["cxr_count"] == 2 and s["cxr_findings"] == ["결절"]


def test_C_batch_then_individual_specimen_rewritten(users):
    specs = [gen(P2, "a", "SRR1", "Beijing", spec="SPA", mode="batch"), gen(P2, "a", "SRR2", "LAM", spec="SP-1")]
    last = check(users, specs, [0])
    assert last[P2]["runs"][0][1] == "SP-1"


def test_D_regimen_consecutive_duplicate_merged(users):
    specs = [reg(P2, "Isoniazid"), reg(P2, "Rifampicin"), reg(P2, "Isoniazid")]
    last = check(users, specs, [1])
    assert last[P2]["regimens"] == [("UPL-29990101-001", [("Isoniazid", "H", 0, 180, "300mg", "Oral")])]


def test_regimen_chain_merge_then_handoff(users):
    specs = [reg(P2, "Isoniazid"), reg(P2, "Isoniazid"), reg(P2, "Rifampicin"), reg(P2, "Isoniazid")]
    check(users, specs, [2, 0, 1])


def test_regimen_merge_into_preexisting(users):
    check(users, [reg(P2, "Rifampicin"), reg(P2, "Isoniazid")], [0], pre={P2: ["regimen"]})


def test_chain_of_four_genomic(users):
    specs = [gen(P2, "a", "SRR1", "Beijing"), gen(P2, "b", "", "LAM"), gen(P2, "c", "SRR3", ""),
             gen(P2, "a", "", "T")]
    check(users, specs, [1, 0, 3])


def test_delete_sharer_only(users):
    last = check(users, [gen(P2, "a", "SRR1", "Beijing"), gen(P2, "a", "SRR2", "LAM")], [1])
    assert (last[P2]["sra_id"], last[P2]["lineage"]) == ("SRR1", "Beijing")


def test_transferred_row_deleted_again(users):
    specs = [img(P3, "a", findings=["Cavity"]), img(P3, "a", findings=["Nodule"]), img(P3, "a", "CT")]
    last = check(users, specs, [0, 1])
    assert [x[1] for x in last[P3]["studies"]] == ["CT"]


def test_same_submission_multiple_patients(users):
    batch = gen(None, None, mode="batch", rows=[(P2, "SPA", "a", "SRR1", "Beijing"), (P3, "SPB", "b", "SRR2", "LAM")])
    specs = [batch, gen(P2, "a", "SRR7", "T"), gen(P3, "c", "", "H")]
    check(users, specs, [0, 2])


def test_resubmission_chain(users):
    specs = [{**gen(P2, "a", "SRR1", "Beijing"), "resubmit": True}, {**gen(P2, "a", "SRR2", "LAM"), "resubmit": True},
             gen(P2, "b", "", "T")]
    check(users, specs, [0, 2])


def test_imaging_modalities_and_empty_findings(users):
    specs = [img(P3, "a", "PET-CT"), img(P3, "b", "CXR", ["Nodule", "Cavity"], date=""), img(P3, "c", "CT"),
             img(P3, "b", "CXR", ["Fibrosis"])]
    check(users, specs, [1, 2], pre={P3: ["imaging"]})


def test_biochem_shared_writers_and_preexisting_row(users):
    specs = [lab(P2, ("ALT", "80")), lab(P2, ("ALT", "80"), ("AST", "50")), lab(P2, ("AST", "50"))]
    last = check(users, specs, [0, 1], pre={P2: ["biochem"]})
    assert ("ALT", "80 U/L", "2025-02-01", "old", "", None) in last[P2]["biochem"]


def test_preexisting_sequence_row_not_folded(users):
    check(users, [gen(P2, "a", "SRR1", "Beijing"), gen(P2, "b", "", "")], [0, 1], pre={P2: ["sequence"]})


# ─── 3차 리뷰 재현: 한 행에 파일 여러 개 + 업로드 간 부분 공유 ───
def test_review3_cxr_partial_overlap(users):
    """A{f1,f2}, B{f1}, C{f2} 승인 후 A 삭제 → B·C 각자 검사 1건, C 의 소견 유지(W2 와 같음)."""
    specs = [img(P2, ("f1", "f2"), findings=["Cavity"]), img(P2, "f1", findings=["Nodule"]),
             img(P2, "f2", findings=["Fibrosis"])]
    last = check(users, specs, [0])
    assert [x[0] for x in last[P2]["studies"]] == ["UPL-29990101-002", "UPL-29990101-003"]
    assert "섬유화" in last[P2]["cxr_findings"]


def test_review3_genomic_partial_overlap(users):
    specs = [gen(P2, ("x1", "x2"), "SRR1"), gen(P2, "x1", "SRR2"), gen(P2, "x2", "SRR3")]
    last = check(users, specs, [0])
    assert len(last[P2]["runs"]) == 2 and last[P2]["sra_id"] == "SRR3"


def test_review3_imaging_superset_after_two_deletions(users):
    """A{f1}, B{f2}, C{f1,f2} 승인 후 A·B 삭제 → C 소유 검사 1건(W2), 같은 검사 2건이 아니다."""
    specs = [img(P2, "f1"), img(P2, "f2"), img(P2, ("f1", "f2"))]
    last = check(users, specs, [0, 1])
    assert [x[0] for x in last[P2]["studies"]] == ["UPL-29990101-003"]


def test_review3_batch_two_rows_share_one_run(users):
    """B(일괄)의 두 행이 A 의 런 하나를 함께 중복으로 건너뜀 → A 삭제 후 B 의 런 2개(W2)."""
    a = gen(P2, ("x1", "x2"), "SRR1", "Beijing")
    b = gen(None, None, mode="batch", rows=[(P2, "SPA", "x1", "SRR2", ""), (P2, "SPB", "x2", "SRR3", "LAM")])
    last = check(users, [a, b], [0])
    assert [r[1] for r in last[P2]["runs"]] == ["SPA", "SPB"] and last[P2]["sra_id"] == "SRR3"


def test_multi_patient_regimen_and_lab(users):
    specs = [reg(None, rows=[(P2, ["Isoniazid"]), (P3, ["Rifampicin"])]), reg(P2, "Rifampicin"),
             reg(None, rows=[(P2, ["Isoniazid"]), (P3, ["Rifampicin"])]),
             lab(None, rows=[(P2, [("ALT", "80")], "2025-02-01"), (P3, [("ALT", "80"), ("AST", "50")], "2025-02-01")]),
             lab(P3, ("ALT", "80"))]
    check(users, specs, [0, 3, 1])


def test_replay_disabled_is_detected(users, monkeypatch):
    """하네스 민감도: 재적용을 끄면(남은 업로드를 다시 반영하지 않으면) 불변식 위반이 잡혀야 한다."""
    monkeypatch.setattr(deletion, "apply_submission", lambda *a, **kw: {})
    with pytest.raises(AssertionError):
        check(users, [img(P2, ("f1", "f2"), findings=["Cavity"]), img(P2, "f1"), img(P2, "f2", findings=["Fibrosis"])],
              [0])


# ─── 시드 고정 무작위 시나리오 ───
POOL = "abcdef"


def _random_files(rng, used: set, most: int = 3) -> tuple:
    """행당 파일 1~3개 — 같은 제출 안에서는 겹치지 않게(업로드 간에는 겹칠 수 있다 = 부분 공유)."""
    free = [x for x in POOL if x not in used]
    keys = tuple(rng.sample(free, rng.randint(1, most)))
    used.update(keys)
    return keys if len(keys) > 1 else keys[0]


def _random_spec(rng, domain):
    cid = rng.choice([P2, P3])
    if domain == "genomic":
        if rng.random() < 0.35:  # 일괄: 여러 환자·같은 환자 여러 행
            used: set = set()
            rows = []
            for k in range(rng.randint(2, 3)):
                rows.append((rng.choice([P2, P3]), f"SP{'ABC'[k]}", _random_files(rng, used, 2),
                             rng.choice(["", "SRR1", "SRR2", "SRR3"]), rng.choice(["", "Beijing", "LAM", "T"])))
            return gen(None, None, mode="batch", rows=rows)
        mode = rng.choice(["individual", "batch"])
        return gen(cid, _random_files(rng, set()), rng.choice(["", "SRR1", "SRR2", "SRR3"]),
                   rng.choice(["", "Beijing", "LAM", "T"]), spec=rng.choice(["SPA", "SPB"]), mode=mode)
    if domain == "imaging":
        return img(cid, _random_files(rng, set()), rng.choice(["CXR", "CXR", "CT", "PET-CT"]),
                   rng.sample(["Cavity", "Nodule", "Atelectasis", "Infiltrate", "Fibrosis"], rng.randint(0, 2)),
                   date=rng.choice(["2025-05-01", "2025-06-01", ""]), activity=rng.choice(["Active", "Inactive"]),
                   impression=rng.choice(["TB", "Old TB"]))
    drug_sets = [["Isoniazid"], ["Rifampicin"], ["Isoniazid", "Rifampicin"]]
    if domain == "regimen":
        if rng.random() < 0.3:
            return reg(None, rows=[(c, rng.choice(drug_sets)) for c in rng.sample([P2, P3], 2)])
        return reg(cid, *rng.choice(drug_sets))
    options = [("ALT", "80"), ("ALT", "30"), ("AST", "50")]
    if rng.random() < 0.3:
        return lab(None, rows=[(c, rng.sample(options, rng.randint(1, 2)), rng.choice(["2025-02-01", "2025-03-01"]))
                               for c in rng.choices([P2, P3], k=rng.randint(2, 3))])
    return lab(cid, *rng.sample(options, rng.randint(1, 2)))


PRE = {"genomic": "sequence", "imaging": "imaging", "regimen": "regimen", "lab": "biochem"}


class _Clock:
    """승인 시각 충돌·역전 흉내 — 'fixed' 는 모든 시각이 같고, 'reverse' 는 부를 때마다 1초씩 과거로 간다.
    정렬이 reviewed_at 이 아니라 approval_seq 만 쓰는지 본다."""

    def __init__(self, mode):
        self.mode, self.t = mode, timezone.now()

    def __call__(self):
        if self.mode == "reverse":
            self.t -= datetime.timedelta(seconds=1)
        return self.t


@pytest.mark.parametrize("seed", range(240))
def test_random_scenarios(users, seed, monkeypatch):
    rng = random.Random(20261127 + seed)
    domain = ["genomic", "imaging", "regimen", "lab"][seed % 4]
    n = rng.randint(2, 5)
    specs = [_random_spec(rng, domain) for _ in range(n)]
    for s in specs:
        if rng.random() < 0.1:
            s["resubmit"] = True
    order = rng.sample(range(n), rng.randint(1, n))
    pre = {rng.choice([P2, P3]): [PRE[domain]]} if rng.random() < 0.4 else None
    clock = rng.choice([None, None, "fixed", "reverse"])
    if clock:
        monkeypatch.setattr(timezone, "now", _Clock(clock))
    check(users, specs, order, pre=pre)
