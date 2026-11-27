"""GAP 화면 — 로그인·권한·CSRF·DAR 게이트·데이터셋 저장·예측 실행/이력·GWAS·유사도·영문."""
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.cohorts.models import Cohort
from apps.genomics.models import PredictionRun, Variant

from .conftest import approve_genomics

pytestmark = pytest.mark.django_db
PAGES = ["/gap", "/gap/search", "/gap/resistance", "/gap/gwas", "/gap/similarity", "/gap/variants/1",
         "/gap/search/export.csv"]


def test_login_required(client, catalog):
    for url in PAGES:
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/login?next="), url
    assert client.post("/gap/search/save", {}).status_code == 302
    assert client.post("/gap/resistance", {}).status_code == 302


def test_pages_render(researcher_client, catalog):
    for url in PAGES[:5]:
        r = researcher_client.get(url)
        assert r.status_code == 200, url
        assert 'class="sidebar"' in r.content.decode()


def test_browser_counts_from_db(researcher_client, catalog):
    body = researcher_client.get("/gap").content.decode()
    assert body.count('data-testid="variant-row"') == 20
    assert "총 20개 변이 · 10개 유전자" in body and "유전체 데이터 보유 대상자 38명" in body
    assert "614" not in body  # 원본 시드 sampleCount 를 쓰지 않는다
    zoom = researcher_client.get("/gap?gene=rpoB").content.decode()
    assert 'data-testid="gene-zoom"' in zoom and zoom.count('data-testid="variant-row"') == 4
    part = researcher_client.get("/gap?q=S531L", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="gap-variant-table")
    assert part.content.decode().count('data-testid="variant-row"') == 1 and "<html" not in part.content.decode()


def test_variant_detail_lists_carriers(researcher_client, catalog):
    v = Variant.objects.get(label="rpoB S450L")
    body = researcher_client.get(f"/gap/variants/{v.pk}").content.decode()
    assert body.count("/cases?id=") == 17 and 'data-testid="build-cohort"' in body
    assert "/depot?mutations=rpoB%20S450L" in body
    none = Variant.objects.get(label="embB M306V")
    assert 'data-testid="build-cohort"' not in researcher_client.get(f"/gap/variants/{none.pk}").content.decode()
    assert researcher_client.get("/gap/variants/99999").status_code == 404


def test_search_and_pagination(researcher_client, catalog, monkeypatch):
    from apps.genomics import views

    body = researcher_client.get("/gap/search?searched=1").content.decode()
    assert "검색 결과 (20건)" in body and body.count('data-testid="search-row"') == 20
    body = researcher_client.get("/gap/search?searched=1&drug=Rifampicin&min_freq=6").content.decode()
    assert body.count('data-testid="search-row"') == 2  # S450L 44.7%, H445Y 10.5% (D435V 5.3% 제외)
    monkeypatch.setattr(views, "SEARCH_PER_PAGE", 6)
    body = researcher_client.get("/gap/search?searched=1&page=4").content.decode()
    assert body.count('data-testid="search-row"') == 2 and "4 / 4" in body
    assert researcher_client.get("/gap/search?searched=1&page=zz").status_code == 200
    r = researcher_client.get("/gap/search?searched=1&gene=nope")
    assert r.status_code == 400 and "알 수 없는 유전자" in r.content.decode()


def test_csv_gate_and_content(researcher_client, demo_users, catalog):
    url = "/gap/search/export.csv?searched=1&gene=rpoB"
    r = researcher_client.get(url)
    assert r.status_code == 403 and 'data-testid="download-denied"' in r.content.decode()
    assert 'data-testid="csv-gated"' in researcher_client.get("/gap/search?searched=1").content.decode()
    approve_genomics(demo_users["researcher"])
    r = researcher_client.get(url)
    text = r.content.decode("utf-8")
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv") and text.startswith("﻿유전자,")
    assert len(text.strip().splitlines()) == 1 + 4 and "rpoB,761155,C,T,S450L,S531L,Rifampicin,44.7,17," in text
    Variant.objects.filter(label="rpoB S450L").update(annotation="=HYPERLINK(\"x\")")
    assert "'=HYPERLINK" in researcher_client.get(url).content.decode()
    assert researcher_client.get("/gap/search/export.csv?searched=1&gene=zz").status_code == 400


def test_save_dataset_creates_cohort(researcher_client, demo_users, catalog):
    from apps.cases.models import Patient

    p = Patient.objects.get(pk=1)
    p.resistance_mutations = ["rpoB S531L"]  # E. coli 표기 — 별칭으로 매칭, 데이터 탐색 조건에 원문도 넣는다
    p.save()
    r = researcher_client.post("/gap/search/save", {"gene": "rpoB", "aa": "S450L"})
    c = Cohort.objects.order_by("-id").first()
    assert r.status_code == 302 and r["Location"] == f"/depot?loaded={c.pk}&run=1&panel=saved"
    assert c.owner == demo_users["researcher"] and c.snapshot_count == 18 and 1 in c.snapshot_ids
    assert c.name == "rpoB S450L 외 1건 변이 보유 데이터셋" and "유전자 rpoB" in c.description
    n = Cohort.objects.count()
    r = researcher_client.post("/gap/search/save", {"gene": "embB", "aa": "M306V"})
    assert r.status_code == 302 and Cohort.objects.count() == n
    r = researcher_client.post("/gap/search/save", {"gene": "bad"})
    assert r.status_code == 302 and Cohort.objects.count() == n


def test_csrf_enforced(catalog, demo_users):
    from django.test import Client

    c = Client(enforce_csrf_checks=True)
    c.force_login(demo_users["researcher"])
    assert c.post("/gap/search/save", {"gene": "rpoB"}).status_code == 403
    assert c.post("/gap/resistance", {"source": "patient", "patient": "4", "algorithms": "tb-profiler"}).status_code == 403
    assert not PredictionRun.objects.exists()


def test_resistance_patient_run_and_history(researcher_client, demo_users, other_user, catalog, client):
    r = researcher_client.post("/gap/resistance", {"source": "patient", "patient": "4",
                                                   "algorithms": ["tb-profiler", "mykrobe", "resfinder"]})
    run = PredictionRun.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/gap/resistance?run={run.pk}#result"
    body = researcher_client.get(f"/gap/resistance?run={run.pk}").content.decode()
    assert 'data-drug="Rifampicin" data-consensus="R"' in body and "카탈로그 기반 규칙" in body
    # 다른 사용자는 볼 수 없다(404), 관리자는 볼 수 있다
    client.force_login(other_user)
    assert client.get(f"/gap/resistance?run={run.pk}").status_code == 404
    assert f"?run={run.pk}" not in client.get("/gap/resistance").content.decode()
    client.force_login(demo_users["admin"])
    assert client.get(f"/gap/resistance?run={run.pk}").status_code == 200


def test_resistance_upload_paths(researcher_client, catalog):
    f = SimpleUploadedFile("sample.vcf", b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\nX\t2155168\t.\tC\tG\n")
    r = researcher_client.post("/gap/resistance", {"source": "upload", "algorithms": "tb-profiler", "assume_wgs": "1",
                                                   "file": f})
    assert r.status_code == 302
    run = PredictionRun.objects.get()
    assert run.input_format == "vcf" and run.sample_name == "sample.vcf"
    assert {d["drug"]: d["consensus"] for d in run.results["drugs"]}["Isoniazid"] == "R"
    r = researcher_client.post("/gap/resistance", {"source": "upload", "algorithms": "mykrobe", "text": "rpoB S450L"})
    assert r.status_code == 302 and PredictionRun.objects.count() == 2


@pytest.mark.parametrize("data,msg", [
    ({"source": "upload", "algorithms": "tb-profiler"}, "변이 파일을 올리거나"),
    ({"source": "patient", "algorithms": "tb-profiler"}, "대상자를 고르세요"),
    ({"source": "patient", "patient": "5", "algorithms": "tb-profiler"}, "유전체 데이터가 있는 대상자가 아닙니다"),
    ({"source": "patient", "patient": "4"}, "알고리즘을 하나 이상"),
    ({"source": "upload", "algorithms": "tb-profiler", "text": "\n\n"}, "변이 파일을 올리거나"),
    ({"source": "upload", "algorithms": "tb-profiler", "text": "x\n" * 6000}, "행이 너무 많습니다"),
])
def test_resistance_errors(researcher_client, catalog, data, msg):
    r = researcher_client.post("/gap/resistance", data)
    assert r.status_code == 400 and msg in r.content.decode()
    assert not PredictionRun.objects.exists()


def test_resistance_bad_files(researcher_client, catalog):
    cases = [(SimpleUploadedFile("a.exe", b"rpoB S450L"), "지원 형식"),
             (SimpleUploadedFile("a.csv", "가".encode("cp949")), "UTF-8"),
             (SimpleUploadedFile("a.csv", b"\x00\x01"), "텍스트 파일이 아닙니다"),
             (SimpleUploadedFile("a.csv", b"a" * (1024 * 1024 + 5)), "너무 큽니다")]
    for f, msg in cases:
        r = researcher_client.post("/gap/resistance", {"source": "upload", "algorithms": "tb-profiler", "file": f})
        assert r.status_code == 400 and msg in r.content.decode(), msg


def test_gwas_view(researcher_client, catalog):
    body = researcher_client.get("/gap/gwas?run=1&drug=RIF").content.decode()
    assert 'data-testid="gwas-result"' in body and 'id="gwas-data"' in body
    assert body.count('data-testid="gwas-row"') == 8
    assert 'data-testid="gwas-insufficient"' in researcher_client.get("/gap/gwas?run=1&drug=EMB").content.decode()
    c = Cohort.objects.create(name="x", owner=None, conditions={"op": "AND", "children": []}, snapshot_ids=[1, 2, 3],
                              created_date="2024-01-01")
    assert 'data-testid="gwas-insufficient"' in researcher_client.get(f"/gap/gwas?run=1&drug=RIF&cohort={c.pk}").content.decode()


def test_similarity_view(researcher_client, catalog):
    body = researcher_client.get("/gap/similarity?run=1&method=lineage&p=2&p=4&p=6").content.decode()
    assert 'data-testid="sim-result"' in body and "계통 가중 거리 행렬" in body
    assert len(re.findall(r'<td class="sim-sticky">', body)) == 3
    r = researcher_client.get("/gap/similarity?run=1&p=2")
    assert r.status_code == 400 and "2명 이상" in r.content.decode()
    many = "&".join(f"p={i}" for i in range(1, 51))
    assert researcher_client.get(f"/gap/similarity?run=1&{many}").status_code == 200
    c = Cohort.objects.create(name="x", owner=None, conditions={"op": "AND", "children": []},
                              snapshot_ids=[2, 4, 5, 6, "x", None], created_date="2024-01-01")
    body = researcher_client.get(f"/gap/similarity?cohort={c.pk}&run=1").content.decode()
    assert "유전체 데이터가 없는 대상자" in body and len(re.findall(r'<td class="sim-sticky">', body)) == 3
    assert researcher_client.get("/gap/similarity?cohort=999999&run=1").status_code == 400


def test_english(researcher_client, catalog):
    researcher_client.post("/lang", {"lang": "en"})
    assert "20 variants · 10 genes" in researcher_client.get("/gap").content.decode()
    assert "Run GWAS" in researcher_client.get("/gap/gwas").content.decode()
    assert "catalogue-based rules" in researcher_client.get("/gap/resistance").content.decode()
    assert "Lineage-weighted Distance" in researcher_client.get("/gap/similarity").content.decode()


def test_post_only_and_get_only(researcher_client, catalog):
    assert researcher_client.get("/gap/search/save").status_code == 405
    assert researcher_client.post("/gap/gwas").status_code == 405
    assert researcher_client.post("/gap").status_code == 405


def test_genomics_dataset_export_uses_catalog_coordinates(researcher_client, demo_users, catalog):
    """/data 유전체 데이터셋 내보내기 — 카탈로그와 매칭되는 표기는 좌표·약제를 카탈로그 값으로 채운다."""
    approve_genomics(demo_users["researcher"])
    r = researcher_client.get("/data/datasets/genomics/download.csv")
    text = b"".join(x if isinstance(x, bytes) else x.encode() for x in r.streaming_content).decode("utf-8")
    assert "katG,2155168,C,G,S315T,Isoniazid,52.6,20" in text and "inhA,1673425,C,T,c-15t (promoter),Isoniazid" in text


def test_multi_drug_variants_consistent_across_screens(researcher_client, demo_users, catalog):
    """1차 리뷰: 카탈로그 변이의 추가 연관 약제가 브라우저·검색·CSV·GWAS 에 같이 반영된다."""
    body = researcher_client.get("/gap?q=kanamycin").content.decode()
    assert body.count('data-testid="variant-row"') == 2  # rrs a1401g(추가 연관), eis c-14t
    body = researcher_client.get("/gap/search?searched=1&drug=Ethionamide").content.decode()
    assert body.count('data-testid="search-row"') == 2 and "c-15t" in body
    assert '<option value="Capreomycin"' in researcher_client.get("/gap/search").content.decode()
    approve_genomics(demo_users["researcher"])
    text = researcher_client.get("/gap/search/export.csv?searched=1&drug=Kanamycin").content.decode()
    assert "rrs,1473246,A,G,a1401g,a1408g,Amikacin;Kanamycin;Capreomycin," in text
    inh = researcher_client.get("/gap/gwas?run=1&drug=INH").content.decode()
    assert 'data-linked="1"' in inh  # katG S315T·inhA c-15t 가 Isoniazid 연관으로 표시
    from apps.genomics import analysis

    pts = {p["label"]: p for p in analysis.gwas("INH")["points"]}
    assert pts["inhA c-15t"]["linked"] and not pts["rpoB S450L"]["linked"]
