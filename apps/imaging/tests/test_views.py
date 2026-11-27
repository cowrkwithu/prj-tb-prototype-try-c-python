"""RAP 화면·전문가 소견 CRUD(POST+CSRF, 작성자·관리자 권한, 좌표·평면 검증)·업로드 영상 파일 게이트·데이터 조건."""
import json
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.cases.models import ImagingStudy, Patient
from apps.datasharing.tests.conftest import make_dar
from apps.imaging.models import ExpertAnnotation

from .conftest import JPEG, make_uploaded_study

pytestmark = pytest.mark.django_db


def rap_data(resp) -> dict:
    m = re.search(r'<script id="rap-data" type="application/json">(.*?)</script>', resp.content.decode(), re.S)
    return json.loads(m.group(1))


def cxr_sample(pk=4):
    return ImagingStudy.objects.filter(patient_id=pk, modality="CXR", is_public_sample=True).order_by("study_date").first()


def vol_sample(pk=4, modality="CT"):
    return ImagingStudy.objects.filter(patient_id=pk, modality=modality, is_public_sample=True).first()


RECT = {"shape": "rect", "x": "10", "y": "20", "width": "15", "height": "12", "finding_type": "공동",
        "location": "우폐", "severity": "중증", "text": "추적 필요"}


def test_login_required(client, samples):
    for url in ("/rap", "/rap/timeseries", "/rap/annotation", "/rap/conditions", "/rap/files/1"):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/login?next="), url
    assert client.post("/rap/annotations", RECT).status_code == 302


def test_pages_and_default_patient(researcher_client, samples):
    for i, url in enumerate(("/rap", "/rap/timeseries", "/rap/annotation")):
        r = researcher_client.get(url)
        assert r.status_code == 200 and 'class="sidebar"' in r.content.decode()
        d = rap_data(r)
        assert d["tab"] == i and d["patient"] == "KR-2024-0002"  # 원본: CXR ≥ 3 이고 CT 가 있는 첫 환자
    d = rap_data(researcher_client.get("/rap?patient=KR-2024-0004"))
    assert len(d["studies"]) == 13 and [s["date"] for s in d["studies"]] == sorted(s["date"] for s in d["studies"])
    cxr = next(s for s in d["studies"] if s["type"] == "CXR")
    assert cxr["kind"] == "cxr" and cxr["image"].startswith("/static/imaging/cxr/") and cxr["rois"] and cxr["sample"]
    ct = next(s for s in d["studies"] if s["type"] == "CT")
    assert ct["kind"] == "volume" and ct["volume"]["planes"] == ["axial", "coronal"]
    pet = next(s for s in d["studies"] if s["type"] == "PET-CT")
    assert pet["volume"]["planes"] == ["axial", "coronal", "mip"] and pet["suv"] == 5.3
    sid = d["studies"][5]["id"]
    assert rap_data(researcher_client.get(f"/rap?patient=KR-2024-0004&study={sid}"))["selected"] == 5
    assert rap_data(researcher_client.get("/rap?patient=NOPE&study=x"))["patient"] == "KR-2024-0002"


def test_empty_db(researcher_client):
    assert 'data-testid="rap-empty"' in researcher_client.get("/rap").content.decode()


def test_create_update_delete_permissions(client, demo_users, other_user, samples):
    st = cxr_sample()
    client.force_login(demo_users["researcher"])
    r = client.post("/rap/annotations", {**RECT, "study": st.pk})
    assert r.status_code == 201
    a = ExpertAnnotation.objects.get()
    assert (a.author, a.author_name, a.finding_type, a.plane, a.slice_index) == (
        demo_users["researcher"], demo_users["researcher"].shown_name, "공동", "", None)
    assert r.json()["annotation"]["can_edit"] is True
    # 화면 데이터에 반영(다른 사용자에게는 can_edit False)
    client.force_login(other_user)
    d = rap_data(client.get("/rap?patient=KR-2024-0004"))
    assert d["annotations"][0]["id"] == a.pk and d["annotations"][0]["can_edit"] is False
    assert client.post(f"/rap/annotations/{a.pk}", {"finding_type": "결절"}).status_code == 403
    assert client.post(f"/rap/annotations/{a.pk}/delete").status_code == 403
    # 작성자 수정
    client.force_login(demo_users["researcher"])
    r = client.post(f"/rap/annotations/{a.pk}", {"finding_type": "결절", "location": "좌폐", "severity": "경미", "text": "x"})
    assert r.status_code == 200 and ExpertAnnotation.objects.get().finding_type == "결절"
    assert client.post(f"/rap/annotations/{a.pk}", {"finding_type": ""}).status_code == 400  # 도형은 소견 유형 필수
    # 관리자 삭제
    client.force_login(demo_users["admin"])
    assert client.post(f"/rap/annotations/{a.pk}/delete").status_code == 200 and not ExpertAnnotation.objects.exists()
    assert client.post(f"/rap/annotations/{a.pk}/delete").status_code == 404


def test_text_annotation(researcher_client, samples):
    st = cxr_sample()
    base = {"study": st.pk, "shape": "text", "x": "50", "y": "50"}
    assert researcher_client.post("/rap/annotations", base).status_code == 400
    r = researcher_client.post("/rap/annotations", {**base, "text": "우상엽 공동 의심"})
    assert r.status_code == 201 and r.json()["annotation"]["finding_type"] == "" and r.json()["annotation"]["location"] == ""
    assert researcher_client.post("/rap/annotations", {**base, "text": "x" * 121}).status_code == 400
    assert researcher_client.post("/rap/annotations", {**base, "text": "a", "width": "5"}).status_code == 400


@pytest.mark.parametrize("override,msg", [
    ({"x": "-1"}, None), ({"x": "100.5"}, None), ({"x": "nan"}, None), ({"x": "inf"}, None), ({"y": "abc"}, None),
    ({"width": "1"}, "너무 작습니다"), ({"x": "95", "width": "10"}, "영상 밖"), ({"finding_type": "암"}, "소견 유형"),
    ({"location": "중앙"}, "위치"), ({"severity": "x"}, "중증도"), ({"finding_type": ""}, "소견 유형을 고르세요"),
    ({"shape": "line"}, None), ({"text": "x" * 301}, None), ({"plane": "axial", "slice": "3"}, "CT·PET-CT"),
    ({"study": "999999"}, "찾을 수 없습니다"), ({"text": "a\x01b"}, "허용되지 않는 문자"),
])
def test_create_validation(researcher_client, samples, override, msg):
    st = cxr_sample()
    r = researcher_client.post("/rap/annotations", {**RECT, "study": st.pk, **override})
    assert r.status_code in (400, 404) and "error" in r.json()
    if msg:
        assert msg in r.json()["error"]
    assert not ExpertAnnotation.objects.exists()


def test_volume_annotation_planes(researcher_client, samples):
    ct, pet = vol_sample(), vol_sample(modality="PET-CT")
    base = {**RECT, "study": ct.pk}
    assert researcher_client.post("/rap/annotations", base).status_code == 400            # 볼륨은 평면·단면 필수
    assert researcher_client.post("/rap/annotations", {**base, "plane": "mip", "slice": "1"}).status_code == 400  # CT 에 MIP 없음
    assert researcher_client.post("/rap/annotations", {**base, "plane": "axial", "slice": "116"}).status_code == 400
    r = researcher_client.post("/rap/annotations", {**base, "plane": "axial", "slice": "70"})
    assert r.status_code == 201 and r.json()["annotation"]["plane"] == "axial" and r.json()["annotation"]["slice"] == 70
    r = researcher_client.post("/rap/annotations", {**RECT, "study": pet.pk, "plane": "mip", "slice": "23"})
    assert r.status_code == 201


def test_csrf_enforced(demo_users, samples):
    c = Client(enforce_csrf_checks=True)
    c.force_login(demo_users["researcher"])
    assert c.post("/rap/annotations", {**RECT, "study": cxr_sample().pk}).status_code == 403
    assert not ExpertAnnotation.objects.exists()


def test_get_not_allowed_on_writes(researcher_client, samples):
    assert researcher_client.get("/rap/annotations").status_code == 405
    assert researcher_client.get("/rap/annotations/1/delete").status_code == 405
    assert researcher_client.post("/rap").status_code == 405


def test_per_study_cap(researcher_client, samples, monkeypatch):
    from apps.imaging import services

    monkeypatch.setattr(services, "MAX_ANNOTATIONS_PER_STUDY", 2)
    st = cxr_sample()
    codes = [researcher_client.post("/rap/annotations", {**RECT, "study": st.pk}).status_code for _ in range(3)]
    assert codes == [201, 201, 400]


def test_uploaded_image_gate(client, demo_users, other_user, samples):
    p = Patient.objects.get(pk=3)
    st = make_uploaded_study(demo_users["researcher"], p)
    sf = st.files.get()
    # 제출자 — 인라인 이미지, 주석 가능
    client.force_login(demo_users["researcher"])
    r = client.get(f"/rap/files/{sf.pk}")
    assert r.status_code == 200 and r["Content-Type"] == "image/jpeg" and b"".join(r.streaming_content) == JPEG
    assert r["X-Content-Type-Options"] == "nosniff"
    d = rap_data(client.get(f"/rap?patient={p.condition_id}"))
    up = next(s for s in d["studies"] if not s["sample"])
    assert up["kind"] == "upload" and up["image"] == f"/rap/files/{sf.pk}" and up["findings"][0]["type"] == "공동"
    assert client.post("/rap/annotations", {**RECT, "study": st.pk}).status_code == 201
    # 다른 연구원(DAR 미승인) — 파일 404, 화면은 잠금, 주석 403
    client.force_login(other_user)
    assert client.get(f"/rap/files/{sf.pk}").status_code == 404
    up = next(s for s in rap_data(client.get(f"/rap?patient={p.condition_id}"))["studies"] if not s["sample"])
    assert up["image"] is None and up["can_view"] is False and up["kind"] == "none"
    assert up["files"][0]["name"] == "file 1"  # 원래 파일명 비공개
    assert client.post("/rap/annotations", {**RECT, "study": st.pk}).status_code == 403
    # 영상 DAR 승인 후 열람 가능
    make_dar(other_user, ["imaging"], "approved")
    assert client.get(f"/rap/files/{sf.pk}").status_code == 200
    client.force_login(demo_users["admin"])
    assert client.get(f"/rap/files/{sf.pk}").status_code == 200


def test_dicom_only_study_not_annotatable(researcher_client, demo_users, samples):
    st = make_uploaded_study(demo_users["researcher"], Patient.objects.get(pk=3), name="a.dcm", data=b"\x00" * 200)
    sf = st.files.get()
    assert researcher_client.get(f"/rap/files/{sf.pk}").status_code == 404
    r = researcher_client.post("/rap/annotations", {**RECT, "study": st.pk})
    assert r.status_code == 400 and "표시할 수 있는 영상" in r.json()["error"]


def test_missing_file_is_404(researcher_client, demo_users, samples):
    import os

    st = make_uploaded_study(demo_users["researcher"], Patient.objects.get(pk=3))
    sf = st.files.get()
    os.remove(sf.file.path)
    assert researcher_client.get(f"/rap/files/{sf.pk}").status_code == 404


def test_conditions_counts(researcher_client, samples):
    body = researcher_client.get("/rap/conditions?finding=공동").content.decode()
    n = sum(1 for s in ImagingStudy.objects.filter(modality="CXR") if any(f["finding"] == "공동" for f in s.findings))
    assert f'data-testid="cond-studies">{n}<' in body and "/depot?finding=%EA%B3%B5%EB%8F%99&amp;run=1" in body
    body = researcher_client.get("/rap/conditions?finding=zz&location=x&severity=y").content.decode()
    assert "/depot?panel=saved" in body


def test_upload_approval_after_samples_keeps_counts(client, demo_users, samples):
    """샘플이 있는 환자에 CXR(JPG) 업로드를 승인하면 영상 수는 +1 만 된다(샘플을 다시 세지 않음)."""
    from apps.uploads.tests.conftest import upload_id_from

    p = Patient.objects.get(pk=3)
    before = p.cxr_count
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/imaging/files", {"mode": "CXR", "condition_id": p.condition_id, "study_date": "2025-05-01",
                                              "files": [SimpleUploadedFile("s.jpg", JPEG, content_type="image/jpeg")],
                                              "action": "next"})
    uid = upload_id_from(r)
    client.post("/upload/imaging/proceed", {"id": uid, "from": "dicom"})
    client.post("/upload/imaging/data", {"id": uid, "study_description": "CXR", "tb_activity": "Active",
                                         "ann_finding": ["Cavity"], "ann_location": ["Right"], "ann_severity": ["Severe"],
                                         "ann_description": [""], "impression": "TB", "action": "next"})
    client.post("/upload/imaging/submit", {"id": uid, "deid": [str(i) for i in range(7)]})
    client.force_login(demo_users["admin"])
    client.post(f"/admin/review/{uid}/approve", {"check": [str(i) for i in range(5)]})
    p.refresh_from_db()
    assert p.cxr_count == before + 1
    up = ImagingStudy.objects.get(submission__upload_id=uid)
    client.force_login(demo_users["researcher"])
    d = rap_data(client.get(f"/rap?patient={p.condition_id}"))
    assert any(s["id"] == up.pk and s["kind"] == "upload" for s in d["studies"])


def test_english(researcher_client, samples):
    researcher_client.post("/lang", {"lang": "en"})
    body = researcher_client.get("/rap").content.decode()
    assert "Expert Findings" in body and "Time Series" in body and "Data Conditions" in body
    d = rap_data(researcher_client.get("/rap"))
    assert d["en"] is True and d["findings"][0]["label"] == "Nodule"


def test_case_editor_modal_shows_db_expert_findings(researcher_client, samples):
    """대상자 등록조회의 영상 상세 모달이 RAP 에서 저장한 전문가 소견(DB)을 보인다(이전: 0건 고정)."""
    from apps.cases import imaging as case_imaging

    p = Patient.objects.get(pk=4)
    gen = case_imaging.generate_images(p)
    st = cxr_sample()
    assert researcher_client.post("/rap/annotations", {**RECT, "study": st.pk}).status_code == 201
    im = next(g for g in gen if g["type"] == "CXR" and g["date"] == st.study_date.isoformat())
    body = researcher_client.get(f"/cases/4/images/{im['id']}").content.decode()
    assert 'data-testid="expert-count">1건' in body and "추적 필요" in body and "공동 · 우폐 · 중증" in body
