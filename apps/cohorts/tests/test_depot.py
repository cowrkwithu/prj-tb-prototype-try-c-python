"""데이터 탐색 화면·저장 코호트: 뷰, 권한, 시드, 분석 집계, CSV, XSS, N+1, 업로드 승인 반영."""
import csv
import io
import json
import re
from collections import Counter
from urllib.parse import quote, urlencode

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.cases.models import Patient
from apps.cohorts import conditions as cond
from apps.cohorts import services
from apps.cohorts.models import Cohort

from .helpers import SEED, condition_ids, expected_ids

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded(seeded_patients, demo_users):
    services.load_cohorts()
    return demo_users


@pytest.fixture
def other(db):
    return User.objects.create_user("other", password="x", role="researcher")


def count_in(body):
    return int(re.search(r'data-testid="match-count"[^>]*>(\d+)<', body).group(1))


def body(resp):
    return resp.content.decode()


def tree_url(tree, **extra):
    return "/depot?" + urlencode([("tree", json.dumps(tree, ensure_ascii=False))] + list(extra.items()), quote_via=quote)


# ── 접근 ──
def test_login_required(client):
    for url in ("/depot", "/depot/cohorts/1/export.csv"):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/login?next=")
    r = client.post("/depot/cohorts/save", {"query": ""})
    assert r.status_code == 302 and r["Location"].startswith("/login")


def test_write_endpoints_require_post(researcher_client, seeded):
    assert researcher_client.get("/depot/cohorts/save").status_code == 405
    assert researcher_client.get("/depot/cohorts/1/delete").status_code == 405
    assert researcher_client.get("/depot/cohorts/1/rename").status_code == 405
    assert researcher_client.post("/depot/cohorts/1/export.csv").status_code == 405


# ── 빌더 ──
def test_initial_page(researcher_client, seeded):
    r = researcher_client.get("/depot")
    b = body(r)
    assert r.status_code == 200
    assert count_in(b) == 50 and "데이터 조건 설정" in b and "데이터셋 분석결과" in b
    assert re.findall(r'data-testid="preview-id">([^<]+)<', b) == [p["conditionId"] for p in SEED[:10]]
    assert b.count('data-testid="saved-card"') == 3 and "총 3개 데이터셋 저장됨" in b


@pytest.mark.parametrize("query,tree", [
    ("country=한국&diag=MDR-TB&diag=Pre-XDR-TB", [
        {"field": "demographics.country", "operator": "in", "value": ["한국"]},
        {"field": "clinical.diagnosisType", "operator": "in", "value": ["MDR-TB", "Pre-XDR-TB"]}]),
    ("logic=OR&comorb=당뇨&finding=공동&mutations=rpoB S450L", {"op": "OR", "children": [
        {"field": "clinical.comorbidities", "operator": "in", "value": ["당뇨"]},
        {"field": "genomics.resistanceMutations", "operator": "in", "value": ["rpoB S450L"]},
        {"field": "imaging.cxrFindings", "operator": "in", "value": ["공동"]}]}),
    ("age_min=30&age_max=55&sex=F&seq=1&mut=1&ct=0", [
        {"field": "demographics.age", "operator": "between", "value": [30, 55]},
        {"field": "demographics.sex", "operator": "in", "value": ["F"]},
        {"field": "genomics.hasSequence", "operator": "eq", "value": True},
        {"field": "genomics.hasMutation", "operator": "eq", "value": True},
        {"field": "imaging.hasCt", "operator": "eq", "value": False}]),
    ("lineage=L2.2.1&cxr=1&pet=0&cxr_min=3&outcome=Cured&outcome=Completed", [
        {"field": "genomics.lineage", "operator": "eq", "value": "L2.2.1"},
        {"field": "imaging.hasCxr", "operator": "eq", "value": True},
        {"field": "imaging.hasPetCt", "operator": "eq", "value": False},
        {"field": "imaging.cxrCount", "operator": "gte", "value": 3},
        {"field": "clinical.treatmentOutcome", "operator": "in", "value": ["Cured", "Completed"]}]),
])
def test_htmx_count_and_preview(researcher_client, seeded, query, tree):
    exp = condition_ids(tree)
    # 폼은 항상 run(빈 값)·tab 을 함께 보낸다 — mutations 만 있는 GAP 연계 주소와 구분
    r = researcher_client.get(f"/depot?{query}&run=&tab=summary", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="depot-result")
    b = body(r)
    assert r.status_code == 200 and "<html" not in b  # 부분 응답
    assert 'id="depot-count" data-testid="match-count" hx-swap-oob="true">%d<' % len(exp) in b
    assert re.findall(r'data-testid="preview-id">([^<]+)<', b) == exp[:10]
    # 같은 주소를 새로 열면(URL 상태) 같은 결과와 체크 상태가 복원된다
    full = body(researcher_client.get(f"/depot?{query}&run="))
    assert count_in(full) == len(exp)


def test_url_state_restores_checked_controls(researcher_client, seeded):
    b = body(researcher_client.get("/depot?country=몽골&logic=OR&seq=0&age_min=20"))
    assert re.search(r'name="country" value="몽골" checked', b)
    assert re.search(r'name="logic" value="OR" checked', b)
    assert re.search(r'name="seq" value="0" checked', b)
    assert 'name="age_min" min="0" max="150" value="20"' in b


def test_run_summary_tab_values_and_case_links(researcher_client, seeded):
    tree = [{"field": "demographics.country", "operator": "in", "value": ["한국", "인도"]}]
    pts = [p for p in SEED if p["demographics"]["country"] in ("한국", "인도")]
    b = body(researcher_client.get("/depot?country=한국&country=인도&run=1"))
    n = len(pts)
    assert f'data-testid="stat-n">{n}명<' in b
    assert f'data-testid="stat-age">{round(sum(p["demographics"]["age"] for p in pts) / n)}세<' in b
    m = sum(1 for p in pts if p["demographics"]["sex"] == "M")
    assert f'data-testid="stat-male">{round(m / n * 100)}%<' in b
    assert f'data-testid="stat-seq">{sum(1 for p in pts if p["genomics"]["hasSequence"])}건<' in b
    links = re.findall(r'href="/cases\?id=(\d+)" data-testid="patient-link">([^<]+)<', b)
    assert [c for _, c in links] == condition_ids(tree)
    assert all(Patient.objects.get(pk=int(i)).condition_id == c for i, c in links)
    data = json.loads(re.search(r'id="depot-chart-data" type="application/json">(.*?)</script>', b, re.S).group(1))
    assert data["sex"]["data"] == [m, n - m]
    assert dict(zip(data["country"]["labels"], data["country"]["data"])) == dict(
        Counter(p["demographics"]["country"] for p in pts))
    assert "데이터셋 저장" in b


def test_analysis_tabs_aggregate_values(researcher_client, seeded):
    pts = [p for p in SEED if p["clinical"]["diagnosisType"] != "DS-TB"]
    q = "diag=MDR-TB&diag=Pre-XDR-TB&diag=XDR-TB&run=1"
    # 임상 비교
    b = body(researcher_client.get(f"/depot?{q}&tab=clinical"))
    assert 'data-testid="tab-clinical"' in b
    data = json.loads(re.search(r'id="depot-chart-data" type="application/json">(.*?)</script>', b, re.S).group(1))
    assert dict(zip(data["diag"]["labels"], data["diag"]["data"])) == dict(Counter(p["clinical"]["diagnosisType"] for p in pts))
    for s in data["outcome"]["series"]:
        for d, v in zip(data["outcome"]["labels"], s["data"]):
            assert v == sum(1 for p in pts if p["clinical"]["diagnosisType"] == d and p["clinical"]["treatmentOutcome"] == s["label"])
    # 유전체
    b = body(researcher_client.get(f"/depot?{q}&tab=genomic"))
    seq = [p for p in pts if p["genomics"]["hasSequence"]]
    assert f"시퀀스 보유 {len(seq)}건" in b
    muts = Counter(m for p in seq for m in p["genomics"]["resistanceMutations"])
    got = re.findall(r'<td class="font-mono">([^<]+)</td><td class="text-right" data-testid="mut-count">(\d+)<', b)
    assert {k: int(v) for k, v in got} == dict(muts.most_common(15))
    # 영상
    b = body(researcher_client.get(f"/depot?{q}&tab=imaging"))
    cxr = [p for p in pts if p["imaging"]["hasCxr"]]
    assert f'data-testid="stat-cxr">{len(cxr)}명<' in b
    assert f'data-testid="stat-ct">{sum(p["imaging"]["hasCt"] for p in pts)}명<' in b
    assert f'data-testid="stat-pet">{sum(p["imaging"]["hasPetCt"] for p in pts)}명<' in b
    assert f'data-testid="stat-cxr-images">{sum(p["imaging"]["cxrCount"] for p in cxr)}장<' in b
    data = json.loads(re.search(r'id="depot-chart-data" type="application/json">(.*?)</script>', b, re.S).group(1))
    assert dict(zip(data["findings"]["labels"], data["findings"]["data"])) == dict(
        Counter(f for p in cxr for f in p["imaging"]["cxrFindings"]))
    # 통계
    b = body(researcher_client.get(f"/depot?{q}&tab=stats"))
    assert 'data-testid="stats-table"' in b
    ps = [float(x) if not x.startswith("<") else 0.0 for x in re.findall(r'data-testid="p-value">([^<]+)<', b)]
    assert ps == sorted(ps) and ps


def test_stats_math():
    # scipy.stats.fisher_exact([[8, 2], [1, 5]]) → p = 0.034965...
    _, p = services.fisher_exact(8, 2, 1, 5)
    assert abs(p - 0.0349650) < 1e-6
    _, p = services.fisher_exact(3, 1, 1, 3)
    assert abs(p - 0.4857143) < 1e-6
    # t=2.0, df=10 양측 p = 0.07338803
    assert abs(services.incomplete_beta(10 / (10 + 4), 5, 0.5) - 0.07338803) < 1e-6
    t, p = services.welch_t([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
    assert abs(t - (-1.8973666)) < 1e-6 and 0.1 < p < 0.12
    assert services.welch_t([1], [2, 3]) is None


def test_stats_untestable_when_single_outcome_group(researcher_client, seeded):
    b = body(researcher_client.get("/depot?outcome=Cured&run=1&tab=stats"))
    assert 'data-testid="stats-untestable"' in b


def test_no_match_message(researcher_client, seeded):
    b = body(researcher_client.get("/depot?country=필리핀&country=미얀마&diag=XDR-TB&outcome=Cured&age_min=99&run=1"))
    assert count_in(b) == 0 and 'data-testid="no-match"' in b


def test_gap_mutation_link_autoruns(researcher_client, seeded):
    url = services.depot_url_for_mutations(["rpoB S450L", "katG S315T"])
    assert url == "/depot?mutations=rpoB%20S450L%2CkatG%20S315T"
    b = body(researcher_client.get(url))
    exp = condition_ids([{"field": "genomics.resistanceMutations", "operator": "in", "value": ["rpoB S450L", "katG S315T"]}])
    assert count_in(b) == len(exp) and 'data-testid="tab-summary"' in b


def test_variant_interface(seeded):
    assert services.mutation_label("inhA", "c-15t (promoter)") == "inhA c-15t"
    assert services.mutation_label("rrs", "") == "rrs"
    assert services.mutation_labels([{"gene": "rpoB", "aminoAcidChange": "S450L"},
                                     {"gene": "rpoB", "aminoAcidChange": "S450L (RRDR)"}]) == ["rpoB S450L"]
    assert services.mutation_cohort_name(["a", "b", "c"], "en") == "Dataset with a and 2 more mutations"
    c = services.save_mutation_cohort(seeded["researcher"], ["rpoB S450L"])
    assert c.name == "rpoB S450L 보유 데이터셋"
    assert sorted(c.snapshot_ids) == expected_ids(
        [{"field": "genomics.resistanceMutations", "operator": "in", "value": ["rpoB S450L"]}])


# ── 고급 조건 트리(URL) ──
NESTED = {"op": "OR", "children": [
    {"op": "AND", "children": [{"field": "demographics.country", "operator": "eq", "value": "한국"},
                               {"field": "clinical.comorbidities", "operator": "in", "value": ["당뇨"]}]},
    {"op": "AND", "children": [{"field": "imaging.cxrFindings", "operator": "in", "value": ["공동"]},
                               {"field": "demographics.age", "operator": "gte", "value": 50}]}]}


def test_tree_mode(researcher_client, seeded):
    b = body(researcher_client.get(tree_url(NESTED)))
    assert count_in(b) == len(expected_ids(NESTED)) and 'data-testid="tree-mode"' in b
    assert 'name="country"' not in b  # 폼 컨트롤 대신 트리 요약


@pytest.mark.parametrize("tree", [
    [{"field": "demographics.site", "operator": "eq", "value": "x"}],
    [{"field": "condition_id", "operator": "in", "value": ["KR-2024-0001"]}],
    [{"field": "demographics.age", "operator": "regex", "value": "1"}],
    [{"field": "demographics.age", "operator": "gte", "value": {"__gt": 1}}],
])
def test_tree_rejects_disallowed_field_or_operator(researcher_client, seeded, tree):
    r = researcher_client.get(tree_url(tree))
    assert r.status_code == 400 and 'data-testid="cond-error"' in body(r)
    assert count_in(body(r)) == 50  # 거부된 조건은 적용하지 않는다


def test_tree_rejects_deep_and_huge(researcher_client, seeded):
    node = {"field": "demographics.sex", "operator": "eq", "value": "M"}
    for _ in range(cond.MAX_DEPTH + 1):
        node = {"op": "AND", "children": [node]}
    assert researcher_client.get(tree_url(node)).status_code == 400
    huge = {"op": "OR", "children": [{"field": "demographics.age", "operator": "eq", "value": i % 100}
                                     for i in range(cond.MAX_NODES + 5)]}
    r = researcher_client.get(tree_url(huge))
    assert r.status_code == 400
    r = researcher_client.get("/depot?tree=" + quote("[" * 5000))
    assert r.status_code == 400
    r = researcher_client.get("/depot?tree=" + quote("x" * (cond.MAX_JSON_CHARS + 10)))
    assert r.status_code == 400


# ── 시드 ──
def test_seed_three_cohorts_idempotent(seeded):
    assert Cohort.objects.count() == 3
    seed = json.loads((__import__("django.conf").conf.settings.SEED_DIR / "cohorts.json").read_text(encoding="utf-8"))
    for s in seed:
        c = Cohort.objects.get(pk=s["id"])
        assert (c.name, c.name_en, c.snapshot_ids, str(c.created_date)) == (s["name"], s["nameEn"], s["patientIds"], s["createdDate"])
        assert c.owner.username == "researcher"
        assert c.conditions == cond.validate(s["conditions"])
    Cohort.objects.filter(pk=2).delete()
    assert services.load_cohorts()["skipped"] is True and not Cohort.objects.filter(pk=2).exists()
    assert services.load_cohorts(force=True)["created"] == 1


def test_seed_cohorts_load_restores_conditions(researcher_client, seeded):
    seed = {c["id"]: c for c in json.loads((__import__("django.conf").conf.settings.SEED_DIR / "cohorts.json")
                                            .read_text(encoding="utf-8"))}
    page = body(researcher_client.get("/depot?panel=saved"))
    urls = dict((int(i), u.replace("&amp;", "&")) for u, i in re.findall(r'href="([^"]+loaded=(\d+)[^"]*)" class="icon-btn" data-testid="load-link"', page))
    assert set(urls) == {1, 2, 3}
    for cid, url in urls.items():
        b = body(researcher_client.get(url))
        exp = expected_ids(seed[cid]["conditions"])
        assert count_in(b) == len(exp), cid
        assert f"저장 시점 {len(seed[cid]['patientIds'])}명, 현재 조건 기준 {len(exp)}명" in b
        assert 'data-testid="tree-mode"' not in b  # 빌더 폼으로 복원
    # 시드 3: 원본 스냅샷 15명, 조건 재계산 16명(원본 Mock 의 불일치를 그대로 보여준다)
    b = body(researcher_client.get(urls[3]))
    assert 'name="cxr_min" min="0" value="3"' in b and re.search(r'name="cxr" value="1" checked', b)


# ── 저장·이름 변경·삭제·내보내기 ──
def test_save_current_conditions(researcher_client, seeded):
    q = "country=한국&diag=MDR-TB&seq=1&run=1"
    r = researcher_client.post("/depot/cohorts/save", {"query": "country=한국&diag=MDR-TB&seq=1", "tab": "genomic"})
    c = Cohort.objects.order_by("-id").first()
    assert r.status_code == 302 and f"loaded={c.pk}" in r["Location"] and "tab=genomic" in r["Location"]
    tree = [{"field": "demographics.country", "operator": "in", "value": ["한국"]},
            {"field": "clinical.diagnosisType", "operator": "in", "value": ["MDR-TB"]},
            {"field": "genomics.hasSequence", "operator": "eq", "value": True}]
    assert c.owner.username == "researcher" and sorted(c.snapshot_ids) == expected_ids(tree)
    assert c.name == "데이터셋 — 국적: 한국 · 내성: MDR-TB" and c.name_en == "Dataset — Nationality: Korea · Resistance: MDR-TB"
    assert c.description.startswith("데이터 탐색 결과로 저장된 데이터셋. 조건(AND): ")
    b = body(researcher_client.get(r["Location"]))
    assert "저장 완료" in b and f'data-id="{c.pk}"' in b
    assert body(researcher_client.get(f"/depot?{q}")).count('data-testid="saved-card"') == 4


def test_save_tree_mode_and_rejects_bad_query(researcher_client, seeded):
    q = urlencode([("tree", json.dumps(NESTED, ensure_ascii=False))])
    researcher_client.post("/depot/cohorts/save", {"query": q})
    c = Cohort.objects.order_by("-id").first()
    assert c.conditions == cond.validate(NESTED) and sorted(c.snapshot_ids) == expected_ids(NESTED)
    assert services.builder_state(c)["filters"] is None  # 불러오면 고급 조건(tree=)으로 복원
    n = Cohort.objects.count()
    bad = urlencode([("tree", json.dumps([{"field": "demographics.site", "operator": "eq", "value": "x"}]))])
    r = researcher_client.post("/depot/cohorts/save", {"query": bad})
    assert r.status_code == 400 and Cohort.objects.count() == n


def test_owner_only_rename_delete_export(client, seeded, other):
    c = Cohort.objects.get(pk=1)  # 소유자 researcher
    client.force_login(other)
    page = body(client.get("/depot?panel=saved"))
    assert page.count('data-testid="saved-card"') == 3 and 'data-testid="export-link"' not in page  # 목록은 공개
    assert client.post(f"/depot/cohorts/{c.pk}/rename", {"name": "hacked"}).status_code == 403
    assert client.post(f"/depot/cohorts/{c.pk}/delete").status_code == 403
    assert client.get(f"/depot/cohorts/{c.pk}/export.csv").status_code == 403
    c.refresh_from_db()
    assert c.name == "한국 MDR-TB 데이터셋"
    # 관리자도 남의 데이터셋은 변경 불가
    client.force_login(seeded["admin"])
    assert client.post(f"/depot/cohorts/{c.pk}/delete").status_code == 403
    assert Cohort.objects.filter(pk=c.pk).exists()
    assert client.post("/depot/cohorts/999/delete").status_code == 404
    # 소유자
    client.force_login(seeded["researcher"])
    r = client.post(f"/depot/cohorts/{c.pk}/rename", {"name": "  새 이름\n", "name_en": "New", "next": "https://evil.example/"})
    assert r.status_code == 302 and r["Location"] == "/depot?panel=saved"
    c.refresh_from_db()
    assert (c.name, c.name_en) == ("새 이름", "New")
    client.post(f"/depot/cohorts/{c.pk}/rename", {"name": "   "})
    c.refresh_from_db()
    assert c.name == "새 이름"
    assert client.get(f"/depot/cohorts/{c.pk}/export.csv").status_code == 200
    assert client.post(f"/depot/cohorts/{c.pk}/delete", {"next": "/depot?panel=saved"}).status_code == 302
    assert not Cohort.objects.filter(pk=c.pk).exists()


def test_export_csv_content_and_formula_defense(researcher_client, seeded):
    p = Patient.objects.get(pk=2)
    p.site = '=HYPERLINK("http://evil","x")'
    p.comorbidities = ["@SUM(1)", "당뇨"]
    p.lineage = None
    p.save()
    c = Cohort.objects.get(pk=1)
    r = researcher_client.get(f"/depot/cohorts/{c.pk}/export.csv")
    assert r["Content-Type"].startswith("text/csv") and 'filename="dataset_1.csv"' in r["Content-Disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0][:3] == ["condition_id", "age", "sex"]
    exp = condition_ids(c.conditions["children"])
    assert [row[0] for row in rows[1:]] == exp
    row = next(row for row in rows[1:] if row[0] == p.condition_id)
    assert row[4] == '\'=HYPERLINK("http://evil","x")' and row[8] == "'@SUM(1);당뇨"
    for row in rows[1:]:
        for cell in row:
            assert not cell or cell[0] not in "=+@\t\r" or cell.startswith("'")


def test_cohort_name_xss_escaped(researcher_client, seeded):
    c = Cohort.objects.get(pk=1)
    c.name = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    c.description = "<b>bold</b>"
    c.save()
    for url in ("/depot?panel=saved", services.load_url(c)):
        b = body(researcher_client.get(url))
        assert "<script>alert" not in b and "<img src=x" not in b and "<b>bold</b>" not in b
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in b
    r = researcher_client.post("/depot/cohorts/save", {"query": "country=한국", "name": "<svg onload=alert(1)>"})
    b = body(researcher_client.get(r["Location"]))
    assert "<svg onload" not in b and "&lt;svg onload=alert(1)&gt;" in b


# ── N+1 ──
def test_query_counts_do_not_grow_with_cohorts(researcher_client, seeded):
    def n_queries(url):
        with CaptureQueriesContext(connection) as ctx:
            assert researcher_client.get(url).status_code == 200
        return len(ctx)

    url = "/depot?comorb=당뇨&finding=공동&run=1&panel=saved"
    base = n_queries(url)
    for i in range(30):
        services.save_cohort(seeded["researcher"], {"op": "AND", "children": [
            {"field": "demographics.age", "operator": "gte", "value": i}]})
    assert n_queries(url) == base
    for tab in services.TAB_KEYS:
        assert n_queries(f"{url}&tab={tab}") <= base
    # CSV 내보내기도 대상자 수와 무관
    with CaptureQueriesContext(connection) as ctx:
        researcher_client.get("/depot/cohorts/3/export.csv")
    assert len(ctx) <= 6


# ── 업로드 승인 → 조건 기반 재계산 ──
def test_upload_approval_reflected_in_cohort(client, seeded, settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.uploads.models import Submission

    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()
    researcher, admin = seeded["researcher"], seeded["admin"]
    client.force_login(researcher)
    client.post("/depot/cohorts/save", {"query": "country=한국&diag=MDR-TB"})
    c = Cohort.objects.order_by("-id").first()
    before = len(c.snapshot_ids)
    load = services.load_url(c)
    assert count_in(body(client.get(load))) == before

    data = (settings.BASE_DIR / "static" / "samples" / "clinical_deidentified.csv").read_bytes()
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": SimpleUploadedFile("c.csv", data, "text/csv")})
    uid = re.search(r"id=(UPL-\d{8}-\d{3,})", r["Location"]).group(1)
    client.post("/upload/clinical/proceed", {"id": uid, "from": "data"})
    client.post("/upload/clinical/submit", {"id": uid, "deid": [str(i) for i in range(7)]})
    client.force_login(admin)
    client.post(f"/admin/review/{uid}/approve", {"check": [str(i) for i in range(5)]})
    assert Submission.objects.get(upload_id=uid).status == "approved"

    new = Patient.objects.filter(country="한국", diagnosis_type="MDR-TB").exclude(pk__in=c.snapshot_ids)
    assert new.exists()
    client.force_login(researcher)
    b = body(client.get(load))
    assert count_in(b) == before + new.count()
    assert f"저장 시점 {before}명, 현재 조건 기준 {before + new.count()}명" in b
    c.refresh_from_db()
    assert len(c.snapshot_ids) == before  # 스냅샷은 그대로
    text = client.get(f"/depot/cohorts/{c.pk}/export.csv").content.decode()
    assert all(p.condition_id in text for p in new)


def test_english_labels(researcher_client, seeded):
    researcher_client.post("/lang", {"lang": "en"})
    b = body(researcher_client.get("/depot?country=한국&run=1&panel=saved"))
    assert "Data Conditions" in b and "Generate Data (" in b and "Korea MDR-TB Dataset" in b and "Nationality: Korea" in b
