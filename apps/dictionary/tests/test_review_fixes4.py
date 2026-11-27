"""3a 4차 리뷰: 관리자 데이터사전 항목도 빌더와 같은 필드명 규칙(연속 밑줄·끝 밑줄 금지)을 한 곳의 함수로 적용한다."""
import json

import pytest

from apps.core import naming
from apps.dictionary import admin_services
from apps.dictionary.models import DictionaryCategory, DictionaryField
from apps.schemas import services as schema_services
from apps.schemas import validation

pytestmark = pytest.mark.django_db

BAD_NAMES = ["bad__name_", "bad__name", "bad_", "a__b", "x___y", "a" * 65]


def _messages(r):
    return [m.message for m in r.wsgi_request._messages]


def test_rule_is_defined_once_and_shared():
    assert admin_services.field_name_problem is naming.field_name_problem
    assert validation.field_name_problem is naming.field_name_problem


@pytest.mark.parametrize("name", BAD_NAMES)
def test_admin_entry_add_rejects_bad_names(admin_client_portal, loaded_dictionary, name):
    cat = DictionaryCategory.objects.filter(version=loaded_dictionary).first()
    r = admin_client_portal.post("/admin/dictionary/entry/save", {"cat": cat.pk, "field_name": name,
                                                                  "display_name": "x", "data_type": "TEXT"})
    assert r.status_code == 302
    assert not DictionaryField.objects.filter(field_name=name).exists()
    assert any("필드명" in m for m in _messages(r))


@pytest.mark.parametrize("name", BAD_NAMES)
def test_admin_entry_rename_to_bad_name_rejected(admin_client_portal, loaded_dictionary, name):
    cat = DictionaryCategory.objects.filter(version=loaded_dictionary).first()
    f = admin_services.add_entry(None, cat, {"field_name": "good_name", "display_name": "x", "data_type": "TEXT"})
    r = admin_client_portal.post("/admin/dictionary/entry/save", {"id": f.pk, "field_name": name,
                                                                  "display_name": "x", "data_type": "TEXT"})
    assert r.status_code == 302
    f.refresh_from_db()
    assert f.field_name == "good_name"


def test_legacy_entry_name_editable_without_rename(admin_client_portal, loaded_dictionary):
    cat = DictionaryCategory.objects.filter(version=loaded_dictionary).first()
    f = admin_services.add_entry(None, cat, {"field_name": "legacy_ok", "display_name": "x", "data_type": "TEXT"})
    DictionaryField.objects.filter(pk=f.pk).update(field_name="legacy__name_")  # 규칙 이전에 저장된 이름
    r = admin_client_portal.post("/admin/dictionary/entry/save", {"id": f.pk, "field_name": "legacy__name_",
                                                                  "display_name": "새 표시명", "data_type": "TEXT"})
    assert r.status_code == 302
    f.refresh_from_db()
    assert f.field_name == "legacy__name_" and f.display_name == "새 표시명"
    # 다른 나쁜 이름으로 바꾸는 것은 안 된다
    with pytest.raises(admin_services.DictAdminError):
        admin_services.edit_entry(None, f, {"field_name": "other__bad", "display_name": "x", "data_type": "TEXT"})


def test_good_admin_entry_importable_into_builder(admin_client_portal, loaded_dictionary, demo_users):
    """관리자가 만든 항목을 빌더가 그대로 가져와 저장할 수 있다(리뷰 재현의 반대)."""
    cat = DictionaryCategory.objects.filter(version=loaded_dictionary).first()
    r = admin_client_portal.post("/admin/dictionary/entry/save", {"cat": cat.pk, "field_name": "good_name2",
                                                                  "display_name": "좋은 이름", "data_type": "TEXT"})
    assert r.status_code == 302
    e = DictionaryField.objects.get(field_name="good_name2")
    payload = {"meta": {"name": "사전 가져오기", "visibility": "private"},
               "fields": [{"fieldId": "k1", "fieldName": e.field_name, "displayName": e.display_name,
                           "dataType": e.data_type, "sourceRef": {"dictionaryId": e.dictionary_id}}]}
    s = schema_services.save_definition(demo_users["researcher"], payload, action="publish")
    assert s.fields.get().field_name == "good_name2"
    admin_client_portal.force_login(demo_users["researcher"])
    r = admin_client_portal.post("/schemas/create", {"payload": json.dumps(payload), "action": "publish"})
    assert r.status_code == 302
