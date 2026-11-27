"""데이터 공유 서비스 — DAR 게이트·신청·검토, 데이터셋 카드 집계, 내보내기(CSV·엑셀), API 키.

원본 pages/DataSharing/{index.tsx(DarGate), DarTab, DatasetsTab, datasetExport.ts, ApiTab}.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import re
import secrets
import tempfile
import time
from collections import Counter

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Max, Sum
from django.utils import timezone

from apps.cases.models import DSTResult, ImagingStudy, Patient, Regimen, RegimenDrug, SequenceRun
from apps.cases.services import related_drug
from apps.uploads.models import SubmissionFile
from apps.uploads.services.csvio import safe_cell
from apps.uploads.services.files import format_size

from . import catalog
from .models import ApiKey, DataAccessRequest

DAR = DataAccessRequest


class DarError(ValueError):
    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en

    def text(self, lang: str) -> str:
        return self.en if lang == "en" else self.ko


def _clean_types(values) -> list[str]:
    """허용 목록 순서대로, 중복 없이."""
    vals = set(v for v in (values or []) if isinstance(v, str))
    return [t for t in catalog.DATA_TYPES if t in vals]


# ═══════════════════════════════════════════════════════════
#  DAR 게이트 (원본 DarGateContext: none | pending | approved — 사용자 × 데이터 유형)
# ═══════════════════════════════════════════════════════════
def dar_states(user) -> dict[str, str]:
    states = {t: "none" for t in catalog.DATA_TYPES}
    if not (user and getattr(user, "is_authenticated", False)):
        return states
    rows = list(DAR.objects.filter(applicant=user, status__in=[DAR.Status.PENDING, DAR.Status.APPROVED])
                .values_list("status", "data_types", "approved_types"))
    for status, types, approved in rows:
        if status == DAR.Status.PENDING:
            for t in _clean_types(types):
                states[t] = "pending"
    for status, types, approved in rows:  # 승인이 심사 중보다 우선
        if status == DAR.Status.APPROVED:
            for t in _clean_types(approved):
                states[t] = "approved"
    return states


def approved_types(user) -> set[str]:
    return {t for t, s in dar_states(user).items() if s == "approved"}


def is_approved(user, data_type: str) -> bool:
    return dar_states(user).get(data_type) == "approved"


def dar_counts() -> dict:
    return {"pending": DAR.objects.filter(status=DAR.Status.PENDING).count(),
            "approved": DAR.objects.filter(status=DAR.Status.APPROVED).count()}


# ═══════════════════════════════════════════════════════════
#  DAR 신청 · 검토
# ═══════════════════════════════════════════════════════════
def _next_request_id() -> str:
    """DAR-YYYY-NNN (그해 순번 최댓값 + 1, 정수 비교)."""
    prefix = f"DAR-{timezone.localdate():%Y}-"
    nums = [int(tail) for tail in (r[len(prefix):] for r in
                                   DAR.objects.filter(request_id__startswith=prefix).values_list("request_id", flat=True))
            if tail.isdigit()]
    return f"{prefix}{(max(nums) + 1 if nums else 1):03d}"


def submit_request(user, data: dict) -> DataAccessRequest:
    """폼(forms.DarForm)으로 검증된 값을 저장한다. 이미 승인됐거나 심사 중인 유형을 다시 신청하면 거부."""
    types = _clean_types(data.get("data_types"))
    if not types:
        raise DarError("요청할 데이터 유형을 선택하세요", "Select the data types to request")
    states = dar_states(user)
    busy = [t for t in types if states[t] != "none"]
    if busy:
        labels_ko = ", ".join(catalog.DATA_TYPE_SHORT[t][0] for t in busy)
        labels_en = ", ".join(catalog.DATA_TYPE_SHORT[t][1] for t in busy)
        raise DarError(f"이미 승인되었거나 심사 중인 데이터 유형입니다: {labels_ko}",
                       f"Already approved or under review: {labels_en}")
    fields = {k: data[k] for k in ("name", "institution", "email", "orcid", "approver_name", "approver_title",
                                   "approver_email", "purpose")}
    for _ in range(10):
        try:
            with transaction.atomic():
                return DAR.objects.create(request_id=_next_request_id(), applicant=user, data_types=types,
                                          dua_agreed_at=timezone.now(), **fields)
        except IntegrityError:  # 같은 순번을 동시에 잡은 경우 다시 채번
            continue
    raise DarError("신청번호를 만들 수 없습니다. 다시 시도하세요", "Could not allocate a request number. Try again")


_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
MAX_COMMENT = 2000


def _clean_text(v) -> str:
    return (v or "").translate(_INVISIBLE).strip() if isinstance(v, str) else ""


def _check_reviewer(dar: DataAccessRequest, reviewer) -> None:
    """직무 분리: 관리자는 자신의 신청을 승인·반려할 수 없다(2단계 업로드 승인 규칙과 같음)."""
    if dar.applicant_id == getattr(reviewer, "pk", None):
        raise DarError("본인이 제출한 신청은 검토할 수 없습니다(직무 분리). 다른 관리자가 검토해야 합니다.",
                       "You cannot review your own request (separation of duties). Another administrator must review it.")


def approve_request(dar: DataAccessRequest, reviewer, types=None, comment: str = "") -> DataAccessRequest:
    _check_reviewer(dar, reviewer)
    requested = _clean_types(dar.data_types)
    chosen = requested if types is None else [t for t in _clean_types(types) if t in requested]
    if not chosen:
        raise DarError("승인할 데이터 유형을 하나 이상 선택하세요.", "Select at least one data type to approve.")
    comment = _clean_text(comment)
    if len(comment) > MAX_COMMENT:
        raise DarError(f"최대 {MAX_COMMENT}자까지 입력할 수 있습니다", f"At most {MAX_COMMENT} characters allowed")
    now = timezone.now()
    changed = DAR.objects.filter(pk=dar.pk, status=DAR.Status.PENDING).update(
        status=DAR.Status.APPROVED, approved_types=chosen, reviewer=reviewer, review_comment=comment,
        reviewed_at=now, updated_at=now)
    if changed != 1:
        raise DarError("이미 처리된 신청입니다(검토중 상태가 아님)", "This request has already been processed (not under review)")
    dar.refresh_from_db()
    return dar


def reject_request(dar: DataAccessRequest, reviewer, reason: str) -> DataAccessRequest:
    _check_reviewer(dar, reviewer)
    reason = _clean_text(reason)
    if not reason:
        raise DarError("반려 사유를 입력해주세요.", "Please enter the reason for rejection.")
    if len(reason) > MAX_COMMENT:
        raise DarError(f"최대 {MAX_COMMENT}자까지 입력할 수 있습니다", f"At most {MAX_COMMENT} characters allowed")
    now = timezone.now()
    changed = DAR.objects.filter(pk=dar.pk, status=DAR.Status.PENDING).update(
        status=DAR.Status.REJECTED, approved_types=[], reviewer=reviewer, review_comment=reason,
        reviewed_at=now, updated_at=now)
    if changed != 1:
        raise DarError("이미 처리된 신청입니다(검토중 상태가 아님)", "This request has already been processed (not under review)")
    dar.refresh_from_db()
    return dar


def dar_row(d: DataAccessRequest, lang: str) -> dict:
    st = catalog.DAR_STATUS.get(d.status, catalog.DAR_STATUS["pending"])
    pick = (lambda pair: pair[1] if lang == "en" else pair[0])
    return {"obj": d, "status_label": pick(st["label"]), "status_cls": st["cls"],
            "types": [pick(catalog.DATA_TYPE_SHORT[t]) for t in _clean_types(d.data_types)],
            "approved": [pick(catalog.DATA_TYPE_SHORT[t]) for t in _clean_types(d.approved_types)]}


# ═══════════════════════════════════════════════════════════
#  데이터셋 카드 (원본 하드코딩 records·myContrib·lastUpdate·size → DB 집계)
# ═══════════════════════════════════════════════════════════
def _date(v):
    if v is None:
        return None
    return timezone.localtime(v).date() if hasattr(v, "tzinfo") and hasattr(v, "hour") else v


def _file_bytes(qs) -> int | None:
    total = SubmissionFile.objects.filter(pk__in=qs.values("pk")).aggregate(s=Sum("size"))["s"]
    return int(total) if total else None


def dataset_stats(slug: str, org: str) -> dict:
    """total(전체 규모)·mine(내 기관 기여)·last(최종 업데이트)·size(저장 파일 합계, 파일이 없는 표 데이터는 None).

    '내 기관'은 로그인 사용자의 소속(organization). 케이스 단위 데이터는 케이스의 등록 기관(site),
    업로드로만 생기는 데이터(레지멘·시퀀스)는 제출자의 소속 기관으로 센다.
    """
    P = Patient.objects
    if slug == "patient-cases":
        return {"total": P.count(), "mine": P.filter(site=org).count() if org else 0,
                "last": P.aggregate(m=Max("last_update_date"))["m"], "size": None}
    if slug == "regimens":
        # 원본 '환자별 약제 처방 이력' — 승인된 레지멘 업로드의 처방(약제) 행 수
        return {"total": RegimenDrug.objects.count(),
                "mine": RegimenDrug.objects.filter(regimen__submission__submitter__organization=org).count() if org else 0,
                "last": _date(Regimen.objects.aggregate(m=Max("created_at"))["m"]), "size": None}
    if slug == "dst-results":
        return {"total": DSTResult.objects.count(),
                "mine": DSTResult.objects.filter(patient__site=org).count() if org else 0,
                "last": P.filter(dst_results__isnull=False).aggregate(m=Max("last_update_date"))["m"], "size": None}
    if slug == "genomics":
        # 유전체 데이터셋 카드는 시퀀스(SequenceRun, 검체 단위) 수를 센다(3c 헤더 결정 — 4단계 이후에도 유지).
        # 단위 표기는 원본 그대로('건').
        return {"total": SequenceRun.objects.count(),
                "mine": SequenceRun.objects.filter(submission__submitter__organization=org).count() if org else 0,
                "last": _date(SequenceRun.objects.aggregate(m=Max("created_at"))["m"]),
                "size": _file_bytes(SubmissionFile.objects.filter(sequence_runs__isnull=False).distinct())}
    ds = catalog.DATASET_BY_SLUG[slug]
    modality = ds["modality"]
    # 영상 수는 케이스의 영상 수(cxr/ct/pet_ct_count)를 합한다 — 시드 케이스의 영상 수와 승인된 영상 업로드
    # (apply 가 케이스 영상 수를 올림)를 모두 포함한다. 최종 업데이트는 업로드로 들어온 검사만 본다
    # (공개 샘플 영상 검사 — 4단계 시드 — 의 촬영일은 제외).
    col = {"CXR": "cxr_count", "CT": "ct_count", "PET-CT": "pet_ct_count"}[modality]
    has = {"CXR": "has_cxr", "CT": "has_ct", "PET-CT": "has_pet_ct"}[modality]
    total = P.aggregate(s=Sum(col))["s"] or 0
    mine = (P.filter(site=org).aggregate(s=Sum(col))["s"] or 0) if org else 0
    last_study = ImagingStudy.objects.filter(modality=modality, is_public_sample=False).aggregate(
        m=Max("study_date"))["m"]
    last_case = P.filter(**{has: True}).aggregate(m=Max("last_update_date"))["m"]
    last = max([d for d in (last_study, last_case) if d], default=None)
    return {"total": total, "mine": mine, "last": last,
            "size": _file_bytes(SubmissionFile.objects.filter(imaging_studies__modality=modality).distinct())}


def _records_text(n: int, unit, lang: str) -> str:
    return f"{n:,} {unit[1]}" if lang == "en" else f"{n:,}{unit[0]}"


def dataset_cards(user, lang: str, states: dict | None = None) -> list[dict]:
    org = (getattr(user, "organization", "") or "").strip()
    states = states or dar_states(user)
    pick = (lambda pair: pair[1] if lang == "en" else pair[0])
    cards = []
    for ds in catalog.DATASETS:
        st = dataset_stats(ds["slug"], org)
        total, mine = st["total"], min(st["mine"], st["total"])
        cards.append({
            **ds, "subtitle_text": pick(ds["subtitle"]), "description_text": pick(ds["description"]),
            "total": total, "mine": mine, "pct": round(mine / total * 100) if total else 0,
            "records": _records_text(total, ds["unit"], lang),
            "last": st["last"].isoformat() if st["last"] else "-",
            "size": format_size(st["size"]) if st["size"] else "-",
            "gate": states.get(ds["dar_type"], "none"),
            "label": f"{ds['title']} ({pick(ds['subtitle'])})",
            "rows": f"{export_row_count(ds['slug']):,}", "total_fmt": f"{total:,}", "mine_fmt": f"{mine:,}",
        })
    return cards


def custom_dataset_cards(user, lang: str, states: dict | None = None) -> list[dict]:
    """원본 '커스텀 데이터셋' — 발행(published)되고 비공개가 아닌 데이터 구조. DAR 승인 후 CSV 서식을 내려받는다."""
    from apps.schemas import services as schema_services
    from apps.schemas import ui as schema_ui
    from apps.schemas.models import Schema

    states = states or dar_states(user)
    # 열람 범위(전체 공개·같은 기관 공유·본인·관리자)는 데이터 구조 화면과 같은 규칙(visible_schemas)
    qs = schema_services.with_counts(
        schema_services.visible_schemas(user).filter(status=Schema.Status.PUBLISHED)
        .exclude(visibility=Schema.Visibility.PRIVATE)
    ).order_by("schema_id")
    out = []
    for s in qs:
        card = schema_ui.schema_card(s, lang)
        dtype = catalog.dar_type_of_schema_category(s.category)
        out.append({**card, "dar_type": dtype, "gate": states.get(dtype, "none"),
                    "label": f"{card['name']} (Custom Dataset)" if lang == "en" else f"{card['name']} (커스텀 데이터셋)"})
    return out


# ═══════════════════════════════════════════════════════════
#  내보내기 (원본 datasetExport.ts — 컬럼·파일명 대응, 데이터는 DB 전체)
# ═══════════════════════════════════════════════════════════
EXPORT_HEADERS = {
    "patient-cases": ["condition_id", "age", "sex", "country", "diagnosis_type", "treatment_outcome", "treatment_days"],
    # 원본 4컬럼(환자 단위) + 처방 행 상세 — 처방(약제) 1건이 1행
    "regimens": ["condition_id", "diagnosis_type", "treatment_days", "comorbidities",
                 "drug_name", "drug_code", "start_day", "end_day", "dosage", "route"],
    "dst-results": ["condition_id", "drug", "result"],
    "genomics": ["gene", "position", "ref", "alt", "amino_acid_change", "drug_resistance", "frequency_pct", "sample_count"],
    "cxr-images": ["condition_id", "cxr_count", "findings"],
    "ct-studies": ["condition_id", "ct_count"],
    "pet-ct-studies": ["condition_id", "pet_ct_count"],
}
ITER_CHUNK = 2000


def export_filename(slug: str, ext: str) -> str:
    """원본 `${title.replace(/\\s+/g, '_')}_deidentified.csv`."""
    title = re.sub(r"\s+", "_", catalog.DATASET_BY_SLUG[slug]["title"])
    return f"{title}_deidentified.{ext}"


def _variant_units():
    """환자 내성 변이를 변이 단위로 묶은 [(키, 표시 정보, 보유 대상자 수)] — GAP 과 같은 매칭 규칙(apps.genomics.services):
    카탈로그 변이는 별칭(E. coli 번호·대소문자·괄호 설명)까지 한 행으로, 그 밖은 정규화 표기(없으면 원문) 단위."""
    from apps.genomics import services as gs

    cat = gs.load_catalog()
    counter: Counter = Counter()
    info: dict = {}
    for muts in Patient.objects.exclude(resistance_mutations=[]).values_list("resistance_mutations", flat=True).iterator(
            chunk_size=ITER_CHUNK):
        units = {}
        for m in gs.clean_mutations(muts):
            v = cat.match(m)
            if v is not None:
                units[f"v:{v.pk}"] = v
            else:
                k = gs.key_of(m)
                units["k:" + (k or " ".join(m.split()).lower())] = " ".join(m.split())
        for key, val in units.items():
            counter[key] += 1
            info.setdefault(key, val)
    return cat, sorted(((k, info[k], n) for k, n in counter.items()), key=lambda r: (-r[2], r[0]))


def _variant_rows():
    """유전체 변이(원본 variants 컬럼) — 변이 단위 보유 대상자 수와 빈도. 빈도 분모는 GAP 과 같은
    '유전체 데이터 보유 대상자'(시퀀스 보유 또는 내성 변이 기록, genomic_patients_q). 카탈로그 변이는 좌표·아미노산
    변이·연관 약제(대표 + 추가, ';' 구분)를 카탈로그 값으로, 카탈로그 밖 표기는 좌표를 빈 칸으로 둔다."""
    from apps.genomics import services as gs

    denom = Patient.objects.filter(gs.genomic_patients_q()).count()
    _, rows = _variant_units()
    for key, val, n in rows:
        freq = f"{n / denom * 100:.1f}" if denom else "0.0"
        if key.startswith("v:"):
            v = val
            yield [v.gene.gene, v.position, v.ref_allele, v.alt_allele, v.amino_acid_change,
                   ";".join(gs.variant_drugs(v)), freq, n]
            continue
        parts = val.split(" ")
        yield [parts[0], "", "", "", parts[1] if len(parts) > 1 else "", related_drug(val), freq, n]


def export_rows(slug: str):
    """행 반복자(전체를 메모리에 올리지 않도록 DB 커서를 청크 단위로 읽는다)."""
    P = Patient.objects
    if slug == "patient-cases":
        return P.order_by("id").values_list(
            "condition_id", "age", "sex", "country", "diagnosis_type", "treatment_outcome", "treatment_duration"
        ).iterator(chunk_size=ITER_CHUNK)
    if slug == "regimens":
        return RegimenDrug.objects.order_by("regimen__patient_id", "regimen_id", "order", "id").values_list(
            "regimen__patient__condition_id", "regimen__patient__diagnosis_type", "regimen__patient__treatment_duration",
            "regimen__patient__comorbidities", "drug_name", "drug_code", "start_day", "end_day", "dosage", "route",
        ).iterator(chunk_size=ITER_CHUNK)
    if slug == "dst-results":
        return DSTResult.objects.order_by("patient_id", "order", "id").values_list(
            "patient__condition_id", "drug", "result").iterator(chunk_size=ITER_CHUNK)
    if slug == "genomics":
        return _variant_rows()
    if slug == "cxr-images":
        return P.filter(has_cxr=True).order_by("id").values_list("condition_id", "cxr_count", "cxr_findings").iterator(
            chunk_size=ITER_CHUNK)
    if slug == "ct-studies":
        return P.filter(has_ct=True).order_by("id").values_list("condition_id", "ct_count").iterator(chunk_size=ITER_CHUNK)
    if slug == "pet-ct-studies":
        return P.filter(has_pet_ct=True).order_by("id").values_list("condition_id", "pet_ct_count").iterator(
            chunk_size=ITER_CHUNK)
    raise KeyError(slug)


def export_row_count(slug: str) -> int:
    P = Patient.objects
    if slug == "patient-cases":
        return P.count()
    if slug == "regimens":
        return RegimenDrug.objects.count()
    if slug == "dst-results":
        return DSTResult.objects.count()
    if slug == "genomics":
        return len(_variant_units()[1])
    has = {"cxr-images": "has_cxr", "ct-studies": "has_ct", "pet-ct-studies": "has_pet_ct"}[slug]
    return P.filter(**{has: True}).count()


def _text(v, strip=None) -> str:
    """셀 문자열: 목록은 ';' 로 잇고, 스프레드시트 수식 주입을 막는다(csvio.safe_cell — 2단계와 같은 규칙).
    safe_cell 이 C0 제어문자(탭·개행 제외)를 먼저 지우고 접두 처리하므로 CSV 에서도 '\x01=1+1' → "'=1+1".
    strip: 엑셀용 추가 제거 정규식(openpyxl 이 거부하는 문자) — 역시 safe_cell 보다 먼저 지운다."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = ";".join(str(x) for x in v)
    elif isinstance(v, bool):
        v = "Y" if v else "N"
    v = str(v)
    if strip is not None:
        v = strip.sub("", v)
    return safe_cell(v)


class _Echo:
    def write(self, value):
        return value


def stream_csv(slug: str):
    """CSV 스트리밍(BOM + 헤더 + 행). 500행씩 묶어 내보낸다."""
    w = csv.writer(_Echo(), lineterminator="\n")
    yield "﻿" + w.writerow(EXPORT_HEADERS[slug])
    buf = []
    for row in export_rows(slug):
        buf.append(w.writerow([_text(v) for v in row]))
        if len(buf) >= 500:
            yield "".join(buf)
            buf = []
    if buf:
        yield "".join(buf)


class ExportTooLarge(Exception):
    pass


def build_xlsx(slug: str):
    """openpyxl write_only 로 임시 파일에 쓴다(행을 메모리에 쌓지 않음). 반환: 처음으로 되감긴 파일 객체.

    엑셀 행 상한(XLSX_MAX_ROWS)을 넘으면 ExportTooLarge — CSV 를 쓰도록 안내한다.
    문자열은 safe_cell 로 수식 주입을 막고, openpyxl 이 거부하는 제어문자는 지운다(500 방지).
    """
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    if export_row_count(slug) > settings.DATASHARING_XLSX_MAX_ROWS:
        raise ExportTooLarge(slug)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=catalog.DATASET_BY_SLUG[slug]["title"][:31])
    ws.append(EXPORT_HEADERS[slug])
    for row in export_rows(slug):
        cells = []
        for v in row:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append(v)
            else:
                cells.append(_text(v, strip=ILLEGAL_CHARACTERS_RE))
        ws.append(cells)
    tmp = tempfile.TemporaryFile(dir=settings.FILE_UPLOAD_TEMP_DIR, suffix=".xlsx")
    try:
        wb.save(tmp)
        tmp.seek(0)
    except Exception:
        tmp.close()
        raise
    return tmp


# ═══════════════════════════════════════════════════════════
#  API 키 (해시만 저장, 1회 표시, 재발급 시 이전 키 폐기)
# ═══════════════════════════════════════════════════════════
KEY_PREFIX = "tbk_"
_KEY_RE = re.compile(r"^tbk_[A-Za-z0-9_-]{20,80}$")


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def active_key(user) -> ApiKey | None:
    return ApiKey.objects.filter(user=user, revoked_at__isnull=True).order_by("-created_at", "-id").first()


def issue_key(user, email: str, institution: str) -> tuple[ApiKey, str]:
    """승인된 DAR 이 하나 이상 있어야 발급된다. 이전 유효 키는 폐기된다. 반환: (키, 원문 — 이때 한 번만 보인다)."""
    if not approved_types(user):
        raise DarError("데이터 접근 신청(DAR)이 승인된 뒤에 API 키를 발급받을 수 있습니다.",
                       "An API key can be issued after a data access request (DAR) is approved.")
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    now = timezone.now()
    with transaction.atomic():
        ApiKey.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=now)
        key = ApiKey.objects.create(user=user, prefix=raw[:12], key_hash=hash_key(raw), email=email,
                                    institution=institution)
    return key, raw


def revoke_keys(user) -> int:
    return ApiKey.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())


def authenticate(header: str | None) -> ApiKey | None:
    """Authorization: Bearer <키>. 형식이 틀리거나, 없는/폐기된 키, 비활성 사용자면 None.

    키 원문은 DB 에 없으므로 SHA-256 해시로 찾고, 찾은 해시와 계산한 해시를 상수 시간으로 비교한다
    (조회 자체는 해시 값 기준이라 원문에 대한 타이밍 정보가 새지 않는다)."""
    if not header or not isinstance(header, str) or len(header) > 200:
        return None
    scheme, _, raw = header.strip().partition(" ")
    raw = raw.strip()
    if scheme.lower() != "bearer" or not _KEY_RE.match(raw):
        return None
    digest = hash_key(raw)
    key = ApiKey.objects.select_related("user").filter(key_hash=digest).first()
    if key is None or not hmac.compare_digest(key.key_hash, digest):
        return None
    if key.revoked_at is not None or not key.user.is_active:
        return None
    now = timezone.now()
    if key.last_used_at is None or (now - key.last_used_at).total_seconds() > 60:  # 쓰기 빈도 제한
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
    return key


def _auth_fail_key(ip: str) -> str:
    # 캐시 키에 사용자 입력을 그대로 넣지 않는다(REMOTE_ADDR 은 서버가 채우지만 길이·문자를 정규화)
    return "tb:api-authfail:" + hashlib.sha256((ip or "-").encode("utf-8", "replace")).hexdigest()[:32] + \
        f":{int(time.time() // 60)}"


def auth_fail_blocked(ip: str) -> tuple[bool, int]:
    """이 클라이언트(REMOTE_ADDR)가 이번 분 창에서 인증 실패 한도(settings.API_AUTH_FAIL_LIMIT_PER_MINUTE)를 넘었는가.
    넘었으면 올바른 키로 요청해도 창이 끝날 때까지 429(키 무차별 대입의 성공 여부를 알려주지 않는다).
    한계는 rate_limit_hit 와 같다(프로세스별 LocMemCache 계수, 고정 창)."""
    now = time.time()
    n = cache.get(_auth_fail_key(ip)) or 0
    return n >= settings.API_AUTH_FAIL_LIMIT_PER_MINUTE, int(60 - (now % 60)) + 1


def auth_fail_hit(ip: str) -> tuple[bool, int]:
    """인증 실패 1회를 센다. 반환: (한도 초과 여부 — 이 요청부터 429, 다음 창까지 남은 초)."""
    now = time.time()
    ck = _auth_fail_key(ip)
    cache.add(ck, 0, timeout=120)
    try:
        n = cache.incr(ck)
    except ValueError:  # 만료와 경합
        cache.set(ck, 1, timeout=120)
        n = 1
    return n > settings.API_AUTH_FAIL_LIMIT_PER_MINUTE, int(60 - (now % 60)) + 1


def rate_limit_hit(key: ApiKey) -> tuple[bool, int]:
    """키당 분당 요청 수 제한(settings.API_RATE_LIMIT_PER_MINUTE). 반환: (초과 여부, 다음 창까지 남은 초).

    한계: Django 캐시(기본 LocMemCache)를 쓰므로 **프로세스별** 계수다 — 단일 프로세스 waitress(스레드 여러 개)
    전제에서는 정확하지만, 여러 프로세스·서버로 늘리면 프로세스마다 따로 센다(공유 캐시 Redis 등으로 바꿔야 함).
    서버를 재시작하면 계수가 초기화된다. 고정 창(분 단위) 방식이라 창 경계에서 최대 2배까지 몰릴 수 있다.
    """
    limit = settings.API_RATE_LIMIT_PER_MINUTE
    now = time.time()
    window = int(now // 60)
    ck = f"tb:api-rl:{key.pk}:{window}"
    cache.add(ck, 0, timeout=120)
    try:
        n = cache.incr(ck)
    except ValueError:  # 만료와 경합
        cache.set(ck, 1, timeout=120)
        n = 1
    return n > limit, int(60 - (now % 60)) + 1

