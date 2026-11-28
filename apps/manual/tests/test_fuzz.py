"""5단계 새 화면 퍼징 — /manual 검색, 대상자 등록조회 영상 탭(DB 검사) — 어떤 입력에도 500 을 내지 않는다."""
import pytest

from apps.cases.models import ImagingStudy

pytestmark = pytest.mark.django_db

ATTACKS = ["가" * 5000, "a\x00b", "\r\n=cmd|' /C calc'!A0", "<script>alert(1)</script>\"'><img src=x onerror=1>",
           "²٣１", "-1", "9" * 60, "1e308", "NaN", "../../etc/passwd", "", " ", "%", "_", "\\", "[" * 3000]


@pytest.fixture
def setup(seeded_patients):
    from apps.imaging.services import load_imaging_samples
    from apps.manual.services import load_manual

    load_manual()
    load_imaging_samples()


@pytest.mark.parametrize("v", ATTACKS)
def test_no_500(client, demo_users, setup, v):
    st = ImagingStudy.objects.filter(patient_id=4).first()
    for user in ("researcher", "admin"):
        client.force_login(demo_users[user])
        for lang in ("ko", "en"):
            client.post("/lang", {"lang": lang})
            for headers in ({}, {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "manual-results"}):
                r = client.get("/manual", {"q": v}, **headers)
                assert r.status_code < 500, (user, lang, v[:20])
            r = client.get("/cases", {"id": v, "tab": v})
            assert r.status_code < 500
            r = client.get("/cases/4/images", {"offset": v})
            assert r.status_code < 500
            r = client.get(f"/cases/4/images/{st.pk}", {"x": v})
            assert r.status_code < 500
    for url in ("/cases/4/images/99999999999", "/cases/99999999/images/1", "/cases/99999999/images"):
        assert client.get(url).status_code in (404, 302), url
