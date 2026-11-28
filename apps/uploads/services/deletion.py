"""업로드 삭제 요청의 관리자 처리 (원본 MyUploadsTab '삭제 요청은 관리자 승인 후 처리됩니다').

승인 = 한 트랜잭션 안에서
  1) 이 업로드가 반영한 데이터를 '환자×도메인 재적용(replay)'으로 되돌리고,
  2) 업로드(행·파일 행·이력)를 지운 뒤 DeletedSubmission 에 기록을 남긴다.
  3) 실제 파일(var/media) 삭제는 **커밋 후**(transaction.on_commit) — 롤백되면 파일은 그대로 남는다.
     지우지 못한 파일은 files.delete_path 가 OrphanFile 로 남겨 cleanup_media 가 다시 지운다.

목표 불변식: '업로드를 승인 순서(approval_seq)대로 승인한 뒤 일부를 삭제' == '남은 업로드만 같은 순서로 승인'.
구성(공유 기록을 유지·이전하지 않는다):
  - 잠금: 영향 환자 집합을 구해 **환자 id 오름차순 한 문장 select_for_update**(승인과 같은 직렬화 지점) → 대상 업로드 행.
    그 뒤에 (잠금 하에서) 대상과 관련 승인 업로드 목록을 다시 읽어 계산한다.
  - 대상 업로드 A 가 건드린 (환자 p, 도메인 d) 마다
    · 다시 적용(replay): p 의 d 도메인 **업로드 소유 행 전부**(레지멘·시퀀스·영상·생화학 — submission FK)를 지우고,
      파생 필드(유전체·영상 요약)와 업로드 밖 생화학 행 값을 기준 스냅샷으로 되돌린 뒤, A 를 뺀 그 (p, d) 의 승인 업로드를
      approval_seq 순으로 apply.apply_submission(o, only={p}, replay=True) — 최초 승인과 같은 코드 — 로 다시 적용한다.
      그래서 남은 업로드는 자기 파일로 다시 연결되고 A 의 파일을 참조하는 행은 남지 않는다(남으면 예외 → 롤백).
    · 예전 건(legacy): 그 (p, d) 를 건드린 업로드(A 포함) 중 재계산 표지(recompute)가 없는 승인 건이 있거나, 기준 스냅샷이
      예전 건 효과를 품고 있으면(legacy_mixed 등) 다시 적용하지 않는다 — A 의 행만 지우고 영상 수만 줄이며 파생 요약은
      그대로 둔다(화면에 (p, d) 별 안내).
  - 재적용은 각 업로드의 apply_summary(승인 당시 결과 — 이력 화면용)를 덮어쓰지 않는다.
  - 임상: 이 업로드가 **새로 만든** 케이스(apply_summary.created_ids)만 지운다 — 다른 업로드·데이터 구조가
    그 케이스를 참조하거나 하위 데이터(DST·영상 등)가 있으면 지우지 않고 '유지'로 남긴다.
    기존 케이스에 덮어쓴 값은 이전 값을 기록하지 않았으므로 되돌릴 수 없다(유지 — 화면에 안내).
  - DST·도말·배양 갱신은 약제별 최신값 병합이라 되돌릴 수 없고 재적용하지도 않는다(유지 — 화면에 안내).
  - 승인 전(검증 대기·반려) 업로드는 반영된 데이터가 없으므로 업로드와 파일만 지운다.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.core.filetx import delete_after_commit
from apps.cases.models import BiochemResult, ImagingStudy, Patient, Regimen, SequenceRun

from ..models import DeletedSubmission, DerivedBaseline, Submission, SubmissionEvent, SubmissionRecord
from . import derived, files
from .apply import apply_submission, lock_patients, patient_ids_for, touched_condition_ids
from .derived import approval_key, int_ids
from .workflow import WorkflowError, _reason, is_own_upload


def pending_deletions():
    """관리자 처리 대기 중인 삭제 요청(요청됨 + 반려되지 않음). 임시저장은 요청 대상이 아니다."""
    return (Submission.objects.filter(deletion_requested_at__isnull=False, deletion_rejected_at__isnull=True)
            .exclude(status=Submission.Status.DRAFT))


def _check_reviewer(sub: Submission, reviewer) -> None:
    if is_own_upload(sub, reviewer):
        raise WorkflowError("본인이 제출한 업로드의 삭제 요청은 처리할 수 없습니다(직무 분리). 다른 관리자가 처리해야 합니다.",
                            "You cannot process the deletion request for your own upload (separation of duties). "
                            "Another administrator must handle it.")


def _other_references(cid: str, sub: Submission) -> bool:
    """이 케이스를 다른 승인 업로드(어느 도메인이든)나 커스텀 데이터 구조 입력이 참조하는가."""
    from apps.schemas.models import SchemaRecord

    if SchemaRecord.objects.filter(condition_id=cid).exists():
        return True
    return (SubmissionRecord.objects.filter(submission__status=Submission.Status.APPROVED, payload__condition_id=cid)
            .exclude(submission=sub).exists())


def _has_child_data(p: Patient) -> bool:
    return (p.regimens.exists() or p.sequence_runs.exists() or p.imaging_studies.exists()
            or p.dst_results.exists() or p.biochem_results.exists())


ROW_MODEL = {"regimen": Regimen, "genomic": SequenceRun, "imaging": ImagingStudy, "lab": BiochemResult}
PLAN_KEY = {"regimen": "regimens", "genomic": "sequence_runs", "imaging": "imaging_studies", "lab": "biochem"}
BASELINE_DOMAIN = {"genomic": DerivedBaseline.Domain.GENOMIC, "imaging": DerivedBaseline.Domain.IMAGING,
                   "lab": DerivedBaseline.Domain.BIOCHEM}
IMAGING_FLAGS = (("has_cxr", "cxr_count", "CXR"), ("has_ct", "ct_count", "CT"), ("has_pet_ct", "pet_ct_count", "PET-CT"))

MODE_LABEL = {"replay": ("다시 적용", "Re-applied"), "legacy": ("예전 건 — 되돌리지 않음", "Older approval — not reverted")}
LEGACY_NOTICE = {
    "genomic": ("이 업로드의 시퀀스만 지우고 유전체 요약(시퀀스 보유·SRA·계통)은 바꾸지 않습니다(유지).",
                "only this upload's sequence runs are deleted; the genomic summary (sequence flag, SRA, lineage) is kept."),
    "imaging": ("이 업로드의 영상 검사만 지우고 영상 수만 줄입니다 — CXR 소견·영상 보유 표시는 바꾸지 않습니다(유지).",
                "only this upload's imaging studies are deleted and image counts reduced; CXR findings and imaging flags "
                "are kept."),
    "regimen": ("이 업로드의 레지멘만 지웁니다 — 같은 구성이라 건너뛴 다른 업로드의 레지멘은 다시 만들지 않습니다.",
                "only this upload's regimens are deleted; regimens of other uploads skipped as identical are not "
                "re-created."),
    "lab": ("이 업로드가 만든 생화학 결과만 지웁니다 — 갱신한 참고범위·판정은 되돌리지 않습니다(유지).",
            "only biochemistry results created by this upload are deleted; updated reference ranges/flags are kept."),
}
REPLAY_NOTE = {
    "genomic": ("표시된 케이스는 이 업로드를 뺀 나머지 승인 업로드를 승인 순서대로 다시 적용합니다 — 시퀀스 행과 유전체 요약"
                "(시퀀스 보유·SRA·계통)이 남은 업로드만 승인했을 때와 같아집니다.",
                "For the listed cases the remaining approved uploads are re-applied in approval order — sequence runs and "
                "the genomic summary (sequence flag, SRA, lineage) become as if only those uploads had been approved."),
    "imaging": ("표시된 케이스는 이 업로드를 뺀 나머지 승인 업로드를 승인 순서대로 다시 적용합니다 — 영상 검사와 영상 보유·"
                "영상 수·CXR 소견이 남은 업로드만 승인했을 때와 같아집니다.",
                "For the listed cases the remaining approved uploads are re-applied in approval order — imaging studies, "
                "imaging flags, counts and CXR findings become as if only those uploads had been approved."),
    "regimen": ("표시된 케이스는 이 업로드를 뺀 나머지 승인 업로드를 승인 순서대로 다시 적용합니다 — 레지멘 목록이 남은 "
                "업로드만 승인했을 때와 같아집니다(같은 구성 연속 중복도 생기지 않음).",
                "For the listed cases the remaining approved uploads are re-applied in approval order — the regimen list "
                "becomes as if only those uploads had been approved (no consecutive identical regimens)."),
    "lab": ("표시된 케이스는 이 업로드를 뺀 나머지 승인 업로드의 생화학 결과를 승인 순서대로 다시 적용합니다 — 기존 결과의 "
            "참고범위·판정은 처음 값에서 다시 계산됩니다.",
            "For the listed cases the biochemistry results of the remaining approved uploads are re-applied in approval "
            "order — reference ranges/flags of existing results are recalculated from their original values."),
}


def _summary(sub: Submission) -> dict:
    return sub.apply_summary if isinstance(sub.apply_summary, dict) else {}


def _touching(domain: str, cids: set[str]) -> dict[str, list[Submission]]:
    """{대상자 ID: 그 대상자를 반영 대상으로 삼은 승인 업로드(이 도메인, approval_seq 순)}."""
    if not cids:
        return {}
    sub_ids = (SubmissionRecord.objects.filter(submission__status=Submission.Status.APPROVED, submission__domain=domain,
                                               payload__condition_id__in=sorted(cids))
               .exclude(status=SubmissionRecord.RowStatus.ERROR).values_list("submission_id", flat=True).distinct())
    out: dict[str, list[Submission]] = {}
    for s in sorted(Submission.objects.filter(pk__in=list(sub_ids)), key=approval_key):
        for cid in touched_condition_ids(s, within=cids):  # 관련 대상자 행만 정리(5단계 성능)
            out.setdefault(cid, []).append(s)
    return out


def _is_legacy_pair(p: Patient, domain: str, subs: list[Submission]) -> bool:
    """이 (환자, 도메인)을 다시 적용할 수 없는가 — 예전 건이 섞였거나 기준 스냅샷이 예전 건 효과를 품고 있다."""
    if any(derived.is_legacy(s) for s in subs):
        return True
    if domain not in BASELINE_DOMAIN:
        return False
    base = DerivedBaseline.objects.filter(patient=p, domain=BASELINE_DOMAIN[domain]).first()
    if base is not None and base.legacy_mixed:
        return True
    if domain in ("genomic", "imaging"):
        model = ROW_MODEL[domain]
        if base is None:  # 업로드 행이 있는데 출발점(기준 값)이 없다
            return model.objects.filter(patient=p, submission__isnull=False).exists()
        pre = int_ids(base.preexisting_ids)
        return bool(pre) and model.objects.filter(pk__in=pre, submission__isnull=False).exists()
    return False


def _pairs(sub: Submission) -> list[dict]:
    """대상 업로드가 건드린 (환자, 도메인) 별 처리 방식 — [{patient, mode: replay|legacy, others: [승인 업로드]}]."""
    if sub.domain not in ROW_MODEL:
        return []
    cids = touched_condition_ids(sub)
    touching = _touching(sub.domain, cids)
    out = []
    for p in Patient.objects.filter(condition_id__in=cids).order_by("pk"):
        subs = touching.get(p.condition_id, [])
        if not any(s.pk == sub.pk for s in subs):
            subs = sorted([*subs, sub], key=approval_key)
        mode = "legacy" if _is_legacy_pair(p, sub.domain, subs) else "replay"
        out.append({"patient": p, "mode": mode, "others": [s for s in subs if s.pk != sub.pk]})
    return out


def plan(sub: Submission, pairs: list[dict] | None = None) -> dict:
    """삭제하면 무엇이 지워지고 무엇이 유지되는지(읽기 전용) — 관리자 확인 화면과 실제 삭제가 같은 계획을 쓴다."""
    out = {"applied": sub.status == Submission.Status.APPROVED, "records": sub.records.count(),
           "files": sub.files.count(), "regimens": 0, "sequence_runs": 0, "imaging_studies": 0, "biochem": 0,
           "delete_patients": [], "keep_patients": [], "irreversible": [], "pairs": [], "notes": []}
    if not out["applied"]:
        return out
    summary = _summary(sub)
    if sub.domain in ROW_MODEL:
        out[PLAN_KEY[sub.domain]] = ROW_MODEL[sub.domain].objects.filter(submission=sub).count()
        if pairs is None:
            pairs = _pairs(sub)
        dom_ko, dom_en = ROW_LABEL[sub.domain]
        for pr in pairs:
            cid, mode = pr["patient"].condition_id, pr["mode"]
            ko, en = MODE_LABEL[mode]
            others = [o.upload_id for o in pr["others"]]
            out["pairs"].append({"condition_id": cid, "mode": mode, "others": others,
                                 "ko": f"{cid} · {dom_ko} — {ko}", "en": f"{cid} · {dom_en} — {en}"})
            if mode == "legacy":
                lko, len_ = LEGACY_NOTICE[sub.domain]
                out["irreversible"].append({
                    "ko": f"{cid} · {dom_ko}: 재계산 기록이 없는 예전 승인 건이 섞여 있어 다시 적용하지 않습니다 — {lko}",
                    "en": f"{cid} · {dom_en}: mixed with older approvals without a recompute record, so it is not "
                          f"re-applied — {len_}"})
        if any(pr["mode"] == "replay" for pr in pairs):
            ko, en = REPLAY_NOTE[sub.domain]
            out["notes"].append({"ko": ko, "en": en})
        if sub.domain == "lab" and summary.get("updated"):
            out["irreversible"].append({
                "ko": "DST·도말·배양 결과는 약제별 최신값으로 병합돼 이전 값을 알 수 없으므로 되돌리지 않습니다(유지).",
                "en": "DST, smear and culture results were merged per drug, so previous values are unknown and are kept."})
    elif sub.domain == "clinical":
        created = [c for c in summary.get("created_ids") or [] if isinstance(c, str)]
        for p in Patient.objects.filter(condition_id__in=created):
            if _has_child_data(p) or _other_references(p.condition_id, sub):
                out["keep_patients"].append(p.condition_id)
            else:
                out["delete_patients"].append(p.condition_id)
        if summary.get("updated") or (summary.get("created") and "created_ids" not in summary):
            out["irreversible"].append({
                "ko": "기존 케이스에 덮어쓴 값(및 생성 기록이 없는 이전 승인 건의 케이스)은 되돌리지 않습니다(유지).",
                "en": "Values written over existing cases (and cases from older approvals without a creation record) are kept."})
        if out["keep_patients"]:
            out["irreversible"].append({
                "ko": "다른 데이터가 연결된 케이스는 지우지 않습니다: " + ", ".join(out["keep_patients"]),
                "en": "Cases linked to other data are kept: " + ", ".join(out["keep_patients"])})
    return out


ROW_LABEL = {"regimen": ("레지멘", "Regimen"), "genomic": ("유전체", "Genomic"),
             "imaging": ("영상", "Imaging"), "lab": ("생화학", "Biochemistry")}


# ─── 되돌리기 ───
def _legacy_imaging(p: Patient, counts: dict[str, int]) -> None:
    """예전 건: 영상 수만 지운 검사 수만큼 줄이고, 남은 수가 있으면 보유 표시를 켠다(소견은 유지)."""
    for flag, count_field, modality in IMAGING_FLAGS:
        left = max(0, getattr(p, count_field) - counts.get(modality, 0))
        setattr(p, count_field, left)
        if left > 0:
            setattr(p, flag, True)


def _delete_legacy(s: Submission, pids: set[int]) -> None:
    """예전 건 경로: A 의 행만 지운다(영상은 수만 줄임). 파생 요약·다른 업로드의 행은 건드리지 않는다."""
    model = ROW_MODEL[s.domain]
    qs = model.objects.filter(submission=s, patient_id__in=sorted(pids))
    counts: dict[int, dict[str, int]] = {}
    if s.domain == "imaging":
        for pid, modality in qs.values_list("patient_id", "modality"):
            counts.setdefault(pid, {}).setdefault(modality, 0)
            counts[pid][modality] += 1
    qs.delete()
    for p in Patient.objects.filter(pk__in=sorted(counts)).order_by("pk"):
        _legacy_imaging(p, counts[p.pk])
        p.save()


def _reset_to_baseline(domain: str, pids: list[int]) -> None:
    """업로드 소유 행을 지운 뒤 — 파생 필드·업로드 밖 생화학 행 값을 기준 스냅샷으로 되돌린다."""
    if domain in ("genomic", "imaging"):
        for p in Patient.objects.filter(pk__in=pids).order_by("pk"):
            if derived.recompute_patient_derived(p, domain):  # 업로드 행이 없으므로 기준 값 그대로
                p.save()
    elif domain == "lab":
        for base in DerivedBaseline.objects.filter(patient_id__in=pids, domain=DerivedBaseline.Domain.BIOCHEM):
            values = base.values if isinstance(base.values, dict) else {}
            for k, v in values.items():
                vals = derived.valid_biochem_values(v)
                if vals is not None and isinstance(k, str) and k.isdigit() and len(k) <= 18:
                    BiochemResult.objects.filter(pk=int(k), patient_id=base.patient_id, submission__isnull=True) \
                        .update(**vals)


def _replay(s: Submission, pairs: list[dict]) -> list[str]:
    """다시 적용: (p, d) 의 업로드 소유 행을 모두 지우고 기준으로 되돌린 뒤 남은 업로드를 approval_seq 순으로 재적용."""
    pids = sorted(pr["patient"].pk for pr in pairs)
    if not pids:
        return []
    model = ROW_MODEL[s.domain]
    model.objects.filter(patient_id__in=pids, submission__isnull=False).delete()  # 레지멘 약물은 CASCADE
    _reset_to_baseline(s.domain, pids)
    scope: dict[int, tuple[Submission, set[int]]] = {}
    for pr in pairs:
        for o in pr["others"]:
            scope.setdefault(o.pk, (o, set()))[1].add(pr["patient"].pk)
    order = sorted(scope.values(), key=lambda t: approval_key(t[0]))
    for o, only in order:
        apply_submission(o, only=only, replay=True)  # 최초 승인과 같은 코드 — 요약은 버린다(이력 보존)
    return [o.upload_id for o, _ in order]


def _apply_deletion(s: Submission, pairs: list[dict], removed: dict) -> dict:
    model = ROW_MODEL[s.domain]
    removed[PLAN_KEY[s.domain]] = model.objects.filter(submission=s).count()
    replay = [pr for pr in pairs if pr["mode"] == "replay"]
    replay_pids = {pr["patient"].pk for pr in replay}
    # 예전 건 (p, d) + (방어) 대상 환자 목록 밖에 남은 A 의 행
    legacy_pids = ({pr["patient"].pk for pr in pairs if pr["mode"] == "legacy"}
                   | set(model.objects.filter(submission=s).exclude(patient_id__in=replay_pids)
                         .values_list("patient_id", flat=True)))
    _delete_legacy(s, legacy_pids)
    replayed = _replay(s, replay)
    if model.objects.filter(submission=s).exists():
        raise RuntimeError(f"rows owned by {s.upload_id} survived the replay")
    if s.domain in ("genomic", "imaging") and \
            model.objects.filter(files__submission=s).exclude(patient_id__in=legacy_pids).exists():
        raise RuntimeError(f"rows still reference files of {s.upload_id} after the replay")
    return {"replayed": replayed,
            "pairs": [{"condition_id": pr["patient"].condition_id, "mode": pr["mode"]} for pr in pairs]}


def approve_deletion(sub: Submission, reviewer) -> DeletedSubmission:
    _check_reviewer(sub, reviewer)
    paths: list[str] = []
    with transaction.atomic():
        # 잠금 순서(승인과 같다): 영향 환자(id 오름차순 한 문장) → 대상 업로드 행. 계산은 잠근 뒤에 다시 읽어서 한다.
        current = Submission.objects.filter(pk=sub.pk).first()
        if current is None:
            raise WorkflowError("이미 처리된 삭제 요청입니다", "This deletion request has already been processed")
        locked_pids = set(lock_patients(patient_ids_for(current)))
        s = pending_deletions().select_for_update().select_related("submitter").filter(pk=sub.pk).first()
        if s is None:
            raise WorkflowError("이미 처리된 삭제 요청입니다", "This deletion request has already been processed")
        extra = patient_ids_for(s) - locked_pids
        if extra:  # 그 사이 등록된 대상자(드묾) — 추가로 잠근다
            lock_patients(extra)
        pairs = _pairs(s) if s.status == Submission.Status.APPROVED else []
        p = plan(s, pairs)
        removed = {"records": p["records"], "files": p["files"], "regimens": 0, "sequence_runs": 0,
                   "imaging_studies": 0, "biochem": 0, "patients": []}
        detail: dict = {}
        if p["applied"]:
            if s.domain in ROW_MODEL:
                detail = _apply_deletion(s, pairs, removed)
            elif s.domain == "clinical" and p["delete_patients"]:
                Patient.objects.filter(condition_id__in=p["delete_patients"]).delete()
                removed["patients"] = p["delete_patients"]
        paths = [f.file.name for f in s.files.all() if f.file.name]
        log = DeletedSubmission.objects.create(
            upload_id=s.upload_id, domain=s.domain, status_at_deletion=s.status, submitter=s.submitter,
            submitter_label=s.submitter.shown_name if s.submitter_id else "", requested_at=s.deletion_requested_at,
            reviewer=reviewer, deleted_at=timezone.now(),
            summary={"removed": removed, "kept_patients": p["keep_patients"], "irreversible": p["irreversible"],
                     **detail})
        s.delete()  # 행·파일 행·이력은 CASCADE
        # 파일은 커밋이 확정된 뒤에만 지운다(롤백되면 DB 가 참조하는 파일이 남아 있어야 한다)
        delete_after_commit(paths, files.delete_path)  # 커밋 뒤, 실패해도 응답 불변(robust)
    return log


def reject_deletion(sub: Submission, reviewer, reason: str) -> Submission:
    _check_reviewer(sub, reviewer)
    reason = _reason(reason, "반려 사유를 입력해주세요.", "Please enter the reason for rejection.")
    now = timezone.now()
    with transaction.atomic():
        changed = pending_deletions().filter(pk=sub.pk).update(
            deletion_rejected_at=now, deletion_reject_reason=reason, deletion_reviewer=reviewer, updated_at=now)
        if changed != 1:
            raise WorkflowError("이미 처리된 삭제 요청입니다", "This deletion request has already been processed")
        SubmissionEvent.objects.create(submission_id=sub.pk, actor=reviewer,
                                       action=SubmissionEvent.Action.DELETION_REJECTED, comment=reason, at=now)
    sub.refresh_from_db()
    return sub


def deletion_counts() -> dict:
    return {"pending": pending_deletions().count()}

