"""데이터셋 탭 — DB 집계 카드, 데이터 구조 모달(데이터사전), 내보내기(승인 강제·형식·수식 주입·스트리밍), 커스텀 데이터셋."""
import csv
import io

import openpyxl
import pytest
from django.db.models import Sum

from apps.cases.models import DSTResult, Patient, Regimen, RegimenDrug
from apps.datasharing import catalog, services

from .conftest import make_dar

pytestmark = pytest.mark.django_db


def _csv(resp):
    text = b"".join(resp.streaming_content).decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))


def test_dataset_cards_are_db_aggregates(researcher_client, seeded_patients, demo_users):
    body = researcher_client.get("/data").content.decode()
    for ds in catalog.DATASETS:
        assert f'data-dataset="{ds["slug"]}"' in body
    n = Patient.objects.count()
    cards = {c["slug"]: c for c in services.dataset_cards(demo_users["researcher"], "ko")}
    assert cards["patient-cases"]["total"] == n and cards["patient-cases"]["records"] == f"{n:,}건"
    mine = Patient.objects.filter(site="분당서울대병원").count()
    assert cards["patient-cases"]["mine"] == mine and cards["patient-cases"]["pct"] == round(mine / n * 100)
    assert cards["cxr-images"]["total"] == Patient.objects.aggregate(s=Sum("cxr_count"))["s"]
    assert cards["cxr-images"]["records"].endswith("장")
    assert cards["ct-studies"]["records"].endswith(" 스터디")
    assert cards["dst-results"]["total"] == DSTResult.objects.count()
    assert cards["regimens"]["total"] == RegimenDrug.objects.count()
    # 원본 하드코딩 수치를 쓰지 않는다
    assert "2,847건" not in body and "15,392건" not in body and "48.2 GB" not in body
    en = services.dataset_cards(demo_users["researcher"], "en")
    assert en[4]["records"].endswith(" images")


def test_counts_follow_db_changes(demo_users, seeded_patients):
    user = demo_users["researcher"]
    before = {c["slug"]: c["total"] for c in services.dataset_cards(user, "ko")}
    p = Patient.objects.first()
    reg = Regimen.objects.create(patient=p)
    RegimenDrug.objects.create(regimen=reg, drug_name="Bedaquiline", drug_code="Bdq", start_day=0, end_day=10)
    Patient.objects.filter(pk=p.pk).update(ct_count=p.ct_count + 3, has_ct=True)
    after = {c["slug"]: c["total"] for c in services.dataset_cards(user, "ko")}
    assert after["regimens"] == before["regimens"] + 1 and after["ct-studies"] == before["ct-studies"] + 3


def test_schema_modal_reads_dictionary(researcher_client, loaded_dictionary):
    body = researcher_client.get("/data/datasets/patient-cases/schema").content.decode()
    assert 'data-testid="schema-modal"' in body and "출처: 표준 데이터사전" in body
    assert "TB.U.PAT.001" in body and "포함 카테고리" in body
    # 레지멘 항목은 임상에서 빠지고 Regimens 에 들어간다(원본 datasetDictionaryMap)
    from apps.dictionary.viewer import current_fields

    entries = list(current_fields())
    pc = catalog.dictionary_entries_for_dataset("patient-cases", entries)
    rg = catalog.dictionary_entries_for_dataset("regimens", entries)
    assert pc and rg and not ({e.pk for e in pc} & {e.pk for e in rg if e.category.code == "tx"})
    assert all(e.category.code in ("tx", "adh") for e in rg)
    dst = catalog.dictionary_entries_for_dataset("dst-results", entries)
    assert all(e.category.code == "dst" or "Xpert/mWRD" in e.display_name for e in dst)
    cxr = {e.dictionary_id for e in catalog.dictionary_entries_for_dataset("cxr-images", entries)}
    ct = {e.dictionary_id for e in catalog.dictionary_entries_for_dataset("ct-studies", entries)}
    assert "TB.U.IMG.035" in ct and "TB.U.IMG.035" not in cxr and "TB.U.IMG.026" in cxr and "TB.U.IMG.026" not in ct
    assert "TB.U.AI.001" in ct and "TB.U.AI.002" in cxr and "TB.U.AI.002" not in ct
    # PET-CT 는 사전 항목이 없어 빈 상태
    body = researcher_client.get("/data/datasets/pet-ct-studies/schema").content.decode()
    assert 'data-testid="schema-empty"' in body and "정의된 필드가 없습니다" in body
    assert researcher_client.get("/data/datasets/nope/schema").status_code == 404


def test_download_requires_approval_server_side(researcher_client, demo_users, seeded_patients):
    for slug in ("patient-cases", "genomics", "cxr-images"):
        for ext in ("csv", "xlsx"):
            r = researcher_client.get(f"/data/datasets/{slug}/download.{ext}")
            assert r.status_code == 403 and 'data-testid="download-denied"' in r.content.decode()
    make_dar(demo_users["researcher"], ["clinical"])  # 심사 중
    r = researcher_client.get("/data/datasets/patient-cases/download.csv")
    assert r.status_code == 403 and 'data-state="pending"' in r.content.decode()
    make_dar(demo_users["researcher"], ["imaging"], "approved")
    assert researcher_client.get("/data/datasets/patient-cases/download.csv").status_code == 403  # 유형별
    assert researcher_client.get("/data/datasets/cxr-images/download.csv").status_code == 200
    assert researcher_client.get("/data/datasets/nope/download.csv").status_code == 404


def test_csv_export_format(researcher_client, demo_users, seeded_patients):
    make_dar(demo_users["researcher"], ["clinical", "genomics", "imaging"], "approved")
    r = researcher_client.get("/data/datasets/patient-cases/download.csv")
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    assert "Patient_Cases_deidentified.csv" in r["Content-Disposition"]
    rows = _csv(r)
    assert rows[0] == ["condition_id", "age", "sex", "country", "diagnosis_type", "treatment_outcome", "treatment_days"]
    assert len(rows) - 1 == Patient.objects.count()
    p = Patient.objects.order_by("id").first()
    assert rows[1] == [p.condition_id, str(p.age), p.sex, p.country, p.diagnosis_type, p.treatment_outcome,
                       str(p.treatment_duration)]
    rows = _csv(researcher_client.get("/data/datasets/dst-results/download.csv"))
    assert rows[0] == ["condition_id", "drug", "result"] and len(rows) - 1 == DSTResult.objects.count()
    rows = _csv(researcher_client.get("/data/datasets/cxr-images/download.csv"))
    assert rows[0] == ["condition_id", "cxr_count", "findings"]
    assert len(rows) - 1 == Patient.objects.filter(has_cxr=True).count()
    rows = _csv(researcher_client.get("/data/datasets/genomics/download.csv"))
    assert rows[0][:2] == ["gene", "position"] and len(rows) - 1 == services.export_row_count("genomics")
    r = researcher_client.get("/data/datasets/genomics/download.xlsx")
    assert "Genomics_deidentified.xlsx" in r["Content-Disposition"]
    # 비식별: 내보내기 어디에도 기관·등록일 같은 추가 항목이 없다
    assert "site" not in rows[0]


def test_formula_injection_csv_and_xlsx(researcher_client, demo_users, seeded_patients):
    make_dar(demo_users["researcher"], ["clinical"], "approved")
    p = Patient.objects.order_by("id").first()
    Patient.objects.filter(pk=p.pk).update(condition_id="=HYPERLINK(\"http://x\")", comorbidities=["+cmd|' /C calc'!A0"])
    reg = Regimen.objects.create(patient_id=p.pk)
    RegimenDrug.objects.create(regimen=reg, drug_name="@SUM(1)", drug_code="-2+3", start_day=0, end_day=5,
                               dosage="\x01\x02=1+1")
    rows = _csv(researcher_client.get("/data/datasets/patient-cases/download.csv"))
    assert rows[1][0] == "'=HYPERLINK(\"http://x\")"
    rows = _csv(researcher_client.get("/data/datasets/regimens/download.csv"))
    assert rows[1][3] == "'+cmd|' /C calc'!A0" and rows[1][4] == "'@SUM(1)" and rows[1][5] == "'-2+3"
    r = researcher_client.get("/data/datasets/regimens/download.xlsx")
    assert r.status_code == 200 and "Regimens_deidentified.xlsx" in r["Content-Disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(b"".join(r.streaming_content)))
    ws = wb.active
    values = [[c.value for c in row] for row in ws.iter_rows()]
    assert values[0][:4] == ["condition_id", "diagnosis_type", "treatment_days", "comorbidities"]
    assert values[1][0] == "'=HYPERLINK(\"http://x\")" and values[1][4] == "'@SUM(1)"
    assert values[1][8] == "'=1+1"  # 제어문자를 먼저 지운 뒤 수식 방어
    for row in ws.iter_rows():
        for c in row:
            assert c.data_type != "f", c.value  # 수식 셀이 없다
    assert isinstance(values[1][2], int)  # 숫자는 숫자 셀


def test_xlsx_row_cap(researcher_client, demo_users, seeded_patients, settings):
    make_dar(demo_users["researcher"], ["clinical"], "approved")
    settings.DATASHARING_XLSX_MAX_ROWS = 3
    r = researcher_client.get("/data/datasets/patient-cases/download.xlsx")
    assert r.status_code == 302 and r["Location"] == "/data?tab=datasets"
    assert researcher_client.get("/data/datasets/patient-cases/download.csv").status_code == 200  # CSV 는 스트리밍


def test_csv_streams_in_chunks(demo_users, seeded_patients):
    chunks = list(services.stream_csv("patient-cases"))
    assert chunks[0].startswith("﻿condition_id,")
    assert sum(c.count("\n") for c in chunks) == Patient.objects.count() + 1


def test_custom_datasets_and_template_gate(client, demo_users, loaded_dictionary):
    from apps.schemas.services import load_sample_schemas
    from apps.schemas.models import Schema

    load_sample_schemas()
    client.force_login(demo_users["researcher"])
    body = client.get("/data").content.decode()
    published = Schema.objects.filter(status="published").exclude(visibility="private")
    shown = [s for s in published if f'data-custom="{s.schema_id}"' in body]
    assert shown
    s = shown[0]
    dtype = catalog.dar_type_of_schema_category(s.category)
    r = client.get(f"/data/custom/{s.schema_id}/template.csv")
    assert r.status_code == 403
    make_dar(demo_users["researcher"], [dtype], "approved")
    r = client.get(f"/data/custom/{s.schema_id}/template.csv")
    assert r.status_code == 200 and r.content.decode("utf-8").lstrip("﻿").startswith(s.link_field_name)
    modal = client.get(f"/data/custom/{s.schema_id}/schema").content.decode()
    assert 'data-kind="custom"' in modal and "연결 키" in modal
    # 비공개·미발행 구조는 목록·모달·서식 모두 404
    hidden = Schema.objects.filter(visibility="private").first() or Schema.objects.exclude(status="published").first()
    if hidden:
        assert f'data-custom="{hidden.schema_id}"' not in body
        assert client.get(f"/data/custom/{hidden.schema_id}/schema").status_code == 404
        assert client.get(f"/data/custom/{hidden.schema_id}/template.csv").status_code == 404


def test_dar_type_of_schema_category():
    assert catalog.dar_type_of_schema_category("genomic_ext") == "genomics"
    assert catalog.dar_type_of_schema_category("imaging_ext") == "imaging"
    assert catalog.dar_type_of_schema_category("clinical_ext") == "clinical"
    assert catalog.dar_type_of_schema_category("custom") == "clinical"


def test_header_nav_and_home_link(researcher_client):
    body = researcher_client.get("/").content.decode()
    assert 'href="/data"' in body and "데이터신청" in body
    page = researcher_client.get("/data").content.decode()
    assert "TBRS Platform의 데이터셋, API, 데이터 접근 신청을 관리합니다." in page
    for label in ("데이터셋", "API", "데이터 접근 신청", "내 업로드 관리"):
        assert label in page
    researcher_client.post("/lang", {"lang": "en"})
    page = researcher_client.get("/data?tab=bogus").content.decode()
    assert "Manage datasets, APIs and data access requests for the TBRS Platform." in page and "My Uploads" in page
