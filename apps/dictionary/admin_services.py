"""관리 → 데이터사전 관리 서비스 (원본 pages/Admin/DictionaryManager.tsx + DictionaryProvider 의 CRUD).

원본과 다른 점(의도):
- 항목 '삭제' 대신 '폐기'(is_deprecated) — 데이터 구조 필드가 FK 로 참조하므로 행을 지우지 않는다. 폐기 항목은 복원할 수 있다.
- 카테고리 삭제는 표준 항목(엑셀 유래)이 없고, 어떤 데이터 구조 필드도 참조하지 않는 카테고리만 가능하다
  (표준 카테고리는 같은 버전 재적재 시 다시 생기므로 삭제 대신 그대로 둔다).
- 엑셀 업로드로 새 버전 적재(0단계 load_dictionary 재사용), 현행 버전 전환, 변경 이력(DictionaryChangeLog).
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q

from apps.core.naming import field_name_problem
from apps.uploads.services import files as upload_files

from . import services
from .models import DataFieldType, DictionaryCategory, DictionaryChangeLog, DictionaryField, DictionaryVersion

CODE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
ICON_OPTIONS = [
    ("Users", "환자 기본정보", "Patient Demographics"), ("Cigarette", "사회행동/위험요인", "Social Behavior / Risk Factors"),
    ("ClipboardList", "선별검사/증상", "Screening / Symptoms"), ("Stethoscope", "진단", "Diagnosis"),
    ("Microscope", "검체/현미경", "Specimen / Microscopy"), ("TestTube", "배양", "Culture"),
    ("ShieldAlert", "약제감수성", "Drug Susceptibility"), ("Droplet", "생화학/혈액", "Biochemistry / Hematology"),
    ("Pill", "치료", "Treatment"), ("ScanLine", "영상", "Imaging"), ("ShieldCheck", "잠복결핵", "Latent TB"),
    ("Dna", "유전체", "Genomic"), ("Brain", "AI 예측", "AI Prediction"), ("Settings", "설정/행정", "Settings / Administration"),
    ("AlertTriangle", "안전성/이상사례", "Safety / Adverse Events"), ("CalendarCheck", "복약순응도", "Adherence"),
    ("Syringe", "약동학", "Pharmacokinetics"), ("MessageSquare", "환자보고결과", "Patient-Reported Outcomes"),
    ("HeartPulse", "생리기능", "Physiological Function"), ("FlaskConical", "실험", "Experiment"), ("Folder", "폴더", "Folder"),
    ("BookOpen", "문서", "Document"), ("Beaker", "비커", "Beaker"), ("FileText", "파일", "File"),
    ("Activity", "활동", "Activity"), ("Heart", "건강", "Health"),
]
COLOR_OPTIONS = [
    ("blue", "파랑", "Blue"), ("rose", "장미", "Rose"), ("amber", "호박", "Amber"), ("emerald", "에메랄드", "Emerald"),
    ("violet", "보라", "Violet"), ("cyan", "청록", "Cyan"), ("orange", "주황", "Orange"), ("pink", "분홍", "Pink"),
    ("lime", "라임", "Lime"), ("indigo", "남색", "Indigo"), ("sky", "하늘", "Sky"), ("red", "빨강", "Red"),
    ("teal", "틸", "Teal"), ("green", "초록", "Green"), ("slate", "슬레이트", "Slate"), ("fuchsia", "자홍", "Fuchsia"),
    ("purple", "자주", "Purple"), ("yellow", "노랑", "Yellow"), ("stone", "회갈색", "Stone"),
]
RANGE_TYPES = {"INTEGER", "DECIMAL", "DATE", "DATETIME", "NUMERIC_RANGE"}
LENGTH_TYPES = {"TEXT", "TEXTAREA"}
OPTION_TYPES = {"SELECT", "MULTISELECT"}


class DictAdminError(Exception):
    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en


def _log(user, action, *, version="", target="", detail=None):
    DictionaryChangeLog.objects.create(actor=user if user and user.pk else None,
                                       actor_label=getattr(user, "shown_name", "") or getattr(user, "username", ""),
                                       action=action, version=version, target=target[:120], detail=detail or {})


def require_version() -> DictionaryVersion:
    ver = services.active_version()
    if ver is None:
        raise DictAdminError("적재된 데이터사전 버전이 없습니다. 먼저 엑셀을 적재하세요.",
                             "No dictionary version is loaded. Load an Excel workbook first.")
    return ver


def categories_with_counts(ver, include_deprecated=False):
    return list(DictionaryCategory.objects.filter(version=ver).annotate(
        n=Count("fields", filter=Q(fields__is_deprecated=False)),
        n_dep=Count("fields", filter=Q(fields__is_deprecated=True))).order_by("order", "id"))


# ── 카테고리 ──
def _reject_bad_chars(data):
    from apps.core.safejson import has_bad_chars

    for k in data:
        vals = data.getlist(k) if hasattr(data, "getlist") else [data.get(k)]
        if any(isinstance(v, str) and has_bad_chars(v) for v in vals):
            raise DictAdminError("허용되지 않는 문자(서로게이트·NUL)가 있습니다", "Disallowed characters (surrogates/NUL)")


def clean_category(data, *, creating: bool) -> dict:
    _reject_bad_chars(data)
    out = {"name": (data.get("name") or "").strip()[:200], "name_en": (data.get("name_en") or "").strip()[:200],
           "description": (data.get("description") or "").strip()[:1000],
           "description_en": (data.get("description_en") or "").strip()[:1000],
           "icon": data.get("icon") if data.get("icon") in {i[0] for i in ICON_OPTIONS} else "Folder",
           "color": data.get("color") if data.get("color") in {c[0] for c in COLOR_OPTIONS} else "blue"}
    if creating:
        code = (data.get("code") or "").strip()
        if not CODE_RE.match(code):
            raise DictAdminError("카테고리 ID 는 영문 소문자·숫자·밑줄 1~20자입니다", "Category ID: 1–20 lowercase letters, digits, underscores")
        out["code"] = code
    if not out["name"]:
        raise DictAdminError("카테고리 이름을 입력하세요", "Enter a category name")
    return out


@transaction.atomic
def add_category(user, data) -> DictionaryCategory:
    ver = require_version()
    d = clean_category(data, creating=True)
    if DictionaryCategory.objects.filter(version=ver, code=d["code"]).exists():
        raise DictAdminError(f'"{d["code"]}" ID가 이미 존재합니다', f'ID "{d["code"]}" already exists')
    order = (DictionaryCategory.objects.filter(version=ver).order_by("-order").values_list("order", flat=True).first() or 0) + 1
    cat = DictionaryCategory.objects.create(version=ver, order=order, **d)
    _log(user, "category_add", version=ver.version, target=cat.code, detail={"name": cat.name})
    return cat


@transaction.atomic
def edit_category(user, cat: DictionaryCategory, data) -> DictionaryCategory:
    d = clean_category(data, creating=False)
    changes = {k: [getattr(cat, k), v] for k, v in d.items() if getattr(cat, k) != v}
    for k, v in d.items():
        setattr(cat, k, v)
    cat.save()
    if changes:
        _log(user, "category_edit", version=cat.version.version, target=cat.code, detail=changes)
    return cat


@transaction.atomic
def delete_category(user, cat: DictionaryCategory) -> None:
    from apps.schemas.models import SchemaField

    if cat.fields.filter(is_standard=True).exists():
        raise DictAdminError("표준 항목(엑셀 유래)이 있는 카테고리는 삭제할 수 없습니다. 필요 없는 항목은 폐기하세요.",
                             "Categories with standard (Excel) items cannot be deleted. Deprecate unneeded items instead.")
    if SchemaField.objects.filter(dictionary_field__category=cat).exists():
        raise DictAdminError("데이터 구조 필드가 참조하는 항목이 있어 삭제할 수 없습니다",
                             "Some items are referenced by data schema fields, so the category cannot be deleted")
    n = cat.fields.count()
    _log(user, "category_delete", version=cat.version.version, target=cat.code, detail={"name": cat.name, "fields": n})
    cat.fields.all().delete()
    cat.delete()


# ── 항목 ──
def _num(v, integer=False):
    """폼 수치 → int/float. NaN·±inf·1e400·거대 정수·절댓값 상한(스키마 정의와 같음) 초과는 ValueError(폼 오류)."""
    from apps.schemas.validation import finite_number

    v = (v or "").strip() if isinstance(v, str) else v
    if v in (None, ""):
        return None
    f = finite_number(v)
    if integer:
        if f != int(f):
            raise ValueError
        return int(f)
    return int(f) if f == int(f) else f


CONSTRAINT_INPUTS = ("maxLength", "min", "max", "precision", "unit")


def clean_entry(data, existing: DictionaryField | None = None) -> dict:
    """항목 폼 → 저장 값. 수정(existing)일 때 폼에 **없는** 키는 기존 값을 유지하고, 폼에 있지만 비운 칸만 지운다.

    (화면이 타입별 입력란만 보내므로, 보이지 않은 제약조건이 무변경 저장으로 사라지지 않게 한다.)
    """
    _reject_bad_chars(data)

    def has(k):
        return k in data

    def get(k, default=""):
        return data.get(k) if has(k) else default

    field_name = (get("field_name", existing.field_name if existing else "") or "").strip()
    # 빌더(커스텀 데이터 구조)와 같은 이름 규칙 — 기존 항목의 이름을 바꾸지 않는 수정은 예외(legacy)
    problem = field_name_problem(field_name, legacy={existing.field_name} if existing else frozenset())
    if problem:
        raise DictAdminError(problem["ko"], problem["en"])
    display = (get("display_name", existing.display_name if existing else "") or "").strip()[:300]
    if not display:
        raise DictAdminError("표시명을 입력하세요", "Enter a display name")
    old_type = existing.data_type if existing else None
    dtype = data.get("data_type") if has("data_type") else (old_type or "TEXT")
    if dtype not in DataFieldType.values:
        raise DictAdminError("데이터 타입이 올바르지 않습니다", "Invalid data type")
    cons = dict(existing.constraints or {}) if existing else {}
    if old_type and dtype != old_type:  # 명시적 타입 변경 — 새 타입에 맞지 않는 제약만 지운다
        from apps.schemas.catalog import CONSTRAINT_KEYS

        cons = {k: v for k, v in cons.items() if k in CONSTRAINT_KEYS.get(dtype, set())}
    try:
        for k in CONSTRAINT_INPUTS:
            if not has(k):
                continue
            raw = data.get(k)
            if raw is None or str(raw).strip() == "":
                cons.pop(k, None)
            elif k == "unit":
                cons[k] = str(raw).strip()[:30]
            elif k in ("maxLength", "precision"):
                cons[k] = _num(raw, True)
            else:
                cons[k] = _num(raw)
        if "precision" in cons and not 0 <= cons["precision"] <= 6:
            raise ValueError
        if cons.get("maxLength") is not None and cons["maxLength"] < 0:
            raise ValueError
        if cons.get("min") is not None and cons.get("max") is not None and cons["min"] > cons["max"]:
            raise DictAdminError("최솟값이 최댓값보다 큽니다", "Min exceeds max")
    except (TypeError, ValueError):
        raise DictAdminError("제약조건 값은 숫자여야 합니다(소수 자릿수 0~6)", "Constraint values must be numbers (precision 0–6)") from None
    options = list(existing.options or []) if existing else []
    if dtype not in OPTION_TYPES:
        options = []
    elif has("options"):
        prev = {o.get("value"): o for o in options}
        seen = []
        for v in (data.get("options") or "").split(","):
            v = v.strip()[:100]
            if v and v not in seen:
                seen.append(v)
        options = [prev.get(v) or {"value": v, "label": v} for v in seen]  # 기존 라벨·영문 라벨 보존
    return {"field_name": field_name, "display_name": display,
            "display_name_en": (get("display_name_en", existing.display_name_en if existing else "") or "").strip()[:300],
            "description": (get("description", existing.description if existing else "") or "").strip()[:4000],
            "description_en": (get("description_en", existing.description_en if existing else "") or "").strip()[:4000],
            "data_type": dtype, "constraints": cons, "options": options,
            "hl7_fhir_mapping": (get("hl7_fhir_mapping", existing.hl7_fhir_mapping if existing else "") or "").strip()[:200]}


def _next_custom_id(ver, cat) -> str:
    """최대 번호 + 1 (동시 추가는 add_entry 가 IntegrityError 재시도로 처리)."""
    prefix = f"CUSTOM.{cat.code.upper()}."
    nums = [int(x[len(prefix):]) for x in DictionaryField.objects.filter(version=ver, dictionary_id__startswith=prefix)
            .values_list("dictionary_id", flat=True) if x[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) if nums else 0) + 1:03d}"


CUSTOM_ID_RETRIES = 5


def add_entry(user, cat: DictionaryCategory, data) -> DictionaryField:
    from django.db import IntegrityError

    ver = cat.version
    d = clean_entry(data)
    if d["field_name"].lower().startswith("tb_u_"):
        raise DictAdminError("tb_u_ 로 시작하는 필드명은 표준 항목용으로 예약되어 있습니다",
                             "Field names starting with tb_u_ are reserved for standard items")
    for attempt in range(CUSTOM_ID_RETRIES):
        if DictionaryField.objects.filter(version=ver, field_name=d["field_name"]).exists():
            raise DictAdminError("같은 필드명이 이미 있습니다", "The field name already exists")
        try:
            with transaction.atomic():
                order = (cat.fields.order_by("-order").values_list("order", flat=True).first() or 0) + 1
                f = DictionaryField.objects.create(version=ver, category=cat, dictionary_id=_next_custom_id(ver, cat),
                                                   is_standard=False, order=order, **d)  # 관리자 추가 → 사용자 정의 항목
                _log(user, "entry_add", version=ver.version, target=f.dictionary_id, detail={"display_name": f.display_name})
                return f
        except IntegrityError:
            continue  # 동시에 같은 번호를 받은 경우 — 다시 채번
    raise DictAdminError("항목 ID 채번이 경합으로 실패했습니다. 다시 시도하세요", "Could not allocate an item ID (contention). Try again.")


@transaction.atomic
def edit_entry(user, f: DictionaryField, data) -> DictionaryField:
    d = clean_entry(data, existing=f)
    if d["field_name"] != f.field_name and DictionaryField.objects.filter(version=f.version, field_name=d["field_name"]).exists():
        raise DictAdminError("같은 필드명이 이미 있습니다", "The field name already exists")
    if f.is_standard and d["field_name"] != f.field_name:
        raise DictAdminError("표준 항목의 필드명은 바꿀 수 없습니다(엑셀 재적재 기준)", "Standard items keep their field name (used by Excel reloads)")
    changes = {}
    for k, v in d.items():
        if getattr(f, k) != v:
            changes[k] = [getattr(f, k), v] if k not in ("constraints", "options") else ["…", "…"]
            setattr(f, k, v)
    if changes:
        f.save()
        _log(user, "entry_edit", version=f.version.version, target=f.dictionary_id, detail=changes)
    return f


@transaction.atomic
def set_deprecated(user, f: DictionaryField, deprecated: bool) -> bool:
    """조건부 UPDATE — 이미 같은 상태면 False."""
    n = DictionaryField.objects.filter(pk=f.pk, is_deprecated=not deprecated).update(is_deprecated=deprecated)
    if n:
        _log(user, "entry_deprecate" if deprecated else "entry_restore", version=f.version.version, target=f.dictionary_id,
             detail={"display_name": f.display_name})
    return bool(n)


# ── 버전 ──
@transaction.atomic
def activate_version(user, ver: DictionaryVersion) -> bool:
    if ver.is_active:
        return False
    DictionaryVersion.objects.exclude(pk=ver.pk).update(is_active=False)
    DictionaryVersion.objects.filter(pk=ver.pk).update(is_active=True)
    _log(user, "version_activate", version=ver.version, target=ver.version)
    return True


XLSX_MAX_UNCOMPRESSED = 300 * 1024 * 1024   # 압축 해제 총량 상한
XLSX_MAX_ENTRIES = 2000                     # zip 엔트리 수 상한
XLSX_MAX_RATIO = 200                        # 엔트리별 압축률 상한


def check_zip_bomb(path) -> None:
    """openpyxl 로 열기 전에 zip 목록만 읽어 압축 폭탄을 거른다."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, OSError):
        raise DictAdminError("엑셀(xlsx) 파일을 열 수 없습니다(손상된 zip)", "Cannot open the Excel file (corrupt zip)") from None
    if len(infos) > XLSX_MAX_ENTRIES:
        raise DictAdminError("엑셀 파일 구성 요소가 너무 많습니다", "The workbook has too many parts")
    total = sum(i.file_size for i in infos)
    if total > XLSX_MAX_UNCOMPRESSED:
        raise DictAdminError("엑셀 압축 해제 크기가 너무 큽니다(압축 폭탄 의심)", "Uncompressed size too large (possible zip bomb)")
    for i in infos:
        if i.compress_size and i.file_size / i.compress_size > XLSX_MAX_RATIO and i.file_size > 10 * 1024 * 1024:
            raise DictAdminError("엑셀 압축률이 비정상적입니다(압축 폭탄 의심)", "Abnormal compression ratio (possible zip bomb)")


def import_workbook(user, uploaded) -> dict:
    """엑셀 업로드 → 검증(업로드 files 서비스) → 임시 파일 → load_dictionary(force=True).

    재적재 정책(0단계): 같은 버전이면 UDE 기준 갱신(엑셀에서 사라진 표준 항목은 폐기 표시),
    다른 버전이면 새 현행 버전으로 추가(이전 버전 행은 보존, 관리자 추가 항목은 새 버전으로 옮겨 적음).
    """
    try:
        name, ext = upload_files.validate_upload(uploaded, "xlsx", "xlsx")
    except upload_files.FileRejected as exc:
        raise DictAdminError(exc.ko, exc.en) from None
    tmp_dir = Path(settings.VAR_DIR) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=tmp_dir)
    path = Path(tmp)
    try:
        with open(fd, "wb") as out:
            for chunk in uploaded.chunks():
                out.write(chunk)
        check_zip_bomb(path)
        try:
            result = services.load_dictionary(path, force=True)
        except services.DictionaryFormatError as exc:
            raise DictAdminError(f"엑셀 형식 오류: {exc}", f"Workbook format error: {exc}") from None
        except (KeyError, ValueError, IndexError, TypeError, OSError) as exc:
            raise DictAdminError(f"통합 데이터사전 엑셀 형식이 아닙니다 ({type(exc).__name__}: {str(exc)[:120]})",
                                 f"Not a unified data dictionary workbook ({type(exc).__name__}: {str(exc)[:120]})") from None
        except Exception as exc:  # openpyxl 의 zip/XML 오류 등
            raise DictAdminError(f"엑셀 파일을 읽을 수 없습니다 ({type(exc).__name__})",
                                 f"Cannot read the Excel file ({type(exc).__name__})") from None
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    DictionaryVersion.objects.filter(version=result["version"]).update(source_file=name[:300])
    _log(user, "version_load", version=result["version"], target=name,
         detail={k: result.get(k) for k in ("categories", "fields", "created", "updated", "deprecated", "carried_custom")})
    return result
