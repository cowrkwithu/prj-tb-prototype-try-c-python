"""업로드 작업 흐름 — 임시저장(draft) · 행 검증 · 파일 · 비식별화 · 제출 · 검토(승인/반려/수정요청).

모든 판정은 서버가 한다. 화면(Alpine)의 즉시 표시는 보조일 뿐이며, 제출·승인 직전에 다시 검증한다.
"""
from __future__ import annotations

from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.safejson import strip_bad_chars
from apps.core.textinput import clean_free_text, free_text_problem

from ..models import Submission, SubmissionEvent, SubmissionFile, SubmissionRecord
from . import csvio, deidentify, dicom, files, schemas, validators

MAX_TEXT = 500


class WorkflowError(ValueError):
    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en


# ─── 업로드 ID (원본 utils/uploadId.ts: UPL-YYYYMMDD-NNN, 그날의 순번 — 여기서는 DB 채번) ───
def _next_upload_id() -> str:
    """같은 날짜의 순번 최댓값 + 1. 문자열 정렬이 아니라 정수로 비교한다(999 → 1000 → 1001)."""
    prefix = f"UPL-{timezone.localdate():%Y%m%d}-"
    nums = [int(tail) for tail in (u[len(prefix):] for u in
                                   Submission.objects.filter(upload_id__startswith=prefix).values_list("upload_id", flat=True))
            if tail.isdigit()]
    return f"{prefix}{(max(nums) + 1 if nums else 1):03d}"


def create_draft(user, domain: str, mode: str, condition_id: str = "", **extra) -> Submission:
    if domain not in schemas.DOMAINS or mode not in schemas.MODES[domain]:
        raise WorkflowError("잘못된 업로드 유형입니다", "Invalid upload type")
    for _ in range(10):
        try:
            with transaction.atomic():
                return Submission.objects.create(
                    upload_id=_next_upload_id(), submitter=user, domain=domain, mode=mode,
                    method=schemas.method_for(domain, mode), condition_id=condition_id[:40], **extra)
        except IntegrityError:  # 같은 순번을 동시에 잡은 경우 다시 채번
            continue
    raise WorkflowError("업로드 ID 를 만들 수 없습니다. 다시 시도하세요", "Could not allocate an upload ID. Try again")


# ─── 편집 경합 방지: 트랜잭션 안에서 잠그고 draft 인지 다시 확인한다 ───
EDIT_FIELDS = ["condition_id", "form_data", "source_filename", "csv_headers", "file_errors", "total_records",
               "valid_records", "warning_records", "error_records", "issue_count", "pii_report", "pii_blocking",
               "mode", "method", "step", "ignore_errors", "updated_at"]


def _lock_draft(sub: Submission) -> Submission:
    locked = Submission.objects.select_for_update().filter(pk=sub.pk, status=Submission.Status.DRAFT).first()
    if locked is None:
        raise WorkflowError("이미 제출된 업로드는 수정할 수 없습니다", "Submitted uploads cannot be edited")
    return locked


def _sync(dst: Submission, src: Submission) -> None:
    dst.__dict__.update({k: v for k, v in src.__dict__.items() if not k.startswith("_")})


def set_step(sub: Submission, step: int, **fields) -> None:
    """draft 일 때만 단계(와 지정 필드)를 바꾼다 — 조건부 UPDATE."""
    changed = Submission.objects.filter(pk=sub.pk, status=Submission.Status.DRAFT).update(
        step=step, updated_at=timezone.now(), **fields)
    if not changed:
        raise WorkflowError("이미 제출된 업로드는 수정할 수 없습니다", "Submitted uploads cannot be edited")
    sub.step = step
    for k, v in fields.items():
        setattr(sub, k, v)


def change_mode(sub: Submission, mode: str) -> None:
    """방식 선택(0단계)에서 방식을 바꾸면 입력한 행·파일을 비운다."""
    with transaction.atomic():
        s = _lock_draft(sub)
        if mode != s.mode:
            for f in list(s.files.all()):
                files.delete_stored(f)
            s.records.all().delete()
            s.mode, s.method = mode, schemas.method_for(s.domain, mode)
            s.form_data, s.csv_headers, s.source_filename, s.file_errors = {}, [], "", []
            s.total_records = s.valid_records = s.warning_records = s.error_records = s.issue_count = 0
            s.pii_report, s.pii_blocking, s.ignore_errors = {}, False, False
        s.step = 1
        s.save(update_fields=EDIT_FIELDS)
    _sync(sub, s)


def record_file_error(sub: Submission, ko: str, en: str) -> None:
    with transaction.atomic():
        s = _lock_draft(sub)
        s.file_errors = [{"ko": ko, "en": en}]
        s.save(update_fields=["file_errors", "updated_at"])
    _sync(sub, s)


def form_text(v) -> str:
    """업로드 웹 폼 입력값(행 검증 전 원값) — NUL·고립 서로게이트는 지운다(PostgreSQL 은 NUL 을 저장하지 못한다.
    값은 이어서 행 검증을 거치므로 지운 결과도 형식 검사를 받는다)."""
    return strip_bad_chars(v).strip()[:MAX_TEXT] if isinstance(v, str) else ""


_txt = form_text


# ─── 폼 → 행 ───
CLINICAL_KEYS = ["condition_id", "registration_date", "age", "sex", "country", "site", "diagnosis_type",
                 "diagnosis_date", "site_of_disease", "treatment_outcome", "outcome_date", "previous_treatment_count"]
GENOMIC_KEYS = ["condition_id", "specimen_id", "platform", "method", "coverage", "sra_accession", "lineage"]


def rows_from_form(domain: str, post, sub: Submission | None) -> tuple[list[dict], dict]:
    """폼 입력(request.POST) → (검증할 행 목록, 이어쓰기용 form_data)."""
    if domain == "clinical":
        row = {k: _txt(post.get(k)) for k in CLINICAL_KEYS}
        prev = bool(post.get("previous_treatment"))
        row["previous_treatment"] = "true" if prev else "false"
        if not prev:
            row["previous_treatment_count"] = ""
        row["comorbidities"] = ",".join(c for c in post.getlist("comorbidities") if c in schemas.COMORBIDITIES)
        if row["treatment_outcome"] == "Ongoing":
            row["outcome_date"] = ""
        return [row], dict(row)
    if domain == "regimen":
        cid = _txt(post.get("condition_id"))
        names, starts, ends = post.getlist("drug_name"), post.getlist("start_day"), post.getlist("end_day")
        doses, routes = post.getlist("dosage"), post.getlist("route")
        drugs, rows = [], []
        for i in range(min(len(names), 200)):
            d = {"drug_name": _txt(names[i]), "start_day": _txt(_at(starts, i)), "end_day": _txt(_at(ends, i)),
                 "dosage": _txt(_at(doses, i)), "route": _txt(_at(routes, i)) or "Oral"}
            if not (d["drug_name"] or d["end_day"]):
                continue
            d["drug_code"] = schemas.DRUG_CODE_BY_NAME.get(d["drug_name"], "")
            drugs.append(d)
            rows.append({"condition_id": cid, **d})
        return rows, {"condition_id": cid, "drugs": drugs}
    if domain == "lab":
        row = {k: _txt(post.get(k)) for k in ("condition_id", "specimen_id", "specimen_date", "specimen_type")}
        flags = {k: bool(post.get(f"show_{k}")) for k in ("microscopy", "culture", "genexpert", "dst", "biochem")}
        if flags["microscopy"]:
            row.update(microscopy_result=_txt(post.get("microscopy_result")), microscopy_date=_txt(post.get("microscopy_date")))
        if flags["culture"]:
            row.update(culture_method=_txt(post.get("culture_method")), culture_result=_txt(post.get("culture_result")),
                       culture_date=_txt(post.get("culture_date")))
            if row["culture_result"] == "Positive":
                row["days_to_positivity"] = _txt(post.get("days_to_positivity"))
        if flags["genexpert"]:
            row["genexpert_mtb"] = _txt(post.get("genexpert_mtb"))
            if row["genexpert_mtb"] == "Detected":  # 원본: MTB Detected 일 때만 RIF 입력 가능
                row["genexpert_rif"] = _txt(post.get("genexpert_rif"))
        if flags["dst"]:
            row["dst_method"] = _txt(post.get("dst_method"))
            for drug in schemas.DST_FORM_DRUGS:
                v = _txt(post.get(f"dst_{drug.lower()}"))
                if v:
                    row[drug.lower()] = v
        if flags["biochem"]:
            tests, values, units, dates = (post.getlist("bc_test"), post.getlist("bc_value"),
                                           post.getlist("bc_unit"), post.getlist("bc_date"))
            row["biochemistry"] = [
                {"test": _txt(tests[i]), "value": _txt(_at(values, i)), "unit": _txt(_at(units, i)), "date": _txt(_at(dates, i))}
                for i in range(min(len(tests), 50)) if _txt(tests[i]) or _txt(_at(values, i))]
        return [row], {**row, "flags": flags}
    if domain == "genomic":
        row = {k: _txt(post.get(k)) for k in GENOMIC_KEYS}
        return [row], dict(row)
    if domain == "imaging":
        base = dict(sub.form_data) if sub else {}
        finds, locs, sevs, descs = (post.getlist("ann_finding"), post.getlist("ann_location"),
                                    post.getlist("ann_severity"), post.getlist("ann_description"))
        findings = [{"finding": _txt(finds[i]), "location": _txt(_at(locs, i)), "severity": _txt(_at(sevs, i)),
                     "description": _txt(_at(descs, i))}
                    for i in range(min(len(finds), 50)) if any((_txt(finds[i]), _txt(_at(locs, i)), _txt(_at(descs, i))))]
        base.update(study_description=_txt(post.get("study_description")), tb_activity=_txt(post.get("tb_activity")),
                    impression=_txt(post.get("impression")), findings=findings)
        return [imaging_row(sub, base)], base
    raise WorkflowError("잘못된 업로드 유형입니다", "Invalid upload type")


def _at(lst, i):
    return lst[i] if i < len(lst) else ""


def imaging_row(sub: Submission | None, data: dict) -> dict:
    return {"condition_id": data.get("condition_id", ""), "modality": sub.mode if sub else data.get("modality", ""),
            "study_date": data.get("study_date", ""), "study_description": data.get("study_description", ""),
            "tb_activity": data.get("tb_activity", ""), "impression": data.get("impression", ""),
            "findings": data.get("findings", [])}


# ─── 행 저장·검증 ───
@transaction.atomic
def store_rows(sub: Submission, rows: list[dict], form: bool) -> dict:
    """행을 검증해 교체 저장한다. 호출자는 sub 를 잠근 상태여야 한다(편집 함수들) — 제출·복제 경로도 트랜잭션 안."""
    results = validators.validate_rows(sub.domain, rows, form=form)
    sub.records.all().delete()
    SubmissionRecord.objects.bulk_create([
        SubmissionRecord(submission=sub, row_no=i, payload=r["payload"], status=r["status"], messages=r["messages"])
        for i, r in enumerate(results, start=1)], batch_size=500)
    summary = validators.summarize(results)
    sub.total_records, sub.valid_records = summary["total"], summary["valid"]
    sub.warning_records, sub.error_records, sub.issue_count = summary["warning"], summary["error"], summary["issues"]
    ids = {r.get("condition_id", "") for r in rows if r.get("condition_id")}
    if len(ids) == 1:
        sub.condition_id = next(iter(ids))[:40]
    elif not ids:
        sub.condition_id = ""
    refresh_pii(sub, rows)
    sub.save(update_fields=EDIT_FIELDS)
    return summary


def refresh_pii(sub: Submission, rows: list[dict] | None = None) -> None:
    if rows is None:
        rows = [r.payload for r in sub.records.all()]
    report = deidentify.scan_rows(rows, schemas.safe_columns(sub.domain))
    if sub.domain == "imaging":
        report = deidentify.merge_dicom(report, _dicom_detections(sub))
    sub.pii_report = report
    sub.pii_blocking = report["blocking"]


def _dicom_detections(sub: Submission) -> list[dict]:
    out = []
    for f in sub.files.filter(kind=SubmissionFile.Kind.IMAGE):
        info = f.meta.get("dicom")
        if not info:
            continue
        if not info.get("parsed"):
            # 식별정보가 없음을 확인할 수 없으면 차단한다(헤더 결정) — 사유를 함께 보인다
            d = deidentify.dicom_detection(f.original_name, "header", "unreadable")
            d.update(confidence="high", masked="-",
                     ko=f"DICOM 헤더를 끝까지 판독하지 못해 식별정보 부재를 확인할 수 없습니다 — 제출 불가 ({info.get('error', '')})",
                     en=f"The DICOM header could not be fully read, so the absence of identifiers cannot be confirmed — "
                        f"submission blocked ({info.get('error_en', info.get('error', ''))})")
            out.append(d)
            continue
        for tag in info.get("tags", []):
            if tag.get("risky"):
                out.append(deidentify.dicom_detection(f.original_name, tag["name"], tag["value"]))
    return out


def save_form(sub: Submission, post) -> dict:
    with transaction.atomic():
        s = _lock_draft(sub)
        rows, form_data = rows_from_form(s.domain, post, s)
        s.form_data = form_data
        summary = store_rows(s, rows, form=True)
    _sync(sub, s)
    return summary


def handle_csv(sub: Submission, upload, lang: str = "ko") -> dict:
    """CSV 파일 검증·파싱·행 검증(잠금 안에서). 파일 형식 거부는 FileRejected."""
    with transaction.atomic():
        s = _lock_draft(sub)
        summary = _handle_csv(s, upload)
        s.ignore_errors = False
        s.save(update_fields=["ignore_errors"])
    _sync(sub, s)
    return summary


def _handle_csv(sub: Submission, upload) -> dict:
    sub.file_errors = []
    name, ext = files.validate_upload(upload, "csv", "csv")
    upload.seek(0)
    data = b"".join(upload.chunks())  # CSV 는 최대 50MB — 파싱을 위해 읽는다
    # 이전 CSV 파일은 교체한다
    for old in sub.files.filter(kind=SubmissionFile.Kind.CSV):
        files.delete_stored(old)
    files.store(sub, upload, SubmissionFile.Kind.CSV, name, ext)
    sub.source_filename = name
    try:
        parsed = csvio.parse(data, delimiter="\t" if ext == ".tsv" else ",")
    except csvio.CsvError as exc:
        sub.file_errors = [{"ko": exc.ko, "en": exc.en}]
        sub.csv_headers = []
        return store_rows(sub, [], form=False)
    sub.csv_headers = parsed["headers"]
    sub.form_data = {"encoding": parsed["encoding"]}
    missing = validators.missing_columns(sub.domain, parsed["headers"])
    if missing:
        cols = ", ".join(missing)
        sub.file_errors = [{"ko": f"필수 컬럼 누락: {cols}", "en": f"Missing required columns: {cols}"}]
        return store_rows(sub, [], form=False)
    if not parsed["rows"]:
        sub.file_errors = [{"ko": "데이터 행이 없습니다", "en": "No data rows"}]
    return store_rows(sub, parsed["rows"], form=False)


# ─── 시퀀스·영상 파일 ───
def add_files(sub: Submission, uploads: list) -> list[dict]:
    """파일을 검증해 저장한다(잠금 안에서). 반환: 거부된 파일 [{name, ko, en}]."""
    with transaction.atomic():
        s = _lock_draft(sub)
        rejected = _add_files(s, uploads)
    _sync(sub, s)
    return rejected


def _add_files(sub: Submission, uploads: list) -> list[dict]:
    rejected = []
    if sub.domain == "genomic":
        limit_key, mime, kind = "sequence", "sequence", SubmissionFile.Kind.SEQUENCE
        max_files = files.settings.UPLOAD_LIMITS["sequence"]["max_files"][sub.mode]
    else:
        limit_key, mime, kind = schemas.image_limit_key(sub.mode), "image", SubmissionFile.Kind.IMAGE
        max_files = files.settings.UPLOAD_LIMITS[limit_key]["max_files"]
    have = sub.files.filter(kind=kind).count()
    for up in uploads:
        shown = files.sanitize_name(getattr(up, "name", ""))
        if have >= max_files:
            rejected.append({"name": shown, "ko": f"최대 {max_files}개까지 올릴 수 있습니다", "en": f"Up to {max_files} files"})
            continue
        try:
            name, ext = files.validate_upload(up, limit_key, mime)
        except files.FileRejected as exc:
            rejected.append({"name": shown, "ko": exc.ko, "en": exc.en})
            continue
        meta = {}
        if ext in (".dcm", ".dicom"):
            up.seek(0)
            meta["dicom"] = dicom.inspect(up)
            up.seek(0)
        stored = files.store(sub, up, kind, name, ext, meta)
        # 같은 업로드 안에 내용이 같은 파일(SHA-256)이 이미 있으면 거부한다 — 해시는 저장하면서 계산하므로
        # 파일을 두 번 읽지 않도록 저장 뒤 비교하고, 중복이면 방금 저장한 것을 지운다.
        dup = sub.files.filter(sha256=stored.sha256).exclude(pk=stored.pk).first()
        if dup is not None:
            files.delete_stored(stored)
            rejected.append({"name": shown, "ko": f"같은 내용의 파일이 이미 이 업로드에 있습니다({dup.original_name})",
                             "en": f"A file with identical content is already in this upload ({dup.original_name})"})
            continue
        have += 1
    if sub.domain == "imaging":
        refresh_pii(sub)
        sub.save(update_fields=EDIT_FIELDS)
    return rejected


def remove_file(sub: Submission, file_id: int) -> None:
    with transaction.atomic():
        s = _lock_draft(sub)
        f = s.files.filter(pk=file_id).exclude(kind=SubmissionFile.Kind.CSV).first()
        if f is None:
            raise WorkflowError("파일을 찾을 수 없습니다", "File not found")
        files.delete_stored(f)
        if s.domain == "imaging":
            refresh_pii(s)
        s.save(update_fields=EDIT_FIELDS)
    _sync(sub, s)


def save_imaging_meta(sub: Submission, post) -> dict:
    """영상 파일 단계의 Condition ID·촬영일 — 행(1건)을 다시 만든다."""
    with transaction.atomic():
        s = _lock_draft(sub)
        data = dict(s.form_data)
        data.update(condition_id=_txt(post.get("condition_id")), study_date=_txt(post.get("study_date")))
        s.form_data = data
        summary = store_rows(s, [imaging_row(s, data)], form=True)
    _sync(sub, s)
    return summary


# ─── 단계 진행 조건 ───
def data_ready(sub: Submission) -> tuple[bool, list[tuple[str, str]]]:
    """데이터 단계를 넘어갈 수 있는가 + 이유."""
    problems = []
    if sub.file_errors:
        problems += [(e["ko"], e["en"]) for e in sub.file_errors]
    if not sub.total_records:
        problems.append(("입력된 데이터가 없습니다", "No data entered"))
    if sub.error_records and not (sub.ignore_errors and sub.method == Submission.Method.CSV):
        problems.append(("오류가 있는 행이 있습니다", "Some rows have errors"))
    return not problems, problems


def files_ready(sub: Submission) -> tuple[bool, list[tuple[str, str]]]:
    kind = SubmissionFile.Kind.SEQUENCE if sub.domain == "genomic" else SubmissionFile.Kind.IMAGE
    if not sub.files.filter(kind=kind).exists():
        if sub.domain == "genomic":
            return False, [("시퀀스 파일을 하나 이상 선택해야 넘어갈 수 있습니다", "Select at least one sequence file to continue.")]
        return False, [("영상 파일을 하나 이상 선택해야 넘어갈 수 있습니다", "Select at least one image file to continue.")]
    if sub.domain == "imaging":
        row = sub.records.first()
        bad = [m for m in (row.messages if row else []) if m["severity"] == "error"
               and m["field"] in ("condition_id", "study_date", "modality")]
        if not row or bad:
            return False, [(m["ko"], m["en"]) for m in bad] or [("Condition ID·촬영일을 입력하세요", "Enter the Condition ID and study date")]
    return True, []


# ─── 제출 ───
def revalidate(sub: Submission) -> None:
    """제출·승인 직전 재검증(대상자 존재 여부 등은 그 사이에 바뀔 수 있다)."""
    rows = [r.payload for r in sub.records.order_by("row_no")]
    store_rows(sub, rows, form=sub.method != Submission.Method.CSV)


def submit(sub: Submission, user, checklist: list[str], signer: str = "", confirm_date: str = "") -> None:
    if sub.submitter_id != user.pk:
        raise WorkflowError("본인 업로드만 제출할 수 있습니다", "You can only submit your own uploads")
    with transaction.atomic():
        locked = Submission.objects.select_for_update().get(pk=sub.pk)
        if locked.status != Submission.Status.DRAFT:
            raise WorkflowError("이미 제출된 업로드입니다", "This upload has already been submitted")
        revalidate(locked)
        ok, problems = data_ready(locked)
        if not ok:
            raise WorkflowError(*problems[0])
        if locked.domain in ("genomic", "imaging"):
            ok, problems = files_ready(locked)
            if not ok:
                raise WorkflowError(*problems[0])
        if locked.pii_blocking:
            raise WorkflowError("개인정보가 탐지되어 제출할 수 없습니다. 해당 값을 제거한 뒤 다시 업로드해야 합니다.",
                                "Cannot submit: personal data was detected. Remove it and upload again.")
        checked = sorted({int(c) for c in checklist if str(c).isdigit() and int(c) < len(schemas.DEID_CHECK_ITEMS)})
        if len(checked) != len(schemas.DEID_CHECK_ITEMS):
            raise WorkflowError("비식별화 체크리스트를 모두 확인해야 제출할 수 있습니다.",
                                "Confirm the whole de-identification checklist to enable submission.")
        locked.deid_checklist = [True] * len(schemas.DEID_CHECK_ITEMS)
        signer = clean_text(signer)
        if len(signer) > 100:
            raise WorkflowError("기관 담당자 서명은 최대 100자까지 입력할 수 있습니다", "The signature can be at most 100 characters")
        check_text(signer, 100)
        locked.deid_signer = signer
        try:
            locked.deid_date = date.fromisoformat(confirm_date) if confirm_date else timezone.localdate()
        except ValueError:
            locked.deid_date = timezone.localdate()
        now = timezone.now()
        _clear_deletion_request(locked, user, now, "재제출 — 이전 삭제 요청 정리",
                                "Resubmitted — previous deletion request cleared")
        locked.status = Submission.Status.PENDING
        locked.revision_requested = False
        locked.submitted_at = now
        locked.step = schemas.step_index(locked.domain, "done")
        locked.save()
        SubmissionEvent.objects.create(submission=locked, actor=user, action=SubmissionEvent.Action.SUBMITTED, at=now)
    sub.refresh_from_db()


# ─── 검토 (관리자) ───
MAX_COMMENT = 2000
clean_text = clean_free_text  # 제로폭 문자 제거 + 공백 다듬기(apps.core.textinput)


def check_text(text: str, max_len: int = MAX_COMMENT) -> str:
    """자유 텍스트(검토 의견·사유·서명) 공통 검사 — NUL·서로게이트·길이 초과는 WorkflowError."""
    problem = free_text_problem(text, max_len)
    if problem:
        raise WorkflowError(*problem)
    return text


def _reason(raw, ko_empty, en_empty) -> str:
    reason = clean_text(raw)
    if not reason:
        raise WorkflowError(ko_empty, en_empty)
    return check_text(reason)


def is_own_upload(sub: Submission, reviewer) -> bool:
    return sub.submitter_id == getattr(reviewer, "pk", None)


def _check_reviewer(sub: Submission, reviewer) -> None:
    """직무 분리: 관리자는 자신이 올린 업로드를 승인·반려·수정요청할 수 없다."""
    if is_own_upload(sub, reviewer):
        raise WorkflowError("본인이 제출한 업로드는 검토할 수 없습니다(직무 분리). 다른 관리자가 검토해야 합니다.",
                            "You cannot review your own upload (separation of duties). Another administrator must review it.")


def _transition(sub_pk: int, reviewer, new_status: str, comment: str, extra: dict | None = None) -> Submission:
    """pending 인 건만 상태를 바꾼다 — 조건부 UPDATE 로 이중 처리(이중 승인)를 막는다."""
    now = timezone.now()
    fields = {"status": new_status, "reviewer": reviewer, "reviewed_at": now, "review_comment": comment, "updated_at": now}
    fields.update(extra or {})
    changed = Submission.objects.filter(pk=sub_pk, status=Submission.Status.PENDING).update(**fields)
    if changed != 1:
        raise WorkflowError("이미 처리된 업로드입니다(검증 대기 상태가 아님)", "This upload has already been processed (not pending)")
    return Submission.objects.select_for_update().get(pk=sub_pk)


def approve(sub: Submission, reviewer, checklist: list[str], comment: str = "") -> Submission:
    from .apply import apply_submission, assign_approval_seq, lock_patients, patient_ids_for

    checked = {int(c) for c in checklist if str(c).isdigit() and int(c) < len(schemas.REVIEW_CHECK_ITEMS)}
    _check_reviewer(sub, reviewer)
    if len(checked) != len(schemas.REVIEW_CHECK_ITEMS):
        raise WorkflowError("체크리스트 항목을 모두 확인해야 승인할 수 있습니다.", "Check every checklist item before you can approve.")
    comment = check_text(clean_text(comment))
    with transaction.atomic():
        # 잠금 순서(삭제 승인과 같다): 대상 환자(id 오름차순 한 문장) → 업로드 행. 승인 순서 번호도 이 잠금 안에서.
        lock_patients(patient_ids_for(sub))
        locked = _transition(sub.pk, reviewer, Submission.Status.APPROVED, comment,
                             {"review_checklist": [True] * len(schemas.REVIEW_CHECK_ITEMS)})
        assign_approval_seq(locked)
        summary = apply_submission(locked)
        locked.apply_summary = summary
        locked.save(update_fields=["apply_summary"])
        SubmissionEvent.objects.create(submission=locked, actor=reviewer, action=SubmissionEvent.Action.APPROVED,
                                       comment=locked.review_comment, at=locked.reviewed_at)
    return locked


def reject(sub: Submission, reviewer, reason: str) -> Submission:
    _check_reviewer(sub, reviewer)
    reason = _reason(reason, "반려 사유를 입력해주세요.", "Please enter the reason for rejection.")
    with transaction.atomic():
        locked = _transition(sub.pk, reviewer, Submission.Status.REJECTED, reason)
        SubmissionEvent.objects.create(submission=locked, actor=reviewer, action=SubmissionEvent.Action.REJECTED,
                                       comment=locked.review_comment, at=locked.reviewed_at)
    return locked


def request_revision(sub: Submission, reviewer, reason: str) -> Submission:
    """원본 '수정 요청' — 제출자에게 되돌린다(draft + revision_requested). 제출자가 고쳐 다시 제출한다."""
    _check_reviewer(sub, reviewer)
    reason = _reason(reason, "수정 사유를 입력해주세요.", "Please enter the reason for revision.")
    with transaction.atomic():
        before = Submission.objects.select_for_update().filter(pk=sub.pk).first()
        locked = _transition(sub.pk, reviewer, Submission.Status.DRAFT, reason,
                             {"revision_requested": True, "step": 1, "submitted_at": None, **DELETION_RESET})
        SubmissionEvent.objects.create(submission=locked, actor=reviewer, action=SubmissionEvent.Action.REVISION,
                                       comment=locked.review_comment, at=locked.reviewed_at)
        if before is not None and _has_deletion_request(before):
            # 임시저장으로 돌아가면 삭제 요청 대상이 아니다 — 남겨 두면 재제출 때 예전 요청이 대기열에 다시 나타난다
            SubmissionEvent.objects.create(submission=locked, actor=reviewer, action=SubmissionEvent.Action.DELETION_CLEARED,
                                           comment="수정 요청으로 임시저장 전환 — 삭제 요청 정리 / "
                                                   "Returned to draft for revision — deletion request cleared",
                                           at=locked.reviewed_at)
    return locked


# 삭제 요청 필드 초기값 — 상태가 임시저장(DRAFT)으로 돌아가는 전이와 재제출에서 정리한다
DELETION_RESET = {"deletion_requested_at": None, "deletion_rejected_at": None, "deletion_reject_reason": "",
                  "deletion_reviewer": None}


def _has_deletion_request(sub: Submission) -> bool:
    return bool(sub.deletion_requested_at or sub.deletion_rejected_at or sub.deletion_reject_reason
                or sub.deletion_reviewer_id)


def _clear_deletion_request(sub: Submission, actor, at, ko: str, en: str) -> None:
    """(잠근) 업로드의 삭제 요청 필드를 정리하고 이력을 남긴다. 호출자가 sub.save() 한다."""
    if not _has_deletion_request(sub):
        return
    for k, v in DELETION_RESET.items():
        setattr(sub, k, v)
    SubmissionEvent.objects.create(submission=sub, actor=actor, action=SubmissionEvent.Action.DELETION_CLEARED,
                                   comment=f"{ko} / {en}", at=at)


# ─── 내 업로드 ───
def delete_draft(sub: Submission, user) -> None:
    if sub.submitter_id != user.pk or sub.status != Submission.Status.DRAFT:
        raise WorkflowError("임시저장 상태인 본인 업로드만 삭제할 수 있습니다", "Only your own drafts can be deleted")
    with transaction.atomic():
        s = _lock_draft(sub)
        for f in list(s.files.all()):
            files.delete_stored(f)
        s.delete()


def request_deletion(sub: Submission, user) -> bool:
    """원본 MyUploadsTab '삭제 요청' — 제출된 업로드는 바로 지우지 않고 관리자에게 요청한다.
    관리자 처리(승인·반려)는 services.deletion. 새로 접수하면 True, 이미 처리 대기 중이면 False(화면에 안내)."""
    if sub.submitter_id != user.pk or sub.status == Submission.Status.DRAFT:
        raise WorkflowError("삭제 요청할 수 없는 업로드입니다", "This upload cannot be requested for deletion")
    now = timezone.now()
    with transaction.atomic():
        locked = Submission.objects.select_for_update().filter(pk=sub.pk).exclude(status=Submission.Status.DRAFT).first()
        if locked is None:
            raise WorkflowError("삭제 요청할 수 없는 업로드입니다", "This upload cannot be requested for deletion")
        if locked.deletion_pending:
            return False
        # 반려된 요청이 있었다면 새 요청으로 다시 접수한다(반려 기록은 이력 SubmissionEvent 에 남아 있다)
        Submission.objects.filter(pk=sub.pk).update(deletion_requested_at=now, deletion_rejected_at=None,
                                                    deletion_reject_reason="", deletion_reviewer=None)
        SubmissionEvent.objects.create(submission=sub, actor=user, action=SubmissionEvent.Action.DELETION_REQUESTED, at=now)
    return True


def clone_for_resubmit(sub: Submission, user) -> Submission:
    """반려된 업로드를 고쳐 다시 내기 위한 새 임시저장(행·폼 값 복사, 파일은 다시 올린다)."""
    if sub.submitter_id != user.pk or sub.status != Submission.Status.REJECTED:
        raise WorkflowError("반려된 본인 업로드만 다시 제출할 수 있습니다", "Only your own rejected uploads can be resubmitted")
    new = create_draft(user, sub.domain, sub.mode, sub.condition_id, form_data=sub.form_data,
                       resubmitted_from=sub, step=1, source_filename=sub.source_filename, csv_headers=sub.csv_headers)
    rows = [r.payload for r in sub.records.order_by("row_no")]
    with transaction.atomic():
        s = _lock_draft(new)
        store_rows(s, rows, form=sub.method != Submission.Method.CSV)
    _sync(new, s)
    return new
