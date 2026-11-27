"""약제내성 예측 — 입력 해석(VCF/CSV/TSV)·검증 상한·카탈로그 대조·규칙 3종·실행 기록."""
import pytest

from apps.cases.models import Patient
from apps.genomics import prediction as pr
from apps.genomics import services as s
from apps.genomics.models import PredictionRun

pytestmark = pytest.mark.django_db

VCF = """##fileformat=VCFv4.2
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
NC_000962.3\t761155\t.\tC\tT\t50\tPASS\t.
NC_000962.3\t2155168\t.\tC\tG\t50\tPASS\t.
NC_000962.3\t7582\t.\tA\tG\t50\tLowQual\t.
NC_000962.3\t4247500\t.\tA\tC,T\t50\t.\t.
NC_000962.3\t3000000\t.\tG\tA\t50\tPASS\t.
NC_000962.3\tabc\t.\tG\tA\t50\tPASS\t.
"""


def test_parse_vcf():
    p = pr.parse_input(VCF, "x.vcf")
    assert p.fmt == "vcf" and p.rows == 6
    assert {(o["position"], o["alt"]) for o in p.observed} == {(761155, "T"), (2155168, "G"), (4247500, "C"),
                                                               (4247500, "T"), (3000000, "A")}
    assert p.skipped == {"filtered": 1, "malformed": 1}


def test_parse_csv_tsv_and_headerless():
    csv_ = pr.parse_input("gene,change,note\nrpoB,S450L,x\nkatG,S315T,\n,,\nbad\n", "a.csv")
    assert csv_.fmt == "csv" and [o["change"] for o in csv_.observed] == ["S450L", "S315T"]
    assert csv_.skipped["malformed"] == 1
    tsv = pr.parse_input("mutation\tother\nrpoB S450L\t1\n", "a.tsv")
    assert tsv.fmt == "tsv" and tsv.observed[0]["gene"] == "rpoB"
    pos = pr.parse_input("position,ref,alt\n761155,C,T\n", "p.csv")
    assert pos.observed == [{"kind": "position", "position": 761155, "ref": "C", "alt": "T"}]
    bare = pr.parse_input("rpoB S450L\nkatG S315T\n")
    assert len(bare.observed) == 2


@pytest.mark.parametrize("text,code", [
    ("", "변이 행이 없습니다"),
    ("a\n" * (pr.MAX_ROWS + 1), "행이 너무 많습니다"),
    ("x" * (pr.MAX_LINE + 1), "한 줄이 너무 깁니다"),
    ("가" * (pr.MAX_BYTES // 3 + 10), "입력이 너무 큽니다"),
    ("rpoB S450L\x00", "허용되지 않는 문자"),
    ("rpoB \ud800", "허용되지 않는 문자"),
])
def test_input_limits(text, code):
    with pytest.raises(pr.InputError) as e:
        pr.parse_input(text, "x.csv")
    assert code in e.value.ko and e.value.en


def test_decode_upload():
    assert pr.decode_upload("﻿rpoB S450L".encode("utf-8")) == "rpoB S450L"
    for raw in (b"\xff\xfe\x00a", "가".encode("cp949"), b"a" * (pr.MAX_BYTES + 1)):
        with pytest.raises(pr.InputError):
            pr.decode_upload(raw)


def test_resolve_catalog_alias_and_uncharacterized(catalog):
    cat = s.load_catalog()
    obs = pr.parse_input("rpoB S531L\nrpoB S450L\nkatG Q999X\nfooA A1B\n").observed
    obs += pr.parse_input(VCF, "x.vcf").observed
    res = pr.resolve(obs, cat)
    by = {r["label"]: r for r in res}
    assert by["rpoB S450L"]["status"] == "catalog" and by["rpoB S450L"]["input"] == "rpoB S531L"
    assert sum(1 for r in res if r["label"] == "rpoB S450L") == 1          # 별칭·VCF 좌표 중복 제거
    assert by["katG S315T"]["status"] == "catalog"
    assert by["katG Q999X"]["status"] == "uncharacterized"
    assert by["fooA A1B"]["status"] == "outside"
    assert by["4247500 A>C"]["status"] == "uncharacterized" and by["4247500 A>C"]["gene"] == "embB"
    assert by["3000000 G>A"]["status"] == "outside"


def _calls(rows):
    return {r["drug"]: {a: c["call"] for a, c in r["calls"].items()} | {"consensus": r["consensus"]} for r in rows}


def test_rules_differ_by_algorithm(catalog):
    cat = s.load_catalog()
    obs = pr.parse_input("rpoB S450L\nrpoB L430P\nembB G406S\npncA Q10X\n").observed
    res = pr.resolve(obs, cat)
    allg = {g.gene for g in cat.genes}
    rows = _calls(pr.predict(res, allg, pr.ALGO_IDS, cat))
    assert rows["Rifampicin"] == {"tb-profiler": "R", "mykrobe": "R", "resfinder": "U", "consensus": "R"}
    # G406S 는 근거 '중간' — Mykrobe 규칙은 U, TB-Profiler 규칙은 R
    assert rows["Ethambutol"]["tb-profiler"] == "R" and rows["Ethambutol"]["mykrobe"] == "U"
    # pncA 미분류 변이 → 연관 유전자에 변이가 있으므로 어느 규칙도 S 가 아니다(불변식)
    assert rows["Pyrazinamide"]["tb-profiler"] == "U" and rows["Pyrazinamide"]["mykrobe"] == "U"
    assert rows["Isoniazid"] == {"tb-profiler": "S", "mykrobe": "S", "resfinder": "U", "consensus": "S"}
    # 검사되지 않은 유전자 → U
    only_rpob = _calls(pr.predict(res, {"rpoB"}, ["tb-profiler"], cat))
    assert only_rpob["Isoniazid"]["tb-profiler"] == "U" and only_rpob["Rifampicin"]["tb-profiler"] == "R"


def test_run_from_patient_saves(catalog, demo_users):
    p = Patient.objects.get(pk=4)  # rpoB S450L, katG S315T, gyrA D94G, rrs a1401g / WGS
    run = pr.run_prediction(demo_users["researcher"], algorithms=["tb-profiler", "zzz"], patient=p)
    assert run.algorithms == ["tb-profiler"] and run.sample_name == p.condition_id and run.lineage == "L2.2.1"
    calls = {r["drug"]: r["consensus"] for r in run.results["drugs"]}
    assert calls["Rifampicin"] == calls["Isoniazid"] == calls["Fluoroquinolones"] == calls["Amikacin"] == "R"
    assert calls["Ethambutol"] == "S" and calls["Pyrazinamide"] == "S"
    assert len(run.inputs["tested_genes"]) == 10


def test_run_upload_and_wgs_flag(catalog, demo_users):
    u = demo_users["researcher"]
    run = pr.run_prediction(u, algorithms=["tb-profiler"], text="rpoB S450L\n", filename="s.csv", assume_wgs=False)
    calls = {r["drug"]: r["consensus"] for r in run.results["drugs"]}
    assert calls["Rifampicin"] == "R" and calls["Isoniazid"] == "U"
    run2 = pr.run_prediction(u, algorithms=["tb-profiler"], text="rpoB S450L\n", filename="", assume_wgs=True)
    assert {r["drug"]: r["consensus"] for r in run2.results["drugs"]}["Isoniazid"] == "S"
    assert run2.sample_name == "pasted_variants"
    with pytest.raises(pr.InputError):
        pr.run_prediction(u, algorithms=[], text="rpoB S450L")


def test_history_retention_and_visibility(catalog, demo_users, other_user, monkeypatch):
    monkeypatch.setattr(pr, "RUNS_KEPT_PER_USER", 3)
    u = demo_users["researcher"]
    for _ in range(5):
        pr.run_prediction(u, algorithms=["mykrobe"], text="katG S315T")
    pr.run_prediction(other_user, algorithms=["mykrobe"], text="katG S315T")
    assert PredictionRun.objects.filter(user=u).count() == 3
    assert pr.visible_runs(u).count() == 3
    assert pr.visible_runs(demo_users["admin"]).count() == 4


# ─── 1차 리뷰 CONFIRMED: 연관 유전자에 다른 약제의 카탈로그 변이가 있는데 S 로 판정 ───
def test_review_cases_kanamycin_and_ethionamide(catalog, demo_users):
    u = demo_users["researcher"]
    p4 = Patient.objects.get(condition_id="KR-2024-0004")   # rrs a1401g 보유
    run = pr.run_prediction(u, algorithms=pr.ALGO_IDS, patient=p4)
    km = next(r for r in run.results["drugs"] if r["drug"] == "Kanamycin")
    assert km["calls"]["tb-profiler"]["call"] == "R" and km["calls"]["mykrobe"]["call"] == "R"
    assert km["consensus"] == "R" and "rrs a1401g" in km["calls"]["tb-profiler"]["evidence"]
    p13 = Patient.objects.get(condition_id="KR-2024-0013")
    assert "inhA c-15t" in p13.resistance_mutations
    run = pr.run_prediction(u, algorithms=pr.ALGO_IDS, patient=p13)
    eth = next(r for r in run.results["drugs"] if r["drug"] == "Ethionamide")
    assert eth["calls"]["tb-profiler"]["call"] == "R" and eth["consensus"] == "R"


def test_variant_drugs_seed_and_resolve(catalog):
    from apps.genomics.models import Variant

    assert Variant.objects.get(label="rrs a1401g").drugs == ["Amikacin", "Kanamycin", "Capreomycin"]
    assert Variant.objects.get(label="inhA c-15t").drugs == ["Isoniazid", "Ethionamide"]
    assert Variant.objects.get(label="eis c-14t").drugs == ["Kanamycin"]
    assert Variant.objects.get(label="rpoB S450L").drugs == ["Rifampicin"]
    # 기존 DB(시드 보정): drugs 를 비운 뒤 force 시드 → 다시 채움(멱등)
    Variant.objects.filter(label="rrs a1401g").update(drugs=[])
    s.load_genomics(force=True)
    s.load_genomics(force=True)
    assert Variant.objects.get(label="rrs a1401g").drugs == ["Amikacin", "Kanamycin", "Capreomycin"]


def test_invariant_no_S_when_linked_gene_mutated(catalog):
    """불변식: 약제의 연관 유전자에서 어떤 변이든 검출되면 그 약제는 S 가 아니다 — 모든 약제·알고리즘·변이에 대해.
    그 약제의 카탈로그 변이면 TB-Profiler 는 R."""
    cat = s.load_catalog()
    allg = {g.gene for g in cat.genes}
    drug_genes = cat.drug_genes()
    inputs = [f"{v.gene.gene} {s.strip_change(v.amino_acid_change)}" for v in cat.variants]
    inputs += [f"{g.gene} Z999Z" for g in cat.genes]  # 유전자마다 미분류 변이
    for text in inputs:
        res = pr.resolve(pr.parse_input(text).observed, cat)
        assert res and res[0]["gene"] in allg, text
        gene = res[0]["gene"]
        for row in pr.predict(res, allg, pr.ALGO_IDS, cat):
            calls = {a: c["call"] for a, c in row["calls"].items()} | {"consensus": row["consensus"]}
            if gene in drug_genes.get(row["drug"], []):
                assert "S" not in calls.values(), (text, row["drug"], calls)
                if res[0]["status"] == "catalog" and row["drug"] in res[0]["drugs"]:
                    assert calls["tb-profiler"] == "R", (text, row["drug"])
            elif res[0]["status"] == "catalog" and row["drug"] in res[0]["drugs"]:
                assert calls["tb-profiler"] == "R", (text, row["drug"])
