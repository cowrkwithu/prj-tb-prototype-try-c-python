"""3c 새 URL 전체 퍼징 — 어떤 입력에도 500 을 내지 않는다(CLAUDE.md 코딩 규칙, apps/schemas/tests/test_fuzz.py 패턴)."""
import json

import pytest

from apps.datasharing.models import ApiKey, DataAccessRequest

from .conftest import VALID_DAR, auth, make_dar

pytestmark = pytest.mark.django_db

ATTACKS = {
    "long": "가" * 5000,
    "nul": "a\x00b",
    "control": "\r\n=cmd|' /C calc'!A0",
    "html": "<script>alert(1)</script>\"'><img src=x onerror=1>",
    "unicode-digits": "²٣１",
    "negative": "-1",
    "huge-int": "9" * 60,
    "float": "1e308",
    "json": '{"a": [1, {"b": "\\ud800"}]}',
    "deep-json": "[" * 5000,
    "path-traversal": "../../etc/passwd",
    "empty": "",
}
GET_PARAMS = ("tab", "types", "submitted", "type", "status", "page", "open", "q", "format", "next")
GET_URLS = ["/data", "/data/datasets/patient-cases/schema", "/data/datasets/genomics/schema",
            "/data/datasets/pet-ct-studies/schema", "/data/datasets/patient-cases/download.csv",
            "/data/datasets/patient-cases/download.xlsx", "/data/datasets/regimens/download.xlsx",
            "/data/datasets/genomics/download.csv", "/data/custom/schema-001/schema", "/data/custom/schema-001/template.csv",
            "/data/custom/none/schema", "/admin/dar", "/admin/deletions", "/admin"]


@pytest.fixture
def world(demo_users, seeded_patients, loaded_dictionary):
    from apps.schemas.services import load_sample_schemas

    load_sample_schemas()
    from apps.accounts.models import User

    other = User.objects.create_user("other", password="x", role="researcher", organization="몽골국립의료원")
    dar = make_dar(other, ["clinical"])  # 관리자 승인·반려 퍼징 대상(검토중)
    make_dar(demo_users["researcher"], ["clinical", "imaging", "genomics"], "approved")
    return dar


def _ok(r, label):
    assert r.status_code < 500, (label, r.status_code)


@pytest.mark.parametrize("name", list(ATTACKS))
def test_no_500_on_get_urls(client, demo_users, world, name):
    v = ATTACKS[name]
    for user in ("researcher", "admin"):
        client.force_login(demo_users[user])
        for url in GET_URLS + [f"/admin/dar/{world.request_id}"]:
            _ok(client.get(url, {p: v for p in GET_PARAMS}), (user, url, name))
        for tab in ("datasets", "api", "dar", "uploads"):
            _ok(client.get("/data", {"tab": tab, **{p: v for p in GET_PARAMS if p != "tab"}}), (user, tab, name))
            _ok(client.get("/data", {"tab": tab, "types": [v, "clinical", v]}), (user, tab, name, "list"))


@pytest.mark.parametrize("name", list(ATTACKS))
def test_no_500_on_post_urls(client, demo_users, world, name):
    v = ATTACKS[name]
    client.force_login(demo_users["researcher"])
    n = DataAccessRequest.objects.count()
    posts = [
        ("/data/dar", {k: v for k in VALID_DAR} | {"data_types": [v, "imaging"]}),
        ("/data/dar", {**VALID_DAR, "data_types": ["clinical"], "orcid": v}),
        ("/data/dar", {**VALID_DAR, "data_types": ["clinical"], "purpose": v * 2}),
        ("/data/api/key", {"email": v, "institution": v}),
        ("/data/api/key", {"email": "a@b.co", "institution": v}),
        ("/data/api/key/revoke", {"x": v}),
        ("/upload/mine/UPL-20250101-001/delete", {"next": v}),
    ]
    for url, data in posts:
        _ok(client.post(url, data), (url, name))
    assert DataAccessRequest.objects.count() <= n + 1
    client.force_login(demo_users["admin"])
    for url, data in [
        (f"/admin/dar/{world.request_id}/approve", {"types": [v, v], "comment": v}),
        (f"/admin/dar/{world.request_id}/reject", {"reason": v}),
        ("/admin/dar/DAR-2099-001/approve", {"types": ["clinical"]}),
        ("/admin/deletions/UPL-20250101-001/approve", {"x": v}),
        ("/admin/deletions/UPL-20250101-001/reject", {"reason": v}),
    ]:
        _ok(client.post(url, data), (url, name))


@pytest.mark.parametrize("name", list(ATTACKS))
def test_no_500_on_api(client, demo_users, world, name):
    from apps.datasharing import services

    v = ATTACKS[name]
    _, raw = services.issue_key(demo_users["researcher"], "kim@snubh.org", "SNUBH")
    for url in ("/api/v1/cases", "/api/v1/cases/1", "/api/v1/images/1", "/api/v1/genomics/variants", "/api/v1/zz"):
        r = client.get(url, {p: v for p in ("page", "limit", "country", "diagnosisType", "include", "format", "file")},
                       **auth(raw))
        assert r.status_code < 500 or r.status_code == 503, (url, name, r.status_code)
    for body in (v, json.dumps({"name": v, "conditions": v}), json.dumps({"name": "x", "conditions": [{"field": v}]}),
                 json.dumps({"name": "x", "description": v, "conditions": [{"field": "demographics.country",
                                                                            "operator": "in", "value": [v]}]})):
        r = client.post("/api/v1/cohorts", body, content_type="application/json", **auth(raw))
        assert r.status_code < 500, (name, r.status_code)
    assert ApiKey.objects.filter(revoked_at__isnull=True).count() == 1


def test_raw_surrogate_and_binary_body(client, demo_users, world):
    from apps.datasharing import services

    _, raw = services.issue_key(demo_users["researcher"], "kim@snubh.org", "SNUBH")
    for body in (b"\xff\xfe\x00", b"\xed\xa0\x80", b'{"name": "\xed\xa0\x80"}', b"\x00" * 100):
        r = client.post("/api/v1/cohorts", body, content_type="application/json", **auth(raw))
        assert r.status_code == 400


# ─── 구조 퍼징: 본문 JSON 각 키(최상위·조건 노드)에 스칼라·list·dict·null·거대 정수·깊은 중첩 ───
def _deep_list(n):
    return "[" * n + "]" * n


def _deep_children(n):
    return '{"children": [' * n + '{"field": "demographics.age", "operator": "gte", "value": 1}' + "]}" * n


STRUCT_VALUES = {
    "int": "5", "true": "true", "false": "false", "float": "1.5", "neg": "-1", "zero": "0", "str": '"x"',
    "empty-str": '""', "null": "null", "list": "[1, \"a\", null]", "empty-list": "[]", "dict": '{"a": 1}',
    "empty-dict": "{}", "huge-int": "1" + "0" * 400, "huge-float": "1e400", "deep-list": _deep_list(200),
    "deep-list-max": _deep_list(5000), "deep-children": _deep_children(300), "deep-children-max": _deep_children(3000),
    "list-of-scalars": "[5, true, 1.5, null]", "nested-scalar-children": '[{"children": 5}, {"children": {"children": true}}]',
}
NODE_KEYS = ("children", "field", "operator", "value", "logic", "op")
TOP_KEYS = ("name", "description", "conditions")
VALID_NODE = {"field": "demographics.age", "operator": "gte", "value": 1}


def _bodies(raw_value):
    """raw_value 는 JSON 조각(문자열) — 그대로 끼워 넣어 거대 정수·깊은 중첩을 Python 왕복 없이 보낸다."""
    base = {"name": "x", "description": "d", "conditions": [VALID_NODE]}
    for k in TOP_KEYS:
        d = {kk: json.dumps(vv) for kk, vv in base.items()}
        d[k] = raw_value
        yield "{" + ", ".join(f'"{kk}": {vv}' for kk, vv in d.items()) + "}"
    for k in NODE_KEYS:
        node = {kk: json.dumps(vv) for kk, vv in VALID_NODE.items()}
        node[k] = raw_value
        node_s = "{" + ", ".join(f'"{kk}": {vv}' for kk, vv in node.items()) + "}"
        yield '{"name": "x", "conditions": [' + node_s + "]}"
        yield '{"name": "x", "conditions": ' + node_s + "}"
        yield '{"name": "x", "conditions": {"logic": "AND", "children": [' + node_s + "]}}"
    yield '{"name": "x", "conditions": ' + raw_value + "}"
    yield raw_value


@pytest.mark.parametrize("label", list(STRUCT_VALUES))
def test_api_cohorts_structure_fuzz_no_500(client, demo_users, world, settings, label):
    from apps.datasharing import services

    settings.API_RATE_LIMIT_PER_MINUTE = 10_000
    _, raw = services.issue_key(demo_users["researcher"], "kim@snubh.org", "SNUBH")
    for body in _bodies(STRUCT_VALUES[label]):
        r = client.post("/api/v1/cohorts", body, content_type="application/json", **auth(raw))
        assert r.status_code < 500, (label, body[:200], r.status_code)
        assert r.status_code in (201, 400, 403), (label, body[:200], r.status_code)
