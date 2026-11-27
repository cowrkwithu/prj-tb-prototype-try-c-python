"""데이터 접근 신청(DAR) — 폼 검증(원본 규칙·문구), 게이트 상태 전이(none→pending→approved/rejected), 관리자 검토·직무 분리, 권한."""
import pytest

from apps.datasharing import services
from apps.datasharing.models import DataAccessRequest

from .conftest import VALID_DAR, make_dar

pytestmark = pytest.mark.django_db


def _post(client, **override):
    data = {**VALID_DAR, "data_types": ["clinical"], **override}
    return client.post("/data/dar", data)


def test_requires_login(client):
    for url in ("/data", "/data?tab=dar", "/data?tab=api", "/data?tab=uploads"):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/login?next=")
    r = client.post("/data/dar", VALID_DAR)
    assert r.status_code == 302 and not DataAccessRequest.objects.exists()


def test_dar_tab_renders_and_prefill(researcher_client):
    body = researcher_client.get("/data?tab=dar&types=imaging,bogus").content.decode()
    assert "데이터 접근 신청 (DAR)" in body and "연구자 정보" in body and "승인권자 정보" in body and "요청 데이터 *" in body
    assert 'value="imaging" checked' in body and 'value="clinical" checked' not in body
    assert "아직 신청한 내역이 없습니다" in body


@pytest.mark.parametrize("override,field,msg", [
    ({"name": " "}, "name", "이름을 입력하세요"),
    ({"institution": ""}, "institution", "소속기관을 입력하세요"),
    ({"email": ""}, "email", "이메일을 입력하세요"),
    ({"email": "a@b"}, "email", "올바른 이메일 형식이 아닙니다"),
    ({"orcid": "1234"}, "orcid", "ORCID 형식: 0000-0000-0000-0000"),
    ({"orcid": "١٢٣٤-0000-0000-0000"}, "orcid", "ORCID 형식"),
    ({"approver_name": ""}, "approver_name", "승인권자 이름을 입력하세요"),
    ({"approver_title": ""}, "approver_title", "직책을 입력하세요"),
    ({"approver_email": "x"}, "approver_email", "올바른 이메일 형식이 아닙니다"),
    ({"data_types": []}, "data_types", "요청할 데이터 유형을 선택하세요"),
    ({"data_types": ["bogus"]}, None, None),
    ({"purpose": "짧은 목적"}, "purpose", "연구 목적을 30자 이상 작성하세요"),
    ({"dua_agreed": ""}, "dua_agreed", "DUA 동의가 필요합니다"),
])
def test_server_validation_messages(researcher_client, override, field, msg):
    r = _post(researcher_client, **override)
    assert r.status_code == 400 and not DataAccessRequest.objects.exists()
    body = r.content.decode()
    if field:
        assert f'data-error="{field}"' in body or field in ("data_types", "purpose", "dua_agreed")
        assert msg in body


def test_validation_messages_english(researcher_client):
    researcher_client.post("/lang", {"lang": "en"})
    body = _post(researcher_client, purpose="short", email="bad").content.decode()
    assert "Describe the research purpose in at least 30 characters" in body and "Invalid email format" in body


def test_submit_creates_pending_and_gate_transitions(researcher_client, demo_users):
    user = demo_users["researcher"]
    assert services.dar_states(user) == {"clinical": "none", "genomics": "none", "imaging": "none"}
    r = _post(researcher_client, data_types=["clinical", "imaging"])
    assert r.status_code == 302
    dar = DataAccessRequest.objects.get()
    assert r["Location"] == f"/data?tab=dar&submitted={dar.request_id}"
    assert dar.request_id.startswith("DAR-") and dar.status == "pending" and dar.data_types == ["clinical", "imaging"]
    assert dar.applicant == user and dar.dua_agreed_at and dar.orcid == "0000-0002-1825-009X"
    body = researcher_client.get(r["Location"]).content.decode()
    assert "신청서가 제출되었습니다" in body and dar.request_id in body
    assert services.dar_states(user) == {"clinical": "pending", "genomics": "none", "imaging": "pending"}
    # 같은 유형을 다시 신청하면 거부(심사 중)
    r = _post(researcher_client, data_types=["clinical"])
    assert r.status_code == 400 and "이미 승인되었거나 심사 중인 데이터 유형입니다" in r.content.decode()
    # 다른 관리자가 영상만 승인(부분 승인)
    services.approve_request(dar, demo_users["admin"], ["imaging"])
    assert services.dar_states(user) == {"clinical": "none", "genomics": "none", "imaging": "approved"}
    assert services.approved_types(user) == {"imaging"}
    # 데이터셋 탭 게이트: CXR 은 승인, 임상은 신청 필요
    body = researcher_client.get("/data").content.decode()
    assert 'data-dataset="cxr-images" data-gate="approved"' in body
    assert 'data-dataset="patient-cases" data-gate="none"' in body
    assert "/data?tab=dar&amp;types=clinical" in body


def test_pending_gate_and_reject(researcher_client, demo_users):
    user = demo_users["researcher"]
    dar = make_dar(user, ["genomics"])
    body = researcher_client.get("/data").content.decode()
    assert 'data-dataset="genomics" data-gate="pending"' in body and "데이터 접근 신청을 심사 중입니다" in body
    with pytest.raises(services.DarError):
        services.reject_request(dar, demo_users["admin"], "  ​ ")  # 사유 필수(공백·제로폭만)
    services.reject_request(dar, demo_users["admin"], "연구 목적 불충분")
    assert services.dar_states(user)["genomics"] == "none"
    body = researcher_client.get("/data?tab=dar").content.decode()
    assert "반려" in body and "연구 목적 불충분" in body
    # 반려 후에는 다시 신청할 수 있다
    assert _post(researcher_client, data_types=["genomics"]).status_code == 302


def test_request_numbers_increment(demo_users, other_researcher):
    a = make_dar(demo_users["researcher"], ["clinical"])
    b = make_dar(other_researcher, ["clinical"])
    assert int(b.request_id.rsplit("-", 1)[1]) == int(a.request_id.rsplit("-", 1)[1]) + 1


def test_admin_review_flow_and_separation_of_duties(client, demo_users):
    admin, dmanager = demo_users["admin"], demo_users["dmanager"]
    own = make_dar(admin, ["clinical"])
    other = make_dar(demo_users["researcher"], ["clinical", "genomics"])
    client.force_login(admin)
    body = client.get("/admin/dar").content.decode()
    assert other.request_id in body and own.request_id in body and 'data-testid="dar-count-pending">2건' in body
    # 본인 신청은 승인·반려 불가
    page = client.get(f"/admin/dar/{own.request_id}").content.decode()
    assert 'data-testid="own-request"' in page and 'data-testid="dar-actions"' not in page
    client.post(f"/admin/dar/{own.request_id}/approve", {"types": ["clinical"]})
    client.post(f"/admin/dar/{own.request_id}/reject", {"reason": "x"})
    assert DataAccessRequest.objects.get(pk=own.pk).status == "pending"
    # 승인 유형 없이 승인 → 거부, 요청하지 않은 유형은 무시
    client.post(f"/admin/dar/{other.request_id}/approve", {"types": ["imaging"]})
    assert DataAccessRequest.objects.get(pk=other.pk).status == "pending"
    r = client.post(f"/admin/dar/{other.request_id}/approve", {"types": ["genomics", "imaging"], "comment": "OK"})
    assert r.status_code == 302 and r["Location"] == "/admin/dar"
    other.refresh_from_db()
    assert other.status == "approved" and other.approved_types == ["genomics"] and other.reviewer == admin
    assert other.reviewed_at and other.review_comment == "OK"
    # 이중 처리 방지
    client.post(f"/admin/dar/{other.request_id}/reject", {"reason": "늦은 반려"})
    assert DataAccessRequest.objects.get(pk=other.pk).status == "approved"
    # 다른 관리자는 admin 의 신청을 처리할 수 있다
    client.force_login(dmanager)
    client.post(f"/admin/dar/{own.request_id}/reject", {"reason": ""})
    assert DataAccessRequest.objects.get(pk=own.pk).status == "pending"  # 사유 필수
    client.post(f"/admin/dar/{own.request_id}/reject", {"reason": "기관 승인서 누락"})
    own.refresh_from_db()
    assert own.status == "rejected" and own.review_comment == "기관 승인서 누락" and own.reviewer == dmanager


def test_admin_dashboard_and_sidebar(client, demo_users):
    make_dar(demo_users["researcher"], ["clinical"])
    make_dar(demo_users["researcher"], ["imaging"], "approved")
    client.force_login(demo_users["admin"])
    body = client.get("/admin").content.decode()
    assert "데이터 접근 신청 대기 1건 · 승인 1건" in body and 'href="/admin/dar"' in body
    assert 'href="/admin/deletions"' in body


def test_admin_screens_permissions(client, researcher_client, demo_users):
    dar = make_dar(demo_users["researcher"], ["clinical"])
    for url in ("/admin/dar", f"/admin/dar/{dar.request_id}"):
        r = researcher_client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/admin/login")
    r = researcher_client.post(f"/admin/dar/{dar.request_id}/approve", {"types": ["clinical"]})
    assert r.status_code == 302 and DataAccessRequest.objects.get(pk=dar.pk).status == "pending"
    client.logout()
    assert client.get("/admin/dar").status_code == 302
    client.force_login(demo_users["admin"])
    assert client.get("/admin/dar/DAR-2099-999").status_code == 404
    assert client.get("/admin/dar?status=bogus").status_code == 200


def test_researcher_sees_only_own_requests(client, demo_users, other_researcher):
    mine = make_dar(demo_users["researcher"], ["clinical"])
    theirs = make_dar(other_researcher, ["imaging"])
    client.force_login(other_researcher)
    body = client.get("/data?tab=dar").content.decode()
    assert theirs.request_id in body and mine.request_id not in body
    # 남의 신청번호로 제출 완료 화면을 열어도 보이지 않는다
    body = client.get(f"/data?tab=dar&submitted={mine.request_id}").content.decode()
    assert 'data-testid="dar-submitted"' not in body
    assert services.dar_states(other_researcher)["clinical"] == "none"


def test_get_only_and_post_only(researcher_client):
    assert researcher_client.post("/data").status_code == 405
    assert researcher_client.get("/data/dar").status_code == 405
    assert researcher_client.get("/data/api/key").status_code == 405
