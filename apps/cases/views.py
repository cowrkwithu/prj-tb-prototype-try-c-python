from urllib.parse import urlencode

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_safe

from apps.accounts.decorators import login_required
from apps.core.i18n import get_lang

from . import imaging, services
from .models import Patient

TABS = [
    ("임상정보", "Clinical", "환자 기본정보, 진단 정보와 치료 레지멘 타임라인 차트를 봅니다.",
     "Patient information, diagnosis and the treatment regimen timeline chart."),
    ("영상", "Imaging", "CXR·CT·PET-CT 영상 썸네일을 보고, 눌러서 AI 어노테이션과 전문가 소견을 확인합니다.",
     "CXR, CT and PET-CT thumbnails; open one to view AI annotations and expert findings."),
    ("유전체", "Genomic", "시퀀스 정보(SRA ID·Lineage)와 검출된 약제내성 변이를 봅니다.",
     "Sequence information (SRA ID, lineage) and detected drug-resistance mutations."),
    ("DST 결과", "DST Results", "1차·2차 약제별 약제감수성검사(DST) 결과를 내성·감수성으로 봅니다.",
     "Drug susceptibility test (DST) results for first- and second-line drugs."),
    ("생화학", "Biochemistry", "혈액·생화학 검사 결과를 참고범위와 높음·낮음 판정과 함께 봅니다.",
     "Blood and biochemistry results with reference ranges and high/low flags."),
    ("타임라인", "Timeline", "등록부터 검사·치료 시작·영상·시퀀싱·치료 종료까지 주요 사건을 시간순으로 봅니다.",
     "Key events in order, from registration through tests, treatment, imaging, sequencing and outcome."),
    ("커스텀", "Custom", "이 환자에 연결된 연구 맞춤형 데이터 구조의 데이터를 보고 새로 입력합니다.",
     "View and enter data in custom research data schemas linked to this patient."),
]


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _list_state(request):
    g = request.GET
    return {
        "q": (g.get("q") or "").strip()[:100],
        "country": g.getlist("country"),
        "type": g.getlist("type"),
        "outcome": g.getlist("outcome"),
        "imaging": g.get("imaging") == "1",
    }


def _list_query(state) -> str:
    """필터·검색 상태만 담은 쿼리스트링 (선택·페이지·탭 제외)."""
    pairs = []
    if state["q"]:
        pairs.append(("q", state["q"]))
    for k in ("country", "type", "outcome"):
        pairs += [(k, v) for v in state[k]]
    if state["imaging"]:
        pairs.append(("imaging", "1"))
    return urlencode(pairs)


THUMB_PAGE = 24  # 영상 썸네일은 24개씩 그린다(나머지는 '더보기'로 지연 로드) — 승인 업로드로 영상 수가 커져도 가볍게


def _image_items(p: Patient, lang: str) -> list[dict]:
    return [{**im,
             "finding_text": ", ".join(services.data_label(f["type"], lang) for f in im["findings"]),
             "findings_l": [{k: services.data_label(v, lang) if isinstance(v, str) else v for k, v in f.items()}
                            for f in im["findings"]],
             "ann_label": services.data_label(im["annotations"][0]["type"], lang) if im["annotations"] else ""}
            for im in imaging.generate_images(p)]


def _detail_context(p: Patient, lang: str, tab: int) -> dict:
    if p is None:
        return {"patient": None}
    images = _image_items(p, lang)
    regimen = services.regimen_data(p)
    return {
        "patient": p,
        "tab": tab,
        "tabs": [{"label": en if lang == "en" else ko, "help": hen if lang == "en" else hko} for ko, en, hko, hen in TABS],
        "resistance_variant": services.RESISTANCE_VARIANT.get(p.diagnosis_type, "neutral"),
        "outcome_variant": services.outcome_variant(p.treatment_outcome),
        "country_label": services.data_label(p.country, lang),
        "site_label": services.data_label(p.site, lang),
        "site_of_disease_label": services.data_label(p.get_site_of_disease_display(), lang) if p.site_of_disease else "",
        "comorbidities_label": ", ".join(services.data_label(c, lang) for c in p.comorbidities),
        "regimen": regimen,
        "regimen_from_upload": p.regimens.exists(),
        "regimen_height": len(regimen) * 40 + 40,
        "images": images[:THUMB_PAGE],
        "image_total": len(images),
        "more_offset": THUMB_PAGE if len(images) > THUMB_PAGE else None,
        "volume": imaging.volume_info(),
        "studies": list(p.imaging_studies.prefetch_related("files")[:50]),
        "sequence_runs": list(p.sequence_runs.prefetch_related("files")[:50]),
        "mutations": services.mutations(p),
        "dst": services.dst_rows(p),
        "biochem": list(p.biochem_results.all()),
        "events": services.timeline(p, lang),
    }


@login_required
@require_safe
def case_images(request, pk):
    """영상 썸네일 다음 묶음(HTMX '더보기')."""
    p = get_object_or_404(Patient, pk=pk)
    offset = max(0, _int(request.GET.get("offset"), 0))
    images = _image_items(p, get_lang(request))
    nxt = offset + THUMB_PAGE
    return render(request, "cases/_thumbs.html", {
        "patient": p, "images": images[offset:nxt], "image_total": len(images),
        "more_offset": nxt if len(images) > nxt else None, "volume": imaging.volume_info()})


@login_required
@require_safe
def case_image_detail(request, pk, image_id):
    """영상 상세 모달 본문(HTMX 지연 로드) — 모달은 화면에 하나만 두고 고른 영상만 그린다."""
    p = get_object_or_404(Patient, pk=pk)
    image_id = int(image_id)
    im = next((i for i in _image_items(p, get_lang(request)) if i["id"] == image_id), None)
    if im is None:
        raise Http404
    return render(request, "cases/_image_modal.html", {"im": im, "volume": imaging.volume_info()})


@login_required
@require_safe
def case_editor(request):
    """대상자 등록조회 (원본 pages/CaseEditor) — 읽기 전용(원본도 편집은 업로드 화면으로 연결)."""
    lang = get_lang(request)
    state = _list_state(request)
    qs = services.filter_patients(state, lang)
    rows, page, pages, total = services.paginate(qs, _int(request.GET.get("page"), 1) - 1)

    # 원본: 처음에는 patients[0] 선택, 필터로 목록에서 빠져도 선택은 유지
    selected = None
    sel_id = _int(request.GET.get("id"), None)
    if sel_id is not None:
        selected = Patient.objects.filter(pk=sel_id).first()
    if selected is None:
        selected = Patient.objects.order_by("id").first()
    if selected is not None:
        selected = Patient.objects.prefetch_related("dst_results", "biochem_results").get(pk=selected.pk)
    tab = min(max(_int(request.GET.get("tab"), 0), 0), len(TABS) - 1)

    list_query = _list_query(state)
    ctx = {
        "state": state,
        "rows": [{"p": r, "resistance_variant": services.RESISTANCE_VARIANT.get(r.diagnosis_type, "neutral"),
                  "outcome_variant": services.outcome_variant(r.treatment_outcome),
                  "country_label": services.data_label(r.country, lang)} for r in rows],
        "page": page + 1, "pages": pages, "total": total,
        "list_query": list_query,
        "selected_id": selected.pk if selected else "",
        "countries": [{"value": c, "label": services.data_label(c, lang)} for c in services.FILTER_COUNTRIES],
        "diag_types": services.FILTER_DIAG_TYPES,
        "outcomes": services.FILTER_OUTCOMES,
        "active_filter_count": len(state["country"]) + len(state["type"]) + len(state["outcome"]) + (1 if state["imaging"] else 0),
        **_detail_context(selected, lang, tab),
    }
    target = request.headers.get("HX-Target") if request.headers.get("HX-Request") else None
    if target == "case-list":
        return render(request, "cases/_list.html", ctx)
    if target == "case-detail":
        ctx["oob_list"] = True
        return render(request, "cases/_detail_with_list.html", ctx)
    return render(request, "cases/editor.html", ctx)
