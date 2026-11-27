"""데이터 마이그레이션 — 삭제 재적용(replay) 설계로 바꾸며 기존 데이터를 채운다.

1. Submission.approval_seq: 이미 승인된 업로드에 (reviewed_at, id) 순으로 1, 2, … (승인 시각이 없으면 created_at).
2. BiochemResult.submission: 승인된 검사실 업로드의 apply_summary.created_biochem_ids 로 소유 업로드를 채운다
   (여러 업로드가 같은 행을 적었으면 승인 순서가 가장 앞선 것).
3. DerivedBaseline.legacy_mixed: 재계산 규칙 이전(recompute 표지 없음)에 승인된 업로드가 건드린 환자의 기준 스냅샷,
   그리고 스냅샷 시점 기존 행(preexisting_ids) 중에 업로드 소유 행이 있는 스냅샷은 예전 건 효과가 섞인 것으로 표시한다.
되돌리기는 아무것도 하지 않는다(0006 의 필드 제거가 값을 함께 없앤다).
"""
from django.db import migrations

BASELINE_DOMAIN = {"genomic": "genomic", "imaging": "imaging", "lab": "biochem"}


def _ints(v):
    return [i for i in v if isinstance(i, int) and not isinstance(i, bool)] if isinstance(v, list) else []


def forwards(apps, schema_editor):
    Submission = apps.get_model("uploads", "Submission")
    SubmissionRecord = apps.get_model("uploads", "SubmissionRecord")
    DerivedBaseline = apps.get_model("uploads", "DerivedBaseline")
    BiochemResult = apps.get_model("cases", "BiochemResult")
    SequenceRun = apps.get_model("cases", "SequenceRun")
    ImagingStudy = apps.get_model("cases", "ImagingStudy")

    approved = list(Submission.objects.filter(status="approved", approval_seq__isnull=True))
    approved.sort(key=lambda s: (s.reviewed_at or s.created_at, s.pk))
    start = max(Submission.objects.exclude(approval_seq=None).values_list("approval_seq", flat=True), default=0)
    for n, s in enumerate(approved, start=start + 1):
        Submission.objects.filter(pk=s.pk).update(approval_seq=n)

    for s in Submission.objects.filter(status="approved", domain="lab").order_by("approval_seq"):
        summary = s.apply_summary if isinstance(s.apply_summary, dict) else {}
        ids = _ints(summary.get("created_biochem_ids"))
        if ids:
            BiochemResult.objects.filter(pk__in=ids, submission__isnull=True).update(submission=s)

    for s in Submission.objects.filter(status="approved", domain__in=list(BASELINE_DOMAIN)):
        summary = s.apply_summary if isinstance(s.apply_summary, dict) else {}
        if "recompute" in summary:
            continue
        cids = {p.get("condition_id") for p in SubmissionRecord.objects.filter(submission=s).values_list("payload", flat=True)
                if isinstance(p, dict) and isinstance(p.get("condition_id"), str)}
        if cids:
            DerivedBaseline.objects.filter(domain=BASELINE_DOMAIN[s.domain], patient__condition_id__in=cids) \
                .update(legacy_mixed=True)

    models = {"genomic": SequenceRun, "imaging": ImagingStudy}
    for base in DerivedBaseline.objects.filter(domain__in=list(models), legacy_mixed=False):
        pre = _ints(base.preexisting_ids)
        if pre and models[base.domain].objects.filter(pk__in=pre, submission__isnull=False).exists():
            DerivedBaseline.objects.filter(pk=base.pk).update(legacy_mixed=True)


class Migration(migrations.Migration):

    dependencies = [
        ("uploads", "0006_approval_seq"),
        ("cases", "0004_biochem_submission"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
