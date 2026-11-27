"""3b 1차 리뷰 지적 재현 테스트.

1. 다중값 조건·스냅샷 조회의 SQL 파라미터 수가 환자 수에 비례하지 않는다(SQLite 변수 한도·Windows 32,766).
2. 정수 쿼리 파라미터는 ASCII 숫자만(유니코드 숫자 '²' → 500 이던 문제).
3. Fisher 정확검정 O(n) — n=10,000 에서도 빠르다.
4. NIT: t-검정 불가 표기, HTMX 오류 partial 200, HX-Push-Url 정리, 시드 소유자 부재 처리.
"""
import random
import re
import time
from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.cases.models import Patient
from apps.cohorts import conditions as cond
from apps.cohorts import services
from apps.cohorts.models import Cohort
from apps.core.seeding import is_seeded

pytestmark = pytest.mark.django_db

COMORB = ["당뇨", "HIV", "COPD", "만성신부전", "간경변", "알코올 의존", "고혈압", "폐암", "B형 간염"]
MUTS = ["rpoB S450L", "katG S315T", "inhA c-15t", "gyrA D94G", "gyrA A90V", "rrs a1401g", "rpoB H445Y"]
FIND = list(Patient.CXR_FINDINGS)


class ParamRecorder:
    """모든 SQL 실행의 파라미터 수를 기록한다."""

    def __init__(self):
        self.counts = []

    def __call__(self, execute, sql, params, many, context):
        self.counts.append(len(params or ()))
        return execute(sql, params, many, context)


def make_patients(n, seed=7):
    rnd = random.Random(seed)
    objs = []
    for i in range(n):
        objs.append(Patient(
            condition_id=f"BULK-{i:06d}", age=rnd.randint(1, 95), sex=rnd.choice("MF"),
            country=rnd.choice(Patient.COUNTRIES[:8]), site="s",
            diagnosis_type=rnd.choice(["DS-TB", "MDR-TB", "Pre-XDR-TB", "XDR-TB"]),
            treatment_outcome=rnd.choice(["Cured", "Completed", "Failed", "Died", "LTFU"]),
            treatment_duration=rnd.randint(30, 700),
            comorbidities=rnd.sample(COMORB, rnd.randint(0, 2)),
            has_sequence=rnd.random() < .6, lineage=rnd.choice([None, "L2.2.1", "L4.0"]),
            resistance_mutations=rnd.sample(MUTS, rnd.randint(0, 3)),
            has_cxr=True, has_ct=rnd.random() < .5, cxr_count=rnd.randint(0, 6),
            cxr_findings=rnd.sample(FIND, rnd.randint(0, 3)),
            registration_date=date(2024, 1, 1), last_update_date=date(2024, 1, 1)))
    Patient.objects.bulk_create(objs, batch_size=500)


def py_match(p, node):
    if "op" in node:
        r = [py_match(p, c) for c in node["children"]]
        return (all(r) if node["op"] == "AND" else any(r)) if r else True
    col = cond.FIELDS[node["field"]]["col"]
    return any(v in (getattr(p, col) or []) for v in node["value"])


def big_or_tree():
    """다중값 조건 99개 OR 트리 (각 1~3개 값, 세 JSON 컬럼 순환)."""
    rnd = random.Random(3)
    leaves = []
    for i in range(99):
        fld, pool = [("clinical.comorbidities", COMORB), ("genomics.resistanceMutations", MUTS),
                     ("imaging.cxrFindings", FIND)][i % 3]
        leaves.append({"field": fld, "operator": "in", "value": rnd.sample(pool, rnd.randint(1, 3))})
    return {"op": "OR", "children": leaves}  # 루트 + 조건 99개 = 노드 상한 100


def test_multivalue_tree_params_bounded_and_correct(researcher_client):
    make_patients(5000)
    tree = cond.validate(big_or_tree())
    rec = ParamRecorder()
    with CaptureQueriesContext(connection) as ctx, connection.execute_wrapper(rec):
        got = list(cond.matching_patients(tree).values_list("id", flat=True))
    assert len(ctx) == 1 and max(rec.counts) < 1000
    exp = sorted(p.id for p in Patient.objects.all() if py_match(p, tree))
    assert got == exp and 0 < len(exp) < 5000

    # 화면(카운트·분석·저장·CSV)도 같은 원칙
    from urllib.parse import quote
    import json
    url = "/depot?tree=" + quote(json.dumps(tree, ensure_ascii=False, separators=(",", ":")))
    assert len(url) < 20000
    rec = ParamRecorder()
    with connection.execute_wrapper(rec):
        for tab in services.TAB_KEYS:
            b = researcher_client.get(f"{url}&run=1&tab={tab}").content.decode()
        assert f'>{len(exp)}<' in b
        researcher_client.post("/depot/cohorts/save", {"query": "tree=" + quote(json.dumps(tree, ensure_ascii=False, separators=(",", ":")))})
        c = Cohort.objects.order_by("-id").first()
        assert sorted(c.snapshot_ids) == exp
        csv_text = researcher_client.get(f"/depot/cohorts/{c.pk}/export.csv").content.decode()
        researcher_client.get("/depot?panel=saved")
    assert max(rec.counts) < 1000
    assert csv_text.count("\n") == len(exp) + 1


def test_snapshot_total_40000_params_bounded(researcher_client, demo_users):
    make_patients(5000)
    ids = list(Patient.objects.values_list("id", flat=True))
    owner = demo_users["researcher"]
    # 스냅샷 합계 40,000 (국적 요약은 아직 계산 전 → 목록이 지연 계산)
    for k in range(8):
        Cohort.objects.create(name=f"big{k}", owner=owner, conditions={"op": "AND", "children": []},
                              snapshot_ids=ids, snapshot_countries=None, created_date=date(2025, 1, 1))
    rec = ParamRecorder()
    with CaptureQueriesContext(connection), connection.execute_wrapper(rec):
        b = researcher_client.get("/depot?panel=saved").content.decode()
    assert max(rec.counts) < 1000
    assert b.count('data-testid="saved-card"') == 8
    expected = list(dict.fromkeys(Patient.objects.order_by("id").values_list("country", flat=True)))
    c = Cohort.objects.get(name="big0")
    assert c.snapshot_countries == expected  # 계산해서 저장해 둠
    # 두 번째부터는 스냅샷 조회 없이 요약만 읽는다
    with CaptureQueriesContext(connection) as ctx:
        researcher_client.get("/depot?panel=saved")
    rec2 = ParamRecorder()
    with connection.execute_wrapper(rec2):
        researcher_client.get("/depot?panel=saved")
    assert max(rec2.counts) < 1000 and len(ctx) < 15


def test_total_values_limit():
    leaves = [{"field": "clinical.comorbidities", "operator": "in", "value": [f"v{i}-{j}" for j in range(50)]}
              for i in range(cond.MAX_TOTAL_VALUES // 50 + 1)]
    with pytest.raises(cond.ConditionError):
        cond.validate({"op": "OR", "children": leaves})


def test_migration_fills_snapshot_countries(seeded_patients, demo_users):
    """0002 데이터 마이그레이션 함수: 기존(요약 없는) 코호트를 청크 조회로 채운다."""
    import importlib

    from django.apps import apps as global_apps
    mig = importlib.import_module("apps.cohorts.migrations.0002_snapshot_countries")
    c = Cohort.objects.create(name="x", owner=demo_users["researcher"], conditions={"op": "AND", "children": []},
                              snapshot_ids=[3, 1, 2, 999999], created_date=date(2025, 1, 1))
    assert c.snapshot_countries is None
    mig.fill(global_apps, None)
    c.refresh_from_db()
    countries = dict(Patient.objects.filter(pk__in=[1, 2, 3]).values_list("id", "country"))
    assert c.snapshot_countries == list(dict.fromkeys([countries[3], countries[1], countries[2]]))


# ── 2. 정수 파라미터 ──
@pytest.mark.parametrize("v", ["²", "٣", "１", "-1", "1.0", " 1e3", "99999999999", ""])
def test_unicode_digits_rejected(v):
    assert cond.parse_uint(v) is None


def test_loaded_and_numbers_with_unicode_digits(researcher_client, seeded_patients):
    for q in ("loaded=²", "loaded=٣", "loaded=99999999999999999999"):
        r = researcher_client.get(f"/depot?{q}")  # 찾을 수 없는 데이터셋 → 오류 문구와 400(500 아님)
        assert r.status_code == 400 and "불러온 데이터셋을 찾을 수 없습니다" in r.content.decode(), q
    for q in ("age_min=²&age_max=٣", "cxr_min=²", "age_min=٣٠"):
        r = researcher_client.get(f"/depot?{q}")
        assert r.status_code == 200, q
    b = researcher_client.get("/depot?age_min=٣٠").content.decode()
    assert 'name="age_min" min="0" max="150" value="0"' in b
    # 경로의 pk 도 ASCII 숫자만 — 유니코드 숫자는 라우트에 걸리지 않는다(500 아님)
    for path in ("/depot/cohorts/²/delete", "/depot/cohorts/٣/rename"):
        assert researcher_client.post(path).status_code in (302, 404, 405)
    assert researcher_client.get("/depot/cohorts/²/export.csv").status_code in (302, 404)
    with pytest.raises(cond.ConditionError):
        cond.validate([{"field": "demographics.age", "operator": "gte", "value": "٣٠"}])


# ── 3. Fisher 성능 ──
def test_fisher_known_values_and_speed():
    assert abs(services.fisher_exact(8, 2, 1, 5)[1] - 0.0349650) < 1e-6
    assert abs(services.fisher_exact(3, 1, 1, 3)[1] - 0.4857143) < 1e-6
    # scipy.stats.fisher_exact([[10, 0], [0, 10]]) → 1.0825088e-05
    assert abs(services.fisher_exact(10, 0, 0, 10)[1] - 1.0825088e-05) < 1e-10
    assert services.fisher_exact(0, 0, 0, 5)[1] == pytest.approx(1.0)
    t0 = time.perf_counter()
    _, p = services.fisher_exact(2600, 2400, 2500, 2500)
    took = time.perf_counter() - t0
    print(f"\n[측정] Fisher n=10,000: {took * 1000:.1f} ms")
    assert took < 0.05
    assert 0 < p < 1


def test_stats_tab_10000_patients_under_one_second(researcher_client):
    make_patients(10000, seed=11)
    researcher_client.get("/depot?run=1&tab=stats")  # 워밍업
    t0 = time.perf_counter()
    r = researcher_client.get("/depot?run=1&tab=stats")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and 'data-testid="stats-table"' in r.content.decode()
    print(f"\n[측정] 통계 탭 응답(환자 10,000명): {elapsed * 1000:.0f} ms")
    assert elapsed < 1.0, elapsed


# ── 4. NIT ──
def test_t_test_untestable_row():
    rows = [{"sex": "M", "age": 40, "diagnosis_type": "DS-TB", "treatment_duration": 180, "cxr_findings": [],
             "lineage": None, "comorbidities": [], "has_sequence": False, "has_ct": False,
             "treatment_outcome": o} for o in ("Cured", "Cured", "Failed", "Failed")]
    rows[0]["sex"] = "F"  # 성별은 변이가 있어 Fisher 행이 생긴다
    rows[2]["age"] = 40  # 나이는 전원 40 → 변이 없음 → 원본 규칙대로 제외
    rows[1]["treatment_duration"] = 180
    rows[3]["treatment_duration"] = 200
    rows[2]["treatment_duration"] = 200  # 치료 기간: 군 내 분산 0, 군 간 차이 있음 → 검정 불가
    res = services.statistics(rows, "ko")
    by = {r["variable"]: r for r in res["rows"]}
    assert by["치료 기간"]["p_text"] == "검정 불가" and by["치료 기간"]["p"] is None
    assert "평균 연령" not in by
    assert res["rows"][-1]["variable"] == "치료 기간"  # 정렬 맨 끝


def test_htmx_error_partial_is_200_and_push_url_clean(researcher_client, seeded_patients):
    bad = '[{"field":"demographics.site","operator":"eq","value":"x"}]'
    r = researcher_client.get("/depot", {"tree": bad, "run": "", "tab": "summary"},
                              HTTP_HX_REQUEST="true", HTTP_HX_TARGET="depot-result")
    assert r.status_code == 200 and 'data-testid="cond-error"' in r.content.decode()
    assert researcher_client.get("/depot", {"tree": bad}).status_code == 400  # 전체 페이지는 400 유지
    r = researcher_client.get("/depot?run=&tab=summary&panel=&logic=AND&country=한국&age_min=0&age_max=100&seq="
                              "&lineage=&mut=&cxr=&ct=&pet=&cxr_min=", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="depot-result")
    assert r["HX-Push-Url"] == "/depot?country=%ED%95%9C%EA%B5%AD"
    r = researcher_client.get("/depot?run=1&tab=stats&panel=saved&seq=1&age_min=0", HTTP_HX_REQUEST="true",
                              HTTP_HX_TARGET="depot-result")
    assert r["HX-Push-Url"] == "/depot?seq=1&run=1&tab=stats&panel=saved"


def test_seed_owner_fallback_and_skip(seeded_patients):
    # 계정이 하나도 없으면 소유자 없는 코호트를 만들지 않고, 적재 기록도 남기지 않는다
    res = services.load_cohorts()
    assert res["created"] == 0 and Cohort.objects.count() == 0 and not is_seeded("cohorts")
    # researcher 가 없으면 관리자 계정이 소유
    admin = User.objects.create_user("boss", password="x", role="admin")
    res = services.load_cohorts()
    assert res["created"] == 3 and set(Cohort.objects.values_list("owner_id", flat=True)) == {admin.pk}
    assert not Cohort.objects.filter(owner__isnull=True).exists()
    assert all(c.snapshot_countries for c in Cohort.objects.all())


def test_no_numeric_regex_with_backslash_d():
    from pathlib import Path
    root = Path(services.__file__).parent
    for f in list(root.glob("*.py")):
        assert not re.search(r"\\d", f.read_text(encoding="utf-8")), f
