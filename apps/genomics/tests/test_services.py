"""카탈로그 시드·매칭 규칙·DB 집계(빈도·샘플 수)·검색 조건·유전체 트랙."""
import json

import pytest
from django.conf import settings
from django.http import QueryDict

from apps.cases.models import Patient
from apps.core.models import SeedLog
from apps.genomics import services as s
from apps.genomics.models import ResistanceGene, Variant

pytestmark = pytest.mark.django_db
SEED = json.loads((settings.SEED_DIR / "genomics.json").read_text(encoding="utf-8"))


def test_seed_idempotent_and_matches_original(catalog):
    assert ResistanceGene.objects.count() == 10 and Variant.objects.count() == 20
    again = s.load_genomics()
    assert again["skipped"] is True
    forced = s.load_genomics(force=True)
    assert forced["genes"] == 0 and forced["variants"] == 0 and Variant.objects.count() == 20
    assert SeedLog.objects.filter(dataset="genomics_catalog").count() == 1
    for i, v in enumerate(SEED["variants"]):
        db = Variant.objects.get(order=i)
        assert (db.gene.gene, db.position, db.ref_allele, db.alt_allele, db.drug_resistance) == (
            v["gene"], v["position"], v["refAllele"], v["altAllele"], v["drugResistance"])
    # 원본 inhA 'c-15t (promoter)' → 환자 표기 'inhA c-15t'
    assert Variant.objects.get(order=4).label == "inhA c-15t"
    rpob = ResistanceGene.objects.get(gene="rpoB")
    assert (rpob.start, rpob.end, rpob.strand) == (759807, 763325, "+")
    assert ResistanceGene.objects.get(gene="katG").strand == "-"


def test_seed_does_not_resurrect_deleted(catalog):
    Variant.objects.filter(label="embB Q497H").delete()
    s.load_genomics()
    assert not Variant.objects.filter(label="embB Q497H").exists()


def test_parse_and_keys():
    assert s.parse_mutation("rpoB S450L") == ("rpoB", "S450L")
    assert s.parse_mutation("  inhA   c-15t (promoter) ") == ("inhA", "c-15t")
    for bad in ("", "rpoB", "rpoB S450L extra", "1abc S450L", None, 5, "a" * 300, "rpoB S4\x0050L"):
        assert s.parse_mutation(bad) is None
    assert s.key_of("RPOB s450l") == s.key_of("rpoB S450L") == "rpob s450l"


def test_ecoli_numbering():
    assert s.to_ecoli("rpoB", "S450L") == "S531L"
    assert s.to_ecoli("gyrA", "D94G") == "D87G"
    assert s.to_ecoli("gyrB", "E540D") == "E466D"
    assert s.to_ecoli("rrs", "a1401g") == "a1408g"
    assert s.to_ecoli("katG", "S315T") is None and s.to_ecoli("inhA", "c-15t") is None


def test_gene_at_regions():
    assert s.gene_at(761155) == "rpoB"
    assert s.gene_at(759807 - 15) == "rpoB"                # + 가닥 상류 프로모터
    assert s.gene_at(2156111 + 10) == "katG"               # − 가닥 상류(끝점 뒤)
    assert s.gene_at(763400) == "rpoC"                     # 이웃 유전자 몸체
    assert s.gene_at(3_000_000) == ""


def test_variant_stats_from_patients(catalog):
    cat = s.load_catalog()
    st = s.variant_stats(cat)
    seqd = Patient.objects.filter(s.genomic_patients_q()).count()
    assert st.denominator == seqd == 38
    by = {v.label: v for v in cat.variants}
    # 시드 환자 표기 집계(원본 sampleCount 614 등 시드 숫자가 아님)
    assert st.count(by["rpoB S450L"]) == 17 and st.count(by["katG S315T"]) == 20
    assert st.count(by["embB M306V"]) == 0
    assert st.freq(by["rpoB S450L"]) == pytest.approx(17 / 38)
    # 새 환자 표기 → 즉시 반영(대소문자·E. coli 번호 별칭)
    p = Patient.objects.get(pk=1)
    p.resistance_mutations = ["RPOB s531l", "embB M306V", "fooX A1B"]
    p.save()
    st2 = s.variant_stats(s.load_catalog())
    assert st2.count(by["rpoB S450L"]) == 18 and st2.count(by["embB M306V"]) == 1
    assert st2.uncatalogued["fooX A1B"] == 1


def test_carriers_returns_raw_labels(catalog):
    p = Patient.objects.get(pk=1)
    p.resistance_mutations = ["rpoB S531L"]
    p.save()
    cat = s.load_catalog()
    v = next(x for x in cat.variants if x.label == "rpoB S450L")
    ids, raw = s.carriers(cat, [v])
    assert 1 in ids and len(ids) == 18
    assert "rpoB S531L" in raw and "rpoB S450L" in raw


def test_parse_search_rejects_bad_values(catalog):
    cat = s.load_catalog()
    p = s.parse_search(QueryDict("gene=rpob&drug=rifampicin&aa=S450&pos_min=1&pos_max=800000&min_freq=10"), cat)
    assert not p.errors and p.gene == "rpoB" and p.drug == "Rifampicin" and p.min_freq == 10
    res = s.search_variants(cat, s.variant_stats(cat), p)
    assert [v.label for v in res] == ["rpoB S450L"]
    bad = s.parse_search(QueryDict("gene=zzz&drug=x&pos_min=-1&pos_max=99999999999&min_freq=101&aa=" + "a" * 50), cat)
    assert len(bad.errors) >= 5
    swapped = s.parse_search(QueryDict("pos_min=10&pos_max=5"), cat)
    assert swapped.errors and swapped.pos_min is None


def test_search_by_ecoli_alias(catalog):
    cat = s.load_catalog()
    p = s.parse_search(QueryDict("aa=S531L"), cat)
    assert [v.label for v in s.search_variants(cat, s.variant_stats(cat), p)] == ["rpoB S450L"]
    assert [v.label for v in s.browser_filter(cat, "s531l")] == ["rpoB S450L"]


def test_tracks(catalog):
    cat = s.load_catalog()
    st = s.variant_stats(cat)
    tr = s.genome_track(cat, st)
    assert len(tr["genes"]) == 10 and all(0 <= g["x"] <= 1000 for g in tr["genes"])
    rpob = ResistanceGene.objects.get(gene="rpoB")
    z = s.gene_track(cat, st, rpob)
    assert {m["position"] for m in z["marks"]} >= {761155, 761139, 761110, 761095}
    assert all(0 <= m["x"] <= 1000 for m in z["marks"])
    top = max(z["marks"], key=lambda m: m["count"])
    assert top["count"] == 17 and top["h"] == pytest.approx(60)
