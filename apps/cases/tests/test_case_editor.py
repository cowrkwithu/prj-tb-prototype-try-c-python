import json
import re

import pytest
from django.conf import settings

from apps.cases import imaging, services
from apps.cases.models import BiochemResult, Patient

pytestmark = pytest.mark.django_db
SEED = json.loads((settings.SEED_DIR / "patients.json").read_text(encoding="utf-8"))


def ids_in(body):
    return re.findall(r'data-testid="case-([A-Z]{2}-\d{4}-\d{4})"', body)


def all_ids(client, query):
    """페이지를 넘기며 목록의 모든 케이스 ID 를 모은다."""
    out, page = [], 1
    while True:
        body = client.get(f"/cases?{query}&page={page}").content.decode()
        got = ids_in(body)
        out += got
        total = int(re.search(r'data-testid="case-total">(?:총 )?(\d+)', body).group(1))
        if len(out) >= total or not got:
            return out, total
        page += 1


def test_requires_login(client):
    r = client.get("/cases")
    assert r.status_code == 302 and r["Location"].startswith("/login?next=")


def test_default_list_and_selection(researcher_client, seeded_patients):
    r = researcher_client.get("/cases")
    body = r.content.decode()
    assert r.status_code == 200
    assert ids_in(body) == [p["conditionId"] for p in SEED[:10]]
    assert 'data-testid="detail-id">KR-2024-0001' in body
    assert "총 50건" in body and "1/5" in body


@pytest.mark.parametrize("query,pred", [
    ("country=한국", lambda p: p["demographics"]["country"] == "한국"),
    ("country=몽골&country=인도", lambda p: p["demographics"]["country"] in ("몽골", "인도")),
    ("type=MDR-TB", lambda p: p["clinical"]["diagnosisType"] == "MDR-TB"),
    ("outcome=Failed&outcome=Died", lambda p: p["clinical"]["treatmentOutcome"] in ("Failed", "Died")),
    ("imaging=1", lambda p: p["imaging"]["hasCxr"] or p["imaging"]["hasCt"] or p["imaging"]["hasPetCt"]),
    ("country=한국&type=DS-TB", lambda p: p["demographics"]["country"] == "한국" and p["clinical"]["diagnosisType"] == "DS-TB"),
    ("q=mn", lambda p: "mn" in p["conditionId"].lower()),
    ("q=xdr", lambda p: "xdr" in p["clinical"]["diagnosisType"].lower()),
    ("q=베트남", lambda p: p["demographics"]["country"] == "베트남"),
])
def test_filters_match_original_rules(researcher_client, seeded_patients, query, pred):
    got, total = all_ids(researcher_client, query)
    expected = [p["conditionId"] for p in SEED if pred(p)]
    assert got == expected and total == len(expected)


def test_english_country_search(researcher_client, seeded_patients):
    researcher_client.post("/lang", {"lang": "en"})
    got, _ = all_ids(researcher_client, "q=south")
    assert got == [p["conditionId"] for p in SEED if p["demographics"]["country"] == "남아공"]


def test_unknown_filter_values_ignored(researcher_client, seeded_patients):
    _, total = all_ids(researcher_client, "country=화성&type=bogus")
    assert total == 50


def test_htmx_partials(researcher_client, seeded_patients):
    r = researcher_client.get("/cases?type=XDR-TB", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="case-list")
    body = r.content.decode()
    assert body.lstrip().startswith('<div id="case-list"') and 'id="case-detail"' not in body
    r = researcher_client.get("/cases?id=4&tab=2", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="case-detail")
    body = r.content.decode()
    assert 'id="case-detail"' in body and 'hx-swap-oob="true"' in body
    assert "caseDetail(2)" in body and 'data-testid="detail-id">KR-2024-0004' in body


def test_selection_kept_when_filtered_out_and_bad_ids(researcher_client, seeded_patients):
    body = researcher_client.get("/cases?country=몽골&id=1").content.decode()
    assert 'data-testid="detail-id">KR-2024-0001' in body
    body = researcher_client.get("/cases?id=9999&page=99&tab=42").content.decode()
    assert 'data-testid="detail-id">KR-2024-0001' in body and "5/5" in body and "caseDetail(6)" in body


def test_detail_tabs_content(researcher_client, seeded_patients):
    body = researcher_client.get("/cases?id=4").content.decode()
    # DST: 원본 dstResults 그대로
    assert body.count("dst-pill dst-R") == 4
    # 유전체 변이 + 관련 약제 규칙
    for m, drug in [("rpoB S450L", "Rifampicin"), ("katG S315T", "Isoniazid"), ("gyrA D94G", "Fluoroquinolones"), ("rrs a1401g", "Amikacin")]:
        assert m in body and drug in body
    # 업로드 연결 링크 (conditionId 전달)
    for path in ("/upload/clinical", "/upload/imaging", "/upload/genomic", "/upload/lab", "/upload/regimen"):
        assert f'{path}?conditionId=KR-2024-0004' in body
    assert 'data-testid="biochem-empty"' in body


def test_biochem_from_db(researcher_client, seeded_patients):
    BiochemResult.objects.create(patient_id=4, test="CRP", value="18.5 mg/L", reference_range="0-10", flag="H")
    body = researcher_client.get("/cases?id=4").content.decode()
    assert "18.5 mg/L" in body and "↑ High" in body and 'data-testid="biochem-empty"' not in body


def test_regimen_and_timeline_rules(seeded_patients):
    p = Patient.objects.get(pk=4)  # XDR-TB, 720일
    assert services.regimen_data(p) == [
        {"drug": "INH", "start": 0, "duration": 720}, {"drug": "RIF", "start": 0, "duration": 720},
        {"drug": "EMB", "start": 0, "duration": 60}, {"drug": "PZA", "start": 0, "duration": 60},
        {"drug": "FQ", "start": 0, "duration": 720}]
    assert [r["drug"] for r in services.regimen_data(Patient.objects.get(pk=1))] == ["INH", "RIF", "EMB", "PZA"]
    ev = services.timeline(p, "ko")
    assert [e["date"] for e in ev] == ["2024-03-01", "2024-03-04", "2024-03-08", "2024-03-15", "2024-03-31", "2024-04-15", "2025-01-10"]
    assert ev[0]["desc"] == "국립결핵병원에서 등록" and ev[-1]["desc"] == "결과: Failed (720일)"
    assert services.timeline(Patient.objects.get(pk=5), "en")[-1]["label"] == "Treatment Ended"
    assert len(services.timeline(Patient.objects.get(pk=5), "ko")) == 6  # 시퀀스 없음


def test_imaging_generation_matches_original_rules(seeded_patients):
    p = Patient.objects.get(pk=4)  # CXR 8, CT 3, PET-CT 2
    imgs = imaging.generate_images(p)
    assert len(imgs) == 13
    assert [i["date"] for i in imgs] == sorted(i["date"] for i in imgs)
    assert [i["id"] for i in imgs] == list(range(1, 14))
    cxr = [i for i in imgs if i["type"] == "CXR"]
    assert cxr[0]["date"] == "2024-01-15" and cxr[0]["src"] == "imaging/cxr/CHNCXR_0448_1.jpg"  # 자산 (0 + id 4 - 1) % 5 = 3번째
    assert [len(c["findings"]) for c in cxr] == [1, 2, 3, 4, 4, 4, 4, 4]
    assert cxr[0]["findings"][0]["severity"] == ["경미", "중등도", "중증"][(1 * 7 + 0) % 3]
    pet = [i for i in imgs if i["type"] == "PET-CT"]
    assert pet[0]["suv_max"] == 5.3 and pet[0]["findings"][0]["type"] == "공동"
    assert all((settings.BASE_DIR / "static" / i["src"]).exists() for i in imgs)
    assert imaging.generate_images(Patient.objects.get(pk=5))[0]["type"] == "CXR"


def test_empty_db_renders(researcher_client):
    r = researcher_client.get("/cases")
    assert r.status_code == 200 and "등록된 케이스가 없습니다" in r.content.decode()


def test_english_detail(researcher_client, seeded_patients):
    researcher_client.post("/lang", {"lang": "en"})
    body = researcher_client.get("/cases?id=2").content.decode()
    assert "Patient Information" in body and "National Tuberculosis Hospital" in body and "Diabetes" in body


def test_post_not_allowed(researcher_client):
    assert researcher_client.post("/cases").status_code == 405
