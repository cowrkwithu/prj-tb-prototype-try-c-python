"""서버측 검증 — 원본 utils/validators.ts(validateRow/validateCsv) + 각 업로드 화면의 폼 규칙.

행 결과: {"status": valid|warning|error, "messages": [{row, field, value, severity, ko, en}]}
- 필수 항목 누락 → error, 값 규칙 위반 → 필수 컬럼이면 error, 선택 컬럼이면 warning (원본 규칙 그대로)
- 원본 폼 규칙(임상 '진단일 ≤ 등록일') 과 서버 보강 규칙(레지멘 종료일 ≥ 시작일, 파일 내 중복 등)을 더한다.
- 대상자 ID 가 DB 에 없으면(임상 제외) 경고 — 승인 시점에도 없으면 그 행은 반영되지 않는다.
"""
from __future__ import annotations

from collections import Counter

from apps.cases.models import Patient

from . import schemas


def _msg(row, field, value, severity, ko, en):
    return {"row": row, "field": field, "value": value, "severity": severity, "ko": ko, "en": en}


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


def validate_row(domain: str, row: dict, row_no: int, form: bool = False) -> list[dict]:
    t = schemas.template(domain)
    required = t["required"]
    rules = dict(schemas.RULES[domain])
    if form:
        rules.update(schemas.FORM_RULES[domain])
    out = []
    for field in required:
        if not _s(row.get(field)):
            out.append(_msg(row_no, field, "", "error", f"필수 항목 누락: {field}", f"Missing required field: {field}"))
    for field, rule in rules.items():
        val = _s(row.get(field))
        if val and not rule(val):
            sev = "error" if field in required or form else "warning"
            out.append(_msg(row_no, field, val, sev, f"유효하지 않은 값: {val}", f"Invalid value: {val}"))
    out += _length_rules(row, row_no)
    out += _domain_rules(domain, row, row_no, form)
    return out


def _length_rules(row, row_no):
    """DB 컬럼 길이·ID 형식 — 넘으면 error(조용히 자르지 않는다)."""
    out = []
    for field, limit in schemas.MAX_LENGTH.items():
        val = _s(row.get(field))
        if len(val) > limit:
            out.append(_msg(row_no, field, val[:40] + "…", "error", f"최대 {limit}자까지 입력할 수 있습니다",
                            f"At most {limit} characters allowed"))
    for field in schemas.ID_FIELDS:
        val = _s(row.get(field))
        if val and len(val) <= schemas.MAX_LENGTH[field] and not schemas.ID_RE.match(val):
            out.append(_msg(row_no, field, val, "error", "영문·숫자·'-'·'_'·'.' 만 쓸 수 있습니다",
                            "Only letters, digits, '-', '_' and '.' are allowed"))
    for key, limits in schemas.SUB_MAX_LENGTH.items():
        for i, item in enumerate(row.get(key) or [], start=1):
            if not isinstance(item, dict):
                continue
            for f, limit in limits.items():
                v = _s(item.get(f))
                if len(v) > limit:
                    out.append(_msg(row_no, f"{key}[{i}].{f}", v[:40] + "…", "error",
                                    f"최대 {limit}자까지 입력할 수 있습니다", f"At most {limit} characters allowed"))
    return out


def clean_row(domain: str, row: dict, row_no: int, form: bool = False) -> tuple[dict, list[dict]]:
    """반영 직전 필드별 재검증: 규칙을 통과한 값만 남기고, 위반한 (선택) 값은 버린다.

    반환: (반영할 행, 버린 필드 [{row, field, value}]). 필수 항목 위반 행은 이미 error 라 여기 오지 않는다.
    """
    rules = dict(schemas.RULES[domain])
    if form:
        rules.update(schemas.FORM_RULES[domain])
    clean, dropped = dict(row), []
    for field, rule in rules.items():
        val = _s(row.get(field))
        if val and not rule(val):
            clean[field] = ""
            dropped.append({"row": row_no, "field": field, "value": val[:60]})
    for field, limit in schemas.MAX_LENGTH.items():
        if len(_s(clean.get(field))) > limit:
            dropped.append({"row": row_no, "field": field, "value": _s(clean.get(field))[:60]})
            clean[field] = ""
    if domain == "lab":
        bc = []
        for b in row.get("biochemistry") or []:
            if _s(b.get("test")) in schemas.BIOCHEM_TESTS and _is_number(_s(b.get("value"))) and \
                    (not _s(b.get("date")) or schemas.is_valid_date(_s(b.get("date")))):
                bc.append(b)
            else:
                dropped.append({"row": row_no, "field": "biochemistry", "value": f"{_s(b.get('test'))} {_s(b.get('value'))}"[:60]})
        if "biochemistry" in row:
            clean["biochemistry"] = bc
    if domain == "imaging":
        fs = []
        for a in row.get("findings") or []:
            if _s(a.get("finding")) in schemas.FINDINGS:
                fs.append({**a, "location": _s(a.get("location")) if _s(a.get("location")) in schemas.LOCATIONS else "",
                           "severity": _s(a.get("severity")) if _s(a.get("severity")) in schemas.SEVERITIES else ""})
            else:
                dropped.append({"row": row_no, "field": "findings", "value": _s(a.get("finding"))[:60]})
        if "findings" in row:
            clean["findings"] = fs
    if domain == "regimen":
        s_, e = _s(clean.get("start_day")), _s(clean.get("end_day"))
        if s_ and e and schemas.is_int_value(s_) and schemas.is_int_value(e) and float(e) < float(s_):
            dropped.append({"row": row_no, "field": "end_day", "value": e})
            clean["end_day"] = ""
    return clean, dropped


def _domain_rules(domain, row, row_no, form):
    out = []
    if domain == "clinical":
        reg, diag = _s(row.get("registration_date")), _s(row.get("diagnosis_date"))
        if schemas.is_valid_date(reg) and schemas.is_valid_date(diag) and diag > reg:
            out.append(_msg(row_no, "diagnosis_date", diag, "error", "진단일은 등록일 이전이어야 합니다",
                            "Diagnosis date must be on or before the registration date"))
        outcome_date = _s(row.get("outcome_date"))
        if schemas.is_valid_date(outcome_date) and schemas.is_valid_date(diag) and outcome_date < diag:
            out.append(_msg(row_no, "outcome_date", outcome_date, "warning", "결과 확정일이 진단일보다 앞섭니다",
                            "Outcome date is earlier than the diagnosis date"))
    elif domain == "regimen":
        s, e = _s(row.get("start_day")), _s(row.get("end_day"))
        if schemas.is_int_value(s) and schemas.is_int_value(e) and s and e and float(e) < float(s):
            out.append(_msg(row_no, "end_day", e, "error", "종료일은 시작일 이후여야 합니다", "End day must be on or after the start day"))
        name, code = _s(row.get("drug_name")), _s(row.get("drug_code"))
        if name in schemas.DRUG_CODE_BY_NAME and code and schemas.DRUG_CODE_BY_NAME[name] != code:
            out.append(_msg(row_no, "drug_code", code, "warning", f"약물명({name})과 코드가 일치하지 않습니다",
                            f"Drug code does not match the drug name ({name})"))
    elif domain == "lab":
        for b_i, b in enumerate(row.get("biochemistry") or [], start=1):
            label = f"biochemistry[{b_i}]"
            if _s(b.get("test")) not in schemas.BIOCHEM_TESTS:
                out.append(_msg(row_no, label, _s(b.get("test")), "error", "검사 항목을 선택하세요", "Select a test item"))
            if not _is_number(_s(b.get("value"))):
                out.append(_msg(row_no, label, _s(b.get("value")), "error", "결과값은 숫자여야 합니다", "Value must be a number"))
            d = _s(b.get("date"))
            if d and not schemas.is_valid_date(d):
                out.append(_msg(row_no, label, d, "error", f"유효하지 않은 값: {d}", f"Invalid value: {d}"))
    elif domain == "imaging":
        for a_i, a in enumerate(row.get("findings") or [], start=1):
            label = f"findings[{a_i}]"
            if _s(a.get("finding")) not in schemas.FINDINGS:
                out.append(_msg(row_no, label, _s(a.get("finding")), "error", "소견 유형을 선택하세요", "Select a finding type"))
            for key, options in (("location", schemas.LOCATIONS), ("severity", schemas.SEVERITIES)):
                v = _s(a.get(key))
                if v and v not in options:
                    out.append(_msg(row_no, label, v, "error", f"유효하지 않은 값: {v}", f"Invalid value: {v}"))
    return out


def _is_number(v: str) -> bool:
    try:
        float(v)
    except ValueError:
        return False
    return v.lower() not in ("nan", "inf", "-inf", "infinity") and "_" not in v


def validate_rows(domain: str, rows: list[dict], form: bool = False) -> list[dict]:
    """행 목록을 검증한다. 반환: [{"payload", "status", "messages"}] (row_no 는 1부터)."""
    results = [{"payload": r, "messages": validate_row(domain, r, i, form)} for i, r in enumerate(rows, start=1)]
    _cross_row_rules(domain, rows, results)
    for r in results:
        sev = {m["severity"] for m in r["messages"]}
        r["status"] = "error" if "error" in sev else ("warning" if "warning" in sev else "valid")
    return results


def _cross_row_rules(domain, rows, results):
    ids = [_s(r.get("condition_id")) for r in rows]
    existing = set(Patient.objects.filter(condition_id__in=[i for i in ids if i]).values_list("condition_id", flat=True))
    if domain == "clinical":
        dup = {k for k, n in Counter(i for i in ids if i).items() if n > 1}
        for n, (res, cid) in enumerate(zip(results, ids), start=1):
            if cid in dup:
                res["messages"].append(_msg(n, "condition_id", cid, "error",
                                            f"파일 안에 중복된 condition_id 입니다 ({cid})", f"Duplicate condition_id in the file ({cid})"))
            elif cid in existing:
                res["messages"].append(_msg(n, "condition_id", cid, "warning",
                                            "이미 등록된 대상자입니다 — 승인 시 기존 케이스를 갱신합니다",
                                            "Existing case — it will be updated on approval"))
        return
    for n, (res, cid) in enumerate(zip(results, ids), start=1):
        if cid and cid not in existing:
            res["messages"].append(_msg(n, "condition_id", cid, "warning",
                                        "등록되지 않은 대상자 ID 입니다 — 승인 시점에도 없으면 반영되지 않습니다",
                                        "Unregistered condition ID — not applied unless the case exists at approval"))
    if domain == "lab":
        spec = Counter((_s(r.get("condition_id")), _s(r.get("specimen_id"))) for r in rows)
        for n, (res, r) in enumerate(zip(results, rows), start=1):
            key = (_s(r.get("condition_id")), _s(r.get("specimen_id")))
            if key[1] and spec[key] > 1:
                res["messages"].append(_msg(n, "specimen_id", key[1], "warning",
                                            f"중복 specimen_id 발견 ({key[1]})", f"Duplicate specimen_id found ({key[1]})"))


def summarize(results: list[dict]) -> dict:
    c = Counter(r["status"] for r in results)
    return {"total": len(results), "valid": c.get("valid", 0), "warning": c.get("warning", 0), "error": c.get("error", 0),
            "issues": sum(len(r["messages"]) for r in results)}


def missing_columns(domain: str, headers: list[str]) -> list[str]:
    return [c for c in schemas.template(domain)["required"] if c not in headers]


def classify_dst(results: dict) -> tuple[str, str]:
    """원본 LabUpload classifyDst — 약제명(대문자 시작) → S/R/I. 화면 표시용 내성 프로파일."""
    r = lambda d: results.get(d) == "R"  # noqa: E731
    inh, rif = r("Isoniazid"), r("Rifampicin")
    fq = any(r(d) for d in ("Levofloxacin", "Moxifloxacin", "Ofloxacin"))
    inj = any(r(d) for d in ("Amikacin", "Kanamycin", "Capreomycin"))
    if inh and rif and fq and inj:
        return ("XDR-TB", "XDR-TB")
    if inh and rif and (fq or inj):
        return ("Pre-XDR-TB", "Pre-XDR-TB")
    if inh and rif:
        return ("MDR-TB", "MDR-TB")
    if inh or rif:
        return ("단약제 내성", "Mono-resistance")
    return ("감수성 (DS-TB)", "Susceptible (DS-TB)")
