"""REST API /api/v1 — 키 인증(없음·잘못됨·폐기·만료 없음), 요청 시점 DAR 승인 판정, 파라미터 검증·퍼징, 페이지 상한,
비식별, POST cohorts(코호트 서비스 재사용·CSRF 면제), 요청 제한, 영상 메타·파일. (변이 목록 API 는 4단계 — apps/genomics/tests/test_api.py)"""
import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.cases.models import ImagingStudy, Patient
from apps.cohorts.models import Cohort
from apps.datasharing import services
from apps.datasharing.models import ApiKey, DataAccessRequest

from .conftest import auth, make_dar

pytestmark = pytest.mark.django_db
URLS = ["/api/v1/cases", "/api/v1/cases/1", "/api/v1/images/1", "/api/v1/genomics/variants"]


def test_no_key_bad_key_revoked_key(client, seeded_patients, api_key, demo_users):
    for url in URLS:
        r = client.get(url)
        assert r.status_code == 401 and r.json()["status"] == "unauthorized" and r["WWW-Authenticate"].startswith("Bearer")
    for header in ("Bearer", "Bearer ", "Basic abc", f"Token {api_key}", "Bearer tbk_" + "x" * 43, "Bearer " + "가" * 50,
                   f"Bearer {api_key}x", "Bearer " + api_key.upper(), "x" * 500):
        assert client.get("/api/v1/cases", HTTP_AUTHORIZATION=header).status_code == 401, header
    assert client.post("/api/v1/cohorts", "{}", content_type="application/json").status_code == 401
    r = client.get("/api/v1/cases", **auth(api_key))
    assert r.status_code == 200
    # 세션 로그인만으로는 호출할 수 없다(키 필수)
    client.force_login(demo_users["researcher"])
    assert client.get("/api/v1/cases").status_code == 401
    # 폐기 후 401
    services.revoke_keys(demo_users["researcher"])
    assert client.get("/api/v1/cases", **auth(api_key)).status_code == 401


def test_key_stored_as_hash_and_no_expiry(api_key, demo_users, client, seeded_patients):
    key = ApiKey.objects.get()
    assert api_key not in (key.key_hash, key.prefix) and len(key.key_hash) == 64
    assert key.key_hash == services.hash_key(api_key) and api_key.startswith(key.prefix)
    ApiKey.objects.filter(pk=key.pk).update(created_at=timezone.now() - timedelta(days=3650))
    assert client.get("/api/v1/cases", **auth(api_key)).status_code == 200  # 만료 없음
    assert ApiKey.objects.get().last_used_at is not None


def test_inactive_user_key_rejected(api_key, demo_users, client, seeded_patients):
    demo_users["researcher"].is_active = False
    demo_users["researcher"].save()
    assert client.get("/api/v1/cases", **auth(api_key)).status_code == 401


def test_permissions_follow_dar_at_request_time(client, demo_users, seeded_patients):
    user = demo_users["researcher"]
    dar = make_dar(user, ["imaging"], "approved")
    key, raw = services.issue_key(user, "kim@snubh.org", "분당서울대병원")
    r = client.get("/api/v1/cases", **auth(raw))
    assert r.status_code == 403 and r.json()["required"] == ["clinical"]
    assert client.get("/api/v1/genomics/variants", **auth(raw)).status_code == 403
    # 발급 뒤에 임상이 승인되면 같은 키로 바로 쓸 수 있다
    make_dar(user, ["clinical"], "approved")
    assert client.get("/api/v1/cases", **auth(raw)).status_code == 200
    assert client.get("/api/v1/cases/1?include=genomics", **auth(raw)).status_code == 403
    assert client.get("/api/v1/cases/1?include=imaging,lab", **auth(raw)).status_code == 200
    # 승인 철회(상태 변경) 시 즉시 반영
    DataAccessRequest.objects.filter(pk=dar.pk).update(status="rejected")
    assert client.get("/api/v1/cases/1?include=imaging", **auth(raw)).status_code == 403


def test_issue_key_requires_approved_dar(researcher_client, demo_users):
    body = researcher_client.get("/data?tab=api").content.decode()
    assert 'data-testid="key-needs-dar"' in body and "/api/v1" in body and "https://api.tb-portal.kr" not in body
    assert "http://testserver/api/v1" in body
    r = researcher_client.post("/data/api/key", {"email": "kim@snubh.org", "institution": "SNUBH"})
    assert r.status_code == 403 and not ApiKey.objects.exists()
    make_dar(demo_users["researcher"], ["clinical"])  # 심사 중으로는 불가
    assert researcher_client.post("/data/api/key", {"email": "kim@snubh.org", "institution": "SNUBH"}).status_code == 403
    make_dar(demo_users["researcher"], ["imaging"], "approved")
    r = researcher_client.post("/data/api/key", {"email": "bad", "institution": ""})
    assert r.status_code == 400 and "올바른 이메일 형식이 아닙니다" in r.content.decode() and not ApiKey.objects.exists()
    r = researcher_client.post("/data/api/key", {"email": "kim@snubh.org", "institution": "SNUBH"})
    assert r.status_code == 200 and r["Cache-Control"] == "no-store"
    body = r.content.decode()
    assert 'data-testid="new-key"' in body
    import re

    raw = re.search(r"(tbk_[A-Za-z0-9_-]+)", body).group(1)
    assert ApiKey.objects.get().key_hash == services.hash_key(raw)
    # 다시 열면 원문은 보이지 않는다(1회 표시)
    page = researcher_client.get("/data?tab=api").content.decode()
    assert raw not in page and 'data-testid="active-key"' in page
    # 재발급 → 이전 키 폐기
    r = researcher_client.post("/data/api/key", {"email": "kim@snubh.org", "institution": "SNUBH"})
    raw2 = re.search(r"(tbk_[A-Za-z0-9_-]+)", r.content.decode()).group(1)
    assert raw2 != raw and ApiKey.objects.filter(revoked_at__isnull=True).count() == 1
    api = Client()
    # 이전 키는 폐기 → 401, 새 키는 영상만 승인 → cases 403
    assert api.get("/api/v1/cases", **auth(raw)).status_code == 401
    assert api.get("/api/v1/cases", **auth(raw2)).status_code == 403
    assert api.get("/api/v1/images/999", **auth(raw)).status_code == 401
    assert api.get("/api/v1/images/999", **auth(raw2)).status_code == 404
    # 사용자 폐기
    researcher_client.post("/data/api/key/revoke")
    assert not ApiKey.objects.filter(revoked_at__isnull=True).exists()
    assert api.get("/api/v1/images/999", **auth(raw2)).status_code == 401


def test_other_users_key_isolation(client, demo_users, other_researcher, api_key):
    client.force_login(other_researcher)
    body = client.get("/data?tab=api").content.decode()
    assert 'data-testid="active-key"' not in body
    client.post("/data/api/key/revoke")
    assert ApiKey.objects.filter(revoked_at__isnull=True).count() == 1  # 남의 키는 폐기되지 않는다


def test_cases_list_pagination_filters_and_deidentification(client, api_key, seeded_patients):
    r = client.get("/api/v1/cases", **auth(api_key))
    data = r.json()
    n = Patient.objects.count()
    assert data["pagination"] == {"page": 1, "limit": 20, "total": n} and len(data["data"]) == min(20, n)
    item = data["data"][0]
    assert set(item) == {"id", "conditionId", "demographics", "clinical"}
    assert set(item["demographics"]) == {"age", "sex", "country"}
    assert set(item["clinical"]) == {"diagnosisType", "treatmentOutcome"}
    r = client.get("/api/v1/cases?limit=100&page=1", **auth(api_key))
    assert len(r.json()["data"]) == min(100, n)
    for bad in ("101", "0", "-1", "abc", "1e2", "²", "１０", "9" * 30):
        assert client.get(f"/api/v1/cases?limit={bad}", **auth(api_key)).status_code == 400, bad
        assert client.get(f"/api/v1/cases?page={bad}", **auth(api_key)).status_code in (400, 200), bad
    assert client.get("/api/v1/cases?page=0", **auth(api_key)).status_code == 400
    r = client.get("/api/v1/cases?page=999999", **auth(api_key))
    assert r.status_code == 200 and r.json()["data"] == []
    kr = Patient.objects.filter(country="한국").count()
    assert client.get("/api/v1/cases?country=한국&limit=100", **auth(api_key)).json()["pagination"]["total"] == kr
    assert client.get("/api/v1/cases?country=korea&limit=100", **auth(api_key)).json()["pagination"]["total"] == kr
    assert client.get("/api/v1/cases?country=Atlantis", **auth(api_key)).status_code == 400
    mdr = Patient.objects.filter(diagnosis_type="MDR-TB").count()
    assert client.get("/api/v1/cases?diagnosisType=MDR-TB", **auth(api_key)).json()["pagination"]["total"] == mdr
    assert client.get("/api/v1/cases?diagnosisType=mdr", **auth(api_key)).status_code == 400


def test_case_detail_includes(client, api_key, seeded_patients):
    p = Patient.objects.filter(has_sequence=True, has_cxr=True).first() or Patient.objects.first()
    r = client.get(f"/api/v1/cases/{p.pk}", **auth(api_key))
    d = r.json()
    assert r.status_code == 200 and d["conditionId"] == p.condition_id and "genomics" not in d and "imaging" not in d
    assert d["demographics"]["site"] == p.site and d["clinical"]["treatmentDuration"] == p.treatment_duration
    d = client.get(f"/api/v1/cases/{p.pk}?include=genomics,imaging,lab", **auth(api_key)).json()
    assert d["genomics"]["sraId"] == p.sra_id and d["imaging"]["cxrCount"] == p.cxr_count and "dstResults" in d["lab"]
    text = json.dumps(d, ensure_ascii=False)
    for forbidden in ("submitter", "original_name", "originalName", "registration", "username", "email"):
        assert forbidden not in text
    for bad in ("x", "genomics,foo", "lab,lab,lab,lab", ","):
        r = client.get(f"/api/v1/cases/{p.pk}?include={bad}", **auth(api_key))
        assert r.status_code in (200, 400), bad
    assert client.get(f"/api/v1/cases/{p.pk}?include=foo", **auth(api_key)).status_code == 400
    assert client.get("/api/v1/cases/9999999", **auth(api_key)).status_code == 404
    assert client.get("/api/v1/cases/99999999999", **auth(api_key)).status_code == 404
    assert client.get("/api/v1/cases/abc", **auth(api_key)).status_code == 404
    assert client.get("/api/v1/nope", **auth(api_key)).json()["status"] == "not_found"


def test_variants_available_after_stage4(client, api_key):
    """3c 에서 503 이던 변이 목록은 4단계에서 구현 — 카탈로그가 비어 있으면 빈 목록."""
    r = client.get("/api/v1/genomics/variants", **auth(api_key))
    assert r.status_code == 200 and r.json()["data"] == [] and r.json()["total"] == 0


def test_method_not_allowed(client, api_key):
    assert client.post("/api/v1/cases", **auth(api_key)).status_code == 405
    assert client.get("/api/v1/cohorts", **auth(api_key)).status_code == 405
    assert client.delete("/api/v1/cases/1", **auth(api_key)).status_code == 405


def _post_cohort(raw, payload, client=None, **extra):
    c = client or Client(enforce_csrf_checks=True)
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    return c.post("/api/v1/cohorts", body, content_type="application/json", **auth(raw), **extra)


def test_post_cohort_creates_real_cohort(api_key, demo_users, seeded_patients):
    conds = [{"field": "demographics.country", "operator": "in", "value": ["한국"]},
             {"field": "clinical.diagnosisType", "operator": "eq", "value": "MDR-TB"}]
    r = _post_cohort(api_key, {"name": "신규 데이터셋", "conditions": conds, "description": "API 로 생성"})
    assert r.status_code == 201, r.content
    d = r.json()
    c = Cohort.objects.get(pk=d["id"])
    expected = sorted(Patient.objects.filter(country="한국", diagnosis_type="MDR-TB").values_list("id", flat=True))
    assert sorted(d["patientIds"]) == expected and d["patientCount"] == len(expected)
    assert c.owner == demo_users["researcher"] and c.name == "신규 데이터셋" and c.description == "API 로 생성"
    assert d["createdDate"] == c.created_date.isoformat()
    # 트리 형식도 허용
    r = _post_cohort(api_key, {"name": "OR", "conditions": {"op": "OR", "children": conds}})
    assert r.status_code == 201


@pytest.mark.parametrize("payload", [
    "", "not json", "[]", "null", '"x"', "{", "[" * 5000, '{"name": "a\\ud800", "conditions": []}',
    {"conditions": [{"field": "demographics.age", "operator": "gte", "value": 10}]},
    {"name": "", "conditions": [{"field": "demographics.age", "operator": "gte", "value": 10}]},
    {"name": 5, "conditions": [{"field": "demographics.age", "operator": "gte", "value": 10}]},
    {"name": "x"}, {"name": "x", "conditions": []}, {"name": "x", "conditions": "abc"},
    {"name": "x", "conditions": [{"field": "secret.column", "operator": "eq", "value": 1}]},
    {"name": "x", "conditions": [{"field": "demographics.age", "operator": "regex", "value": ".*"}]},
    {"name": "x", "conditions": [{"field": "demographics.age", "operator": "gte", "value": "1e9"}]},
    {"name": "x", "conditions": [{"field": "demographics.country", "operator": "in", "value": ["x"] * 600}]},
    {"name": "x", "description": 123, "conditions": [{"field": "demographics.age", "operator": "gte", "value": 10}]},
    {"name": "x", "description": "a" * 3000, "conditions": [{"field": "demographics.age", "operator": "gte", "value": 10}]},
    {"name": "x", "conditions": {"op": "XOR", "children": []}},
])
def test_post_cohort_bad_input_400(api_key, seeded_patients, payload):
    n = Cohort.objects.count()
    r = _post_cohort(api_key, payload)
    assert r.status_code == 400, (payload, r.status_code, r.content[:200])
    assert r.json()["status"] == "bad_request" and Cohort.objects.count() == n


def test_post_cohort_body_limit_and_type_permissions(client, demo_users, seeded_patients, settings):
    user = demo_users["researcher"]
    make_dar(user, ["clinical"], "approved")
    _, raw = services.issue_key(user, "kim@snubh.org", "SNUBH")
    settings.API_MAX_BODY_BYTES = 100
    r = _post_cohort(raw, {"name": "x" * 200, "conditions": [{"field": "demographics.age", "operator": "gte", "value": 1}]})
    assert r.status_code == 400
    settings.API_MAX_BODY_BYTES = 64 * 1024
    # 유전체 조건은 genomics 승인 필요
    r = _post_cohort(raw, {"name": "g", "conditions": [{"field": "genomics.hasSequence", "operator": "eq", "value": True}]})
    assert r.status_code == 403 and r.json()["required"] == ["genomics"]
    r = _post_cohort(raw, {"name": "a", "conditions": [{"field": "demographics.age", "operator": "gte", "value": 1}]})
    assert r.status_code == 201


def test_post_cohort_csrf_exempt_but_cookie_session_cannot_call(demo_users, seeded_patients, api_key):
    c = Client(enforce_csrf_checks=True)
    c.force_login(demo_users["researcher"])
    body = json.dumps({"name": "x", "conditions": [{"field": "demographics.age", "operator": "gte", "value": 1}]})
    r = c.post("/api/v1/cohorts", body, content_type="application/json")  # 쿠키 세션만 → 401
    assert r.status_code == 401
    r = c.post("/api/v1/cohorts", body, content_type="application/json", **auth(api_key))  # 키 → CSRF 토큰 없이도 201
    assert r.status_code == 201


def test_rate_limit(client, api_key, seeded_patients, settings, frozen_api_clock):
    settings.API_RATE_LIMIT_PER_MINUTE = 3
    codes = [client.get("/api/v1/cases", **auth(api_key)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = client.get("/api/v1/cases", **auth(api_key))
    assert r.json()["status"] == "rate_limited" and int(r["Retry-After"]) == 31  # 고정 시각 …:30초 → 다음 창까지
    # 키 없는 요청은 계수와 무관하게 401
    assert client.get("/api/v1/cases").status_code == 401


def test_rate_limit_window_resets_at_minute_boundary(client, api_key, seeded_patients, settings, frozen_api_clock):
    """고정 창(분) 경계 — 다음 분이 되면 계수가 새로 시작한다(시계 고정으로 결정적)."""
    settings.API_RATE_LIMIT_PER_MINUTE = 2
    frozen_api_clock.now = 1_800_000_059.9
    codes = [client.get("/api/v1/cases", **auth(api_key)).status_code for _ in range(3)]
    frozen_api_clock.now = 1_800_000_060.05
    codes += [client.get("/api/v1/cases", **auth(api_key)).status_code for _ in range(3)]
    assert codes == [200, 200, 429, 200, 200, 429]


def _imaging_study(demo_users, client):
    """영상 업로드 → 승인으로 ImagingStudy(DICOM 파일 포함)를 만든다."""
    from apps.uploads.models import Submission
    from apps.uploads.services import dicom
    from apps.uploads.tests.conftest import upload_id_from

    client.force_login(demo_users["researcher"])
    data = dicom.build_minimal_dicom({(0x0008, 0x0080): "SNUBH"})
    r = client.post("/upload/imaging/files", {"mode": "CXR", "condition_id": "KR-2024-0003", "study_date": "2025-05-01",
                                              "files": [SimpleUploadedFile("s.dcm", data, content_type="application/dicom")],
                                              "action": "next"})
    uid = upload_id_from(r)
    client.post("/upload/imaging/proceed", {"id": uid, "from": "dicom"})
    client.post("/upload/imaging/data", {"id": uid, "study_description": "CXR", "tb_activity": "Active",
                                         "ann_finding": ["Cavity"], "ann_location": ["Right"], "ann_severity": ["Severe"],
                                         "ann_description": [""], "impression": "TB", "action": "next"})
    client.post("/upload/imaging/submit", {"id": uid, "deid": [str(i) for i in range(7)]})
    client.force_login(demo_users["admin"])
    client.post(f"/admin/review/{uid}/approve", {"check": [str(i) for i in range(5)]})
    client.logout()
    assert Submission.objects.get(upload_id=uid).status == "approved"
    return ImagingStudy.objects.get(submission__upload_id=uid), data


def test_images_metadata_and_files(client, demo_users, seeded_patients, api_key):
    study, data = _imaging_study(demo_users, client)
    api = Client()
    r = api.get(f"/api/v1/images/{study.pk}", **auth(api_key))
    d = r.json()
    assert r.status_code == 200 and d["type"] == "CXR" and d["patientId"] == study.patient_id and d["date"] == "2025-05-01"
    assert d["findings"] == ["Cavity"] and d["annotations"][0]["type"] == "Cavity" and d["files"] == {"dicom": 1, "jpg": 0}
    assert d["downloadUrl"] == f"/api/v1/images/{study.pk}?format=dicom"
    assert "s.dcm" not in json.dumps(d)  # 업로드 원래 파일명은 내보내지 않는다
    r = api.get(f"/api/v1/images/{study.pk}?format=dicom", **auth(api_key))
    assert r.status_code == 200 and b"".join(r.streaming_content) == data
    assert api.get(f"/api/v1/images/{study.pk}?format=jpg", **auth(api_key)).status_code == 404
    assert api.get(f"/api/v1/images/{study.pk}?format=dicom&file=1", **auth(api_key)).status_code == 404
    assert api.get(f"/api/v1/images/{study.pk}?format=png", **auth(api_key)).status_code == 400
    assert api.get(f"/api/v1/images/{study.pk}?format=dicom&file=-1", **auth(api_key)).status_code == 400
    assert api.get("/api/v1/images/999999", **auth(api_key)).status_code == 404
    # 저장 파일이 사라졌으면 404(500 아님)
    import os

    os.remove(study.files.get().file.path)
    assert api.get(f"/api/v1/images/{study.pk}?format=dicom", **auth(api_key)).status_code == 404


def test_images_require_imaging_approval(client, demo_users, seeded_patients):
    user = demo_users["researcher"]
    make_dar(user, ["clinical"], "approved")
    _, raw = services.issue_key(user, "kim@snubh.org", "SNUBH")
    r = client.get("/api/v1/images/1", **auth(raw))
    assert r.status_code == 403 and r.json()["required"] == ["imaging"]


FUZZ = ["", " ", "\x00", "가" * 3000, "<script>", "' OR 1=1 --", "%00", "../../etc/passwd", "1e308", "-0", "NaN",
        "２", "1,2", "[]", "{}", "\r\n"]


@pytest.mark.parametrize("v", FUZZ)
def test_api_param_fuzz_no_500(client, api_key, seeded_patients, v):
    for url in ("/api/v1/cases", "/api/v1/cases/1", "/api/v1/images/1", "/api/v1/genomics/variants"):
        for p in ("page", "limit", "country", "diagnosisType", "include", "format", "file", "gene", "drug", "minFrequency"):
            r = client.get(url, {p: v}, **auth(api_key))
            assert r.status_code < 500, (url, p, v, r.status_code)
    r = client.get("/api/v1/cases", HTTP_AUTHORIZATION=f"Bearer {v}")
    assert r.status_code == 401
    r = _post_cohort(api_key, {"name": v, "description": v, "conditions": [{"field": v, "operator": v, "value": v}]})
    assert r.status_code < 500
