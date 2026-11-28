"""사용자 매뉴얼(/manual) — 시드 멱등·DB 화면·검색(원본 규칙)·한/영·교정 반영."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

from apps.core.models import SeedLog
from apps.manual import services
from apps.manual.models import ManualBlock, ManualSection, ManualTopic

pytestmark = pytest.mark.django_db
ROOT = Path(settings.BASE_DIR)
ORIGINAL = ROOT.parent / "prj-tb-prototype-try-b" / "src" / "pages" / "Manual" / "manualContent.ts"


@pytest.fixture
def manual(db):
    return services.load_manual()


def _seed():
    return json.loads((settings.SEED_DIR / "manual.json").read_text(encoding="utf-8"))


# ─── 시드 ───
def test_seed_counts_and_idempotent(manual):
    assert manual == {"sections": 5, "topics": 26, "blocks": 135, "skipped": False}
    assert (ManualSection.objects.count(), ManualTopic.objects.count(), ManualBlock.objects.count()) == (5, 26, 135)
    assert services.load_manual()["skipped"] is True
    services.load_manual(force=True)
    services.load_manual(force=True)
    assert (ManualSection.objects.count(), ManualTopic.objects.count(), ManualBlock.objects.count()) == (5, 26, 135)
    assert SeedLog.objects.filter(dataset="manual").count() == 1


def test_seed_does_not_resurrect_deleted_topic_without_force(manual):
    ManualTopic.objects.filter(key="start-overview").delete()
    services.load_manual()
    assert not ManualTopic.objects.filter(key="start-overview").exists()
    services.load_manual(force=True)
    assert ManualTopic.objects.filter(key="start-overview").exists()


def test_force_restores_seed_text_and_keeps_admin_topics(manual):
    t = ManualTopic.objects.get(key="start-login")
    t.title = "바뀐 제목"
    t.save()
    extra = ManualTopic.objects.create(section=t.section, key="admin-extra", order=99, title="관리자 추가")
    services.load_manual(force=True)
    assert ManualTopic.objects.get(key="start-login").title == "로그인과 사용자 권한"
    assert ManualTopic.objects.filter(pk=extra.pk).exists()


def test_search_text_flattened(manual):
    t = ManualTopic.objects.get(key="comp-upload")
    assert "개인정보" in t.search_text and "personal data" in t.search_text_en
    assert "비식별화 확인" in t.search_text  # 이미지 캡션도 포함(원본 blockText)


def test_all_images_exist_in_static(manual):
    for b in ManualBlock.objects.filter(kind="image"):
        assert (ROOT / "static" / b.content["src"]).is_file(), b.content["src"]


@pytest.mark.skipif(not ORIGINAL.exists(), reason="원본 저장소 없음")
def test_seed_json_matches_converter():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "convert_manual_ts.py"), "--check"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_overrides_documented_and_applied():
    data = _seed()
    text = json.dumps(data, ensure_ascii=False)
    for gone in ("브라우저 세션에 저장되므로", "이 브라우저에 환자별로 저장됩니다", "SNP Distance·cgMLST·Phylogenetic 중",
                 "FASTQ/FASTA 업로드 후 다중 알고리즘", "데이터셋 2개(Case/Control)", "전체 N건 중 샘플 M건",
                 "자동 탐지 대상이 아니므로", "새로고침하면 시연용 초기 상태", "등록되지는 않습니다", "측정, 회전",
                 "결과를 PDF로", "접수/검토중/승인/반려", "고른 소견 조건은 데이터 탐색으로 전달되지 않습니다"):
        assert gone not in text, gone
    ov = json.loads((settings.SEED_DIR / "manual_overrides.json").read_text(encoding="utf-8"))["overrides"]
    assert len(ov) == 40 and all(o.get("reason") and o.get("id") for o in ov)
    assert len({o["id"] for o in ov}) == len(ov)


# ─── 화면 ───
def test_login_required(client, manual):
    r = client.get("/manual")
    assert r.status_code == 302 and r["Location"].startswith("/login?next=")


def test_renders_from_db(researcher_client, manual):
    body = researcher_client.get("/manual").content.decode()
    assert "사용자 매뉴얼" in body and 'data-testid="manual-toc"' in body
    assert body.count('class="manual-section"') == 5 and body.count('class="manual-topic"') == 26
    assert 'x-data="{ open: true }" @manual-open.window="if ($event.detail === \'start\')' in body  # 첫 장만 펼침
    assert "/static/manual/layout-home.png" in body and "플랫폼 개요" in body
    # DB 를 고치면 화면이 바뀐다
    ManualTopic.objects.filter(key="start-overview").update(title="DB 에서 읽은 제목")
    assert "DB 에서 읽은 제목" in researcher_client.get("/manual").content.decode()


def test_nav_link_active(researcher_client, manual):
    body = researcher_client.get("/manual").content.decode()
    assert 'href="/manual"' in body


def test_english(researcher_client, manual):
    researcher_client.post("/lang", {"lang": "en"})
    body = researcher_client.get("/manual").content.decode()
    assert "User Manual" in body and "Getting Started" in body and "Platform Overview" in body
    assert "/static/manual/en/layout-home.png" in body  # 영어 캡처가 있으면 영어
    assert "플랫폼 개요" not in body


def test_english_image_fallback(researcher_client, manual):
    b = ManualBlock.objects.filter(kind="image").first()
    b.content = {**b.content, "src": "manual/layout-home.png"}
    b.save()
    assert services.image_src("manual/no-such-en.png", "en") == "manual/no-such-en.png"
    assert services.image_src("manual/layout-home.png", "en") == "manual/en/layout-home.png"


def test_search_topic_match(researcher_client, manual):
    body = researcher_client.get("/manual", {"q": "개인정보"}).content.decode()
    assert 'data-testid="manual-toc"' not in body  # 검색 중에는 목차 숨김
    assert 'data-topic="comp-upload"' in body and 'data-topic="start-overview"' not in body
    assert 'x-data="{ open: true }" data-topic="comp-upload"' in body  # 일치 소주제는 펼침


def test_search_section_match_includes_all_topics(researcher_client, manual):
    body = researcher_client.get("/manual", {"q": "그 외 구성요소"}).content.decode()
    n = ManualTopic.objects.filter(section__key="components").count()
    assert body.count('class="manual-topic"') == n


def test_search_case_insensitive_and_english(researcher_client, manual):
    researcher_client.post("/lang", {"lang": "en"})
    body = researcher_client.get("/manual", {"q": "DATASET"}).content.decode()
    assert 'data-section="analysis"' in body
    # 영어 화면에서는 영어 문구로 찾는다 — 한국어 검색어는 일치하지 않는다
    assert 'data-testid="manual-empty"' in researcher_client.get("/manual", {"q": "개인정보"}).content.decode()


def test_search_no_result_and_special_chars(researcher_client, manual):
    # LIKE 와일드카드(%, _)는 글자 그대로 찾는다(ORM 이 이스케이프)
    for q in ("없는검색어xyz", "%%%", "_q_z_", "\\", "' OR 1=1 --"):
        r = researcher_client.get("/manual", {"q": q})
        assert r.status_code == 200
        assert 'data-testid="manual-empty"' in r.content.decode(), q


def test_search_query_capped(researcher_client, manual):
    r = researcher_client.get("/manual", {"q": "업로드" + "x" * 500})
    assert r.status_code == 200 and 'value="업로드' + "x" * 97 + '"' in r.content.decode()


def test_htmx_partial(researcher_client, manual):
    r = researcher_client.get("/manual", {"q": "업로드"}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="manual-results")
    body = r.content.decode()
    assert r.status_code == 200 and "<html" not in body and 'class="manual-section"' in body


def test_malformed_block_skipped_and_escaped(researcher_client, manual):
    t = ManualTopic.objects.get(key="start-overview")
    ManualBlock.objects.create(topic=t, order=50, kind="paragraph", content={"text": "깨진 형식"})
    ManualBlock.objects.create(topic=t, order=51, kind="image", content={"src": "../secret", "caption": {"ko": "x", "en": "x"}})
    ManualBlock.objects.create(topic=t, order=52, kind="note", content={"text": {"ko": "<script>alert(1)</script>", "en": "x"}})
    ManualBlock.objects.create(topic=t, order=53, kind="table", content={"headers": [{"ko": "a", "en": "a"}], "rows": [["x"]]})
    r = researcher_client.get("/manual")
    body = r.content.decode()
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "../secret" not in body


def test_empty_db_message(researcher_client):
    body = researcher_client.get("/manual").content.decode()
    assert 'data-testid="manual-empty"' in body and "load_manual" in body


def test_bootstrap_loads_manual(db):
    from django.core.management import call_command

    call_command("load_manual")
    assert ManualSection.objects.count() == 5
    call_command("load_manual")
    assert ManualSection.objects.count() == 5
