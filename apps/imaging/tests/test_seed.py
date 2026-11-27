"""공개 샘플 영상 시드 — 멱등, 환자 영상 수와 일치, 원본 generateMockImages 규칙, 업로드 파생 값(fold) 비간섭."""
import pytest

from apps.cases import imaging as case_imaging
from apps.cases.models import ImagingStudy, Patient
from apps.core.models import SeedLog
from apps.imaging import services
from apps.imaging.models import ImagingAsset

pytestmark = pytest.mark.django_db


def test_counts_match_patients(samples):
    assert ImagingAsset.objects.count() == 7  # CXR 5 + CT/PET-CT 볼륨 2
    for p in Patient.objects.all():
        qs = ImagingStudy.objects.filter(patient=p, is_public_sample=True)
        assert qs.filter(modality="CXR").count() == p.cxr_count
        assert qs.filter(modality="CT").count() == p.ct_count
        assert qs.filter(modality="PET-CT").count() == p.pet_ct_count
    assert samples["studies"] == ImagingStudy.objects.count()


def test_same_rules_as_case_editor(samples):
    p = Patient.objects.get(pk=4)
    gen = case_imaging.generate_images(p)
    rows = list(ImagingStudy.objects.filter(patient=p).order_by("study_date", "id"))
    assert [r.study_date.isoformat() for r in rows] == [g["date"] for g in gen]
    assert [r.modality for r in rows] == [g["type"] for g in gen]
    cxr = [r for r in rows if r.modality == "CXR"]
    assert cxr[0].sample_asset.key == "cxr/CHNCXR_0448_1.jpg" and cxr[0].sample_asset.rois
    pet = next(r for r in rows if r.modality == "PET-CT")
    assert pet.suv_max == 5.3 and pet.sample_asset.key == "volume/petct" and pet.findings[0]["finding"] == "공동"


def test_idempotent_and_force(samples):
    n = ImagingStudy.objects.count()
    assert services.load_imaging_samples()["skipped"]
    forced = services.load_imaging_samples(force=True)
    assert forced["studies"] == 0 and forced["assets"] == 0 and ImagingStudy.objects.count() == n
    assert SeedLog.objects.filter(dataset="imaging_samples").count() == 1
    # 사용자가 지운 샘플은 bootstrap 재실행으로 되살아나지 않는다
    ImagingStudy.objects.filter(patient_id=1).delete()
    services.load_imaging_samples()
    assert not ImagingStudy.objects.filter(patient_id=1).exists()


def test_uploaded_studies_reduce_samples(seeded_patients, demo_users):
    from .conftest import make_uploaded_study

    p = Patient.objects.get(pk=3)  # CXR 2
    make_uploaded_study(demo_users["researcher"], p)
    services.load_imaging_samples()
    assert ImagingStudy.objects.filter(patient=p, is_public_sample=True, modality="CXR").count() == 1


def test_recompute_ignores_samples(samples):
    """업로드 재계산(fold)은 공개 샘플 검사를 접지 않는다 — 기준 스냅샷 이후에 샘플이 생겨도 영상 수 불변."""
    from apps.uploads.models import DerivedBaseline
    from apps.uploads.services import derived

    p = Patient.objects.get(pk=2)
    DerivedBaseline.objects.create(patient=p, domain="imaging", values=derived._snapshot(p, "imaging"),
                                   preexisting_ids=[], legacy_mixed=False)
    before = (p.cxr_count, p.ct_count, p.pet_ct_count)
    assert derived.recompute_patient_derived(p, "imaging")
    assert (p.cxr_count, p.ct_count, p.pet_ct_count) == before


def test_dataset_last_update_excludes_samples(samples):
    from apps.datasharing.services import dataset_stats

    st = dataset_stats("cxr-images", "")
    assert st["total"] == sum(Patient.objects.values_list("cxr_count", flat=True))
    latest_case = max(Patient.objects.filter(has_cxr=True).values_list("last_update_date", flat=True))
    assert st["last"] == latest_case
