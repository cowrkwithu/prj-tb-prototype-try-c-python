"""5단계 — 삭제 재적용 범위 축소(대상 환자 행만 정제)와 생화학 일괄 조회의 회귀 테스트.

결과 동등성(전부 승인 후 일부 삭제 == 남은 것만 승인)은 test_deletion_invariant 가 검증한다. 여기서는
  - 재적용·관련 업로드 탐색이 대상 환자 행만 clean_row 로 정리하는지(다른 환자 행 수와 무관),
  - 쿼리 수가 행 수에 비례해 폭증하지 않는지,
  - 같은 업로드 안 중복 키·업로드 밖 기존 행 갱신(기준 스냅샷) 규칙이 그대로인지 확인한다.
"""
import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.cases.models import BiochemResult, Patient
from apps.uploads.models import DerivedBaseline
from apps.uploads.services import apply as apply_mod
from apps.uploads.services import validators

from .test_deletion_invariant import World, lab

pytestmark = pytest.mark.django_db

P1, P2, P3 = "KR-2024-0001", "KR-2024-0002", "KR-2024-0003"


@pytest.fixture
def users(demo_users, seeded_patients):
    return demo_users


def _many_rows(cids, n):
    return lab(None, rows=[(cids[i % len(cids)], [("ALT", str(20 + i % 50))], f"2025-03-{1 + i % 28:02d}")
                           for i in range(n)])


def test_replay_cleans_only_target_patient_rows(users, monkeypatch):
    w = World(users)
    others = [p.condition_id for p in Patient.objects.exclude(condition_id=P1).order_by("pk")[:10]]
    big = [w.build(_many_rows(others + [P1], 220), k) for k in range(3)]  # P1 행은 업로드당 20행
    victim = w.build(lab(P1, ("AST", "33")), 3)
    for s in [*big, victim]:
        w.approve(s)
    calls = []
    real = validators.clean_row

    def counting(domain, row, row_no, form=False):
        calls.append(row.get("condition_id"))
        return real(domain, row, row_no, form)

    monkeypatch.setattr(apply_mod, "clean_row", counting)
    w.delete(victim.upload_id)
    # 대상 환자 P1 행만 정리했다(다른 환자 행 600여 개는 읽지 않음)
    assert calls and set(calls) == {P1}
    assert len(calls) <= 3 * 20 * 3 + 10  # 업로드 3건 × P1 20행 × (탐색·잠금·재적용) 여유


def test_biochem_queries_do_not_scale_per_row(users):
    w = World(users)
    small = w.build(_many_rows([P1, P2], 10), 0)
    large = w.build(_many_rows([P1, P2], 200), 1)
    counts = []
    for s in (small, large):
        with CaptureQueriesContext(connection) as ctx:
            w.approve(s)
        counts.append(len(ctx.captured_queries))
    # 새 행은 행마다 INSERT 1번이 남는다 — 예전(행마다 약 9쿼리)과 달리 그 밖의 쿼리는 행 수와 무관
    assert counts[1] - counts[0] <= (200 - 10) * 1.2, counts


def test_duplicate_key_in_same_upload_updates_same_row(users):
    w = World(users)
    s = w.build(lab(None, rows=[(P1, [("ALT", "80")], "2025-02-01"), (P1, [("ALT", "80")], "2025-02-01")]), 0)
    w.approve(s)
    rows = BiochemResult.objects.filter(patient__condition_id=P1, test="ALT")
    assert rows.count() == 1 and rows.get().flag == "H" and rows.get().submission_id == s.pk
    assert [d["condition_id"] for d in s.__class__.objects.get(pk=s.pk).apply_summary["duplicates"]] == [P1]


def test_preexisting_row_update_captures_baseline_once(users):
    p = Patient.objects.get(condition_id=P3)
    pre = BiochemResult.objects.create(patient=p, test="ALT", value="80 U/L", measured_on=datetime.date(2025, 2, 1),
                                       reference_range="old", flag="")
    w = World(users)
    a, b = w.build(lab(P3, ("ALT", "80")), 0), w.build(lab(P3, ("ALT", "80")), 1)
    w.approve(a)
    w.approve(b)
    pre.refresh_from_db()
    assert (pre.reference_range, pre.flag, pre.submission_id) == ("7-56", "H", None)
    base = DerivedBaseline.objects.get(patient=p, domain=DerivedBaseline.Domain.BIOCHEM)
    assert base.values == {str(pre.pk): {"reference_range": "old", "flag": ""}}  # 처음 갱신 직전 값만
    w.delete(a.upload_id)
    w.delete(b.upload_id)
    pre.refresh_from_db()
    assert (pre.reference_range, pre.flag) == ("old", "")  # 둘 다 지우면 기준 값으로
