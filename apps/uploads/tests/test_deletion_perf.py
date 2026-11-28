"""삭제 재적용(replay) 성능 측정 — 기본 실행에서는 건너뛴다(TB_PERF=1 일 때만).

    TB_PERF=1 .venv/bin/pytest apps/uploads/tests/test_deletion_perf.py -s

시나리오(5단계 이월 '1000행×8 업로드, 하나 삭제'):
  wide   — 검사실(생화학) 업로드 8건, 각 1000행이 시드 환자 50명 전체에 고루 퍼짐. 그중 하나 삭제(모든 환자 재적용).
  narrow — 7건은 위와 같고, 삭제하는 1건만 1000행이 환자 5명에 몰림(재적용 대상 5명).
측정값은 표준 출력에 남긴다(-s). 단언은 하지 않는다(기계마다 다름) — 결과 상태가 '남은 것만 승인'과 같은지는
test_deletion_invariant 가 검증한다.
"""
from __future__ import annotations

import os
import time

import pytest
from django.utils import timezone

from apps.cases.models import BiochemResult, Patient
from apps.uploads.models import Submission, SubmissionRecord
from apps.uploads.services import deletion, schemas, workflow

pytestmark = [pytest.mark.django_db,
              pytest.mark.skipif(os.environ.get("TB_PERF") != "1", reason="성능 측정은 TB_PERF=1 일 때만")]

CHECKLIST = [str(i) for i in range(len(schemas.REVIEW_CHECK_ITEMS))]
ROWS = 1000
UPLOADS = 8
TESTS = ["ALT", "AST", "Bilirubin", "Creatinine", "Albumin", "Hemoglobin", "WBC"]


def _make(tag: int, cids: list[str], researcher) -> Submission:
    sub = Submission.objects.create(
        upload_id=f"UPL-29991231-{tag:03d}", submitter=researcher, domain="lab", mode="csv",
        method=schemas.method_for("lab", "csv"), status=Submission.Status.PENDING, submitted_at=timezone.now(),
        csv_headers=["biochemistry", "condition_id", "specimen_date", "specimen_id", "specimen_type"])
    recs = []
    for i in range(ROWS):
        cid = cids[i % len(cids)]
        day = f"2025-{1 + (i // 28) % 12:02d}-{1 + i % 28:02d}"
        recs.append(SubmissionRecord(submission=sub, row_no=i + 1, status="valid", payload={
            "condition_id": cid, "specimen_id": f"SP-{tag}-{i}", "specimen_date": day, "specimen_type": "Sputum",
            "biochemistry": [{"test": TESTS[(i + tag) % len(TESTS)], "value": str(10 + (i * 7 + tag) % 90),
                              "unit": "U/L", "date": day}]}))
    SubmissionRecord.objects.bulk_create(recs)
    return sub


@pytest.mark.parametrize("scenario", ["wide", "narrow"])
def test_deletion_replay_timing(scenario, demo_users, seeded_patients):
    admin, researcher = demo_users["admin"], demo_users["researcher"]
    cids = list(Patient.objects.order_by("pk").values_list("condition_id", flat=True))
    subs = []
    t0 = time.perf_counter()
    for k in range(UPLOADS):
        target = cids[:5] if (scenario == "narrow" and k == 3) else cids
        sub = _make(k + 1, target, researcher)
        workflow.approve(sub, admin, CHECKLIST)
        subs.append(sub)
    t_approve = time.perf_counter() - t0
    victim = subs[3]
    Submission.objects.filter(pk=victim.pk).update(deletion_requested_at=timezone.now())
    victim.refresh_from_db()
    rows_before = BiochemResult.objects.count()
    t0 = time.perf_counter()
    deletion.approve_deletion(victim, admin)
    t_delete = time.perf_counter() - t0
    print(f"\n[perf:{scenario}] patients={len(cids)} uploads={UPLOADS}x{ROWS} rows  "
          f"approve-all={t_approve:.2f}s  delete-one={t_delete:.2f}s  biochem {rows_before}->{BiochemResult.objects.count()}")
