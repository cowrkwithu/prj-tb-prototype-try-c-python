import csv
import io
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.schemas import services
from apps.schemas.models import PendingEntry, Schema, SchemaField, SchemaFile, SchemaLog, SchemaRecord

from .conftest import simple_payload


def _post_builder(client, url, payload, action="save", rev=0):
    return client.post(url, {"payload": json.dumps(payload), "action": action, "rev": rev})


# ═══ 시드 ═══
@pytest.mark.django_db
def test_seed_counts_and_idempotent(seeded_schemas):
    assert {k: seeded_schemas[k] for k in ("skipped", "schemas", "fields", "records")} == \
        {"skipped": False, "schemas": 4, "fields": 48, "records": 14}
    assert (Schema.objects.count(), SchemaField.objects.count(), SchemaRecord.objects.count()) == (4, 48, 14)
    assert services.load_sample_schemas() == {"skipped": True}
    assert services.load_sample_schemas(force=True)["records"] == 0
    assert SchemaRecord.objects.count() == 14
    # 원본 sourceRef 는 옛 사전 UDE(TB.U.TX.006.005)를 가리킨다 — v3.2.0 에 없으므로 FK 는 비고 참조 기록만 남는다
    f = SchemaField.objects.get(schema__schema_id="schema-001", field_name="event_date")
    assert f.dictionary_field is None and f.source_dictionary_id == "TB.U.TX.006.005" and f.source_modified
    # 카드 집계(원본 usageCount·dataCount 와 일치)
    s = services.with_counts(Schema.objects.filter(schema_id="schema-001")).get()
    assert (s.field_count, s.data_count, s.usage_count) == (12, 5, 1)


@pytest.mark.django_db
def test_seed_deleted_record_not_revived(seeded_schemas):
    SchemaRecord.objects.filter(record_id="row-ae-001").delete()
    services.load_sample_schemas()
    assert not SchemaRecord.objects.filter(record_id="row-ae-001").exists()


# ═══ 빌더 저장·발행·버전 ═══
@pytest.mark.django_db
def test_builder_create_save_publish_and_version_bump(researcher_client, demo_users):
    r = _post_builder(researcher_client, "/schemas/create", simple_payload(), action="draft")
    assert r.status_code == 302
    s = Schema.objects.get()
    assert s.owner == demo_users["researcher"] and s.status == "draft" and s.version == "1.0"
    assert s.owner_institution == "분당서울대병원"
    assert list(s.fields.values_list("field_name", flat=True)) == ["score", "note"]
    # 발행
    r = _post_builder(researcher_client, f"/schemas/{s.schema_id}/edit", simple_payload(), action="publish", rev=s.rev)
    s.refresh_from_db()
    assert s.status == "published" and s.published_at is not None
    # 발행 후 저장 → 버전업 + 이력
    p = simple_payload(name="이름 변경")
    r = _post_builder(researcher_client, f"/schemas/{s.schema_id}/edit", p, rev=s.rev)
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.version == "1.1" and s.name == "이름 변경"
    assert set(SchemaLog.objects.filter(schema=s).values_list("action", flat=True)) >= {"create", "publish", "save"}


@pytest.mark.django_db
def test_builder_rejects_invalid_and_keeps_input(researcher_client):
    p = simple_payload(fields=[{"fieldId": "a", "fieldName": "bad name", "displayName": "", "dataType": "TEXT"}])
    r = _post_builder(researcher_client, "/schemas/create", p)
    assert r.status_code == 400 and Schema.objects.count() == 0
    body = r.content.decode()
    assert "builder-errors" in body and "bad name" in body  # 입력값을 다시 채워 보여 준다(json_script)


@pytest.mark.django_db
def test_builder_concurrent_edit_conflict(researcher_client, published_schema):
    s = published_schema
    _post_builder(researcher_client, f"/schemas/{s.schema_id}/edit", simple_payload(name="A"), rev=s.rev)
    r = _post_builder(researcher_client, f"/schemas/{s.schema_id}/edit", simple_payload(name="B"), rev=s.rev)  # 옛 rev
    assert r.status_code == 400 and "다른 사용자가 먼저" in r.content.decode()
    s.refresh_from_db()
    assert s.name == "A"


@pytest.mark.django_db
def test_field_with_data_cannot_be_deleted_renamed_or_retyped(researcher_client, published_schema, demo_users):
    s = published_schema
    SchemaRecord.objects.create(schema=s, record_id="R1", condition_id="X1", data={"score": 3}, author_label="x",
                                created_at=s.created_at, updated_at=s.created_at)
    base = simple_payload()
    for change in ("delete", "rename", "retype"):
        p = json.loads(json.dumps(base))
        if change == "delete":
            p["fields"] = [f for f in p["fields"] if f["fieldName"] != "score"]
        elif change == "rename":
            p["fields"][0]["fieldName"] = "score2"
        else:
            p["fields"][0]["dataType"] = "TEXT"
            p["fields"][0]["constraints"] = {}
        s.refresh_from_db()
        r = _post_builder(researcher_client, f"/schemas/{s.schema_id}/edit", p, rev=s.rev)
        assert r.status_code == 400, change
        assert "입력된 데이터가 있는 필드" in r.content.decode()
    # 데이터가 없는 필드(note)는 삭제할 수 있다
    p = json.loads(json.dumps(base))
    p["fields"] = [f for f in p["fields"] if f["fieldName"] != "note"]
    s.refresh_from_db()
    assert _post_builder(researcher_client, f"/schemas/{s.schema_id}/edit", p, rev=s.rev).status_code == 302
    assert list(s.fields.values_list("field_name", flat=True)) == ["score"]


# ═══ 권한 ═══
@pytest.mark.django_db
def test_other_researcher_cannot_edit_or_manage(client, published_schema, other_researcher):
    s = published_schema
    client.force_login(other_researcher)
    assert client.get(f"/schemas/{s.schema_id}").status_code == 200  # 전체 공개는 열람 가능
    r = _post_builder(client, f"/schemas/{s.schema_id}/edit", simple_payload(name="hacked"), rev=s.rev)
    assert r.status_code == 302
    s.refresh_from_db()
    assert s.name == "테스트 구조"
    assert client.post(f"/schemas/{s.schema_id}/status", {"action": "archive"}).status_code == 403
    assert client.post(f"/schemas/{s.schema_id}/delete").status_code == 403
    s.refresh_from_db()
    assert s.status == "published"


@pytest.mark.django_db
def test_visibility_rules(client, demo_users, other_researcher, same_org_researcher):
    owner = demo_users["researcher"]
    priv = services.save_definition(owner, simple_payload(visibility="private"), action="draft")
    shared = services.save_definition(owner, simple_payload(name="공유", visibility="shared"), action="draft")
    client.force_login(other_researcher)
    assert client.get(f"/schemas/{priv.schema_id}").status_code == 404
    assert client.get(f"/schemas/{shared.schema_id}").status_code == 404
    assert client.get(f"/schemas/{priv.schema_id}/records.csv").status_code == 404
    assert priv.schema_id not in client.get("/schemas").content.decode()
    client.force_login(same_org_researcher)
    assert client.get(f"/schemas/{shared.schema_id}").status_code == 200
    assert client.get(f"/schemas/{priv.schema_id}").status_code == 404
    client.force_login(demo_users["dmanager"])
    assert client.get(f"/schemas/{priv.schema_id}").status_code == 200


@pytest.mark.django_db
def test_record_modify_permissions(client, published_schema, demo_users, other_researcher):
    s = published_schema
    rec = SchemaRecord.objects.create(schema=s, record_id="REC-OTHERS-ROW-QZ7X", condition_id="X1", data={"score": 3},
                                      author=demo_users["researcher"], author_label="researcher",
                                      created_at=s.created_at, updated_at=s.created_at)
    client.force_login(other_researcher)
    r = client.post(f"/schemas/{s.schema_id}/records/REC-OTHERS-ROW-QZ7X/delete")
    assert SchemaRecord.objects.filter(pk=rec.pk).exists()
    r = client.post(f"/schemas/{s.schema_id}/data/check", {"mode": "edit", "record_id": "REC-OTHERS-ROW-QZ7X", "link_value": "X1", "f__score": "9"})
    assert r.status_code == 403
    rec.refresh_from_db()
    assert rec.data == {"score": 3}
    # 다른 연구원의 '기존 데이터 수정' 목록에는 남의 행이 보이지 않는다
    page = client.get(f"/schemas/{s.schema_id}/data?method=edit").content.decode()
    # 짧은 ID 를 페이지 전체에서 찾으면 CSRF 토큰과 우연히 겹칠 수 있다 — 충돌 불가능한 긴 ID 로 검사
    assert "REC-OTHERS-ROW-QZ7X" not in page
    # 작성자 본인은 삭제 가능
    client.force_login(demo_users["researcher"])
    client.post(f"/schemas/{s.schema_id}/records/REC-OTHERS-ROW-QZ7X/delete")
    assert not SchemaRecord.objects.filter(pk=rec.pk).exists()


@pytest.mark.django_db
def test_researcher_blocked_from_admin_pages(researcher_client, seeded_schemas):
    for url in ["/admin/schemas", "/admin/dictionary", "/admin/schemas/schema-001", "/admin/dictionary?view=versions"]:
        r = researcher_client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/admin/login"), url
    for url, data in [("/admin/schemas/schema-004/action", {"action": "publish"}), ("/admin/schemas/schema-004/delete", {}),
                      ("/admin/schemas/schema-001/visibility", {"visibility": "private"}),
                      ("/admin/dictionary/category/save", {"code": "x", "name": "x"}),
                      ("/admin/dictionary/version/1/activate", {})]:
        r = researcher_client.post(url, data)
        assert r.status_code == 302 and r["Location"].startswith("/admin/login"), url
    assert Schema.objects.get(schema_id="schema-004").status == "draft"
    assert Schema.objects.get(schema_id="schema-001").visibility == "public"


# ═══ 상태 전이 (관리자) ═══
@pytest.mark.django_db
def test_admin_status_transitions(admin_client_portal, seeded_schemas):
    c = admin_client_portal
    c.post("/admin/schemas/schema-004/action", {"action": "publish", "note": "검토 완료"})
    s4 = Schema.objects.get(schema_id="schema-004")
    assert s4.status == "published" and s4.published_at
    assert SchemaLog.objects.filter(schema=s4, action="publish", by_admin=True, note="검토 완료").exists()
    c.post("/admin/schemas/schema-004/action", {"action": "to_draft"})  # 데이터 0건 → 가능
    assert Schema.objects.get(schema_id="schema-004").status == "draft"
    c.post("/admin/schemas/schema-001/action", {"action": "to_draft"})  # 데이터 있음 → 불가
    assert Schema.objects.get(schema_id="schema-001").status == "published"
    c.post("/admin/schemas/schema-001/action", {"action": "restore"})  # 발행됨 → 복원 불가(잘못된 전이)
    assert Schema.objects.get(schema_id="schema-001").status == "published"
    c.post("/admin/schemas/schema-001/action", {"action": "archive"})
    assert Schema.objects.get(schema_id="schema-001").status == "archived"
    c.post("/admin/schemas/schema-001/action", {"action": "archive"})  # 중복 처리 → 상태 그대로, 이력 1건
    assert SchemaLog.objects.filter(schema__schema_id="schema-001", action="archive").count() == 1
    c.post("/admin/schemas/schema-001/action", {"action": "restore"})
    assert Schema.objects.get(schema_id="schema-001").status == "published"
    c.post("/admin/schemas/schema-001/visibility", {"visibility": "private"})
    assert Schema.objects.get(schema_id="schema-001").visibility == "private"
    assert SchemaLog.objects.filter(schema__schema_id="schema-001", action="visibility", detail="public→private").exists()


@pytest.mark.django_db
def test_delete_rules(admin_client_portal, seeded_schemas):
    admin_client_portal.post("/admin/schemas/schema-001/delete")
    assert Schema.objects.filter(schema_id="schema-001").exists()  # 데이터 5건 → 삭제 불가
    admin_client_portal.post("/admin/schemas/schema-004/delete")
    assert not Schema.objects.filter(schema_id="schema-004").exists()
    from django.db.models import ProtectedError
    with pytest.raises(ProtectedError):  # DB 차원에서도 보호
        Schema.objects.filter(schema_id="schema-001").delete()


@pytest.mark.django_db
def test_archived_blocks_entry_and_edit(client, demo_users, published_schema):
    s = published_schema
    client.force_login(demo_users["researcher"])
    services.transition(s, "archive", demo_users["researcher"])
    r = client.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "X1", "f__score": "3"})
    assert r.status_code == 302 and not PendingEntry.objects.exists()
    r = client.get(f"/schemas/{s.schema_id}/edit")
    assert r.status_code == 302
    assert "not-published" in client.get(f"/schemas/{s.schema_id}/data").content.decode()


# ═══ 데이터 입력 흐름 ═══
@pytest.mark.django_db
def test_form_entry_flow_and_double_submit(researcher_client, published_schema, seeded_patients):
    s = published_schema
    c = researcher_client
    r = c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "KR-2024-0001", "f__score": "11", "f__note": "x"})
    pending = PendingEntry.objects.get()
    assert r["Location"] == f"/schemas/{s.schema_id}/data/review/{pending.pk}"
    assert pending.errors and pending.errors[0]["field"] == "score"
    assert c.post(f"/schemas/{s.schema_id}/data/submit/{pending.pk}").status_code == 302
    assert SchemaRecord.objects.count() == 0  # 오류가 있으면 제출 불가
    # 수정 화면으로 돌아가면 입력값이 남아 있다
    page = c.get(f"/schemas/{s.schema_id}/data?method=form&pending={pending.pk}").content.decode()
    assert 'value="11"' in page and "err-score" in page
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "KR-2024-0001", "f__score": "7"})
    p2 = PendingEntry.objects.latest("pk")
    assert not p2.errors and not p2.warnings  # 등록된 환자 → 경고 없음
    c.post(f"/schemas/{s.schema_id}/data/submit/{p2.pk}")
    c.post(f"/schemas/{s.schema_id}/data/submit/{p2.pk}")  # 이중 제출
    assert SchemaRecord.objects.count() == 1
    rec = SchemaRecord.objects.get()
    assert rec.data == {"score": 7} and rec.condition_id == "KR-2024-0001" and rec.author_label == "researcher"
    assert "1" in c.get(f"/schemas/{s.schema_id}/data/done/{p2.pk}").content.decode()
    # 케이스 '커스텀' 탭
    tab = c.get("/schemas/case/KR-2024-0001").content.decode()
    assert s.name in tab and "badge-success" in tab


@pytest.mark.django_db
def test_unknown_patient_is_warning(researcher_client, published_schema):
    researcher_client.post(f"/schemas/{published_schema.schema_id}/data/check", {"mode": "form", "link_value": "ZZ-1", "f__score": "1"})
    p = PendingEntry.objects.get()
    assert not p.errors and p.warnings[0]["field"] == "condition_id"


@pytest.mark.django_db
def test_unique_field_enforced(researcher_client, demo_users):
    fields = [{"fieldId": "u", "fieldName": "sample_id", "displayName": "샘플", "dataType": "TEXT", "isUnique": True, "isRequired": True}]
    s = services.save_definition(demo_users["researcher"], simple_payload(fields=fields), action="publish")
    c = researcher_client
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__sample_id": "S-1"})
    c.post(f"/schemas/{s.schema_id}/data/submit/{PendingEntry.objects.latest('pk').pk}")
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A2", "f__sample_id": "S-1"})
    assert PendingEntry.objects.latest("pk").errors[0]["field"] == "sample_id"
    data = b"condition_id,sample_id\nA3,S-2\nA4,S-2\n"
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "csv", "file": SimpleUploadedFile("d.csv", data, "text/csv")})
    assert any("고유값" in e["ko"] for e in PendingEntry.objects.latest("pk").errors)
    assert SchemaRecord.objects.count() == 1


@pytest.mark.django_db
def test_csv_import_flow(researcher_client, seeded_schemas):
    c = researcher_client
    s = Schema.objects.get(schema_id="schema-003")
    tpl = c.get("/schemas/schema-003/template.csv").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(tpl)))
    assert rows[0][0] == "condition_id" and "survey_date" in rows[0]
    body = ("condition_id,survey_date,survey_timepoint,physical_function,pain_level,fatigue_score,sleep_quality,anxiety_score,"
            "depression_score,daily_activity,overall_qol,unknown_col\n"
            "KR-2024-0002,2026-01-05,baseline,50,5,5,fair,10,10,mild,60,zzz\n"
            "KR-2024-0003,2026-01-06,3m,60,4,4,good,8,8,normal,70,\n")
    c.post("/schemas/schema-003/data/check", {"mode": "csv", "file": SimpleUploadedFile("qol.csv", body.encode(), "text/csv")})
    p = PendingEntry.objects.latest("pk")
    assert not p.errors, p.errors
    assert any(w["field"] == "unknown_col" for w in p.warnings)
    c.post(f"/schemas/schema-003/data/submit/{p.pk}")
    assert s.records.count() == 7
    # 오류 행: 범위·선택지·필수
    bad = ("condition_id,survey_date,survey_timepoint,physical_function,pain_level,fatigue_score,sleep_quality,anxiety_score,"
           "depression_score,daily_activity,overall_qol\nKR-2024-0004,2026-13-01,never,150,5,5,fair,10,10,mild,\n")
    c.post("/schemas/schema-003/data/check", {"mode": "csv", "file": SimpleUploadedFile("bad.csv", bad.encode(), "text/csv")})
    fields = {e["field"] for e in PendingEntry.objects.latest("pk").errors}
    assert {"survey_date", "survey_timepoint", "physical_function", "overall_qol"} <= fields
    # 파일 검증(업로드 files 서비스 재사용): 확장자 위장
    r = c.post("/schemas/schema-003/data/check", {"mode": "csv", "file": SimpleUploadedFile("x.csv", b"PK\x03\x04zip", "text/csv")})
    assert r.status_code == 302 and s.records.count() == 7


@pytest.mark.django_db
def test_edit_existing_record_with_rev_conflict(researcher_client, published_schema, demo_users):
    s = published_schema
    rec = SchemaRecord.objects.create(schema=s, record_id="R1", condition_id="X1", data={"score": 3},
                                      author=demo_users["researcher"], author_label="researcher",
                                      created_at=s.created_at, updated_at=s.created_at)
    c = researcher_client
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "edit", "record_id": "R1", "link_value": "X1", "f__score": "4"})
    p = PendingEntry.objects.latest("pk")
    SchemaRecord.objects.filter(pk=rec.pk).update(rev=5)  # 그 사이 다른 사람이 수정
    c.post(f"/schemas/{s.schema_id}/data/submit/{p.pk}")
    rec.refresh_from_db()
    assert rec.data == {"score": 3}
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "edit", "record_id": "R1", "link_value": "X1", "f__score": "4"})
    c.post(f"/schemas/{s.schema_id}/data/submit/{PendingEntry.objects.latest('pk').pk}")
    rec.refresh_from_db()
    assert rec.data == {"score": 4} and rec.rev == 6


@pytest.mark.django_db
def test_file_field_upload_and_constraints(researcher_client, demo_users):
    fields = [{"fieldId": "f", "fieldName": "report", "displayName": "보고서", "dataType": "FILE", "isRequired": True,
               "constraints": {"allowedFileTypes": [".pdf"], "maxFileSizeMB": 0.001}}]
    s = services.save_definition(demo_users["researcher"], simple_payload(fields=fields), action="publish")
    c = researcher_client
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__report": SimpleUploadedFile("a.exe", b"MZ", "application/octet-stream")})
    assert any("허용되지 않는 파일 형식" in e["ko"] for e in PendingEntry.objects.latest("pk").errors)
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__report": SimpleUploadedFile("a.pdf", b"x" * 5000, "application/pdf")})
    assert any("너무 큽니다" in e["ko"] for e in PendingEntry.objects.latest("pk").errors)
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "form", "link_value": "A1", "f__report": SimpleUploadedFile("../../a.pdf", b"%PDF-1", "application/pdf")})
    p = PendingEntry.objects.latest("pk")
    assert not p.errors
    c.post(f"/schemas/{s.schema_id}/data/submit/{p.pk}")
    rec = SchemaRecord.objects.get()
    sf = SchemaFile.objects.get()
    assert rec.data["report"]["name"] == "a.pdf" and sf.record == rec and ".." not in sf.file.name
    r = c.get(f"/schemas/{s.schema_id}/files/{sf.pk}")
    assert r.status_code == 200 and b"".join(r.streaming_content) == b"%PDF-1"
    # 기존 첨부 유지하며 수정 → 같은 파일 유지
    c.post(f"/schemas/{s.schema_id}/data/check", {"mode": "edit", "record_id": rec.record_id, "link_value": "A2", "f__report__keep": "1"})
    c.post(f"/schemas/{s.schema_id}/data/submit/{PendingEntry.objects.latest('pk').pk}")
    rec.refresh_from_db()
    assert rec.condition_id == "A2" and rec.data["report"]["file_id"] == sf.pk


# ═══ 내보내기·XSS·N+1 ═══
@pytest.mark.django_db
def test_csv_export_formula_injection_defense(researcher_client, published_schema, demo_users):
    s = published_schema
    SchemaRecord.objects.create(schema=s, record_id="R1", condition_id="X1", data={"score": 3, "note": "=HYPERLINK(\"x\")"},
                                author=demo_users["researcher"], author_label="@evil", created_at=s.created_at, updated_at=s.created_at)
    r = researcher_client.get(f"/schemas/{s.schema_id}/records.csv")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][:2] == ["record_id", "condition_id"]
    row = rows[1]
    assert row[rows[0].index("note")] == "'=HYPERLINK(\"x\")" and row[rows[0].index("created_by")] == "'@evil"
    assert row[rows[0].index("score")] == "3"
    assert r["Content-Type"].startswith("text/csv") and r["X-Content-Type-Options"] == "nosniff"


@pytest.mark.django_db
def test_xss_escaped(researcher_client, demo_users):
    xss = "<script>alert(1)</script>"
    fields = [{"fieldId": "a", "fieldName": "memo", "displayName": xss, "dataType": "SELECT",
               "constraints": {"options": [{"value": "v1", "label": xss}]}},
              {"fieldId": "b", "fieldName": "txt", "displayName": "t", "dataType": "TEXT"}]
    s = services.save_definition(demo_users["researcher"], simple_payload(name=xss, fields=fields, tags=[xss]), action="publish")
    SchemaRecord.objects.create(schema=s, record_id="R1", condition_id="X1", data={"memo": "v1", "txt": xss},
                                author=demo_users["researcher"], author_label="r", created_at=s.created_at, updated_at=s.created_at)
    for url in ["/schemas", f"/schemas/{s.schema_id}", f"/schemas/{s.schema_id}?tab=data", f"/schemas/{s.schema_id}/edit",
                f"/schemas/{s.schema_id}/data?method=form", f"/schemas/{s.schema_id}/data?method=edit",
                f"/schemas/{s.schema_id}/records/R1", "/schemas/case/X1", "/schemas/select"]:
        body = researcher_client.get(url).content.decode()
        assert "<script>alert(1)</script>" not in body, url
    r = researcher_client.post("/schemas/builder/preview", {"payload": json.dumps(simple_payload(fields=fields))})
    assert "<script>alert(1)</script>" not in r.content.decode()


@pytest.mark.django_db
def test_explorer_and_admin_list_no_n_plus_1(researcher_client, admin_client_portal, seeded_schemas, django_assert_max_num_queries, demo_users):
    for i in range(8):
        services.save_definition(demo_users["researcher"], simple_payload(name=f"추가 {i}"), action="publish")
    with django_assert_max_num_queries(12):
        assert researcher_client.get("/schemas").status_code == 200
    with django_assert_max_num_queries(12):
        assert admin_client_portal.get("/admin/schemas").status_code == 200
    with django_assert_max_num_queries(16):
        assert researcher_client.get("/schemas/schema-001?tab=data").status_code == 200


@pytest.mark.django_db
def test_duplicate_and_json_roundtrip(researcher_client, seeded_schemas):
    c = researcher_client
    assert c.post("/schemas/schema-002/duplicate").status_code == 404  # 기관 공유(다른 기관) → 보이지 않음
    r = c.post("/schemas/schema-003/duplicate")
    copy = Schema.objects.exclude(schema_id__startswith="schema-").get()
    assert copy.status == "draft" and copy.version == "1.0" and copy.visibility == "private" and copy.name.endswith("(복제)")
    assert copy.fields.count() == 15 and copy.records.count() == 0
    exported = c.get("/schemas/schema-003/export.json").content
    obj = json.loads(exported)
    assert "owner" not in obj and "dataCount" not in obj and len(obj["fields"]) == 15
    r = c.post("/schemas/import", {"file": SimpleUploadedFile("s.json", exported, "application/json")})
    assert "import-name" in r.content.decode()
    r = c.post("/schemas/import", {"confirm": "1", "json": exported.decode()})
    assert r.status_code == 204 and r["HX-Redirect"].startswith("/schemas/SCH-")
    imported = Schema.objects.filter(schema_id__startswith="SCH-").exclude(pk=copy.pk).get()
    assert imported.status == "draft" and imported.fields.count() == 15
    r = c.post("/schemas/import", {"file": SimpleUploadedFile("bad.json", b'{"name": 1}', "application/json")})
    assert "import-error" in r.content.decode()


@pytest.mark.django_db
def test_dashboard_stats_from_db(client, seeded_schemas):
    from apps.core import dashboard

    assert dashboard.custom_schema_stats() == {"active": 3, "records": 14}
    Schema.objects.filter(schema_id="schema-001").update(status="archived")
    assert dashboard.custom_schema_stats() == {"active": 2, "records": 9}
    assert "활성 커스텀 데이터 구조" in client.get("/").content.decode()


@pytest.mark.django_db
def test_hub_custom_card_and_select_modal(researcher_client, seeded_schemas):
    hub = researcher_client.get("/upload").content.decode()
    assert 'hx-get="/schemas/select"' in hub
    sel = researcher_client.get("/schemas/select").content.decode()
    assert "/schemas/schema-001/data" in sel and "/schemas/schema-004/data" not in sel  # 발행된 구조만
