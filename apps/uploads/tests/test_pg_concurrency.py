"""PostgreSQL 동시성 — approval_seq 채번 재시도(apply.assign_approval_seq)를 실제 경합으로 확인한다.

PostgreSQL(READ COMMITTED)에서는 환자가 겹치지 않는 두 승인이 동시에 진행될 수 있어, 둘 다 같은 max+1 을 읽고
같은 approval_seq 를 쓰려 한다. 뒤의 UPDATE 는 앞 트랜잭션의 커밋을 (unique 인덱스에서) 기다렸다가 IntegrityError →
savepoint 로 되돌리고 다시 읽어 다음 번호를 받아야 한다.
SQLite 는 쓰기 트랜잭션이 IMMEDIATE 로 직렬화돼 두 번째 쓰기가 시작조차 못 하므로 이 경합이 생기지 않는다(해당 없음).
"""
from __future__ import annotations

import threading
import time

import pytest
from django.db import connection, transaction

from apps.uploads.models import Submission
from apps.uploads.services.apply import assign_approval_seq


def _sub(user, uid):
    return Submission.objects.create(upload_id=uid, submitter=user, domain="lab", method="csv", mode="csv",
                                     status=Submission.Status.PENDING)


def _wait_for_lock_waiter(timeout=5.0) -> bool:
    """다른 백엔드가 잠금 대기(wait_event_type='Lock') 중인지 — 경합이 실제로 일어났는지 확인."""
    from django.db import connections

    deadline = time.monotonic() + timeout
    with connections["default"].cursor() as c:
        while time.monotonic() < deadline:
            c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                      "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()")
            if c.fetchone()[0]:
                return True
            time.sleep(0.05)
    return False


@pytest.mark.django_db(transaction=True)
def test_approval_seq_retry_under_real_contention(demo_users):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL 전용: SQLite 는 IMMEDIATE 트랜잭션으로 쓰기가 직렬화돼 같은 번호 경합이 생기지 않는다")
    user = demo_users["admin"]
    a, b = _sub(user, "UPL-PG-A"), _sub(user, "UPL-PG-B")
    a_assigned, saw_waiter, result, errors = threading.Event(), [], {}, []

    def first():
        try:
            with transaction.atomic():
                result["a"] = assign_approval_seq(a)
                a_assigned.set()
                saw_waiter.append(_wait_for_lock_waiter())  # b 의 UPDATE 가 같은 번호에서 막힐 때까지 커밋을 미룬다
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
            a_assigned.set()
        finally:
            connection.close()

    t = threading.Thread(target=first)
    t.start()
    assert a_assigned.wait(10)
    with transaction.atomic():
        result["b"] = assign_approval_seq(b)  # a 커밋 전에 max+1 을 읽음 → 대기 → IntegrityError → 재시도
    t.join(30)
    assert not errors, errors
    assert saw_waiter == [True], "경합이 재현되지 않았다(b 가 a 의 번호에서 대기하지 않음)"
    assert result["b"] == result["a"] + 1
    assert dict(Submission.objects.filter(pk__in=[a.pk, b.pk]).values_list("upload_id", "approval_seq")) == {
        "UPL-PG-A": result["a"], "UPL-PG-B": result["b"]}
