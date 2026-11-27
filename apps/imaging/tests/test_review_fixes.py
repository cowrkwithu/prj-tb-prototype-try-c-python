"""4단계 1차 리뷰 수정 — 공개 샘플 검사의 외부 공유·케이스 목록 제외, 볼 권한 없는 영상의 소견 비노출, 모달 검사 연결,
유전체 내보내기 변이 단위 묶음."""
import csv
import io
import re

import pytest

from apps.cases import imaging as case_imaging
from apps.cases.models import ImagingStudy, Patient
from apps.datasharing import services as ds
from apps.datasharing.tests.conftest import auth, make_dar
from apps.imaging.models import ExpertAnnotation

from .conftest import make_uploaded_study
from .test_views import RECT, cxr_sample, rap_data

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_key(demo_users, samples):
    make_dar(demo_users["researcher"], ["clinical", "imaging", "genomics"], "approved")
    _, raw = ds.issue_key(demo_users["researcher"], "kim@snubh.org", "SNUBH")
    return raw


# ─── 2) 케이스 상세 '업로드된 영상 검사' 목록 ───
def test_case_detail_lists_only_uploaded_studies(researcher_client, demo_users, samples):
    p = Patient.objects.get(pk=3)
    body = researcher_client.get(f"/cases?id={p.pk}&tab=2").content.decode()
    assert 'data-testid="imaging-studies"' not in body  # 샘플만 있는 환자 — 목록 없음
    make_uploaded_study(demo_users["researcher"], p)
    body = researcher_client.get(f"/cases?id={p.pk}&tab=2").content.decode()
    block = body.split('data-testid="imaging-studies"', 1)[1].split("</ul>", 1)[0]
    assert block.count("<li>") == 1 and "2025-06-01" in block


# ─── 3) 외부 공유(API·내보내기)에서 공개 샘플 제외 ───
def test_api_excludes_public_samples(client, demo_users, api_key):
    p = Patient.objects.get(pk=4)
    up = make_uploaded_study(demo_users["researcher"], p)
    d = client.get(f"/api/v1/cases/{p.pk}?include=imaging", **auth(api_key)).json()
    assert [s["id"] for s in d["imaging"]["studies"]] == [up.pk]
    sample = cxr_sample()
    r = client.get(f"/api/v1/images/{sample.pk}", **auth(api_key))
    assert r.status_code == 404 and r.json()["status"] == "not_found"
    assert client.get(f"/api/v1/images/{sample.pk}?format=jpg", **auth(api_key)).status_code == 404
    assert client.get(f"/api/v1/images/{up.pk}", **auth(api_key)).status_code == 200


def test_imaging_exports_have_no_sample_rows(researcher_client, demo_users, samples):
    make_dar(demo_users["researcher"], ["imaging"], "approved")
    for slug, has in (("cxr-images", "has_cxr"), ("ct-studies", "has_ct"), ("pet-ct-studies", "has_pet_ct")):
        r = researcher_client.get(f"/data/datasets/{slug}/download.csv")
        rows = list(csv.reader(io.StringIO(b"".join(r.streaming_content).decode("utf-8")[1:])))
        # 케이스 단위 행만(검사 행 없음) — 공개 샘플 검사 수(207)와 무관
        assert len(rows) - 1 == Patient.objects.filter(**{has: True}).count()
        assert not any("sample" in ",".join(row).lower() for row in rows)


# ─── 4) 볼 권한 없는 업로드 영상의 전문가 소견 ───
def test_hidden_expert_findings_for_unviewable_study(client, demo_users, other_user, samples):
    p = Patient.objects.get(pk=3)
    up = make_uploaded_study(demo_users["researcher"], p)
    client.force_login(demo_users["researcher"])
    assert client.post("/rap/annotations", {**RECT, "study": up.pk, "text": "비밀 메모"}).status_code == 201
    client.force_login(other_user)
    resp = client.get(f"/rap?patient={p.condition_id}")
    d = rap_data(resp)
    assert d["annotations"] == [] and "비밀 메모" not in resp.content.decode()
    s = next(x for x in d["studies"] if x["id"] == up.pk)
    assert s["hidden_expert"] == 1
    make_dar(other_user, ["imaging"], "approved")
    d = rap_data(client.get(f"/rap?patient={p.condition_id}"))
    assert [a["text"] for a in d["annotations"]] == ["비밀 메모"]
    assert next(x for x in d["studies"] if x["id"] == up.pk)["hidden_expert"] == 0


def test_case_modal_links_by_study_not_date(researcher_client, demo_users, samples):
    p = Patient.objects.get(pk=4)
    gen = case_imaging.generate_images(p)
    cxrs = [g for g in gen if g["type"] == "CXR"]
    second = ImagingStudy.objects.get(seed_key=f"sample:{p.condition_id}:CXR:2")
    assert second.study_date.isoformat() == cxrs[1]["date"]
    assert researcher_client.post("/rap/annotations", {**RECT, "study": second.pk, "text": "둘째 검사"}).status_code == 201
    # 같은 날짜·모달리티의 다른 검사(업로드)에 붙은 소견은 섞이지 않는다
    other = make_uploaded_study(demo_users["researcher"], p)
    ImagingStudy.objects.filter(pk=other.pk).update(study_date=second.study_date)
    ExpertAnnotation.objects.create(study=other, shape="text", x=1, y=1, text="다른 검사", author_name="x")
    body = researcher_client.get(f"/cases/4/images/{cxrs[1]['id']}").content.decode()
    assert "둘째 검사" in body and "다른 검사" not in body and 'data-testid="expert-count">1건' in body
    body = researcher_client.get(f"/cases/4/images/{cxrs[0]['id']}").content.decode()
    assert "둘째 검사" not in body and 'data-testid="expert-count">0건' in body


# ─── 5) 유전체 데이터셋 내보내기 — 카탈로그 변이 단위·GAP 과 같은 분모 ───
def test_genomics_export_groups_aliases(researcher_client, demo_users, seeded_patients):
    from apps.genomics.services import genomic_patients_q, load_genomics

    load_genomics()
    p = Patient.objects.get(pk=1)
    p.resistance_mutations = ["rpoB S531L", "RPOB s450l", "fooX A1B"]  # 별칭 두 표기 → 한 행, 한 명
    p.has_sequence = False
    p.save()
    make_dar(demo_users["researcher"], ["genomics"], "approved")
    r = researcher_client.get("/data/datasets/genomics/download.csv")
    rows = list(csv.reader(io.StringIO(b"".join(r.streaming_content).decode("utf-8")[1:])))
    rpob = [row for row in rows if row[0] == "rpoB" and row[4] == "S450L"]
    denom = Patient.objects.filter(genomic_patients_q()).count()
    assert len(rpob) == 1 and rpob[0][7] == "18" and rpob[0][6] == f"{18 / denom * 100:.1f}"
    assert not any(row[4] == "S531L" for row in rows)
    assert len(rows) - 1 == ds.export_row_count("genomics")
    assert re.fullmatch(r"\d+\.\d", next(row for row in rows if row[0] == "fooX")[6])
