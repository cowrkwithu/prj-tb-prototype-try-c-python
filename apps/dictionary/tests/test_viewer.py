"""표준 데이터사전 뷰어·검색·AI 도우미.

검색 기대 건수는 원본 매뉴얼(src/pages/Manual/manualContent.ts)에 적힌 도우미 시나리오 결과 수:
내성 27, 흉부 84(영문 chest 85), 순응도 37, 변이 23, 약물농도 18.
원본 저장소가 있으면 standardDictionary.ts 에 원본 matchDictionaryEntries 규칙을 그대로 적용한 결과와 순서까지 비교한다.
"""
import json

import pytest
from django.conf import settings

from apps.dictionary import viewer
from apps.dictionary.models import DictionaryField

pytestmark = pytest.mark.django_db
ORIGINAL_TS = settings.BASE_DIR.parent / "prj-tb-prototype-try-b" / "src" / "data" / "standardDictionary.ts"


def ids(entries):
    return [e.dictionary_id for e in entries]


@pytest.mark.parametrize("q,n", [("내성", 27), ("흉부", 84), ("순응도", 37), ("변이", 23), ("약물농도", 18), ("chest", 85)])
def test_search_counts_match_manual(loaded_dictionary, q, n):
    assert len(viewer.match_entries(list(viewer.current_fields()), q)) == n


def _ts_match(entries, q):
    """원본 utils/dictionarySearch.ts 를 글자 그대로 옮긴 참조 구현 (TS 항목 dict 대상)."""
    query = q.strip().lower()
    if not query:
        return []
    has = lambda s: query in (s or "").lower()  # noqa: E731
    by_name = [e for e in entries if has(e.get("displayName")) or has(e.get("displayNameEn")) or has(e.get("fieldName"))]
    return by_name if by_name else [e for e in entries if has(e.get("description")) or has(e.get("descriptionEn"))]


@pytest.mark.parametrize("q", ["내성", "resistance", "흉부", "BMI", "  Weight ", "tb_u_dst_01", "결핵균 유전형", "zzzz-none", "PHQ"])
def test_search_same_order_as_original(loaded_dictionary, q):
    if not ORIGINAL_TS.exists():
        pytest.skip("원본 저장소 없음")
    from scripts.convert_mock_ts import extract_exports

    ts = extract_exports(ORIGINAL_TS.read_text(encoding="utf-8"))["STANDARD_DICTIONARY"]
    expected = [e["dictionaryId"] for e in _ts_match(ts, q)]
    assert ids(viewer.match_entries(list(viewer.current_fields()), q)) == expected


def test_description_fallback(loaded_dictionary):
    entries = list(viewer.current_fields())
    word = "고유하게"  # 항목명에는 없고 설명에만 있는 낱말
    assert not any(word in (e.display_name or "") for e in entries)
    res = viewer.match_entries(entries, word)
    assert res and all(word in (e.description or "") or word in (e.description_en or "").lower() for e in res)


def test_filter_entries_category_type_and_search(loaded_dictionary):
    dst = viewer.filter_entries("dst", "", [])
    assert len(dst) == 194 and all(e.category.code == "dst" for e in dst)
    sel = viewer.filter_entries("dst", "", ["SELECT"])
    assert sel and all(e.data_type == "SELECT" for e in sel)
    # 검색은 카테고리와 무관 (원본 동일)
    assert len(viewer.filter_entries("pat", "내성", [])) == 27


def test_deprecated_hidden(loaded_dictionary):
    DictionaryField.objects.filter(dictionary_id="TB.U.PAT.001").update(is_deprecated=True)
    assert "TB.U.PAT.001" not in ids(viewer.current_fields())
    cats = {c.code: c.n for c in viewer.current_categories()}
    assert cats["pat"] == 48


def test_view_requires_login(client):
    assert client.get("/dictionary")["Location"].startswith("/login?next=")
    assert client.get("/dictionary/field/TB.U.PAT.001")["Location"].startswith("/login?next=")
    assert client.post("/dictionary/assistant", {"scenario": "dst"})["Location"].startswith("/login?next=")


def test_card_view(researcher_client, loaded_dictionary):
    body = researcher_client.get("/dictionary").content.decode()
    assert "① 환자 기본정보" in body and len(__import__("re").findall(r'class="dict-cat(?: active)?"', body)) == 19
    assert body.count('class="dict-card"') == 24 and "1 / 3 페이지" in body and "(총 49건)" in body
    body = researcher_client.get("/dictionary?cat=dst&page=9").content.decode()
    assert "9 / 9 페이지" in body and body.count('class="dict-card"') == 194 - 8 * 24


def test_search_and_type_filter_view(researcher_client, loaded_dictionary):
    body = researcher_client.get("/dictionary?q=내성").content.decode()
    assert '검색 결과: "내성"' in body and 'data-testid="dict-total">27건' in body
    body = researcher_client.get("/dictionary?cat=dst&type=SELECT&type=SELECT&type=BOGUS").content.decode()
    n = len(viewer.filter_entries("dst", "", ["SELECT"]))
    assert f"(총 {n}건)" in body or body.count('class="dict-card"') == n
    assert body.count('class="type-pill"') == 1


def test_table_view_search_sort_page(researcher_client, loaded_dictionary):
    body = researcher_client.get("/dictionary?view=table&cat=pat&ps=50").content.decode()
    assert "총 49건" in body and body.count('data-testid="row-') == 49
    body = researcher_client.get("/dictionary?view=table&cat=pat&sort=fieldName&dir=desc").content.decode()
    rows = [l.split('data-testid="row-')[1].split('"')[0] for l in body.split("\n") if 'data-testid="row-' in l]
    assert rows[0] == "TB.U.PAT.049" and len(rows) == 10
    body = researcher_client.get("/dictionary?view=table&cat=pat&tq=tb_u_pat_00").content.decode()
    assert "총 9건" in body
    res = viewer.table_view(viewer.filter_entries("pat", "", []), "ko", "", "cdiscMapping", "asc", 0, 50)
    vals = [r["cdiscMapping"] for r in res.rows]
    assert vals == sorted([v for v in vals if v], key=str.casefold) + [v for v in vals if not v]


def test_field_detail(researcher_client, loaded_dictionary):
    sel = DictionaryField.objects.filter(data_type="SELECT").first()
    body = researcher_client.get(f"/dictionary/field/{sel.dictionary_id}").content.decode()
    assert "필드 상세 정보" in body and sel.field_name in body and f"선택지 ({len(sel.options)}개)" in body
    assert researcher_client.get("/dictionary/field/TB.U.NOPE.999").status_code == 404
    DictionaryField.objects.filter(pk=sel.pk).update(is_deprecated=True)
    assert researcher_client.get(f"/dictionary/field/{sel.dictionary_id}").status_code == 404


def test_field_detail_english(researcher_client, loaded_dictionary):
    researcher_client.post("/lang", {"lang": "en"})
    body = researcher_client.get("/dictionary/field/TB.U.PAT.001").content.decode()
    f = DictionaryField.objects.get(dictionary_id="TB.U.PAT.001")
    assert "Field Details" in body and f.display_name_en in body


def test_assistant_scenario(researcher_client, loaded_dictionary):
    r = researcher_client.post("/dictionary/assistant", {"scenario": "dst"})
    body = r.content.decode()
    assert "총 27개 항목을 찾아 아래 목록에 표시했습니다." in body
    assert body.count('class="ai-result"') == 6 and "외 21개 항목을 메인 목록에 표시했어요." in body
    assert json.loads(r["HX-Trigger"]) == {"dictApplySearch": {"q": "내성"}}


def test_assistant_english_scenario_and_free_text(researcher_client, loaded_dictionary):
    researcher_client.post("/lang", {"lang": "en"})
    r = researcher_client.post("/dictionary/assistant", {"scenario": "img"})
    assert "Found 85 items" in r.content.decode() and json.loads(r["HX-Trigger"])["dictApplySearch"]["q"] == "chest"
    r = researcher_client.post("/dictionary/assistant", {"text": "Find data items related to drug resistance"})
    assert "I found items related to drug resistance." in r.content.decode()


def test_assistant_help_and_no_result(researcher_client, loaded_dictionary):
    r = researcher_client.post("/dictionary/assistant", {"text": "무엇을 질문할 수 있어?"})
    assert "프로토타입 단계" in r.content.decode() and "HX-Trigger" not in r
    r = researcher_client.post("/dictionary/assistant", {"help": "1"})
    assert r.content.decode().count("ai-suggestion") == 5
    r = researcher_client.post("/dictionary/assistant", {"text": "qqqzzz"})
    assert "일치하는 항목을 찾지 못했어요" in r.content.decode() and "HX-Trigger" not in r
    r = researcher_client.post("/dictionary/assistant", {"text": "<script>alert(1)</script>"})
    assert "<script>alert(1)" not in r.content.decode()


def test_assistant_methods(researcher_client, loaded_dictionary):
    assert researcher_client.get("/dictionary/assistant").status_code == 405
    from django.test import Client

    from apps.accounts.models import User

    c = Client(enforce_csrf_checks=True)
    c.force_login(User.objects.get(username="researcher"))
    assert c.post("/dictionary/assistant", {"scenario": "dst"}).status_code == 403


def test_no_dictionary_loaded(researcher_client):
    r = researcher_client.get("/dictionary")
    assert r.status_code == 200 and "적재된 표준 데이터사전이 없습니다" in r.content.decode()


def test_nav_links_point_to_real_pages(researcher_client, loaded_dictionary, seeded_patients):
    body = researcher_client.get("/").content.decode()
    assert 'href="/dictionary"' in body and 'href="/cases"' in body
    for path in ("/cases", "/dictionary"):
        assert "준비 중입니다" not in researcher_client.get(path).content.decode()
