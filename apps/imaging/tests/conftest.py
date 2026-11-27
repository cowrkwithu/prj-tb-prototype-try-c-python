import pytest
from django.core.files.base import ContentFile

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def _media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()
    return settings.MEDIA_ROOT


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def samples(seeded_patients):
    from apps.imaging.services import load_imaging_samples

    return load_imaging_samples()


@pytest.fixture
def other_user(db):
    from apps.accounts.models import User

    return User.objects.create_user("other", password="x", role="researcher", organization="몽골국립의료원")


def make_uploaded_study(submitter, patient, name="cxr.jpg", data=JPEG, modality="CXR"):
    """승인된 영상 업로드(파일 1개) + ImagingStudy 를 직접 만든다(업로드 흐름 자체는 apps/uploads 테스트가 검증)."""
    from apps.cases.models import ImagingStudy
    from apps.uploads.models import Submission, SubmissionFile

    n = Submission.objects.count() + 1
    sub = Submission.objects.create(upload_id=f"UPL-20250101-{n:03d}", submitter=submitter, domain="imaging",
                                    method="form", mode=modality, status="approved")
    sf = SubmissionFile(submission=sub, kind="image", original_name=name, size=len(data), sha256=f"{n:064d}")
    sf.file.save(f"x{n}{name[-4:]}", ContentFile(data), save=False)
    sf.save()
    st = ImagingStudy.objects.create(patient=patient, submission=sub, modality=modality, study_date="2025-06-01",
                                     findings=[{"finding": "Cavity", "location": "Right", "severity": "Severe",
                                                "description": "upper lobe"}])
    st.files.set([sf])
    return st
