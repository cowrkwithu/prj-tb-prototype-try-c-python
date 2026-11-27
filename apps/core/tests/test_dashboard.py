"""홈 대시보드 집계 — 기대값은 원본 patients.ts 를 변환한 data/seed/patients.json 에서 직접 계산."""
import json
from collections import Counter
from datetime import date

import pytest
from django.conf import settings
from django.utils import timezone

from apps.cases.models import Patient
from apps.core import dashboard

pytestmark = pytest.mark.django_db
SEED = json.loads((settings.SEED_DIR / "patients.json").read_text(encoding="utf-8"))


def test_stat_cards_match_seed(seeded_patients):
    s = dashboard.stat_cards()
    assert s["total_cases"] == len(SEED) == 50
    assert s["total_sequences"] == sum(p["genomics"]["hasSequence"] for p in SEED)
    assert s["total_images"] == sum(p["imaging"]["cxrCount"] + p["imaging"]["ctCount"] + p["imaging"]["petCtCount"] for p in SEED)
    assert s["partner_sites"] == len({p["demographics"]["site"] for p in SEED})
    assert s["countries"] == len({p["demographics"]["country"] for p in SEED}) == 8


def test_distributions_match_seed(seeded_patients):
    countries = Counter(p["demographics"]["country"] for p in SEED)
    got = {r["country"]: r["cases"] for r in dashboard.country_distribution()}
    assert got == dict(countries)
    rows = dashboard.country_distribution()
    assert [r["cases"] for r in rows] == sorted((r["cases"] for r in rows), reverse=True)
    assert rows[0]["pct_of_max"] == 100
    seq = Counter(p["demographics"]["country"] for p in SEED if p["genomics"]["hasSequence"])
    assert {r["country"]: r["sequences"] for r in rows} == {k: seq.get(k, 0) for k in countries}

    res = {r["key"]: r["count"] for r in dashboard.resistance_distribution()}
    assert res == dict(Counter(p["clinical"]["diagnosisType"] for p in SEED))
    out = dashboard.outcome_distribution()
    assert {r["key"]: r["count"] for r in out} == dict(Counter(p["clinical"]["treatmentOutcome"] for p in SEED))
    assert [r["key"] for r in out] == ["Cured", "Completed", "Failed", "Died", "LTFU"]
    assert round(sum(r["percentage"] for r in out)) == 100


def test_monthly_trends_window(seeded_patients):
    latest = max(date.fromisoformat(p["registrationDate"]) for p in SEED)
    rows = dashboard.monthly_trends()
    assert len(rows) == 12 and rows[-1]["month"] == latest.strftime("%Y-%m")
    months = {r["month"] for r in rows}
    in_window = [p for p in SEED if p["registrationDate"][:7] in months]
    assert sum(r["cases"] for r in rows) == len(in_window)
    assert sum(r["sequences"] for r in rows) == sum(p["genomics"]["hasSequence"] for p in in_window)
    by_month = Counter(p["registrationDate"][:7] for p in in_window)
    assert {r["month"]: r["cases"] for r in rows} == {m: by_month.get(m, 0) for m in months}


def test_change_vs_previous_month(seeded_patients):
    anchor = dashboard.anchor_month().strftime("%Y-%m")
    prev = sum(p["registrationDate"][:7] < anchor for p in SEED)
    cur = sum(p["registrationDate"][:7] <= anchor for p in SEED)
    assert dashboard.stat_cards()["change_cases"] == round((cur - prev) / prev * 100, 1)


def test_contributions_last_30_days(seeded_patients):
    stats = {c["key"]: c for c in dashboard.contribution_stats()}
    assert stats["cases"]["count"] == 0  # 시드는 2024~2025 등록
    Patient.objects.create(condition_id="NEW-1", age=30, sex="F", country="한국", site="x", diagnosis_type="DS-TB",
                           treatment_outcome="Cured", treatment_duration=180,
                           registration_date=timezone.localdate(), last_update_date=timezone.localdate())
    stats = {c["key"]: c for c in dashboard.contribution_stats()}
    assert stats["cases"]["count"] == 1 and sum(stats["cases"]["spark"]) == 1
    assert stats["lab"]["count"] == stats["genomic"]["count"] == stats["imaging"]["count"] == 0
    assert dashboard.recent_upload_activity() == []


def test_empty_db_aggregates():
    s = dashboard.stat_cards()
    assert s["total_cases"] == 0 and s["change_cases"] is None
    assert len(dashboard.monthly_trends()) == 12
    assert dashboard.country_distribution() == []
    assert all(r["count"] == 0 for r in dashboard.resistance_distribution())


def test_home_renders_on_empty_db(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b'id="home-chart-data"' in r.content and "등록된 데이터가 없습니다" in r.content.decode()


def test_home_view_public_with_db_values(client, seeded_patients):
    r = client.get("/")
    assert r.status_code == 200
    body = r.content.decode()
    assert 'data-countup="50"' in body
    assert 'data-testid="activity-empty"' in body
    import re

    data = json.loads(re.search(r'<script id="home-chart-data" type="application/json">(.*?)</script>', body, re.S).group(1))
    assert sum(data["resistance"]["data"]) == 50 and len(data["trend"]["labels"]) == 12
    assert 'href="/login"' in body  # 비로그인: 데이터 탐색 시작 → 로그인


def test_home_english(client, seeded_patients):
    client.post("/lang", {"lang": "en"})
    body = client.get("/").content.decode()
    assert "Total Patient Cases" in body and "Korea" in body and "Analysis Tools" in body


def test_home_chart_data_is_xss_safe(client, seeded_patients):
    Patient.objects.filter(pk=1).update(diagnosis_type="</script><script>alert(1)</script>",
                                        country="<img src=x onerror=alert(1)>")
    body = client.get("/").content.decode()
    assert "</script><script>alert(1)" not in body
    assert "<img src=x onerror" not in body
