"""DB 잠금(쓰기 경합) → 500 대신 503 안내 (5단계, apps.core.middleware.DatabaseBusyMiddleware).

실제 잠금 재현: 테스트 DB(공유 캐시 메모리 SQLite)에 두 번째 연결을 열어 대상 테이블에 쓰기 트랜잭션을 잡아 둔 채
요청을 보낸다 → 뷰의 쓰기가 OperationalError('database table is locked') 로 실패한다. 운영(파일 DB·WAL)에서는
같은 상황이 busy_timeout(5초) 뒤 'database is locked' 로 나타난다 — 문구 판별은 단위 테스트로 확인한다.
PostgreSQL 테스트 DB 에서는 두 번째 연결의 LOCK TABLE 로 lock_timeout 초과(LockNotAvailable)를 실제로 일으킨다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

import pytest
from django.db import IntegrityError, OperationalError, connection
from django.test import override_settings

from apps.cohorts.models import Cohort
from apps.core.middleware import is_db_busy


@contextmanager
def table_locked(model):
    """다른 연결이 model 의 테이블에 쓰기 잠금을 잡고 있는 상태(백엔드별로 실제 잠금을 만든다)."""
    connection.ensure_connection()
    if connection.vendor == "postgresql":
        with _pg_table_locked(model):
            yield
        return
    name = connection.settings_dict["NAME"]
    if connection.vendor != "sqlite" or "mode=memory" not in str(name):
        pytest.skip("공유 캐시 메모리 SQLite 또는 PostgreSQL 테스트 DB 에서만 재현")
    other = sqlite3.connect(str(name), uri=True, isolation_level=None, check_same_thread=False)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute(f'DELETE FROM "{model._meta.db_table}" WHERE 1 = 0')  # 테이블 쓰기 잠금만 잡는다
        yield
    finally:
        other.execute("ROLLBACK")
        other.close()


@contextmanager
def _pg_table_locked(model):
    """PostgreSQL: 두 번째 연결이 LOCK TABLE … IN EXCLUSIVE MODE(쓰기와 충돌, 읽기는 허용)를 잡는다.
    요청 연결의 lock_timeout 을 잠시 짧게 해(운영 기본 5초) 실제 'canceling statement due to lock timeout' 을
    빠르게 일으킨다 — 미들웨어는 운영과 같은 오류 문구를 받는다."""
    other = connection.copy()
    other.ensure_connection()
    other.set_autocommit(False)
    with connection.cursor() as c:
        c.execute("SHOW lock_timeout")
        saved = c.fetchone()[0]
        c.execute("SET lock_timeout = '300ms'")
    try:
        with other.cursor() as oc:
            oc.execute(f'LOCK TABLE "{model._meta.db_table}" IN EXCLUSIVE MODE')
        yield
    finally:
        other.rollback()
        other.close()
        with connection.cursor() as c:
            c.execute("SELECT set_config('lock_timeout', %s, false)", [saved])


SAVE = {"query": "country=한국&diag=MDR-TB", "name": "잠금 테스트"}


# ─── 판별 ───
@pytest.mark.parametrize("msg", ["database is locked", "database table is locked: cohorts_cohort",
                                 "database schema is locked: main",
                                 "canceling statement due to lock timeout", "deadlock detected",
                                 "could not serialize access due to concurrent update"])
def test_busy_messages_detected(msg):
    assert is_db_busy(OperationalError(msg))


@pytest.mark.parametrize("exc", [OperationalError("no such table: x"), OperationalError("disk I/O error"),
                                 IntegrityError("database is locked"), ValueError("database is locked")])
def test_other_errors_not_swallowed(exc):
    assert not is_db_busy(exc)


# ─── 실제 잠금 재현 ───
@pytest.mark.django_db(transaction=True)
def test_html_post_while_locked_gives_503_page(researcher_client, seeded_patients):
    with table_locked(Cohort):
        r = researcher_client.post("/depot/cohorts/save", SAVE)
    assert r.status_code == 503
    assert r["Retry-After"] == "5"
    body = r.content.decode()
    assert "잠시 후 다시 시도해주세요" in body and "저장되지 않았습니다" in body
    assert "<html" in body  # 안내 화면(base 레이아웃)
    assert not Cohort.objects.filter(name="잠금 테스트").exists()
    # 잠금이 풀리면 같은 요청이 정상 처리된다
    r = researcher_client.post("/depot/cohorts/save", SAVE)
    assert r.status_code == 302 and Cohort.objects.filter(name="잠금 테스트").count() == 1


@pytest.mark.django_db(transaction=True)
def test_english_message(researcher_client, seeded_patients):
    researcher_client.post("/lang", {"lang": "en"})
    with table_locked(Cohort):
        r = researcher_client.post("/depot/cohorts/save", SAVE)
    assert r.status_code == 503 and "Please try again shortly" in r.content.decode()


@pytest.mark.django_db(transaction=True)
def test_htmx_post_while_locked_gives_toast_trigger(researcher_client, seeded_patients):
    with table_locked(Cohort):
        r = researcher_client.post("/depot/cohorts/save", SAVE, HTTP_HX_REQUEST="true")
    assert r.status_code == 503
    assert r["HX-Reswap"] == "none"
    trig = json.loads(r["HX-Trigger"])
    assert trig["showToast"]["type"] == "error" and "잠시 후" in trig["showToast"]["message"]
    r["HX-Trigger"].encode("ascii")  # 헤더는 ASCII(\u 이스케이프)


@pytest.mark.django_db(transaction=True)
def test_json_fetch_while_locked_gives_json_503(researcher_client, seeded_patients):
    """영상 전문가 소견 저장(fetch, Accept: application/json) — rap.js 가 읽는 error 키로 안내."""
    from apps.imaging.models import ExpertAnnotation
    from apps.imaging.services import load_imaging_samples

    load_imaging_samples()
    from apps.cases.models import ImagingStudy

    study = ImagingStudy.objects.filter(is_public_sample=True, modality="CXR").first()
    data = {"study": study.pk, "shape": "rect", "x": "10", "y": "20", "width": "15", "height": "12",
            "finding_type": "공동", "location": "우폐", "severity": "중증", "text": "추적 필요"}
    with table_locked(ExpertAnnotation):
        r = researcher_client.post("/rap/annotations", data, HTTP_ACCEPT="application/json")
    assert r.status_code == 503 and r["Retry-After"] == "5"
    j = r.json()
    assert j["status"] == "db_busy" and "잠시 후" in j["error"] and j["message"] == j["error"]
    assert not ExpertAnnotation.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_xhr_upload_style_request_gets_json(researcher_client, seeded_patients):
    """파일 업로드 XHR(X-Requested-With) 은 uploads.js 가 읽는 message 키로 안내."""
    with table_locked(Cohort):
        r = researcher_client.post("/depot/cohorts/save", SAVE, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert r.status_code == 503 and "잠시 후" in r.json()["message"]


@pytest.mark.django_db(transaction=True)
def test_api_post_while_locked_gives_json_503(demo_users, seeded_patients):
    """REST API POST /api/v1/cohorts — API 형식(JSON)으로 503."""
    from django.core.cache import cache
    from django.test import Client

    from apps.datasharing import services as ds
    from apps.datasharing.tests.conftest import make_dar

    cache.clear()
    make_dar(demo_users["researcher"], ["clinical"], "approved")
    _, raw = ds.issue_key(demo_users["researcher"], "kim@snubh.org", "분당서울대병원")
    conds = [{"field": "clinical.diagnosisType", "operator": "eq", "value": "MDR-TB"}]
    body = json.dumps({"name": "API 잠금", "conditions": conds})
    with table_locked(Cohort):
        r = Client().post("/api/v1/cohorts", body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 503, r.content
    assert r.json()["status"] == "db_busy" and r["Retry-After"] == "5"
    assert not Cohort.objects.filter(name="API 잠금").exists()
    cache.clear()


# ─── 잠금 아닌 DB 오류는 그대로 드러난다(500) ───
@pytest.mark.django_db
@override_settings(DEBUG=False)
def test_non_lock_operational_error_still_500(researcher_client, monkeypatch):
    from apps.cohorts import services

    def boom(*a, **k):
        raise OperationalError("no such column: oops")

    monkeypatch.setattr(services, "save_cohort", boom)
    monkeypatch.setattr(services, "resolve_conditions", lambda q, lang: {"error": "", "tree": {}, "filters": {}})
    researcher_client.raise_request_exception = False
    r = researcher_client.post("/depot/cohorts/save", SAVE)
    assert r.status_code == 500


@pytest.mark.django_db
def test_simulated_sqlite_busy_timeout(researcher_client, monkeypatch):
    """파일 DB 의 busy_timeout 초과 문구('database is locked')도 503."""
    from apps.cohorts import services

    def busy(*a, **k):
        raise OperationalError("database is locked")

    monkeypatch.setattr(services, "save_cohort", busy)
    monkeypatch.setattr(services, "resolve_conditions", lambda q, lang: {"error": "", "tree": {}, "filters": {}})
    r = researcher_client.post("/depot/cohorts/save", SAVE)
    assert r.status_code == 503 and r["Retry-After"] == "5"


# ─── PostgreSQL 잠금 계열 오류: SQLSTATE 로 판별(서버 메시지 언어와 무관) ───
class _FakePgError(Exception):
    def __init__(self, sqlstate):
        super().__init__("메시지 언어가 달라도")  # lc_messages=ko_KR 등 — 문구 표식이 없다
        self.sqlstate = sqlstate


@pytest.mark.parametrize("code, busy", [("55P03", True), ("40P01", True), ("40001", True),
                                        ("23505", False), ("42P01", False), (None, False)])
def test_pg_sqlstate_classification(code, busy):
    exc = OperationalError("localized text")
    exc.__cause__ = _FakePgError(code)
    assert is_db_busy(exc) is busy


@pytest.mark.django_db
@pytest.mark.parametrize("code, busy", [("55P03", True), ("40P01", True), ("40001", True), ("53300", False)])
def test_pg_real_errors_classified(code, busy):
    """실제 PostgreSQL 이 낸 오류(psycopg → Django 래핑)를 판별한다. SQLite 에는 SQLSTATE·PL/pgSQL 이 없어 해당 없음."""
    from django.db import DatabaseError, transaction

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL 전용: SQLSTATE 오류를 서버에서 직접 발생시킨다(SQLite 는 위 문구 판별 테스트가 담당)")
    with pytest.raises(DatabaseError) as ei, transaction.atomic(), connection.cursor() as c:
        c.execute(f"DO $$ BEGIN RAISE EXCEPTION 'x' USING ERRCODE = '{code}'; END $$")
    assert is_db_busy(ei.value) is busy


@pytest.mark.django_db(transaction=True)
def test_pg_real_deadlock_is_busy():
    """두 연결이 서로의 행 잠금을 기다리게 만들어 PostgreSQL 교착 탐지(40P01)를 실제로 일으킨다."""
    import threading

    from django.db import DatabaseError

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL 전용: SQLite 는 데이터베이스 단위 잠금이라 행 교착이 생기지 않는다(busy_timeout 으로 대기)")
    from apps.accounts.models import User

    u1 = User.objects.create_user("dl1", password="x")
    u2 = User.objects.create_user("dl2", password="x")
    table = User._meta.db_table
    barrier = threading.Barrier(2)
    errors = []

    def worker(first, second):
        conn = connection.copy()
        try:
            conn.set_autocommit(False)
            with conn.cursor() as c:
                c.execute("SET deadlock_timeout = '100ms'")
                c.execute(f'SELECT 1 FROM "{table}" WHERE id = %s FOR UPDATE', [first])
                barrier.wait(timeout=10)
                c.execute(f'SELECT 1 FROM "{table}" WHERE id = %s FOR UPDATE', [second])
        except DatabaseError as e:
            errors.append(e)
        finally:
            conn.rollback()
            conn.close()

    ts = [threading.Thread(target=worker, args=(u1.pk, u2.pk)), threading.Thread(target=worker, args=(u2.pk, u1.pk))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(errors) == 1 and is_db_busy(errors[0]), errors


# ─── 503 '저장되지 않았습니다' 가 참인가 — 요청 중간(첫 쓰기 뒤)에 실제 잠금 오류가 나도 앞의 쓰기가 남지 않는다 ───
@contextmanager
def lock_error_after(first_write_table: str, trigger_sql_prefix: str, trigger_table: str):
    """요청이 first_write_table 에 쓴 뒤, trigger 문장(예: UPDATE "cohorts_cohort") 직전에 잠금 오류를 일으킨다.

    - PostgreSQL: 실제 잠금 — 다른 연결이 SeedLog 테이블을 EXCLUSIVE 로 잡아 두고, 그 시점에 요청 연결이 SeedLog 에
      쓰게 해 lock_timeout(55P03)을 DB 가 실제로 낸다(요청 쪽 테이블은 건드리지 않는다).
    - SQLite: 트랜잭션 중간의 잠금 오류는 실제로 만들 수 없다 — 쓰기 트랜잭션은 시작(BEGIN IMMEDIATE)할 때 DB 쓰기 잠금을
      잡으므로 이후 문장은 잠금 대기를 하지 않는다(이번 수정이 기대는 성질). 공유 캐시 테스트 DB 에서도 다른 연결은
      우리 트랜잭션이 끝날 때까지 쓰기를 시작하지 못한다. 그래서 운영 busy_timeout 초과와 같은 오류('database is locked')를
      그 지점에서 던져 '중간 실패 시 앞의 쓰기도 롤백된다'를 확인한다."""
    from django.db import OperationalError as DjOperationalError

    from apps.core.models import SeedLog

    state = {"wrote": False, "fired": False}
    pg = connection.vendor == "postgresql"

    def wrapper(execute, sql, params, many, context):
        head = sql.lstrip()[:160]
        if state["wrote"] and not state["fired"] and head.upper().startswith(trigger_sql_prefix) and trigger_table in head:
            state["fired"] = True
            if pg:
                with connection.cursor() as c:  # Django 커서 → django.db.OperationalError 로 감싸진다(운영과 같음)
                    c.execute(f'DELETE FROM "{SeedLog._meta.db_table}" WHERE 1 = 0')
            else:
                raise DjOperationalError("database is locked")
        if head.upper().startswith(("INSERT", "UPDATE")) and first_write_table in head:
            state["wrote"] = True
        return execute(sql, params, many, context)

    if pg:
        with table_locked(SeedLog), connection.execute_wrapper(wrapper):
            yield state
    else:
        with connection.execute_wrapper(wrapper):
            yield state


@pytest.mark.django_db(transaction=True)
def test_api_cohort_lock_after_insert_saves_nothing(client, demo_users, seeded_patients):
    """(리뷰 3a) POST /api/v1/cohorts — 생성 뒤 설명 저장에서 잠금 → 503, 코호트도 남지 않는다."""
    from django.core.cache import cache

    from apps.datasharing import services as ds
    from apps.datasharing.tests.conftest import auth, make_dar

    cache.clear()
    make_dar(demo_users["researcher"], ["clinical"], "approved")
    _, raw = ds.issue_key(demo_users["researcher"], "kim@snubh.org", "x")
    body = json.dumps({"name": "부분 저장", "description": "설명",
                       "conditions": [{"field": "demographics.age", "operator": "gte", "value": 10}]})
    before = Cohort.objects.count()
    with lock_error_after('"cohorts_cohort"', "UPDATE", '"cohorts_cohort"') as st:
        r = client.post("/api/v1/cohorts", body, content_type="application/json", **auth(raw))
    assert st["fired"], "잠금 오류 지점(설명 UPDATE)에 도달하지 않았다"
    assert r.status_code == 503 and r.json()["status"] == "db_busy"
    assert Cohort.objects.count() == before
    cache.clear()


@pytest.mark.django_db(transaction=True)
def test_upload_files_lock_after_draft_saves_nothing(client, demo_users, settings, tmp_path):
    """(리뷰 3b) 새 업로드의 첫 파일 — 임시저장(draft) 생성 뒤 파일 행 저장에서 잠금 → 503 JSON,
    draft 도 저장된 파일도 남지 않는다."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.uploads.models import Submission, SubmissionFile
    from apps.uploads.tests.conftest import make_fastq_gz

    settings.MEDIA_ROOT = tmp_path / "media"
    client.force_login(demo_users["researcher"])
    f = SimpleUploadedFile("TB_SP-1.fastq.gz", make_fastq_gz(), content_type="application/gzip")
    with lock_error_after('"uploads_submission"', "INSERT", SubmissionFile._meta.db_table) as st:
        r = client.post("/upload/genomic/files", {"mode": "individual", "files": [f]},
                        HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert st["fired"], "잠금 오류 지점(파일 행 INSERT)에 도달하지 않았다"
    assert r.status_code == 503 and r.json()["status"] == "db_busy"
    assert not Submission.objects.exists() and not SubmissionFile.objects.exists()
    left = [p for p in (tmp_path / "media").rglob("*") if p.is_file()]
    assert left == []  # 롤백된 요청이 저장한 파일은 지운다(apps.core.filetx)
