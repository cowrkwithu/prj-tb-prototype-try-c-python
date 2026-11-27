"""삭제 승인 동시성 (3c 2차 리뷰 PLAUSIBLE) — 공유하는 두 업로드를 동시에 삭제해도 500 이 나지 않고 상태가 일관된다.

테스트 DB 는 공유 캐시 in-memory SQLite 라 세션 미들웨어까지 동시에 돌리면 테이블 잠금이 테스트 환경에서만 생긴다.
그래서 스레드에서는 뷰 함수(deletion_approve → _action)를 RequestFactory 요청으로 직접 호출한다(쿠키 메시지 저장소)."""
from __future__ import annotations

import threading

import pytest
from django.contrib.messages.storage.cookie import CookieStorage
from django.db import OperationalError, connection
from django.test import RequestFactory
from django.utils import timezone

from apps.cases.models import Patient, SequenceRun
from apps.uploads import deletion_views
from apps.uploads.models import Submission
from apps.uploads.services import deletion

from .test_deletion_invariant import World, gen


def _two_sharing_uploads(users):
    w = World(users)
    a = w.build(gen("KR-2024-0002", "a", "SRR1", "Beijing"), 0)
    b = w.build(gen("KR-2024-0002", "a", "SRR2", "LAM"), 1)  # 같은 파일 → a 의 행을 공유
    w.approve(a)
    w.approve(b)
    Submission.objects.filter(pk__in=[a.pk, b.pk]).update(deletion_requested_at=timezone.now())
    return a.upload_id, b.upload_id


@pytest.mark.django_db(transaction=True)
def test_concurrent_deletion_of_sharing_uploads_no_500(demo_users, seeded_patients):
    ua, ub = _two_sharing_uploads(demo_users)
    results, errors = {}, []
    barrier = threading.Barrier(2)

    def run(uid, user):
        try:
            req = RequestFactory().post(f"/admin/deletions/{uid}/approve")
            req.user = user
            req.session = {}
            req._messages = CookieStorage(req)
            barrier.wait(timeout=10)
            results[uid] = deletion_views.deletion_approve(req, uid).status_code
        except Exception as exc:  # 테스트가 실패 원인을 보이도록 모은다
            errors.append(repr(exc))
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(ua, demo_users["admin"])),
               threading.Thread(target=run, args=(ub, demo_users["dmanager"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert results == {ua: 302, ub: 302}
    left = set(Submission.objects.filter(upload_id__in=[ua, ub]).values_list("upload_id", flat=True))
    p = Patient.objects.get(condition_id="KR-2024-0002")
    runs = list(SequenceRun.objects.filter(patient=p).select_related("submission"))
    if not left:  # 둘 다 처리됨 → 행도 없고 시드 값으로
        assert not runs and (p.sra_id, p.lineage) == ("SRR180002", "L2.2.1")
    else:  # 겹쳐서 하나가 실패(다시 시도 안내) → 남은 업로드가 행을 소유하고 값도 그 업로드 기준
        assert len(runs) == 1 and runs[0].submission.upload_id in left
        for uid in left:
            assert deletion.pending_deletions().filter(upload_id=uid).exists()


@pytest.mark.django_db
def test_database_error_in_deletion_becomes_message(client, demo_users, seeded_patients, monkeypatch):
    ua, _ = _two_sharing_uploads(demo_users)

    def boom(*a, **kw):
        raise OperationalError("database is locked")

    monkeypatch.setattr(deletion, "approve_deletion", boom)
    client.force_login(demo_users["admin"])
    r = client.post(f"/admin/deletions/{ua}/approve", follow=True)
    assert r.status_code == 200 and "다시 시도" in r.content.decode()
    assert Submission.objects.filter(upload_id=ua).exists()


@pytest.mark.django_db
def test_sequential_deletion_of_sharing_uploads(demo_users, seeded_patients):
    ua, ub = _two_sharing_uploads(demo_users)
    deletion.approve_deletion(Submission.objects.get(upload_id=ua), demo_users["admin"])
    run = SequenceRun.objects.get(patient__condition_id="KR-2024-0002")
    assert run.submission.upload_id == ub and (run.sra_accession, run.lineage) == ("SRR2", "LAM")
    deletion.approve_deletion(Submission.objects.get(upload_id=ub), demo_users["admin"])
    assert not SequenceRun.objects.filter(patient__condition_id="KR-2024-0002").exists()
