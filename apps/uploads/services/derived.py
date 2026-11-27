"""환자 파생 값의 재계산(fold) — apply(승인)와 deletion(삭제 승인 = 재적용 replay)이 같은 규칙을 쓴다.

파생 값
- 유전체 요약: has_sequence·sra_id·lineage
- 영상 요약: has_cxr·has_ct·has_pet_ct, cxr_count·ct_count·pet_ct_count, cxr_findings
- 생화학 참고범위·판정(업로드 밖에서 생긴 행을 업로드가 '갱신만' 한 경우)

규칙
1. 기준 스냅샷(DerivedBaseline): 업로드가 그 환자의 파생 값을 **처음** 건드리기 직전 값. 한 번 저장되면 덮어쓰지 않는다.
   스냅샷 시점에 이미 있던 시퀀스/영상 행(preexisting_ids)은 값에 이미 반영된 것으로 보고 다시 접지 않는다.
   그 시점에 재계산 규칙 이전(apply_summary 에 recompute 표지 없음) 승인 건의 효과가 섞여 있으면 legacy_mixed 로 표시한다.
2. 재계산: 기준 값에서 시작해, 남아 있는 시퀀스/영상 행을 **소유 업로드의 승인 순서(approval_seq)**(업로드가 없는
   행은 맨 앞)로 정렬해 행 단위 효과(fold_sequence / fold_imaging)로 접는다. apply 도 행을 만든 뒤 같은 함수로 계산한다.
3. 기준 스냅샷이 없는 환자는 재계산하지 않는다(값을 건드리지 않는 쪽).
"""
from __future__ import annotations

from apps.cases.models import ImagingStudy, Patient, Regimen, SequenceRun

from ..models import DerivedBaseline, Submission, SubmissionRecord

FINDING_KO = {"Infiltrate": "침윤", "Consolidation": "침윤", "Nodule": "결절", "Cavity": "공동", "Cavitary lesion": "공동",
              "Pleural effusion": "흉수", "Fibrosis": "섬유화", "Calcification": "석회화", "Atelectasis": "무기폐"}

GENOMIC_FIELDS = ("has_sequence", "sra_id", "lineage")
IMAGING_FIELDS = ("has_cxr", "has_ct", "has_pet_ct", "cxr_count", "ct_count", "pet_ct_count", "cxr_findings")
DERIVED_FIELDS = {"genomic": GENOMIC_FIELDS, "imaging": IMAGING_FIELDS}
ROW_MODEL = {"genomic": SequenceRun, "imaging": ImagingStudy}
BIOCHEM_FIELDS = ("reference_range", "flag")


def int_ids(v) -> list[int]:
    return [i for i in v if isinstance(i, int) and not isinstance(i, bool)] if isinstance(v, list) else []


def approval_key(sub) -> tuple:
    """업로드의 승인 순서 키 — approval_seq(승인 트랜잭션에서 잠금 하에 부여). 시계(reviewed_at)는 쓰지 않는다."""
    seq = sub.approval_seq
    return (seq if isinstance(seq, int) else 0, sub.pk)


def owner_key(row) -> tuple:
    """도메인 행(레지멘·시퀀스·영상)의 접는 순서 — 업로드가 없는 행은 맨 앞(pk 순), 나머지는 소유 업로드 승인 순 → 행 pk."""
    s = row.submission
    if s is None:
        return (0, 0, 0, row.pk)
    return (1, *approval_key(s), row.pk)


def is_legacy(sub) -> bool:
    """재계산 규칙 이전에 승인된 건(apply_summary 에 recompute 표지 없음)."""
    return not (isinstance(sub.apply_summary, dict) and "recompute" in sub.apply_summary)


def legacy_touched(p: Patient, submission_domain: str, exclude=None) -> bool:
    """이 환자를 행으로 언급한 승인 업로드(해당 도메인, exclude 제외 — 지금 승인 중인 건) 중 예전 건이 있는가.
    기준 스냅샷의 legacy_mixed 판단용."""
    sub_ids = (SubmissionRecord.objects.filter(submission__status=Submission.Status.APPROVED,
                                               submission__domain=submission_domain,
                                               payload__condition_id=p.condition_id)
               .exclude(status=SubmissionRecord.RowStatus.ERROR).exclude(submission=exclude)
               .values_list("submission_id", flat=True).distinct())
    return any(is_legacy(s) for s in Submission.objects.filter(pk__in=list(sub_ids)).only("pk", "apply_summary"))


# ─── 기준 스냅샷 ───
def _snapshot(p: Patient, domain: str) -> dict:
    out = {f: getattr(p, f) for f in DERIVED_FIELDS[domain]}
    if "cxr_findings" in out:
        out["cxr_findings"] = list(out["cxr_findings"]) if isinstance(out["cxr_findings"], list) else []
    return out


def capture_baseline(p: Patient, domain: str, sub=None) -> None:
    """업로드가 이 환자의 파생 값을 처음 건드리기 직전에 호출한다(이미 있으면 그대로 둔다)."""
    if DerivedBaseline.objects.filter(patient=p, domain=domain).exists():
        return
    model = ROW_MODEL[domain]
    pre = list(model.objects.filter(patient=p).order_by("pk").values_list("pk", flat=True))
    # 예전 건 효과가 이미 값에 들어 있다 — 그 업로드 소유 행이 남아 있거나 예전 건이 이 환자를 건드렸다
    mixed = model.objects.filter(pk__in=pre, submission__isnull=False).exists() or legacy_touched(p, domain, sub)
    DerivedBaseline.objects.create(patient=p, domain=domain, values=_snapshot(p, domain), preexisting_ids=pre,
                                   legacy_mixed=mixed)


def capture_biochem_baseline(p: Patient, row_pk: int, before: dict, sub=None) -> None:
    """업로드 밖에서 생긴 생화학 행을 업로드가 처음 '갱신'하기 직전 값(행별 최초 1회)."""
    base = DerivedBaseline.objects.filter(patient=p, domain=DerivedBaseline.Domain.BIOCHEM).first()
    if base is None:
        base = DerivedBaseline.objects.create(patient=p, domain=DerivedBaseline.Domain.BIOCHEM, values={},
                                              legacy_mixed=legacy_touched(p, "lab", sub))
    values = base.values if isinstance(base.values, dict) else {}
    if str(row_pk) not in values:
        values[str(row_pk)] = {k: before.get(k, "") for k in BIOCHEM_FIELDS}
        base.values = values
        base.save(update_fields=["values"])


def _str_or_none(v):
    return v if v is None or isinstance(v, str) else None


def _count(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _start_state(base: DerivedBaseline, domain: str) -> dict:
    v = base.values if isinstance(base.values, dict) else {}
    if domain == "genomic":
        return {"has_sequence": v.get("has_sequence") is True, "sra_id": _str_or_none(v.get("sra_id")),
                "lineage": _str_or_none(v.get("lineage"))}
    found = v.get("cxr_findings")
    return {"has_cxr": v.get("has_cxr") is True, "has_ct": v.get("has_ct") is True,
            "has_pet_ct": v.get("has_pet_ct") is True, "cxr_count": _count(v.get("cxr_count")),
            "ct_count": _count(v.get("ct_count")), "pet_ct_count": _count(v.get("pet_ct_count")),
            "cxr_findings": [f for f in found if isinstance(f, str)] if isinstance(found, list) else []}


# ─── 행 단위 효과(apply 와 삭제 재계산이 공유) ───
def fold_sequence(state: dict, run: SequenceRun) -> None:
    """시퀀스 1건: 보유 표시, SRA·계통은 **값이 있을 때만** 덮어쓴다(빈 값은 이전 값 유지)."""
    state["has_sequence"] = True
    if run.sra_accession:
        state["sra_id"] = run.sra_accession
    if run.lineage:
        state["lineage"] = run.lineage


def fold_imaging(state: dict, study: ImagingStudy) -> None:
    """영상 1건: 모달리티별 보유·수 +1, CXR 이면 소견(한글)을 목록 끝에 중복 없이 추가."""
    if study.modality == "CXR":
        state["has_cxr"], state["cxr_count"] = True, state["cxr_count"] + 1
        found = state["cxr_findings"]
        for f in study.findings or []:
            ko = FINDING_KO.get(f.get("finding", "")) if isinstance(f, dict) else None
            if ko and ko not in found:
                found.append(ko)
    elif study.modality == "CT":
        state["has_ct"], state["ct_count"] = True, state["ct_count"] + 1
    else:
        state["has_pet_ct"], state["pet_ct_count"] = True, state["pet_ct_count"] + 1


FOLD = {"genomic": fold_sequence, "imaging": fold_imaging}


def recompute_patient_derived(p: Patient, domain: str) -> bool:
    """기준 스냅샷 + 남은 행(승인 순)을 접어 p 의 파생 필드를 다시 쓴다(저장은 호출자). 스냅샷이 없으면 False(미변경)."""
    base = DerivedBaseline.objects.filter(patient=p, domain=domain).first()
    if base is None:
        return False
    state = _start_state(base, domain)
    skip = set(int_ids(base.preexisting_ids))
    rows = [r for r in ROW_MODEL[domain].objects.filter(patient=p).select_related("submission") if r.pk not in skip]
    rows.sort(key=owner_key)
    step = FOLD[domain]
    for r in rows:
        step(state, r)
    for k, v in state.items():
        setattr(p, k, v)
    return True


# ─── 레지멘 ───
def regimen_drugs(reg: Regimen) -> list[tuple]:
    return [(d.drug_name, d.drug_code, d.start_day, d.end_day, d.dosage, d.route) for d in reg.drugs.all()]


def regimen_sequence(p: Patient) -> list[Regimen]:
    """환자의 레지멘을 접는 순서(업로드 없는 것 먼저, 나머지는 소유 업로드 승인 순)로."""
    regs = list(p.regimens.select_related("submission").prefetch_related("drugs"))
    regs.sort(key=owner_key)
    return regs


def valid_biochem_values(v) -> dict | None:
    if isinstance(v, dict) and all(isinstance(v.get(k), str) for k in BIOCHEM_FIELDS):
        return {k: v[k] for k in BIOCHEM_FIELDS}
    return None
