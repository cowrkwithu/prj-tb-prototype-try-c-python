"""대상자 등록조회 영상 탭 — DB ImagingStudy 기반 통합(5단계 이월).

영상 분석(RAP)과 같은 목록(apps.imaging.services.patient_imaging)·같은 열람 규칙을 쓰는지 확인한다.
"""
import pytest

from apps.cases.models import ImagingStudy, Patient
from apps.imaging.models import ExpertAnnotation
from apps.imaging.tests.conftest import make_uploaded_study

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()


@pytest.fixture
def samples(seeded_patients):
    from apps.imaging.services import load_imaging_samples

    return load_imaging_samples()


@pytest.fixture
def other_user(db):
    from apps.accounts.models import User

    return User.objects.create_user("other2", password="x", role="researcher", organization="몽골국립의료원")


def test_thumbnails_follow_db_studies_not_patient_counts(researcher_client, samples):
    p = Patient.objects.get(pk=4)
    n = ImagingStudy.objects.filter(patient=p).count()
    body = researcher_client.get("/cases?id=4&tab=1").content.decode()
    assert f"영상 검사 {n}건" in body and body.count('class="thumb"') == min(n, 24)
    # 영상 수 파생 값만 바꿔도 목록은 DB 검사 그대로
    Patient.objects.filter(pk=4).update(cxr_count=99)
    assert researcher_client.get("/cases?id=4&tab=1").content.decode().count('class="thumb"') == min(n, 24)
    first = ImagingStudy.objects.filter(patient=p).order_by("study_date", "id").first()
    assert f'data-image="{first.pk}"' in body and "/static/imaging/" in body


def test_same_order_as_rap(researcher_client, samples):
    from apps.imaging.services import patient_studies

    p = Patient.objects.get(pk=4)
    order = [s.pk for s in patient_studies(p)]
    body = researcher_client.get("/cases?id=4&tab=1").content.decode()
    pos = [body.index(f'data-image="{pk}"') for pk in order[:24]]
    assert pos == sorted(pos)


def test_volume_modal_has_planes(researcher_client, samples):
    pet = ImagingStudy.objects.filter(patient_id=4, modality="PET-CT", is_public_sample=True).first()
    ct = ImagingStudy.objects.filter(patient_id=4, modality="CT", is_public_sample=True).first()
    body = researcher_client.get(f"/cases/4/images/{pet.pk}").content.decode()
    assert 'data-testid="plane-axial"' in body and 'data-testid="plane-coronal"' in body and 'data-testid="plane-mip"' in body
    assert "/static/imaging/petct/" in body and "TCIA Lung-PET-CT-Dx" in body and "SUVmax" in body
    body = researcher_client.get(f"/cases/4/images/{ct.pk}").content.decode()
    assert 'data-testid="plane-coronal"' in body and 'data-testid="plane-mip"' not in body


def test_volume_expert_annotation_bound_to_slice(researcher_client, samples):
    ct = ImagingStudy.objects.filter(patient_id=4, modality="CT", is_public_sample=True).first()
    r = researcher_client.post("/rap/annotations", {"study": ct.pk, "shape": "ellipse", "x": "10", "y": "10",
                                                    "width": "10", "height": "10", "finding_type": "결절",
                                                    "location": "", "severity": "", "text": "",
                                                    "plane": "axial", "slice": "7"})
    assert r.status_code == 201, r.content
    body = researcher_client.get(f"/cases/4/images/{ct.pk}").content.decode()
    assert 'class="expert-box ellipse"' in body and "=== 7" in body and "axial 8" in body


def test_uploaded_study_visible_to_submitter(researcher_client, demo_users, samples):
    p = Patient.objects.get(pk=3)
    st = make_uploaded_study(demo_users["researcher"], p)
    tab = researcher_client.get("/cases?id=3&tab=1").content.decode()
    assert f'src="/rap/files/{st.files.first().pk}"' in tab and 'data-testid="thumb-upload"' in tab
    body = researcher_client.get(f"/cases/3/images/{st.pk}").content.decode()
    assert f'/rap/files/{st.files.first().pk}' in body and "cxr.jpg" in body and "승인된 업로드 파일" in body
    assert "공동 · 우폐 · 중증" in body


def test_uploaded_study_hidden_from_others(client, demo_users, other_user, samples):
    p = Patient.objects.get(pk=3)
    st = make_uploaded_study(demo_users["researcher"], p)
    ExpertAnnotation.objects.create(study=st, shape="text", x=5, y=5, text="비밀 메모", author_name="x")
    client.force_login(other_user)
    tab = client.get("/cases?id=3&tab=1").content.decode()
    assert "/rap/files/" not in tab and "열람 권한 없음" in tab and 'data-testid="thumb-expert"' in tab
    body = client.get(f"/cases/3/images/{st.pk}").content.decode()
    assert "/rap/files/" not in body and "비밀 메모" not in body and "cxr.jpg" not in body
    assert 'data-testid="no-preview"' in body and 'data-testid="expert-hidden"' in body
    assert "공동" in body  # 메타데이터(소견)는 로그인 사용자에게 보인다(4단계 수용 정책)


def test_dicom_only_upload_shows_no_preview(researcher_client, demo_users, samples):
    p = Patient.objects.get(pk=3)
    st = make_uploaded_study(demo_users["researcher"], p, name="scan.dcm", data=b"DICM" * 10)
    tab = researcher_client.get("/cases?id=3&tab=1").content.decode()
    assert "미리보기 없음 (DICOM)" in tab
    body = researcher_client.get(f"/cases/3/images/{st.pk}").content.decode()
    assert "DICOM 미리보기는 지원하지 않습니다" in body and "scan.dcm" in body


def test_patient_without_studies(researcher_client, seeded_patients):
    body = researcher_client.get("/cases?id=4&tab=1").content.decode()  # 샘플 시드 전
    assert "영상 데이터가 없습니다" in body


def test_english_labels(researcher_client, samples):
    researcher_client.post("/lang", {"lang": "en"})
    body = researcher_client.get("/cases?id=4&tab=1").content.decode()
    assert "imaging studies" in body and "Public sample" in body
    st = ImagingStudy.objects.filter(patient_id=4, modality="CXR").first()
    assert "Expert Findings" in researcher_client.get(f"/cases/4/images/{st.pk}").content.decode()


def test_more_endpoint_uses_study_ids(researcher_client, samples):
    p = Patient.objects.get(pk=4)
    asset = ImagingStudy.objects.filter(patient=p, modality="CXR").first().sample_asset
    ImagingStudy.objects.bulk_create([ImagingStudy(patient=p, modality="CXR", study_date="2026-01-01",
                                                   is_public_sample=True, sample_asset=asset, seed_key=f"m:{k}")
                                      for k in range(30)])
    total = ImagingStudy.objects.filter(patient=p).count()
    more = researcher_client.get("/cases/4/images?offset=24").content.decode()
    assert more.count('class="thumb"') == min(24, total - 24)
    last_id = ImagingStudy.objects.filter(patient=p).order_by("study_date", "id").last().pk
    assert f'data-image="{last_id}"' in researcher_client.get(f"/cases/4/images?offset={total - 1}").content.decode()
