import json

import pytest

from .conftest import simple_payload


@pytest.mark.django_db
def test_pages_render(researcher_client, seeded_schemas, seeded_patients):
    for url in ["/schemas", "/schemas?q=QoL&sort=name", "/schemas/create", "/schemas/schema-001", "/schemas/schema-001?tab=data&sort=onset_day&dir=desc",
                "/schemas/schema-001/data", "/schemas/schema-001/data?method=form", "/schemas/schema-001/data?method=csv",
                "/schemas/select", "/schemas/import", "/schemas/builder/dictionary?cat=pat", "/schemas/builder/dictionary?q=체중",
                "/schemas/case/KR-2024-0001", "/schemas/schema-001/records/row-ae-001", "/schemas/schema-001/template.csv",
                "/schemas/schema-001/records.csv", "/schemas/schema-001/export.json"]:
        r = researcher_client.get(url)
        assert r.status_code == 200, (url, r.status_code)
    r = researcher_client.post("/schemas/builder/preview", {"payload": json.dumps(simple_payload()), "fill": "1"})
    assert r.status_code == 200


@pytest.mark.django_db
def test_admin_pages_render(admin_client_portal, seeded_schemas):
    from apps.dictionary.models import DictionaryCategory

    cat_pk = DictionaryCategory.objects.order_by("pk").values_list("pk", flat=True).first()  # PostgreSQL 은 테스트마다 시퀀스가 이어진다(1 고정 불가)
    for url in ["/admin/schemas", "/admin/schemas?status=published&q=PFT", "/admin/schemas/schema-001", "/admin/schemas/schema-004/confirm?action=publish",
                "/admin/dictionary", "/admin/dictionary?cat=soc&dep=1", "/admin/dictionary?view=versions", "/admin/dictionary?view=history",
                "/admin/dictionary/category/modal", f"/admin/dictionary/entry/modal?cat={cat_pk}", "/schemas/schema-004/edit", "/schemas/schema-001/data?method=edit"]:
        r = admin_client_portal.get(url)
        assert r.status_code == 200, (url, r.status_code)


@pytest.mark.django_db
def test_pages_render_in_english(admin_client_portal, seeded_schemas):
    c = admin_client_portal
    c.post("/lang", {"lang": "en"})
    pages = {"/schemas": "Custom Data Schemas", "/schemas/schema-001": "Adverse Drug Reaction Tracking Data",
             "/schemas/schema-001/data?method=form": "Web Form Entry", "/schemas/create": "Data Schema Info",
             "/admin/schemas": "Data Schema Management", "/admin/dictionary": "Data Dictionary Management",
             "/admin/dictionary?view=versions": "Excel reload policy"}
    for url, text in pages.items():
        r = c.get(url)
        assert r.status_code == 200 and text in r.content.decode(), url
