"""관리 → 데이터사전 관리(/admin/dictionary) + 데이터 구조 필드 FK 와 사전 재적재 공존."""
import copy
import io
import json
import zipfile

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.dictionary import services
from apps.dictionary.models import DictionaryCategory, DictionaryChangeLog, DictionaryField, DictionaryVersion


@pytest.fixture(autouse=True)
def _media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()


@pytest.fixture
def any_path_parse(monkeypatch, loaded_dictionary):
    """업로드된 임시 파일 경로도 캐시된 v3.2.0 파싱 결과를 돌려준다(엑셀 재파싱 시간 절약). 수정 함수를 넘기면 결과를 바꾼다."""
    base = services.parse_workbook(settings.DICTIONARY_XLSX)
    state = {"mutate": None}

    def fake(path):
        d = copy.deepcopy(base)
        if state["mutate"]:
            state["mutate"](d)
        return d

    monkeypatch.setattr(services, "parse_workbook", fake)
    return state


def _schema_with_dict_field(user, dictionary_id):
    from apps.schemas import services as ss

    e = DictionaryField.objects.get(version=services.active_version(), dictionary_id=dictionary_id)
    payload = {"meta": {"name": "사전 참조 구조", "visibility": "public"}, "fields": [{
        "fieldId": "k", "fieldName": e.field_name, "displayName": e.display_name, "dataType": e.data_type,
        "constraints": {**e.constraints, **({"options": e.options} if e.options else {})},
        "sourceRef": {"dictionaryId": e.dictionary_id, "fieldName": e.field_name, "isModified": False}}]}
    return ss.save_definition(user, payload, action="publish"), e


@pytest.mark.django_db
def test_category_and_entry_crud_with_history(admin_client_portal, loaded_dictionary):
    c = admin_client_portal
    c.post("/admin/dictionary/category/save", {"code": "epi", "name": "역학 조사", "icon": "Users", "color": "rose"})
    cat = DictionaryCategory.objects.get(version=loaded_dictionary, code="epi")
    assert cat.icon == "Users" and cat.color == "rose"
    c.post("/admin/dictionary/category/save", {"code": "epi", "name": "중복"})  # 중복 ID
    assert DictionaryCategory.objects.filter(code="epi").count() == 1
    c.post("/admin/dictionary/entry/save", {"cat": cat.pk, "field_name": "contact_count", "display_name": "접촉자 수",
                                            "data_type": "INTEGER", "min": "0", "max": "100", "unit": "명"})
    e = DictionaryField.objects.get(field_name="contact_count")
    assert e.is_standard is False and e.dictionary_id == "CUSTOM.EPI.001"
    assert e.constraints == {"min": 0, "max": 100, "unit": "명"}
    c.post("/admin/dictionary/entry/save", {"id": e.pk, "field_name": "contact_count", "display_name": "밀접 접촉자 수",
                                            "data_type": "SELECT", "options": "A, B, A"})
    e.refresh_from_db()
    assert e.display_name == "밀접 접촉자 수" and e.options == [{"value": "A", "label": "A"}, {"value": "B", "label": "B"}]
    assert e.constraints == {}
    c.post("/admin/dictionary/entry/save", {"cat": cat.pk, "field_name": "tb_u_x", "display_name": "x", "data_type": "TEXT"})
    assert not DictionaryField.objects.filter(field_name="tb_u_x").exists()  # 표준 접두어 예약
    c.post(f"/admin/dictionary/entry/{e.pk}/deprecate")
    e.refresh_from_db()
    assert e.is_deprecated
    page = c.get("/admin/dictionary?cat=epi").content.decode()
    assert "contact_count" not in page
    assert "contact_count" in c.get("/admin/dictionary?cat=epi&dep=1").content.decode()
    c.post(f"/admin/dictionary/entry/{e.pk}/deprecate", {"restore": "1"})
    e.refresh_from_db()
    assert not e.is_deprecated
    actions = list(DictionaryChangeLog.objects.values_list("action", flat=True))
    assert {"category_add", "entry_add", "entry_edit", "entry_deprecate", "entry_restore"} <= set(actions)
    assert "밀접 접촉자 수" in c.get("/admin/dictionary?view=history").content.decode()


@pytest.mark.django_db
def test_category_delete_rules(admin_client_portal, loaded_dictionary, demo_users):
    c = admin_client_portal
    pat = DictionaryCategory.objects.get(version=loaded_dictionary, code="pat")
    c.post(f"/admin/dictionary/category/{pat.pk}/delete")
    assert DictionaryCategory.objects.filter(pk=pat.pk).exists()  # 표준 항목 → 삭제 불가
    c.post("/admin/dictionary/category/save", {"code": "tmp", "name": "임시"})
    tmp = DictionaryCategory.objects.get(code="tmp")
    c.post("/admin/dictionary/entry/save", {"cat": tmp.pk, "field_name": "tmp_a", "display_name": "A", "data_type": "TEXT"})
    from apps.schemas import services as ss

    ss.save_definition(demo_users["admin"], {"meta": {"name": "S"}, "fields": [{
        "fieldName": "tmp_a", "displayName": "A", "dataType": "TEXT",
        "sourceRef": {"dictionaryId": "CUSTOM.TMP.001", "fieldName": "tmp_a"}}]}, action="draft")
    c.post(f"/admin/dictionary/category/{tmp.pk}/delete")
    assert DictionaryCategory.objects.filter(pk=tmp.pk).exists()  # 데이터 구조가 참조 → 삭제 불가
    from apps.schemas.models import Schema
    Schema.objects.all().delete()
    c.post(f"/admin/dictionary/category/{tmp.pk}/delete")
    assert not DictionaryCategory.objects.filter(pk=tmp.pk).exists()


@pytest.mark.django_db
def test_schema_field_fk_survives_same_version_reload(any_path_parse, demo_users, admin_client_portal):
    schema, e = _schema_with_dict_field(demo_users["researcher"], "TB.U.PAT.001")
    f = schema.fields.get()
    assert f.dictionary_field_id == e.pk
    # 같은 버전 재적재: 엑셀에서 그 항목이 사라짐 → 지우지 않고 폐기 표시(FK PROTECT 와 충돌 없음)
    any_path_parse["mutate"] = lambda d: d.update(fields=[x for x in d["fields"] if x["dictionary_id"] != "TB.U.PAT.001"])
    res = services.load_dictionary(settings.DICTIONARY_XLSX, force=True)
    assert res["deprecated"] == 1
    e.refresh_from_db()
    f.refresh_from_db()
    assert e.is_deprecated and f.dictionary_field_id == e.pk
    # 빌더 데이터사전 목록에서는 빠지고, 구조 상세는 정상 표시
    c = admin_client_portal
    assert "TB.U.PAT.001" not in c.get("/schemas/builder/dictionary?cat=pat").content.decode()
    assert c.get(f"/schemas/{schema.schema_id}").status_code == 200
    # 다시 원래 엑셀로 → 폐기 해제
    any_path_parse["mutate"] = None
    services.load_dictionary(settings.DICTIONARY_XLSX, force=True)
    e.refresh_from_db()
    assert not e.is_deprecated


@pytest.mark.django_db
def test_new_version_upload_keeps_old_rows_and_carries_custom(any_path_parse, admin_client_portal, demo_users):
    c = admin_client_portal
    old = services.active_version()
    schema, e = _schema_with_dict_field(demo_users["researcher"], "TB.U.PAT.002")
    cat = DictionaryCategory.objects.get(version=old, code="pat")
    c.post("/admin/dictionary/entry/save", {"cat": cat.pk, "field_name": "my_custom", "display_name": "사용자 항목", "data_type": "TEXT"})
    any_path_parse["mutate"] = lambda d: d.update(version="v9.9.9")
    xlsx = settings.DICTIONARY_XLSX.read_bytes()
    r = c.post("/admin/dictionary/version/upload", {"file": SimpleUploadedFile("dict_v9.xlsx", xlsx,
                                                                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 302
    new = services.active_version()
    assert new.version == "v9.9.9" and new.source_file == "dict_v9.xlsx" and not DictionaryVersion.objects.get(pk=old.pk).is_active
    assert DictionaryField.objects.filter(version=new, field_name="my_custom", is_standard=False).exists()  # 사용자 항목 이관
    assert DictionaryField.objects.filter(version=old).count() > 1000  # 이전 버전 행 보존
    f = schema.fields.get()
    assert f.dictionary_field_id == e.pk  # 옛 버전 항목을 계속 참조
    log = DictionaryChangeLog.objects.get(action="version_load")
    assert log.version == "v9.9.9" and log.detail["carried_custom"] == 1
    # 저장하면 현행 버전의 같은 UDE 로 다시 연결
    from apps.schemas import services as ss
    payload = {"meta": {"name": schema.name, "visibility": "public"}, "fields": [ss.field_to_json(f)]}
    ss.save_definition(demo_users["researcher"], payload, schema=schema)
    assert schema.fields.get().dictionary_field.version == new
    # 현행 버전 되돌리기
    c.post(f"/admin/dictionary/version/{old.pk}/activate")
    assert services.active_version().pk == old.pk
    assert DictionaryChangeLog.objects.filter(action="version_activate").exists()


@pytest.mark.django_db
def test_upload_rejects_bad_files(admin_client_portal, loaded_dictionary):
    c = admin_client_portal
    before = DictionaryVersion.objects.count()
    r = c.post("/admin/dictionary/version/upload", {"file": SimpleUploadedFile("x.xlsx", b"not a zip", "application/octet-stream")})
    assert r.status_code == 302
    msgs = [m.message for m in list(r.wsgi_request._messages)]
    assert any("엑셀(xlsx) 파일 형식이 아닙니다" in m for m in msgs)
    r = c.post("/admin/dictionary/version/upload", {"file": SimpleUploadedFile("x.csv", b"a,b", "text/csv")})
    assert any("허용되지 않는 파일 형식" in m.message for m in r.wsgi_request._messages)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    r = c.post("/admin/dictionary/version/upload", {"file": SimpleUploadedFile("z.xlsx", buf.getvalue(), "application/octet-stream")})
    assert any("엑셀" in m.message for m in r.wsgi_request._messages)
    assert DictionaryVersion.objects.count() == before
    assert "엑셀 재적재 정책" in c.get("/admin/dictionary?view=versions").content.decode()


@pytest.mark.django_db
def test_admin_dictionary_xss_escaped(admin_client_portal, loaded_dictionary):
    c = admin_client_portal
    c.post("/admin/dictionary/category/save", {"code": "xss", "name": "<script>alert(1)</script>"})
    cat = DictionaryCategory.objects.get(code="xss")
    c.post("/admin/dictionary/entry/save", {"cat": cat.pk, "field_name": "x1", "display_name": "<img src=x onerror=alert(1)>", "data_type": "TEXT"})
    body = c.get("/admin/dictionary?cat=xss").content.decode()
    assert "<script>alert(1)</script>" not in body and "<img src=x" not in body
    assert "<img src=x" not in c.get("/admin/dictionary?view=history").content.decode()
    _ = json
