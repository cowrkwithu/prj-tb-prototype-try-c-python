"""GET /api/v1/genomics/variants — 3c 키 인증·DAR(유전체)·요청 제한 그대로, 필터·페이지·DB 집계 빈도."""
import pytest

from apps.datasharing import services as ds
from apps.datasharing.tests.conftest import auth, make_dar

pytestmark = pytest.mark.django_db
URL = "/api/v1/genomics/variants"


@pytest.fixture
def key(demo_users, catalog):
    make_dar(demo_users["researcher"], ["genomics"], "approved")
    _, raw = ds.issue_key(demo_users["researcher"], "kim@snubh.org", "SNUBH")
    return raw


def test_requires_key_and_genomics_dar(client, demo_users, catalog):
    assert client.get(URL).status_code == 401
    make_dar(demo_users["researcher"], ["clinical"], "approved")
    _, raw = ds.issue_key(demo_users["researcher"], "kim@snubh.org", "SNUBH")
    r = client.get(URL, **auth(raw))
    assert r.status_code == 403 and r.json()["required"] == ["genomics"]


def test_list_and_filters(client, key):
    r = client.get(URL, **auth(key)).json()
    assert r["total"] == 20 and len(r["data"]) == 20 and r["frequencyDenominator"] == 38
    first = r["data"][0]
    assert first == {"id": first["id"], "gene": "rpoB", "position": 761155, "refAllele": "C", "altAllele": "T",
                     "aminoAcidChange": "S450L", "aminoAcidChangeEcoli": "S531L", "drugResistance": "Rifampicin",
                     "drugs": ["Rifampicin"],
                     "frequency": round(17 / 38, 4), "sampleCount": 17,
                     "annotation": "Most common RIF resistance mutation globally"}
    r = client.get(URL, {"gene": "RPOB", "minFrequency": "0.1"}, **auth(key)).json()
    assert [d["aminoAcidChange"] for d in r["data"]] == ["S450L", "H445Y"]
    r = client.get(URL, {"drug": "isoniazid"}, **auth(key)).json()
    assert r["total"] == 3
    # 추가 연관 약제도 필터·출력에 반영(rrs a1401g → Kanamycin, inhA c-15t → Ethionamide)
    r = client.get(URL, {"drug": "kanamycin"}, **auth(key)).json()
    assert {d["aminoAcidChange"] for d in r["data"]} == {"a1401g", "c-14t (promoter)"}
    assert next(d for d in r["data"] if d["gene"] == "rrs")["drugs"] == ["Amikacin", "Kanamycin", "Capreomycin"]
    assert [d["gene"] for d in client.get(URL, {"drug": "Ethionamide"}, **auth(key)).json()["data"]] == ["inhA", "ethA"]
    r = client.get(URL, {"page": "2", "limit": "15"}, **auth(key)).json()
    assert len(r["data"]) == 5 and r["pagination"] == {"page": 2, "limit": 15, "total": 20}


@pytest.mark.parametrize("params", [{"gene": "zz"}, {"drug": "x"}, {"minFrequency": "2"}, {"minFrequency": "-1"},
                                    {"minFrequency": "nan"}, {"minFrequency": "1e-3"}, {"page": "0"}, {"limit": "101"}])
def test_bad_params_400(client, key, params):
    r = client.get(URL, params, **auth(key))
    assert r.status_code == 400 and r.json()["status"] == "bad_request"


def test_rate_limit_applies(client, key, settings, frozen_api_clock):
    settings.API_RATE_LIMIT_PER_MINUTE = 2
    codes = [client.get(URL, **auth(key)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
