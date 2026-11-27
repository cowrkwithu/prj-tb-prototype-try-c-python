import json
from datetime import date

import pytest
from django.conf import settings
from django.core.management import call_command

from apps.accounts.models import User
from apps.cases.models import DSTResult, Patient
from apps.cases.services import load_patients
from apps.core.models import SeedLog
from apps.dictionary.models import DictionaryField

pytestmark = pytest.mark.django_db

SEED = json.loads((settings.SEED_DIR / "patients.json").read_text(encoding="utf-8"))


def test_seed_json_has_all_original_patients():
    assert len(SEED) == 50
    assert len({p["conditionId"] for p in SEED}) == 50


def test_load_patients_counts():
    res = load_patients()
    assert res == {"created": 50, "existing": 0, "skipped": False, "total": 50}
    assert DSTResult.objects.count() == sum(len(p["lab"]["dstResults"]) for p in SEED)
    assert SeedLog.objects.filter(dataset="patients").exists()


def test_load_patients_idempotent_and_ids_preserved():
    load_patients()
    res = load_patients()
    assert res["skipped"] and Patient.objects.count() == 50
    p2 = Patient.objects.get(pk=2)
    assert p2.condition_id == "KR-2024-0002"
    assert p2.comorbidities == ["당뇨"] and p2.resistance_mutations == ["rpoB S450L", "katG S315T"]
    assert p2.has_pet_ct and p2.pet_ct_count == 1 and p2.cxr_findings == ["공동", "침윤", "섬유화"]
    assert list(p2.dst_results.values_list("drug", "result")) == [
        ("INH", "R"), ("RIF", "R"), ("EMB", "S"), ("PZA", "S"), ("FQ", "S")]
    p5 = Patient.objects.get(condition_id="KR-2024-0005")
    assert p5.sra_id is None and p5.lineage is None and not p5.has_sequence


def test_new_patient_after_seed_gets_fresh_id():
    load_patients()
    p = Patient.objects.create(
        condition_id="NEW-0001", age=40, sex="M", country="한국", site="테스트", diagnosis_type="DS-TB",
        treatment_outcome="Cured", treatment_duration=180, registration_date=date(2025, 1, 1),
        last_update_date=date(2025, 1, 1))
    assert p.pk > 50
    DSTResult.objects.create(patient=p, drug="INH", result="S")


def test_sequence_reset_called(monkeypatch):
    called = []
    import apps.cases.services as svc

    monkeypatch.setattr(svc, "reset_sequences", lambda *m: called.append(m))
    load_patients()
    assert called == [(Patient, DSTResult)]


def test_deleted_patient_not_restored_by_bootstrap():
    call_command("bootstrap")
    Patient.objects.filter(condition_id="KR-2024-0002").delete()
    call_command("bootstrap")
    assert not Patient.objects.filter(condition_id="KR-2024-0002").exists()
    assert Patient.objects.count() == 49
    call_command("seed_demo")
    assert Patient.objects.count() == 49
    call_command("seed_demo", "--force")
    assert Patient.objects.filter(condition_id="KR-2024-0002").exists() and Patient.objects.count() == 50


def test_force_does_not_overwrite_edits():
    load_patients()
    Patient.objects.filter(pk=1).update(site="수정됨")
    load_patients(force=True)
    assert Patient.objects.get(pk=1).site == "수정됨"


def test_bootstrap_idempotent():
    call_command("bootstrap")
    call_command("bootstrap")
    assert User.objects.count() == 3
    assert Patient.objects.count() == 50
    assert DictionaryField.objects.count() == 1257
    assert set(SeedLog.objects.values_list("dataset", flat=True)) == {"accounts", "patients", "dictionary", "home_content",
                                                                      "cohorts", "schemas", "genomics_catalog",
                                                                      "imaging_samples"}
