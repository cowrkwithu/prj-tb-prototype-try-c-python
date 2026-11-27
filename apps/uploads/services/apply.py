"""승인된 업로드를 도메인 테이블에 반영한다 (workflow.approve 의 트랜잭션 안에서 호출).

원칙
- 오류 행은 반영하지 않는다. 경고 행은 반영하되 **필드별로 다시 검증해 규칙을 통과한 값만** 쓰고,
  위반한 선택 값은 버린 뒤 요약의 dropped(버린 필드)에 남긴다(검토 상세에 표시).
- 대상자(condition_id)가 없는 행은 건너뛰고 요약에 남긴다(임상 제외 — 임상은 새로 만든다).
- 임상 갱신은 **CSV 헤더(폼은 입력 항목)에 있는 컬럼만**, 그리고 **값이 있는 칸만** 반영한다.
  빈 칸은 '기존 값 유지'다(빈 칸으로 기존 값을 지우지 않는다).
- 같은 데이터를 다시 승인해도 중복 생성되지 않는다: 생화학은 (환자, 항목, 검사일, 결과) upsert, DST 는 약제별 갱신,
  시퀀스·영상은 같은 파일(SHA-256)이 이미 그 환자에 연결돼 있으면 만들지 않고 count 도 올리지 않는다,
  레지멘은 최신 레지멘과 약물 구성이 같으면 만들지 않는다. → 요약 duplicates(중복 건너뜀).
- 원본 LabUpload 의 내성 분류(MDR/Pre-XDR/XDR)는 화면 표시용이었고 진단 유형을 바꾸지 않았다 → 여기서도
  diagnosis_type 은 바꾸지 않고 분류 결과만 요약에 남긴다.
- updated 는 갱신된 **환자 수**다.
- 임상 삭제 승인(services.deletion)이 이 업로드가 **새로 만든** 케이스만 지울 수 있게 created_ids 를 요약에 남긴다.
- 삭제 승인과의 대칭(services.deletion — '환자×도메인 재적용(replay)')
  · 삭제 승인은 영향받은 (환자, 도메인)마다 업로드 소유 행을 모두 지우고 파생 값을 기준 스냅샷으로 되돌린 뒤, 남은 승인
    업로드를 승인 순서(approval_seq)대로 **이 함수로 다시 적용**한다(apply_submission(sub, only={환자}, replay=True)).
    최초 승인과 재적용이 같은 코드(같은 중복 판정·파일 매칭·fold)를 타므로 '일부 삭제' == '남은 업로드만 승인'이 된다.
  · 모든 도메인 행(레지멘·시퀀스·영상·생화학)은 submission FK 로 소유 업로드를 남긴다.
  · 유전체·영상 파생 값은 행을 만든 뒤 **재계산**한다: 기준 스냅샷(DerivedBaseline) + 남은 행을 승인 순으로 접는다.
  · 업로드 밖에서 생긴 생화학 행을 처음 '갱신'할 때는 직전 값을 기준 스냅샷(biochem)에 남긴다.
  · shared_*_ids · biochem_values · created_biochem_ids 는 승인 당시 결과(이력 화면용)로만 남는다 — 삭제 경로는 읽지 않는다.
    재적용은 apply_summary 를 덮어쓰지 않는다(요약은 버린다).
  · 레지멘의 '최신'은 승인 순서(derived.regimen_sequence)의 마지막이다.
  · 재적용은 임상 갱신, 도말·배양, DST 병합을 다시 하지 않는다(되돌릴 기록이 없음 — 삭제 화면에 안내).
  · last_update_date 는 '마지막 반영일' 기록이라 되돌리지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from django.utils import timezone

from apps.cases.models import (BiochemResult, DSTResult, ImagingStudy, Patient, Regimen, RegimenDrug,
                               SequenceRun)

from ..models import Submission, SubmissionFile
from . import derived
from .derived import FINDING_KO  # noqa: F401 — 예전 import 경로 호환
from .validators import classify_dst, clean_row

COUNTRY_KO = {en: ko for ko, en in Patient.COUNTRY_EN.items()}
COMORBIDITY_KO = {"Diabetes": "당뇨", "Hepatitis B": "B형 간염", "Hepatitis C": "C형 간염", "Renal Disease": "신장질환",
                  "Cancer": "암", "HIV": "HIV", "COPD": "COPD", "Hypertension": "고혈압"}
TRUTHY = {"true", "yes", "y", "1", "예", "있음", "o"}
FALSY = {"false", "no", "n", "0", "아니오", "없음", "x"}

DST_CODE = {"isoniazid": "INH", "rifampicin": "RIF", "ethambutol": "EMB", "pyrazinamide": "PZA",
            "streptomycin": "SM", "levofloxacin": "FQ", "moxifloxacin": "FQ", "ofloxacin": "FQ",
            "amikacin": "AMK", "kanamycin": "KAN", "capreomycin": "CAP", "bedaquiline": "BDQ",
            "linezolid": "LZD", "clofazimine": "CFZ"}
DST_ORDER = ["INH", "RIF", "EMB", "PZA", "SM", "FQ", "AMK", "KAN", "CAP", "ETH", "CS", "LZD", "BDQ", "DLM", "CFZ"]
RESULT_RANK = {"S": 0, "I": 1, "R": 2}
BIOCHEM_REF = {"ALT": ("7-56", 7, 56), "AST": ("10-40", 10, 40), "Bilirubin": ("0.1-1.2", 0.1, 1.2),
               "Creatinine": ("0.7-1.3", 0.7, 1.3), "Albumin": ("3.5-5.5", 3.5, 5.5), "Hemoglobin": ("12-16", 12, 16),
               "WBC": ("4,500-11,000", 4500, 11000)}


def _d(v) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def _int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):  # inf·1e400
        return default


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def clean_rows(sub: Submission) -> tuple[list[tuple[int, dict]], list[dict]]:
    """(행번호, 정리된 행) 목록과 버린 필드 목록. 오류 행은 제외."""
    form = sub.method != Submission.Method.CSV
    rows, dropped = [], []
    for rec in sub.records.order_by("row_no"):
        if rec.status == "error":
            continue
        clean, d = clean_row(sub.domain, rec.payload, rec.row_no, form)
        rows.append((rec.row_no, clean))
        dropped += d
    return rows, dropped


def present_columns(sub: Submission, rows) -> set[str]:
    """임상 갱신에 쓸 수 있는 컬럼: CSV 는 헤더, 폼은 입력 항목."""
    if sub.method == Submission.Method.CSV:
        return set(sub.csv_headers or [])
    cols = set()
    for _, r in rows:
        cols |= set(r)
    return cols


class Scope:
    """적용 범위. only = 적용할 환자 pk(None 이면 전부). replay = 삭제 승인의 재적용
    (승인 당시 '등록되지 않은 대상자'로 건너뛴 행은 그대로 건너뛰고, 도말·배양·DST 는 다시 쓰지 않는다)."""

    def __init__(self, sub: Submission, only=None, replay: bool = False):
        self.only = set(only) if only is not None else None
        self.replay = replay
        self.skipped_rows = skipped_rows(sub) if replay else set()

    def target(self, patients: dict, cid, row_no: int):
        """(환자 또는 None, 이 행을 조용히 건너뛸지). 환자 None = 등록되지 않은 대상자(요약에 남김)."""
        p = None if row_no in self.skipped_rows else patients.get(cid)
        if p is not None and self.only is not None and p.pk not in self.only:
            return None, True
        return p, False


def skipped_rows(sub: Submission) -> set[int]:
    """승인 당시 '등록되지 않은 대상자'로 건너뛴 행 번호(원래 apply_summary 의 skipped)."""
    summary = sub.apply_summary if isinstance(sub.apply_summary, dict) else {}
    items = summary.get("skipped")
    return {x["row"] for x in items if isinstance(x, dict) and isinstance(x.get("row"), int)} \
        if isinstance(items, list) else set()


def touched_condition_ids(sub: Submission, rows=None) -> set[str]:
    """이 업로드가 반영 대상으로 삼은 대상자 ID(오류 행·승인 당시 미등록으로 건너뛴 행 제외)."""
    if rows is None:
        rows, _ = clean_rows(sub)
    skip = skipped_rows(sub) if sub.status == Submission.Status.APPROVED else set()
    return {r.get("condition_id") for i, r in rows if i not in skip and isinstance(r.get("condition_id"), str)
            and r.get("condition_id")}


def lock_patients(pids) -> list[int]:
    """직렬화 지점 — 환자 행을 id 오름차순 **한 문장**으로 잠근다(승인·삭제 승인 공통, 교착 순환 방지)."""
    ids = sorted({int(i) for i in pids})
    if not ids:
        return []
    return list(Patient.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list("pk", flat=True))


def patient_ids_for(sub: Submission) -> set[int]:
    """이 업로드의 행이 가리키는 (등록된) 환자 id — 잠금 대상. 승인 상태와 무관하게 행에서 구한다."""
    return set(Patient.objects.filter(condition_id__in=touched_condition_ids(sub)).values_list("pk", flat=True))


def assign_approval_seq(sub: Submission) -> int:
    """승인 순서 번호(max+1). 환자 잠금 안에서 부른다. 환자가 겹치지 않는 승인이 동시에 같은 번호를 잡으면(PostgreSQL)
    unique 위반 → savepoint 로 되돌리고 다시 읽는다(SQLite 는 IMMEDIATE 트랜잭션이라 쓰기가 이미 직렬화돼 있다)."""
    from django.db import IntegrityError, transaction
    from django.db.models import Max

    last_exc = None
    for _ in range(10):
        n = (Submission.objects.aggregate(m=Max("approval_seq"))["m"] or 0) + 1
        try:
            with transaction.atomic():
                Submission.objects.filter(pk=sub.pk).update(approval_seq=n)
            sub.approval_seq = n
            return n
        except IntegrityError as exc:
            last_exc = exc
    raise last_exc


def apply_submission(sub: Submission, only=None, replay: bool = False) -> dict:
    """업로드 1건을 도메인 테이블에 반영한다. only/replay 는 삭제 승인의 재적용(services.deletion)용 — Scope 참고.

    호출자는 대상 환자 행을 먼저 잠근다(workflow.approve · deletion.approve_deletion — 환자 id 오름차순 한 문장)."""
    if replay and sub.domain == "clinical":
        raise ValueError("clinical uploads are not replayed")  # 임상 덮어쓰기는 되돌릴 기록이 없다
    rows, dropped = clean_rows(sub)
    handler = {"clinical": _clinical, "regimen": _regimen, "lab": _lab, "genomic": _genomic, "imaging": _imaging}[sub.domain]
    scope = Scope(sub, only, replay)
    summary = {"created": 0, "updated": 0, "skipped": [], "duplicates": [], "dropped": dropped, "rows": len(rows)}
    # recompute 표지 — 없으면 '재계산 규칙 이전에 승인된 건'(예전 건)으로 보고 삭제 시 다시 적용하지 않는다(services.deletion)
    summary["recompute"] = 1
    if sub.domain == "genomic":
        summary["shared_sequence_ids"] = []
    elif sub.domain == "imaging":
        summary["shared_imaging_ids"] = []
    elif sub.domain == "regimen":
        summary["shared_regimen_ids"] = []
    elif sub.domain == "lab":
        summary.update(biochem_values={}, shared_biochem_ids=[])
    handler(sub, rows, summary, scope)
    return summary


def _patients(rows) -> dict[str, Patient]:
    """행의 대상자 — 잠금은 호출자가 이미 잡았다(환자 id 오름차순)."""
    ids = {r.get("condition_id") for _, r in rows if isinstance(r.get("condition_id"), str) and r.get("condition_id")}
    return {p.condition_id: p for p in Patient.objects.filter(condition_id__in=ids)}


def _skip(summary, row_no, cid):
    summary["skipped"].append({"row": row_no, "condition_id": cid,
                               "ko": "등록되지 않은 대상자 — 반영하지 않음", "en": "Unregistered case — not applied"})


def _dup(summary, row_no, cid, ko, en):
    summary["duplicates"].append({"row": row_no, "condition_id": cid, "ko": ko, "en": en})


# ─── clinical ───
def _comorbidities(raw: str) -> list[str] | None:
    """None = 값 없음(유지). 'None/없음' 만 있으면 [] (동반질환 없음으로 명시)."""
    parts = [p.strip() for p in (raw or "").replace(";", ",").split(",") if p.strip()]
    if not parts:
        return None
    out = []
    for c in parts:
        if c.lower() == "none" or c == "없음":
            continue
        c = COMORBIDITY_KO.get(c, c)
        if c not in out:
            out.append(c)
    return out


def _bool(v):
    s = (v or "").strip().lower()
    if s in TRUTHY:
        return True
    if s in FALSY:
        return False
    return None


def _clinical_fields(r: dict, cols: set[str]) -> dict:
    """헤더에 있고 값이 있는(규칙 통과) 컬럼만 Patient 필드로 바꾼다."""
    out = {}

    def has(c):
        return c in cols and _s(r.get(c))

    if has("age"):
        out["age"] = _int(r["age"], 0)
    if has("sex"):
        out["sex"] = "F" if r["sex"] == "Female" else "M"
    if has("country"):
        out["country"] = COUNTRY_KO.get(r["country"], r["country"])
    if has("site"):
        out["site"] = r["site"]
    if has("diagnosis_type"):
        out["diagnosis_type"] = r["diagnosis_type"]
    if has("treatment_outcome"):
        out["treatment_outcome"] = r["treatment_outcome"]
    if has("comorbidities"):
        c = _comorbidities(r["comorbidities"])
        if c is not None:
            out["comorbidities"] = c
    for c in ("registration_date", "diagnosis_date", "outcome_date"):
        if has(c) and _d(r[c]):
            out[c] = _d(r[c])
    if has("site_of_disease") and r["site_of_disease"] in Patient.SiteOfDisease.values:
        out["site_of_disease"] = r["site_of_disease"]
    if has("previous_treatment") and _bool(r["previous_treatment"]) is not None:
        out["previous_treatment"] = _bool(r["previous_treatment"])
    if has("previous_treatment_count") and _int(r["previous_treatment_count"]) is not None:
        out["previous_treatment_count"] = _int(r["previous_treatment_count"])
    return out


def _duration(diag, reg, out_date, outcome, today):
    start = diag or reg
    if out_date and start:
        return max(0, (out_date - start).days)
    if outcome == "Ongoing" and start:
        return max(0, (today - start).days)
    return None


def _clinical(sub, rows, summary, scope):
    existing = _patients(rows)
    cols = present_columns(sub, rows)
    today = timezone.localdate()
    for _, r in rows:
        cid = r["condition_id"]
        fields = _clinical_fields(r, cols)
        p = existing.get(cid)
        if p is None:
            dur = _duration(fields.get("diagnosis_date"), fields.get("registration_date"), fields.get("outcome_date"),
                            fields.get("treatment_outcome"), today)
            fields.setdefault("age", 0)
            Patient.objects.create(condition_id=cid, treatment_duration=dur or 0, last_update_date=today, **fields)
            summary["created"] += 1
            # 삭제 요청 승인(services.deletion) 때 이 업로드가 만든 케이스만 골라 지울 수 있게 남긴다
            summary.setdefault("created_ids", []).append(cid)
        else:
            for k, v in fields.items():
                setattr(p, k, v)
            dur = _duration(p.diagnosis_date, p.registration_date, p.outcome_date, p.treatment_outcome, today)
            if dur is not None and any(k in fields for k in ("diagnosis_date", "registration_date", "outcome_date", "treatment_outcome")):
                p.treatment_duration = dur
            p.last_update_date = today
            p.save()
            summary["updated"] += 1


# ─── regimen ───
def _regimen(sub, rows, summary, scope):
    patients = _patients(rows)
    groups = defaultdict(list)
    for i, r in rows:
        groups[r.get("condition_id", "")].append((i, r))
    today = timezone.localdate()
    for cid, items in groups.items():
        p, ignore = scope.target(patients, cid, items[0][0])
        if ignore:
            continue
        if p is None:
            for i, _ in items:
                _skip(summary, i, cid)
            continue
        drugs = [(r.get("drug_name", ""), r.get("drug_code", ""), _int(r.get("start_day"), 0), _int(r.get("end_day"), 0),
                  r.get("dosage", ""), r.get("route", "")) for _, r in items]
        seq = derived.regimen_sequence(p)  # 승인 순서(approval_seq)의 마지막이 최신
        latest = seq[-1] if seq else None
        if latest and derived.regimen_drugs(latest) == drugs:
            _dup(summary, items[0][0], cid, "최신 레지멘과 약물 구성이 같아 새로 만들지 않음",
                 "Same drugs as the latest regimen — not created again")
            _share(summary, "shared_regimen_ids", [latest.pk])
            continue
        reg = Regimen.objects.create(patient=p, submission=sub)
        RegimenDrug.objects.bulk_create([
            RegimenDrug(regimen=reg, drug_name=n, drug_code=c, start_day=s_, end_day=e, dosage=dose, route=route, order=k)
            for k, (n, c, s_, e, dose, route) in enumerate(drugs)])
        p.last_update_date = today
        p.save(update_fields=["last_update_date", "updated_at"])
        summary["created"] += 1


# ─── lab ───
def _lab(sub, rows, summary, scope):
    patients = _patients(rows)
    today = timezone.localdate()
    profiles = []
    touched = set()
    for i, r in sorted(rows, key=lambda t: t[1].get("specimen_date", "")):
        cid = r.get("condition_id", "")
        p, ignore = scope.target(patients, cid, i)
        if ignore:
            continue
        if p is None:
            _skip(summary, i, cid)
            continue
        if not scope.replay:  # 도말·배양·DST 는 최신값 병합이라 재적용 대상이 아니다(되돌리지 않음)
            _lab_merge(p, r, cid, profiles)
        _biochem(sub, p, r, i, cid, summary)
        p.last_update_date = today
        touched.add(p.pk)
    for p in patients.values():
        if p.pk in touched:
            p.save()
    summary["updated"] = len(touched)
    summary["dst_profiles"] = profiles


def _lab_merge(p, r, cid, profiles) -> None:
    """도말·배양(값이 있으면 덮어씀)과 DST(약제별 갱신) — 최초 승인 때만."""
    if r.get("microscopy_result"):
        p.microscopy = r["microscopy_result"]
    if r.get("culture_result"):
        p.culture = r["culture_result"]
    merged: dict[str, str] = {}
    for key, code in DST_CODE.items():
        v = r.get(key, "")
        if v in RESULT_RANK and (code not in merged or RESULT_RANK[v] > RESULT_RANK[merged[code]]):
            merged[code] = v
    for code, result in merged.items():
        DSTResult.objects.update_or_create(patient=p, drug=code, defaults={
            "result": result, "order": DST_ORDER.index(code) if code in DST_ORDER else 99})
    if merged:
        named = {k.capitalize(): r.get(k) for k in DST_CODE if r.get(k)}
        ko, en = classify_dst(named)
        profiles.append({"condition_id": cid, "specimen_id": r.get("specimen_id", ""), "ko": ko, "en": en})


def _biochem(sub, p, r, i, cid, summary) -> None:
    """생화학 (환자, 항목, 검사일, 결과) upsert — 새 행은 이 업로드 소유(submission FK)."""
    for b in r.get("biochemistry") or []:
        ref = BIOCHEM_REF.get(b.get("test", ""))
        val = _float(b.get("value"))
        flag = ""
        if ref and val is not None:
            flag = "H" if val > ref[2] else ("L" if val < ref[1] else "")
        value = f"{b.get('value', '')} {b.get('unit', '')}".strip()
        measured = _d(b.get("date")) or _d(r.get("specimen_date"))
        lookup = {"patient": p, "test": b.get("test", ""), "measured_on": measured, "value": value}
        new_vals = {"reference_range": ref[0] if ref else "", "flag": flag}
        before = (BiochemResult.objects.filter(**lookup).order_by("id")
                  .values("pk", "reference_range", "flag", "submission_id").first())
        obj, created = BiochemResult.objects.update_or_create(**lookup, defaults=new_vals,
                                                              create_defaults={**new_vals, "submission": sub})
        summary["biochem_values"][str(obj.pk)] = dict(new_vals)  # 이력용(승인 당시 쓴 값)
        if created:
            obj.order = BiochemResult.objects.filter(patient=p).count()
            obj.save(update_fields=["order"])
            summary.setdefault("created_biochem_ids", []).append(obj.pk)
            continue
        if obj.submission_id != sub.pk:
            _share(summary, "shared_biochem_ids", [obj.pk])  # 이력용
        if before and before["pk"] == obj.pk and before["submission_id"] is None:
            # 업로드 밖에서 생긴 행을 처음 갱신 — 직전 값을 기준 스냅샷에 남긴다(삭제 재적용의 출발점)
            derived.capture_biochem_baseline(p, obj.pk, before, sub)
        _dup(summary, i, cid, f"생화학 {obj.test} {obj.value} ({measured}) 이미 있음 — 갱신만",
             f"Biochemistry {obj.test} {obj.value} ({measured}) already exists — updated only")


# ─── 파일 중복(같은 SHA-256 이 이미 그 환자에 연결됨) ───
def _linked_hashes(patient: Patient, kind: str) -> set[str]:
    if kind == "sequence":
        qs = SubmissionFile.objects.filter(sequence_runs__patient=patient)
    else:
        qs = SubmissionFile.objects.filter(imaging_studies__patient=patient)
    return set(qs.values_list("sha256", flat=True))


def _share(summary, key, ids) -> None:
    """중복이라 건너뛴 대상 행 id(이미 있던 행을 이 업로드도 함께 쓴다) — 승인 당시 결과(이력용)."""
    lst = summary.setdefault(key, [])
    for i in ids:
        if i not in lst:
            lst.append(i)


# ─── genomic ───
def apply_date(sub: Submission) -> date:
    """반영일(영상 촬영일 기본값) — 승인 시각의 현지 날짜. 삭제 재적용 때도 같은 값을 쓴다."""
    return timezone.localdate(sub.reviewed_at) if sub.reviewed_at else timezone.localdate()


def files_for_row(sub: Submission, seq_files: list, n_rows: int, r: dict) -> list:
    """이 행에 묶을 시퀀스 파일 — 개별(또는 1행)은 전부, 일괄은 파일명에 specimen_id(없으면 condition_id)가 든 파일."""
    if sub.mode == "individual" or n_rows == 1:
        return list(seq_files)
    cid = r.get("condition_id", "")
    spec = (r.get("specimen_id") or "").lower()
    return [f for f in seq_files if spec and spec in f.original_name.lower()] or \
           [f for f in seq_files if cid.lower() in f.original_name.lower()]


def sequence_values(r: dict) -> dict:
    """행 → SequenceRun 메타데이터."""
    return {"specimen_id": r.get("specimen_id", ""), "platform": r.get("platform", ""), "method": r.get("method", ""),
            "coverage": _float(r.get("coverage")), "sra_accession": r.get("sra_accession", ""),
            "lineage": r.get("lineage", "")}


def imaging_values(sub: Submission, r: dict) -> dict:
    """행 → ImagingStudy 메타데이터."""
    return {"modality": r.get("modality") or sub.mode, "study_date": _d(r.get("study_date")) or apply_date(sub),
            "description": r.get("study_description", ""), "tb_activity": r.get("tb_activity", ""),
            "impression": r.get("impression", ""), "findings": r.get("findings") or []}


def _recompute(patients: dict, touched: set, domain: str, today) -> None:
    for p in patients.values():
        if p.pk in touched:
            derived.recompute_patient_derived(p, domain)  # 기준 스냅샷은 행을 만들기 전에 남겼다
            p.last_update_date = today
            p.save()


def _genomic(sub, rows, summary, scope):
    patients = _patients(rows)
    seq_files = list(sub.files.filter(kind=SubmissionFile.Kind.SEQUENCE))
    today = timezone.localdate()
    unmatched = {f.pk for f in seq_files}
    touched = set()
    for i, r in rows:
        cid = r.get("condition_id", "")
        p, ignore = scope.target(patients, cid, i)
        if ignore:
            continue
        if p is None:
            _skip(summary, i, cid)
            continue
        mine = files_for_row(sub, seq_files, len(rows), r)  # 파일 매칭은 범위와 무관하게 전체 행 수 기준
        unmatched -= {f.pk for f in mine}
        if mine and {f.sha256 for f in mine} <= _linked_hashes(p, "sequence"):
            _dup(summary, i, cid, "같은 시퀀스 파일(SHA-256)이 이미 이 환자에 있어 건너뜀",
                 "Same sequence file (SHA-256) already linked to this case — skipped")
            _share(summary, "shared_sequence_ids", SequenceRun.objects.filter(
                patient=p, files__sha256__in=[f.sha256 for f in mine]).exclude(submission=sub).values_list("pk", flat=True))
            continue
        derived.capture_baseline(p, "genomic", sub)  # 처음 건드리기 직전 값(이미 있으면 그대로)
        run = SequenceRun.objects.create(patient=p, submission=sub, **sequence_values(r))
        run.files.set(mine)
        touched.add(p.pk)
        summary["created"] += 1
    _recompute(patients, touched, "genomic", today)
    summary["updated"] = len(touched)
    summary["unmatched_files"] = [f.original_name for f in seq_files if f.pk in unmatched]


# ─── imaging ───
def _imaging(sub, rows, summary, scope):
    patients = _patients(rows)
    images = list(sub.files.filter(kind=SubmissionFile.Kind.IMAGE))
    today = timezone.localdate()
    touched = set()
    for i, r in rows:
        cid = r.get("condition_id", "")
        p, ignore = scope.target(patients, cid, i)
        if ignore:
            continue
        if p is None:
            _skip(summary, i, cid)
            continue
        if images and {f.sha256 for f in images} <= _linked_hashes(p, "image"):
            _dup(summary, i, cid, "같은 영상 파일(SHA-256)이 이미 이 환자에 있어 건너뜀(영상 수 증가 없음)",
                 "Same image file (SHA-256) already linked to this case — skipped (count unchanged)")
            _share(summary, "shared_imaging_ids", ImagingStudy.objects.filter(
                patient=p, files__sha256__in=[f.sha256 for f in images]).exclude(submission=sub).values_list("pk", flat=True))
            continue
        derived.capture_baseline(p, "imaging", sub)
        study = ImagingStudy.objects.create(patient=p, submission=sub, **imaging_values(sub, r))
        study.files.set(images)
        touched.add(p.pk)
        summary["created"] += 1
    _recompute(patients, touched, "imaging", today)
    summary["updated"] = len(touched)


# ─── 검토 화면용 사전 점검(승인 전) ───
def preview_duplicates(sub: Submission) -> list[dict]:
    """'기존 데이터와 중복 없음' 체크리스트의 근거 — 승인하면 중복으로 건너뛰거나 갱신될 항목."""
    rows, _ = clean_rows(sub)
    out = []
    ids = {r.get("condition_id") for _, r in rows if r.get("condition_id")}
    pats = {p.condition_id: p for p in Patient.objects.filter(condition_id__in=ids)}

    def add(i, cid, ko, en):
        out.append({"row": i, "condition_id": cid, "ko": ko, "en": en})

    if sub.domain == "clinical":
        cols = present_columns(sub, rows)
        for i, r in rows:
            if r["condition_id"] in pats:
                n = len(_clinical_fields(r, cols))
                add(i, r["condition_id"], f"기존 케이스 갱신 — 값이 있는 {n}개 항목만 반영, 빈 칸은 기존 값 유지",
                    f"Existing case update — {n} filled field(s) applied, blank cells keep existing values")
    elif sub.domain == "lab":
        for i, r in rows:
            p = pats.get(r.get("condition_id"))
            if not p:
                continue
            for b in r.get("biochemistry") or []:
                measured = _d(b.get("date")) or _d(r.get("specimen_date"))
                value = f"{b.get('value', '')} {b.get('unit', '')}".strip()
                if BiochemResult.objects.filter(patient=p, test=b.get("test", ""), measured_on=measured, value=value).exists():
                    add(i, p.condition_id, f"생화학 {b.get('test')} {value} 이미 있음", f"Biochemistry {b.get('test')} {value} already exists")
            if any(r.get(k) for k in DST_CODE) and p.dst_results.exists():
                add(i, p.condition_id, "기존 DST 결과를 약제별로 갱신", "Existing DST results will be updated per drug")
    elif sub.domain in ("genomic", "imaging"):
        kind = SubmissionFile.Kind.SEQUENCE if sub.domain == "genomic" else SubmissionFile.Kind.IMAGE
        hashes = {f.sha256: f.original_name for f in sub.files.filter(kind=kind)}
        for i, r in rows:
            p = pats.get(r.get("condition_id"))
            if p:
                for h in set(hashes) & _linked_hashes(p, "sequence" if sub.domain == "genomic" else "image"):
                    add(i, p.condition_id, f"같은 파일이 이미 이 환자에 있음: {hashes[h]}", f"Same file already linked: {hashes[h]}")
        other = (SubmissionFile.objects.filter(sha256__in=list(hashes), submission__status=Submission.Status.APPROVED)
                 .exclude(submission=sub).select_related("submission"))
        for f in other:
            add(0, "", f"승인된 다른 업로드({f.submission.upload_id})에 같은 파일: {f.original_name}",
                f"Same file in another approved upload ({f.submission.upload_id}): {f.original_name}")
    elif sub.domain == "regimen":
        for cid in {r.get("condition_id") for _, r in rows}:
            p = pats.get(cid)
            if p and p.regimens.exists():
                add(0, cid, "기존 레지멘이 있음 — 승인하면 새 레지멘이 최신이 됨(구성이 같으면 건너뜀)",
                    "Existing regimen — the new one becomes latest (skipped if identical)")
    return out
