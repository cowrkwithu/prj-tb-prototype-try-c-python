"""GWAS(Fisher·Bonferroni·표본 부족·범위·상한)와 유사도(대칭·대각 0·방법별 정의)."""
import math
import time

import pytest

from apps.cases.models import DSTResult, Patient
from apps.cohorts.services import fisher_exact
from apps.genomics import analysis as an

pytestmark = pytest.mark.django_db


def test_gwas_rif_matches_manual_fisher(catalog):
    g = an.gwas("RIF")
    n_r = DSTResult.objects.filter(drug="RIF", result="R").count()
    n_s = DSTResult.objects.filter(drug="RIF", result="S").count()
    assert (g["n_r"], g["n_s"]) == (n_r, n_s) == (23, 27)
    assert g["tests"] == 8 and g["threshold"] == pytest.approx(0.05 / 8)
    top = g["points"][0]
    assert top["label"] in ("katG S315T", "rpoB S450L") and top["significant"]
    s450 = next(p for p in g["points"] if p["label"] == "rpoB S450L")
    _, p = fisher_exact(s450["a"], s450["b"], s450["c"], s450["d"])
    assert s450["p"] == p and s450["neglog"] == pytest.approx(-math.log10(p))
    assert s450["position"] == 761155 and not s450["approx"] and s450["ecoli"] == "S531L"
    assert [p["p"] for p in g["points"]] == sorted(p["p"] for p in g["points"])
    assert g["significant"] == sum(1 for p in g["points"] if p["p"] < g["threshold"])


def test_gwas_insufficient_and_scope(catalog):
    assert an.gwas("EMB")["insufficient"]          # R 1명
    assert an.gwas("ZZZ")["insufficient"] and an.gwas("ZZZ")["n"] == 0
    scoped = an.gwas("RIF", {1, 2, 3})
    assert scoped["n"] == 3 and scoped["insufficient"]


def test_gwas_offcatalog_position_is_approximate(catalog):
    p = Patient.objects.get(pk=2)
    p.resistance_mutations = list(p.resistance_mutations) + ["katG Q999X", "zzz A1B"]
    p.save()
    g = an.gwas("RIF")
    q = next(x for x in g["points"] if x["label"] == "katG Q999X")
    assert q["approx"] and q["position"] == (2153889 + 2156111) // 2 and not q["catalog"]
    z = next(x for x in g["points"] if x["label"] == "zzz A1B")
    assert z["position"] is None


def test_gwas_caps_and_speed(catalog, monkeypatch):
    """대상자 1,000명 × 서로 다른 변이 600종 → 검정 상한 MAX_TESTS, 수 초 이내."""
    monkeypatch.setattr(an, "MAX_TESTS", 500)
    rows = []
    for i in range(1000):
        rows.append(Patient(condition_id=f"ZZ-9999-{i:04d}", age=30, sex="M", country="한국", site="x",
                            diagnosis_type="MDR-TB", treatment_outcome="Cured", treatment_duration=1,
                            resistance_mutations=[f"rpoB X{i % 600}Y", "katG S315T"],
                            registration_date="2024-01-01", last_update_date="2024-01-01"))
    Patient.objects.bulk_create(rows)
    ids = list(Patient.objects.filter(condition_id__startswith="ZZ-9999").values_list("id", flat=True))
    DSTResult.objects.bulk_create([DSTResult(patient_id=pid, drug="RIF", result="R" if k % 2 else "S")
                                   for k, pid in enumerate(ids)])
    t0 = time.monotonic()
    g = an.gwas("RIF")
    assert time.monotonic() - t0 < 5
    assert g["tests"] == 500 and g["skipped_tests"] > 0


def test_similarity_properties(catalog):
    pats = list(Patient.objects.filter(has_sequence=True).order_by("id")[:12])
    for method in an.METHOD_IDS:
        r = an.similarity(pats, method)
        m = r["matrix"]
        assert r["n"] == 12
        for i in range(12):
            assert m[i][i] == 0
            for j in range(12):
                assert m[i][j] == m[j][i] and m[i][j] >= 0
        if method == "jaccard":
            assert all(0 <= x <= 1 for row in m for x in row)


def test_similarity_definitions():
    a, b = frozenset({"x", "y"}), frozenset({"y", "z"})
    assert an.distance("snp", a, b, None, None) == 2
    assert an.distance("jaccard", a, b, None, None) == pytest.approx(1 - 1 / 3)
    assert an.distance("jaccard", frozenset(), frozenset(), None, None) == 0
    assert an.distance("lineage", a, b, "L2", "L4") == 2 + an.LINEAGE_PENALTY
    assert an.distance("lineage", a, b, "L2", None) == 2


def test_similarity_uses_normalized_keys(catalog):
    p1, p2 = Patient.objects.get(pk=2), Patient.objects.get(pk=6)
    p1.resistance_mutations, p2.resistance_mutations = ["rpoB S450L"], ["RPOB s531l"]
    p1.lineage = p2.lineage = "L2.2.1"
    r = an.similarity([p1, p2], "snp")
    assert r["matrix"][0][1] == 0
