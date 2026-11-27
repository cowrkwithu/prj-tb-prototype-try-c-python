"""REST API /api/v1 (원본 ApiTab 문서의 엔드포인트를 실제로 구현).

인증: `Authorization: Bearer <API 키>` 만 받는다. 세션 쿠키는 보지 않으므로(request.user 미사용) CSRF 검사는 면제한다 —
CSRF 는 브라우저가 쿠키를 자동으로 붙이는 요청을 노리는데, 이 API 는 쿠키로는 인증되지 않고
브라우저가 다른 출처에 Authorization 헤더를 임의로 붙일 수 없다(CORS 허용 헤더를 내보내지 않음).

권한: 키 자체에는 허용 유형을 저장하지 않고, **요청할 때마다** 키 소유자의 DAR 승인 상태로 판정한다.
  cases(목록·상세 기본)·lab·cohorts → clinical, include=genomics → genomics, include=imaging·images → imaging.
응답 코드: 키 없음/잘못됨/폐기 401, 미승인 유형 403, 잘못된 파라미터 400, 없는 대상 404, 요청 과다 429,
  변이 목록(4단계 이월) 503. 어떤 입력에도 500 을 내지 않는다.
비식별: 화면(케이스 에디터)이 보여주는 항목만 내보낸다 — 이름·연락처 같은 직접 식별자는 원래 DB 에 없고,
  제출자 계정·업로드 원문 파일명·DICOM 헤더 원값·내부 메모는 응답에 넣지 않는다.
"""
from __future__ import annotations

import json
import re
from functools import wraps

from django.conf import settings
from django.http import FileResponse, JsonResponse
from django.http.request import RawPostDataException
from django.core.exceptions import RequestDataTooBig
from django.views.decorators.csrf import csrf_exempt

from apps.cases.models import ImagingStudy, Patient
from apps.cases.services import FILTER_DIAG_TYPES
from apps.core.safejson import loads_user_json

from . import services

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
MAX_PAGE = 1_000_000
_UINT = re.compile(r"^[0-9]{1,7}$")
COUNTRY_BY_EN = {en.lower(): ko for ko, en in Patient.COUNTRY_EN.items()}
INCLUDE_TYPE = {"genomics": "genomics", "imaging": "imaging", "lab": "clinical"}
FORMATS = ("metadata", "dicom", "jpg")
FILE_EXTS = {"dicom": (".dcm", ".dicom"), "jpg": (".jpg", ".jpeg")}
FIELD_TYPE = {"demographics": "clinical", "clinical": "clinical", "genomics": "genomics", "imaging": "imaging"}


def _err(status: int, code: str, message: str, **extra) -> JsonResponse:
    resp = JsonResponse({"error": message, "status": code, **extra}, status=status,
                        json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store"
    return resp


def _ok(data, status=200) -> JsonResponse:
    resp = JsonResponse(data, status=status, safe=isinstance(data, dict), json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store"
    return resp


def api_view(methods=("GET",)):
    """API 키 인증 · 메서드 검사 · 요청 제한 · 예기치 못한 입력 방어를 묶는다."""
    allowed = set(methods) | ({"HEAD"} if "GET" in methods else set())

    def deco(view):
        @csrf_exempt
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in allowed:
                resp = _err(405, "method_not_allowed", "Method not allowed")
                resp["Allow"] = ", ".join(sorted(allowed))
                return resp
            # 인증 실패 제한은 REMOTE_ADDR 만 쓴다 — X-Forwarded-For 등 클라이언트가 보낸 헤더는 위조할 수 있어
            # 신뢰하지 않는다(역방향 프록시 뒤에 두면 프록시 주소 하나로 묶이므로 프록시에서 따로 제한해야 한다).
            ip = request.META.get("REMOTE_ADDR") or ""
            blocked, retry = services.auth_fail_blocked(ip)
            if blocked:
                return _auth_limited(retry)
            key = services.authenticate(request.headers.get("Authorization"))
            if key is None:
                over, retry = services.auth_fail_hit(ip)
                if over:
                    return _auth_limited(retry)
                resp = _err(401, "unauthorized", "A valid API key is required (Authorization: Bearer <key>)")
                resp["WWW-Authenticate"] = 'Bearer realm="tbrs-api"'
                return resp
            limited, retry = services.rate_limit_hit(key)
            if limited:
                resp = _err(429, "rate_limited",
                            f"Too many requests (limit {settings.API_RATE_LIMIT_PER_MINUTE}/minute)")
                resp["Retry-After"] = str(retry)
                return resp
            request.api_key = key
            request.api_types = services.approved_types(key.user)  # 요청 시점의 DAR 승인 상태
            return view(request, *args, **kwargs)

        wrapper.portal_access = "api_key"
        return wrapper

    return deco


def _auth_limited(retry: int) -> JsonResponse:
    resp = _err(429, "rate_limited", "Too many failed authentication attempts "
                f"(limit {settings.API_AUTH_FAIL_LIMIT_PER_MINUTE}/minute per client)")
    resp["Retry-After"] = str(retry)
    return resp


def _require(request, *types):
    missing = sorted({t for t in types if t not in request.api_types})
    if missing:
        return _err(403, "forbidden", "Data access request (DAR) approval is required for: " + ", ".join(missing),
                    required=missing)
    return None


def _uint_param(request, name, default, lo, hi):
    raw = request.GET.get(name)
    if raw is None or raw == "":
        return default, None
    if not _UINT.match(raw) or not lo <= int(raw) <= hi:
        return None, _err(400, "bad_request", f"'{name}' must be an integer between {lo} and {hi}")
    return int(raw), None


# ═══ 직렬화 (원본 문서 예시 응답 구조) ═══
def _case_summary(p: Patient) -> dict:
    return {"id": p.pk, "conditionId": p.condition_id,
            "demographics": {"age": p.age, "sex": p.sex, "country": p.country},
            "clinical": {"diagnosisType": p.diagnosis_type, "treatmentOutcome": p.treatment_outcome}}


def _case_detail(p: Patient, include: list[str]) -> dict:
    out = {"id": p.pk, "conditionId": p.condition_id,
           "demographics": {"age": p.age, "sex": p.sex, "country": p.country, "site": p.site},
           "clinical": {"diagnosisType": p.diagnosis_type, "treatmentOutcome": p.treatment_outcome,
                        "treatmentDuration": p.treatment_duration, "comorbidities": list(p.comorbidities or [])}}
    if "genomics" in include:
        out["genomics"] = {
            "hasSequence": p.has_sequence, "sraId": p.sra_id, "lineage": p.lineage,
            "resistanceMutations": list(p.resistance_mutations or []),
            "sequences": [{"specimenId": r.specimen_id, "platform": r.platform, "method": r.method,
                           "coverage": r.coverage, "sraAccession": r.sra_accession, "lineage": r.lineage}
                          for r in p.sequence_runs.all()[:100]],
        }
    if "imaging" in include:
        out["imaging"] = {
            "hasCxr": p.has_cxr, "hasCt": p.has_ct, "hasPetCt": p.has_pet_ct, "cxrCount": p.cxr_count,
            "ctCount": p.ct_count, "petCtCount": p.pet_ct_count, "cxrFindings": list(p.cxr_findings or []),
            "studies": [{"id": s.pk, "type": s.modality, "date": s.study_date.isoformat()}
                        for s in p.imaging_studies.all()[:100]],
        }
    if "lab" in include:
        out["lab"] = {
            "microscopy": p.microscopy, "culture": p.culture,
            "dstResults": [{"drug": d.drug, "result": d.result} for d in p.dst_results.all()],
            "biochemistry": [{"test": b.test, "value": b.value, "referenceRange": b.reference_range, "flag": b.flag,
                              "date": b.measured_on.isoformat() if b.measured_on else None}
                             for b in p.biochem_results.all()[:200]],
        }
    return out


# ═══ 엔드포인트 ═══
@api_view()
def cases(request):
    denied = _require(request, "clinical")
    if denied:
        return denied
    page, err = _uint_param(request, "page", 1, 1, MAX_PAGE)
    if err:
        return err
    limit, err = _uint_param(request, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    if err:
        return err
    qs = Patient.objects.order_by("id")
    country = request.GET.get("country")
    if country:
        c = country.strip()
        ko = c if c in Patient.COUNTRIES else COUNTRY_BY_EN.get(c.lower())
        if ko is None:
            return _err(400, "bad_request", "Unknown 'country'",
                        allowed=Patient.COUNTRIES + [Patient.COUNTRY_EN[x] for x in Patient.COUNTRIES])
        qs = qs.filter(country=ko)
    dtype = request.GET.get("diagnosisType")
    if dtype:
        if dtype not in FILTER_DIAG_TYPES:
            return _err(400, "bad_request", "Unknown 'diagnosisType'", allowed=FILTER_DIAG_TYPES)
        qs = qs.filter(diagnosis_type=dtype)
    total = qs.count()
    start = (page - 1) * limit
    rows = list(qs[start:start + limit]) if start < total else []
    return _ok({"data": [_case_summary(p) for p in rows], "pagination": {"page": page, "limit": limit, "total": total}})


@api_view()
def case_detail(request, pk):
    raw = request.GET.get("include", "")
    include = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
    bad = [x for x in include if x not in INCLUDE_TYPE]
    if bad or len(include) > 3:
        return _err(400, "bad_request", "'include' accepts genomics, imaging, lab", allowed=list(INCLUDE_TYPE))
    include = list(dict.fromkeys(include))
    denied = _require(request, "clinical", *[INCLUDE_TYPE[x] for x in include])
    if denied:
        return denied
    p = Patient.objects.filter(pk=int(pk)).first() if len(pk) <= 10 else None
    if p is None:
        return _err(404, "not_found", "Case not found")
    return _ok(_case_detail(p, include))


def _study_files(study: ImagingStudy, fmt: str):
    exts = FILE_EXTS[fmt]
    return [f for f in study.files.order_by("id") if f.original_name.lower().endswith(exts)]


@api_view()
def image_detail(request, pk):
    denied = _require(request, "imaging")
    if denied:
        return denied
    fmt = request.GET.get("format") or "metadata"
    if fmt not in FORMATS:
        return _err(400, "bad_request", "'format' must be one of metadata, dicom, jpg", allowed=list(FORMATS))
    study = ImagingStudy.objects.select_related("patient").filter(pk=int(pk)).first() if len(pk) <= 10 else None
    if study is None:
        return _err(404, "not_found", "Image not found")
    if fmt == "metadata":
        dicoms, jpgs = _study_files(study, "dicom"), _study_files(study, "jpg")
        download = "dicom" if dicoms else ("jpg" if jpgs else None)
        return _ok({
            "id": study.pk, "patientId": study.patient_id, "conditionId": study.patient.condition_id,
            "type": study.modality, "date": study.study_date.isoformat(),
            "findings": [f.get("finding", "") for f in (study.findings or []) if isinstance(f, dict) and f.get("finding")],
            "annotations": [{"type": f.get("finding", ""), "location": f.get("location", ""),
                             "severity": f.get("severity", ""), "description": f.get("description", "")}
                            for f in (study.findings or []) if isinstance(f, dict)],
            "tbActivity": study.tb_activity, "impression": study.impression,
            "files": {"dicom": len(dicoms), "jpg": len(jpgs)},
            "downloadUrl": f"/api/v1/images/{study.pk}?format={download}" if download else None,
        })
    index, err = _uint_param(request, "file", 0, 0, 10_000)
    if err:
        return err
    files = _study_files(study, fmt)
    if index >= len(files):
        return _err(404, "not_found", f"No {fmt} file for this image")
    sf = files[index]
    try:
        fh = sf.file.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        return _err(404, "not_found", "Stored file is missing")
    ext = "dcm" if fmt == "dicom" else "jpg"
    resp = FileResponse(fh, as_attachment=True, filename=f"image_{study.pk}_{index + 1}.{ext}",
                        content_type="application/dicom" if fmt == "dicom" else "image/jpeg")
    resp["Cache-Control"] = "no-store"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


def _condition_types(node) -> set[str]:
    """조건 트리가 쓰는 필드의 데이터 유형(demographics·clinical → clinical, genomics.*, imaging.*).

    사용자 JSON 이므로 children 은 list/dict 일 때만 따라간다 — 스칼라(5, true, 1.5, "x")·null 은 무시하고
    형식 오류 판정은 resolve_conditions 가 400 으로 한다. 깊은 중첩에도 재귀 한도에 걸리지 않게 스택으로 순회한다."""
    out = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "children" in cur:
                kids = cur.get("children")
                if isinstance(kids, list):
                    stack.extend(kids)
                elif isinstance(kids, dict):
                    stack.append(kids)
            elif isinstance(cur.get("field"), str):
                out.add(FIELD_TYPE.get(cur["field"].split(".", 1)[0], "clinical"))
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


@api_view(methods=("POST",))
def cohorts(request):
    """원본 '새 데이터셋 생성' — apps.cohorts 의 단일 진입점(resolve_conditions)으로 조건을 검증해 실제 Cohort 를 만든다."""
    from apps.cohorts import services as cohort_services

    denied = _require(request, "clinical")
    if denied:
        return denied
    try:
        body = request.body
    except (RequestDataTooBig, RawPostDataException):
        return _err(400, "bad_request", "Request body too large")
    if len(body) > settings.API_MAX_BODY_BYTES:
        return _err(400, "bad_request", f"Request body too large (max {settings.API_MAX_BODY_BYTES} bytes)")
    try:
        text = body.decode("utf-8")
        data = loads_user_json(text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _err(400, "bad_request", "Body must be a JSON object (UTF-8)")
    if not isinstance(data, dict):
        return _err(400, "bad_request", "Body must be a JSON object")
    name = data.get("name")
    if not isinstance(name, str) or not cohort_services.clean_name(name):
        return _err(400, "bad_request", "'name' (string) is required")
    desc = data.get("description", "")
    if desc is None:
        desc = ""
    if not isinstance(desc, str) or len(desc) > 2000:
        return _err(400, "bad_request", "'description' must be a string of at most 2000 characters")
    conds = data.get("conditions")
    if not isinstance(conds, (list, dict)) or not conds:
        return _err(400, "bad_request", "'conditions' (non-empty array of filter conditions) is required")
    denied = _require(request, *_condition_types(conds))
    if denied:
        return denied
    try:
        tree_text = json.dumps(conds, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        return _err(400, "bad_request", "Malformed conditions")
    state = cohort_services.resolve_conditions({"tree": tree_text}, "en")
    if state["error"]:
        return _err(400, "bad_request", state["error"])
    c = cohort_services.save_cohort(request.api_key.user, state["tree"], None, name)
    clean_desc = re.sub(r"[\x00-\x1f\x7f]", " ", desc).strip()
    if clean_desc:
        c.description, c.description_en = clean_desc, ""
        c.save(update_fields=["description", "description_en", "updated_at"])
    return _ok({"id": c.pk, "name": c.name, "description": c.description, "patientIds": list(c.snapshot_ids),
                "patientCount": c.snapshot_count, "createdDate": c.created_date.isoformat()}, status=201)


@api_view()
def variants(request):
    """유전체 변이 목록 — 변이 모델은 4단계(GAP)에서 생긴다. 그때까지 '준비 중' 응답(문서 화면에는 원본대로 표시)."""
    denied = _require(request, "genomics")
    if denied:
        return denied
    return _err(503, "not_available", "Genomic variant API is not available yet (planned for the genomics module).")


@csrf_exempt
def not_found(request, *args, **kwargs):
    return _err(404, "not_found", "Unknown API endpoint")
