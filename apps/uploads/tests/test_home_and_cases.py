"""홈 편집 콘텐츠 DB화 · 홈 업로드 연동 · 케이스 상세 영상 썸네일 상한/지연 로드 · 1단계 NIT."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.cases.models import Patient
from apps.core.home_content import seed_home_content
from apps.core.models import BoardPost, NewsItem, Notice, RelatedSite, ResearchProgram

pytestmark = pytest.mark.django_db


def test_home_content_seeded_once_and_rendered_from_db(client):
    res = seed_home_content()
    assert res["news"] == 4 and res["board"] == 15 and res["notices"] == 3 and res["programs"] == 4 and res["sites"] == 6
    assert seed_home_content()["skipped"]
    body = client.get("/").content.decode()
    assert "AI 기반 결핵 흉부 X선 판독 정확도 97% 달성" in body and "결핵 백신·예방 연구" in body
    # '전체' 탭 = 분류 무관 최신 5건 (원본 BOARD_TABS 'all' 과 같은 순서)
    from apps.core.home_content import home_sections

    all_tab = home_sections("ko")["board"][0]["items"]
    assert [i["date"] for i in all_tab] == ["2026-02-28", "2026-02-26", "2026-02-24", "2026-02-20", "2026-02-18"]
    # DB 를 고치면 홈이 바로 바뀐다(/django-admin 편집과 같은 효과)
    n = NewsItem.objects.order_by("-date").first()
    n.title, n.title_en = "새 소식 제목", "Fresh headline"
    n.save()
    Notice.objects.create(date="2026-03-05", title="새 공지", title_en="New notice")
    BoardPost.objects.filter(category="careers").update(is_published=False)
    body = client.get("/").content.decode()
    assert "새 소식 제목" in body and "새 공지" in body and "바이오인포매틱스 분석 전문인력 채용" not in body
    client.post("/lang", {"lang": "en"})
    assert "Fresh headline" in client.get("/").content.decode()
    # 사이트 URL 이 있으면 새 창 링크
    RelatedSite.objects.filter(name="국립보건연구원").update(url="https://www.nih.go.kr")
    assert 'href="https://www.nih.go.kr"' in client.get("/").content.decode()


def test_home_content_registered_in_django_admin(client, demo_users):
    seed_home_content()
    client.force_login(demo_users["admin"])
    for model in ("newsitem", "boardpost", "notice", "researchprogram", "relatedsite"):
        assert client.get(f"/django-admin/core/{model}/").status_code == 200, model


def test_home_query_count_bounded(client, seeded_patients):
    seed_home_content()
    client.get("/")  # 캐시 워밍
    with CaptureQueriesContext(connection) as ctx:
        assert client.get("/").status_code == 200
    # 홈 콘텐츠(5) + 업로드 연동(2) 을 더해도 일정한 수 안에서 끝난다(콘텐츠 건수와 무관)
    first = len(ctx.captured_queries)
    BoardPost.objects.bulk_create([BoardPost(category="press", tag="보도", date="2025-01-01", title=f"t{i}") for i in range(30)])
    ResearchProgram.objects.create(icon="dna", title="추가 연구")
    with CaptureQueriesContext(connection) as ctx2:
        client.get("/")
    assert len(ctx2.captured_queries) == first and first < 40


def test_contribution_range_shown(client):
    from django.utils import timezone

    body = client.get("/").content.decode()
    assert 'data-testid="contrib-range"' in body and timezone.localdate().isoformat() in body


def test_case_thumbnails_capped_and_lazy(client, demo_users, seeded_patients):
    client.force_login(demo_users["researcher"])
    p = Patient.objects.get(pk=1)
    p.cxr_count, p.has_cxr = 60, True
    p.save()
    body = client.get("/cases?id=1&tab=1").content.decode()
    assert body.count('class="thumb"') == 24 and 'data-testid="thumb-more"' in body
    assert body.count('data-testid="image-modal"') == 1 and "data-image-detail" not in body  # 모달 본문은 지연 로드
    nxt = client.get("/cases/1/images?offset=24").content.decode()
    assert nxt.count('class="thumb"') == 24 and "offset=48" in nxt
    last = client.get("/cases/1/images?offset=48").content.decode()
    assert last.count('class="thumb"') == 12 + 0 and "thumb-more" not in last
    detail = client.get("/cases/1/images/3").content.decode()
    assert 'data-image-detail="3"' in detail
    assert "전문의가 그린 영역·텍스트(하늘색) 표시를 숨깁니다." in detail
    assert client.get("/cases/1/images/999").status_code == 404
    client.logout()
    assert client.get("/cases/1/images/1").status_code == 302


def test_dictionary_table_tooltips(client, demo_users, loaded_dictionary):
    client.force_login(demo_users["researcher"])
    body = client.get("/dictionary?view=table&page=2").content.decode()
    assert "이전 페이지의 목록을 봅니다." in body
    assert body.count("행을 누르면 이 필드의 정의·제약조건·코드 매핑을 자세히 봅니다.") >= 10
    js = (__import__("pathlib").Path(__file__).resolve().parents[3] / "static" / "js" / "dictionary.js").read_text(encoding="utf-8")
    assert "htmx:sendError" in js
