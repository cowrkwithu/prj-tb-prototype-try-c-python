"""3a 3차 리뷰: 관리자 데이터사전 항목 저장 — 1e400·inf·NaN·거대 정수 제약값이 500 이 아닌 폼 오류가 된다."""
import pytest

from apps.dictionary import admin_services
from apps.dictionary.models import DictionaryCategory, DictionaryField

pytestmark = pytest.mark.django_db

BAD = ["1e400", "inf", "-inf", "1e999", "nan", "Infinity", "9" * 400, "1e16"]


def _messages(r):
    return [m.message for m in r.wsgi_request._messages]


@pytest.mark.parametrize("value", BAD)
@pytest.mark.parametrize("key", ["min", "max", "maxLength", "precision"])
def test_entry_save_rejects_nonfinite_numbers(admin_client_portal, loaded_dictionary, key, value):
    c = admin_client_portal
    cat = DictionaryCategory.objects.filter(version=loaded_dictionary).first()
    # 새 항목 추가
    r = c.post("/admin/dictionary/entry/save", {"cat": cat.pk, "field_name": "ovf_new", "display_name": "x",
                                                "data_type": "DECIMAL", key: value})
    assert r.status_code == 302
    assert not DictionaryField.objects.filter(field_name="ovf_new").exists()
    assert any("숫자" in m for m in _messages(r))
    # 기존 항목 수정
    e = DictionaryField.objects.filter(version=loaded_dictionary, data_type="DECIMAL").first()
    before = dict(e.constraints or {})
    r = c.post("/admin/dictionary/entry/save", {"id": e.pk, "field_name": e.field_name, "display_name": e.display_name,
                                                "data_type": "DECIMAL", key: value})
    assert r.status_code == 302
    e.refresh_from_db()
    assert (e.constraints or {}) == before


def test_num_helper():
    for v in BAD + [[1], {"a": 1}]:
        with pytest.raises(ValueError):
            admin_services._num(v)
    assert admin_services._num(" 12 ") == 12 and admin_services._num("1.5") == 1.5
    assert admin_services._num("3", True) == 3
