"""영상 분석(RAP) 화면 — 원본 src/pages/Rap/*.

/rap, /rap/timeseries, /rap/annotation   같은 화면, 우측 패널 탭(소견 정보 · 타임시리즈 · 데이터 조건)만 다르다(원본과 같음)
  ?patient=<케이스 ID>&study=<검사 id>
/rap/annotations                  POST 전문가 소견 생성(JSON)
/rap/annotations/<id>             POST 수정(작성자 본인·관리자)
/rap/annotations/<id>/delete      POST 삭제(작성자 본인·관리자)
/rap/files/<id>                   업로드 영상 파일(JPG·PNG) 인라인 — 제출자·관리자·영상 DAR 승인자만
/rap/conditions                   데이터 조건 탭 — 조건에 맞는 CXR 검사·대상자 수(HTMX)
"""
from __future__ import annotations

from urllib.parse import quote, urlencode

from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import login_required
from apps.cases.models import ImagingStudy
from apps.cases.services import data_label
from apps.cohorts import conditions as cond
from apps.core.i18n import get_lang, t
from apps.core.views import render_page
from apps.uploads.models import SubmissionFile

from . import services
from .forms import AnnotationCreateForm, AnnotationInputForm
from .models import ExpertAnnotation

TABS = ["/rap", "/rap/timeseries", "/rap/annotation"]
TAB_LABELS = [("소견 정보", "Findings"), ("타임시리즈", "Time Series"), ("데이터 조건", "Data Conditions")]
TAB_HELP = [
    ("지금 영상의 AI 소견과 전문가 소견을 확인하고 입력합니다.", "Review and enter AI and expert findings for the current image."),
    ("촬영 시점별 영상과 소견 변화를 타임라인으로 봅니다.", "See images and findings change over time on a timeline."),
    ("CXR 소견 조건을 골라 데이터 탐색용 조건을 만듭니다.", "Pick CXR finding conditions to build a data query."),
]
ERRORS = {
    "finding": ("알 수 없는 소견 유형입니다", "Unknown finding type"),
    "location": ("알 수 없는 위치입니다", "Unknown location"),
    "severity": ("알 수 없는 중증도입니다", "Unknown severity"),
    "chars": ("허용되지 않는 문자가 있습니다", "The text contains disallowed characters"),
    "text_required": ("영상에 표시할 텍스트를 입력하세요", "Enter the text to show on the image"),
    "text_long": (f"텍스트는 {services.TEXT_MAX}자 이하입니다", f"Text must be {services.TEXT_MAX} characters or fewer"),
    "finding_required": ("소견 유형을 고르세요", "Choose a finding type"),
    "text_size": ("텍스트 소견은 크기가 없어야 합니다", "A text note must not have a size"),
    "too_small": (f"영역이 너무 작습니다(가로·세로 {services.MIN_SIZE}% 이상)",
                  f"The region is too small (at least {services.MIN_SIZE}% wide and tall)"),
    "out_of_bounds": ("영역이 영상 밖으로 나갑니다", "The region extends outside the image"),
    "max_length": ("입력이 너무 깁니다", "The input is too long"),
}


def _pick(lang, pair):
    return pair[1] if lang == "en" else pair[0]


def _json_error(request, status, ko, en):
    return JsonResponse({"error": t(request, ko, en)}, status=status, json_dumps_params={"ensure_ascii": False})


def _form_error(request, form):
    lang = get_lang(request)
    for errs in form.errors.as_data().values():
        for e in errs:
            if e.code in ERRORS:
                return JsonResponse({"error": _pick(lang, ERRORS[e.code])}, status=400,
                                    json_dumps_params={"ensure_ascii": False})
    return _json_error(request, 400, "입력 값을 확인하세요", "Check the input values")


# ═══ 화면 ═══
@login_required
@require_safe
def rap(request):
    lang = get_lang(request)
    path = request.path.rstrip("/") or "/rap"
    tab = TABS.index(path) if path in TABS else 0
    patients = list(services.imaging_patients())
    cid = (request.GET.get("patient") or "").strip()[:40]
    patient = next((p for p in patients if p.condition_id == cid), None) if cid else None
    if patient is None:
        patient = services.default_patient(patients)
    studies, sel = [], 0
    if patient is not None:
        # 볼 권한이 없는 업로드 영상의 전문가 소견은 텍스트·좌표를 내보내지 않고 개수만(검사별 hidden_expert)
        # — 대상자 등록조회 영상 탭과 같은 함수(services.patient_imaging)
        studies, annotations = services.patient_imaging(patient, request.user, lang)
        sid = cond.parse_uint(request.GET.get("study") or "")
        sel = next((i for i, s in enumerate(studies) if s["id"] == sid), 0) if sid is not None else 0
    else:
        annotations = []
    data = {
        "studies": studies, "selected": sel, "annotations": annotations, "tab": tab,
        "patient": patient.condition_id if patient else "",
        "user_name": request.user.shown_name, "expert_color": services.EXPERT_COLOR,
        "findings": [{"value": f, "label": data_label(f, lang)} for f in ExpertAnnotation.FINDING_OPTIONS],
        "locations": [{"value": v, "label": data_label(v, lang)} for v in ExpertAnnotation.LOCATIONS],
        "severities": [{"value": v, "label": data_label(v, lang)} for v in ExpertAnnotation.SEVERITIES],
        "en": lang == "en", "min_size": services.MIN_SIZE, "text_max": services.TEXT_MAX,
        "note_max": services.NOTE_MAX,
    }
    return render_page(request, "imaging/rap.html", {
        "patients": patients, "patient": patient, "tab": tab,
        "tabs": [{"href": TABS[i], "label": _pick(lang, TAB_LABELS[i]), "help": _pick(lang, TAB_HELP[i])}
                 for i in range(3)],
        "rap_data": data, "studies": studies,
        "finding_options": data["findings"], "location_options": data["locations"],
        "severity_options": data["severities"],
        "sex_label": t(request, "남", "M") if patient and patient.sex == "M" else t(request, "여", "F"),
        "country_label": data_label(patient.country, lang) if patient else "",
    }, sidebar="rap")


# ═══ 전문가 소견 CRUD (POST + CSRF, JSON) ═══
def _study_for_write(request, study_id):
    study = (ImagingStudy.objects.select_related("sample_asset", "submission").prefetch_related("files")
             .filter(pk=study_id).first())
    if study is None:
        return None, _json_error(request, 404, "영상 검사를 찾을 수 없습니다", "Imaging study not found")
    if not services.can_view_files(request.user, study):
        return None, _json_error(request, 403, "이 영상을 볼 권한이 없습니다", "You do not have access to this image")
    if not study.is_public_sample and not services.display_files(study):
        return None, _json_error(request, 400, "표시할 수 있는 영상이 없어 소견을 입력할 수 없습니다",
                                 "There is no displayable image to annotate")
    return study, None


@login_required
@require_POST
def annotation_create(request):
    form = AnnotationCreateForm(request.POST)
    if not form.is_valid():
        return _form_error(request, form)
    d = form.cleaned_data
    study, err = _study_for_write(request, d["study"])
    if err:
        return err
    plane, sl = d.get("plane") or "", d.get("slice")
    vol = services.volume_meta(study.sample_asset) if study.is_public_sample else None
    if vol is None:
        if plane or sl is not None:
            return _json_error(request, 400, "단면 정보는 CT·PET-CT 볼륨에만 붙일 수 있습니다",
                               "Plane and slice apply only to CT/PET-CT volumes")
    else:
        count = services.plane_count(study, plane) if plane else None
        if count is None or sl is None or not 0 <= sl < count:
            return _json_error(request, 400, "평면·단면 값이 올바르지 않습니다", "Invalid plane or slice")
    with transaction.atomic():
        if ExpertAnnotation.objects.filter(study=study).count() >= services.MAX_ANNOTATIONS_PER_STUDY:
            return _json_error(request, 400, "이 영상의 전문가 소견이 너무 많습니다",
                               "Too many expert findings on this image")
        a = ExpertAnnotation.objects.create(
            study=study, shape=d["shape"], x=d["x"], y=d["y"], width=d["width"], height=d["height"],
            plane=plane, slice_index=sl if plane else None, finding_type=d.get("finding_type", ""),
            location=d.get("location", ""), severity=d.get("severity", ""), text=d.get("text", ""),
            author=request.user, author_name=request.user.shown_name[:150])
    return JsonResponse({"annotation": services.annotation_view(a, request.user, get_lang(request))}, status=201,
                        json_dumps_params={"ensure_ascii": False})


def _own_annotation(request, pk):
    a = ExpertAnnotation.objects.select_related("study", "study__submission").filter(pk=int(pk)).first()
    if a is None:
        return None, _json_error(request, 404, "전문가 소견을 찾을 수 없습니다", "Expert finding not found")
    if not services.can_edit_annotation(request.user, a):
        return None, _json_error(request, 403, "작성자 본인 또는 관리자만 수정·삭제할 수 있습니다",
                                 "Only the author or an administrator can edit or delete this")
    return a, None


@login_required
@require_POST
def annotation_update(request, pk):
    a, err = _own_annotation(request, pk)
    if err:
        return err
    form = AnnotationInputForm(request.POST)
    if form.is_valid():
        form.check_content(a.shape)
    if not form.is_valid() or form.errors:
        return _form_error(request, form)
    d = form.cleaned_data
    a.finding_type, a.location, a.severity, a.text = d["finding_type"], d["location"], d["severity"], d["text"]
    a.save(update_fields=["finding_type", "location", "severity", "text", "updated_at"])
    return JsonResponse({"annotation": services.annotation_view(a, request.user, get_lang(request))},
                        json_dumps_params={"ensure_ascii": False})


@login_required
@require_POST
def annotation_delete(request, pk):
    a, err = _own_annotation(request, pk)
    if err:
        return err
    a.delete()
    return JsonResponse({"deleted": int(pk)})


# ═══ 업로드 영상 파일 ═══
@login_required
@require_safe
def study_file(request, file_id):
    sf = SubmissionFile.objects.filter(pk=int(file_id)).first() if len(file_id) <= 10 else None
    if sf is None or not (sf.original_name or "").lower().endswith(services.DISPLAY_EXTS):
        raise Http404
    study = sf.imaging_studies.select_related("submission").first()
    if study is None or not services.can_view_files(request.user, study):
        raise Http404
    try:
        fh = sf.file.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        raise Http404 from None
    ctype = "image/png" if sf.original_name.lower().endswith(".png") else "image/jpeg"
    resp = FileResponse(fh, content_type=ctype)
    resp["Content-Disposition"] = "inline"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    resp["Content-Security-Policy"] = "default-src 'none'; img-src 'self'"
    return resp


# ═══ 데이터 조건 탭 ═══
@login_required
@require_safe
def conditions(request):
    lang = get_lang(request)
    fs = [f for f in dict.fromkeys(request.GET.getlist("finding")[:20]) if f in ExpertAnnotation.FINDING_OPTIONS]
    loc = request.GET.get("location") or ""
    loc = loc if loc in ExpertAnnotation.LOCATIONS else ""
    sev = request.GET.get("severity") or ""
    sev = sev if sev in ExpertAnnotation.SEVERITIES else ""
    counts = services.condition_counts(fs, loc, sev)
    depot = "/depot?" + urlencode([("finding", f) for f in fs] + [("run", "1")], quote_via=quote) if fs \
        else "/depot?panel=saved"
    return render(request, "imaging/_conditions.html", {
        "counts": counts, "depot_url": depot, "selected": [data_label(f, lang) for f in fs],
        "location": data_label(loc, lang) if loc else "", "severity": data_label(sev, lang) if sev else "",
    })
