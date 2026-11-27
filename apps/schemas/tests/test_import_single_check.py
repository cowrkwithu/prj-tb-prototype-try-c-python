"""3a NIT(3c 처리) — 가져오기 확정은 정의 검사(정규식 판정)를 한 번만 한다: 파싱과 저장이 같은 CheckContext 를 쓴다."""
import json

import pytest

from apps.schemas import validation
from apps.schemas.models import Schema

pytestmark = pytest.mark.django_db


def test_import_confirm_checks_each_pattern_once(researcher_client, monkeypatch):
    calls = []
    real = validation._regex_problem

    def spy(pattern, ctx, n):
        calls.append(pattern)
        return real(pattern, ctx, n)

    monkeypatch.setattr(validation, "_regex_problem", spy)
    validation.compile_cache_clear()
    obj = {"name": "가져오기", "category": "clinical_ext", "fields": [
        {"fieldId": "a", "fieldName": "code_a", "displayName": "A", "dataType": "TEXT", "constraints": {"pattern": "^[A-Z]{2}[0-9]{3}$"}},
        {"fieldId": "b", "fieldName": "code_b", "displayName": "B", "dataType": "TEXT", "constraints": {"pattern": "^x+$"}},
    ]}
    r = researcher_client.post("/schemas/import", {"confirm": "1", "json": json.dumps(obj)})
    assert r.status_code == 204 and Schema.objects.count() == 1
    assert sorted(calls) == ["^[A-Z]{2}[0-9]{3}$", "^x+$"]  # 파싱 1회 + 저장 재사용(추가 판정 없음)
