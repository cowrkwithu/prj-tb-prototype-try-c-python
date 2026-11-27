"""표준 데이터사전 적재 검증.

기대값 근거: 원본 src/data/standardDictionary.ts 헤더 주석
"원천: 261031-4개원천데이터사전_통합데이터사전_v3.2.0.xlsx … 통합 1,257 항목 / 19 카테고리" 와
DICTIONARY_CATEGORIES 의 fieldCount. 원본 저장소가 옆에 있으면 .ts 와 항목 단위로 전수 비교한다.
"""
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command

from apps.dictionary.models import DictionaryCategory, DictionaryField, DictionaryVersion
from apps.dictionary.services import load_dictionary, parse_workbook

EXPECTED_FIELD_COUNTS = {
    "pat": 49, "soc": 76, "scr": 24, "dx": 36, "lab": 61, "cul": 10, "dst": 194, "bio": 63, "tx": 156,
    "img": 47, "tpt": 27, "gen": 32, "ai": 72, "etc": 87, "saf": 113, "adh": 44, "pka": 70, "pro": 76, "fun": 20,
}
ORIGINAL_TS = settings.BASE_DIR.parent / "prj-tb-prototype-try-b" / "src" / "data" / "standardDictionary.ts"


@pytest.fixture(scope="module")
def parsed():
    return parse_workbook(settings.DICTIONARY_XLSX)


@pytest.mark.django_db
def test_load_counts_match_original():
    res = load_dictionary(settings.DICTIONARY_XLSX)
    assert res["version"] == "v3.2.0" and res["loaded"] is True
    assert (res["categories"], res["fields"], res["created"], res["updated"], res["deprecated"]) == (19, 1257, 1257, 0, 0)
    ver = DictionaryVersion.objects.get()
    assert ver.is_active
    assert DictionaryCategory.objects.count() == 19
    assert DictionaryField.objects.count() == 1257
    got = {c.code: c.fields.count() for c in DictionaryCategory.objects.all()}
    assert got == EXPECTED_FIELD_COUNTS
    assert list(DictionaryCategory.objects.values_list("code", flat=True)) == list(EXPECTED_FIELD_COUNTS)
    pat = DictionaryCategory.objects.get(code="pat")
    assert pat.name == "① 환자 기본정보" and pat.name_en == "① Patient Demographics"
    assert pat.description and pat.description_en and pat.icon == "Users" and pat.color == "blue"


@pytest.mark.django_db
def test_load_is_idempotent():
    load_dictionary(settings.DICTIONARY_XLSX)
    res = load_dictionary(settings.DICTIONARY_XLSX)
    assert res["loaded"] is False and res["skipped"] is True and res["fields"] == 1257
    assert DictionaryVersion.objects.count() == 1 and DictionaryField.objects.count() == 1257
    call_command("load_dictionary")
    assert DictionaryField.objects.count() == 1257


@pytest.mark.django_db
def test_force_reload_updates_in_place_and_deprecates_missing():
    load_dictionary(settings.DICTIONARY_XLSX)
    pks_before = dict(DictionaryField.objects.values_list("dictionary_id", "pk"))
    cat = DictionaryCategory.objects.get(code="pat")
    cat_pk = cat.pk
    # 엑셀에 없는 항목(예: 이전 버전에만 있던 UDE)과 관리자 수정을 흉내낸다
    extra = DictionaryField.objects.create(version=cat.version, category=cat, dictionary_id="TB.U.PAT.999",
                                           field_name="tb_u_pat_999", display_name="사라진 항목", data_type="TEXT")
    DictionaryField.objects.filter(dictionary_id="TB.U.PAT.001").update(display_name="임시 수정")

    res = load_dictionary(settings.DICTIONARY_XLSX, force=True)
    assert (res["created"], res["updated"], res["deprecated"]) == (0, 1257, 1)
    # 삭제·재생성 없이 같은 PK 유지 → 이후 단계의 FK 참조가 끊기지 않는다
    assert dict(DictionaryField.objects.exclude(pk=extra.pk).values_list("dictionary_id", "pk")) == pks_before
    assert DictionaryCategory.objects.get(code="pat").pk == cat_pk
    extra.refresh_from_db()
    assert extra.is_deprecated
    assert DictionaryField.objects.filter(is_deprecated=False).count() == 1257
    assert DictionaryField.objects.get(dictionary_id="TB.U.PAT.001").display_name != "임시 수정"


@pytest.mark.django_db
def test_force_reload_keeps_referenced_rows():
    """force 재적재가 버전·필드 행을 지우지 않는다 → 이를 FK 로 참조할 행이 CASCADE 로 사라지지 않는다."""
    load_dictionary(settings.DICTIONARY_XLSX)
    f = DictionaryField.objects.get(dictionary_id="TB.U.PAT.001")
    load_dictionary(settings.DICTIONARY_XLSX, force=True)
    assert DictionaryField.objects.filter(pk=f.pk).exists()
    assert DictionaryVersion.objects.filter(pk=f.version_id).exists()


@pytest.mark.django_db
def test_new_version_becomes_active(monkeypatch):
    import apps.dictionary.services as svc

    load_dictionary(settings.DICTIONARY_XLSX)
    real = svc.parse_workbook

    def fake(path):
        d = real(path)
        d["version"] = "v9.9.9"
        return d

    monkeypatch.setattr(svc, "parse_workbook", fake)
    load_dictionary(settings.DICTIONARY_XLSX, force=True)
    assert DictionaryVersion.objects.get(version="v9.9.9").is_active
    assert not DictionaryVersion.objects.get(version="v3.2.0").is_active
    assert DictionaryField.objects.count() == 2 * 1257  # 이전 버전 행 보존


@pytest.mark.django_db
def test_field_content_bilingual():
    load_dictionary(settings.DICTIONARY_XLSX)
    f = DictionaryField.objects.select_related("category").first()
    assert f.dictionary_id.startswith("TB.U.") and f.field_name.startswith("tb_u_")
    assert DictionaryField.objects.exclude(display_name_en="").count() > 1000
    sel = DictionaryField.objects.filter(data_type="SELECT")
    assert sel.exists()
    opt = sel.first().options[0]
    assert {"value", "label"} <= set(opt)
    assert DictionaryField.objects.filter(data_type="SELECT", options=[]).count() == 0
    assert all("options" not in c for c in DictionaryField.objects.values_list("constraints", flat=True))


def test_parse_matches_original_ts_exactly(parsed):
    if not ORIGINAL_TS.exists():
        pytest.skip("원본 저장소(prj-tb-prototype-try-b)가 없어 전수 비교를 건너뜀")
    from scripts.convert_mock_ts import extract_exports

    ts = extract_exports(ORIGINAL_TS.read_text(encoding="utf-8"))
    cats, ents = ts["DICTIONARY_CATEGORIES"], ts["STANDARD_DICTIONARY"]
    assert len(cats) == len(parsed["categories"]) == 19
    assert len(ents) == len(parsed["fields"]) == 1257
    for a, b in zip(cats, parsed["categories"]):
        exp = {"id": b["code"], "name": b["name"], "description": b["description"], "icon": b["icon"],
               "color": b["color"], "fieldCount": b["field_count"]}
        if b["name_en"]:
            exp["nameEn"] = b["name_en"]
        if b["description_en"]:
            exp["descriptionEn"] = b["description_en"]
        assert a == exp
    for a, b in zip(ents, parsed["fields"]):
        cons = dict(b["constraints"])
        if b["options"]:
            cons["options"] = b["options"]
        exp = {"dictionaryId": b["dictionary_id"], "category": b["category"], "fieldName": b["field_name"],
               "displayName": b["display_name"], "description": b["description"], "dataType": b["data_type"],
               "constraints": cons, "isStandard": True}
        for src, dst in (("display_name_en", "displayNameEn"), ("description_en", "descriptionEn"),
                         ("cdisc_mapping", "cdiscMapping")):
            if b[src]:
                exp[dst] = b[src]
        assert a == exp, b["dictionary_id"]


def test_seed_excel_present():
    assert Path(settings.DICTIONARY_XLSX).exists()
