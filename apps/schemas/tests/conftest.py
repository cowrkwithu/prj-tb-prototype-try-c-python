import pytest


@pytest.fixture(autouse=True)
def _media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()
    return settings.MEDIA_ROOT


@pytest.fixture
def seeded_schemas(db, loaded_dictionary):
    from apps.schemas.services import load_sample_schemas

    return load_sample_schemas()


@pytest.fixture
def other_researcher(db):
    from apps.accounts.models import User

    return User.objects.create_user("other", password="x", role="researcher", organization="몽골국립의료원")


@pytest.fixture
def same_org_researcher(db):
    from apps.accounts.models import User

    return User.objects.create_user("colleague", password="x", role="researcher", organization="분당서울대병원")


def simple_payload(name="테스트 구조", fields=None, **meta):
    m = {"name": name, "category": "clinical_ext", "visibility": "public", "linkFieldName": "condition_id"}
    m.update(meta)
    return {"meta": m, "fields": fields if fields is not None else [
        {"fieldId": "k1", "fieldName": "score", "displayName": "점수", "dataType": "INTEGER",
         "constraints": {"min": 0, "max": 10}, "isRequired": True},
        {"fieldId": "k2", "fieldName": "note", "displayName": "메모", "dataType": "TEXT", "constraints": {"maxLength": 20}},
    ]}


@pytest.fixture
def published_schema(db, demo_users):
    from apps.schemas import services

    s = services.save_definition(demo_users["researcher"], simple_payload(), action="publish")
    return s
