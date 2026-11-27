"""홈 대시보드 집계 (원본 src/pages/Home/index.tsx + data/statistics.ts 구조).

원본은 statistics.ts 의 하드코딩 수치(총 2,847건 등)를 보였지만, 여기서는 전부 DB 에서 집계한다.
따라서 화면 수치는 현재 DB 내용(시드 50명 등)을 그대로 반영한다.

기준 월(anchor): 월별 추이·전월 대비는 '가장 최근 등록일이 속한 달'을 기준으로 계산한다.
시드 데이터(2024~2025년 등록)가 오늘 기준 최근 12개월 밖에 있어도 추이가 비지 않게 하려는 것이다.
DB 가 비어 있으면 오늘이 속한 달을 기준으로 한다.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Count, F, Max, Q, Sum
from django.utils import timezone

from apps.cases.models import Patient

COUNTRY_FLAGS = {
    "한국": "🇰🇷", "몽골": "🇲🇳", "베트남": "🇻🇳", "인도": "🇮🇳",
    "남아공": "🇿🇦", "인도네시아": "🇮🇩", "필리핀": "🇵🇭", "미얀마": "🇲🇲",
}
DIAGNOSIS_ORDER = ["DS-TB", "MDR-TB", "Pre-XDR-TB", "XDR-TB"]
OUTCOME_ORDER = ["Cured", "Completed", "Failed", "Died", "LTFU"]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def anchor_month() -> date:
    latest = Patient.objects.aggregate(m=Max("registration_date"))["m"]
    return _month_start(latest or timezone.localdate())


def _image_sum(qs) -> int:
    return qs.aggregate(n=Sum(F("cxr_count") + F("ct_count") + F("pet_ct_count")))["n"] or 0


def _change(qs_until, anchor: date, metric) -> float | None:
    """전월 대비 증감률(%): 기준 월 말까지 누적 vs 전월 말까지 누적. 전월 누적이 0 이면 None."""
    end_prev = anchor  # 기준 월 1일 = 전월 말 다음날
    end_cur = _add_months(anchor, 1)
    prev = metric(qs_until.filter(registration_date__lt=end_prev))
    cur = metric(qs_until.filter(registration_date__lt=end_cur))
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def stat_cards() -> dict:
    qs = Patient.objects.all()
    anchor = anchor_month()
    total = qs.count()
    sequences = qs.filter(has_sequence=True).count()
    images = _image_sum(qs)
    sites = qs.values("site").distinct().count()
    countries = qs.values("country").distinct().count()
    return {
        "total_cases": total,
        "total_sequences": sequences,
        "total_images": images,
        "partner_sites": sites,
        "countries": countries,
        "change_cases": _change(qs, anchor, lambda q: q.count()),
        "change_sequences": _change(qs, anchor, lambda q: q.filter(has_sequence=True).count()),
        "change_images": _change(qs, anchor, _image_sum),
        "change_sites": _change(qs, anchor, lambda q: q.values("site").distinct().count()),
    }


def monthly_trends(months: int = 12) -> list[dict]:
    """기준 월까지 최근 N개월의 월별 등록 케이스·시퀀스(보유 환자)·영상 수 (registration_date 기준)."""
    anchor = anchor_month()
    first = _add_months(anchor, -(months - 1))
    rows = {m: {"cases": 0, "sequences": 0, "images": 0} for m in
            (_add_months(first, i) for i in range(months))}
    for p in Patient.objects.filter(registration_date__gte=first,
                                    registration_date__lt=_add_months(anchor, 1)).values(
            "registration_date", "has_sequence", "cxr_count", "ct_count", "pet_ct_count"):
        r = rows[_month_start(p["registration_date"])]
        r["cases"] += 1
        r["sequences"] += 1 if p["has_sequence"] else 0
        r["images"] += p["cxr_count"] + p["ct_count"] + p["pet_ct_count"]
    return [{"month": m.strftime("%Y-%m"), **v} for m, v in rows.items()]


def country_distribution() -> list[dict]:
    rows = (Patient.objects.values("country")
            .annotate(cases=Count("id"), sequences=Count("id", filter=Q(has_sequence=True)))
            .order_by("-cases", "country"))
    out = [{"country": r["country"], "en": Patient.COUNTRY_EN.get(r["country"], r["country"]),
            "flag": COUNTRY_FLAGS.get(r["country"], "🏳️"), "cases": r["cases"], "sequences": r["sequences"]}
           for r in rows]
    top = max((r["cases"] for r in out), default=0)
    for r in out:
        r["pct_of_max"] = round(r["cases"] / top * 100, 1) if top else 0
    return out


def _distribution(field: str, order: list[str]) -> list[dict]:
    counts = dict(Patient.objects.values_list(field).annotate(n=Count("id")).values_list(field, "n"))
    total = sum(counts.values())
    keys = order + sorted(k for k in counts if k not in order)
    return [{"key": k, "count": counts.get(k, 0),
             "percentage": round(counts.get(k, 0) / total * 100, 1) if total else 0} for k in keys]


def resistance_distribution() -> list[dict]:
    return _distribution("diagnosis_type", DIAGNOSIS_ORDER)


def outcome_distribution() -> list[dict]:
    return _distribution("treatment_outcome", OUTCOME_ORDER)


def _spark(dates: list[date], start: date, days: int, buckets: int = 7) -> list[int]:
    out = [0] * buckets
    for d in dates:
        idx = min(buckets - 1, (d - start).days * buckets // days)
        if 0 <= idx < buckets:
            out[idx] += 1
    return out


def contribution_range(days: int = 30) -> tuple[date, date]:
    """데이터 기여 현황 기준 기간(오늘 포함 최근 N일)."""
    today = timezone.localdate()
    return today - timedelta(days=days - 1), today


def contribution_stats(days: int = 30) -> list[dict]:
    """데이터 기여 현황(최근 N일). 케이스는 등록일 기준, 검사·유전체·영상은 승인된 업로드(검토일) 기준."""
    start, today = contribution_range(days)
    case_dates = list(Patient.objects.filter(registration_date__gte=start, registration_date__lte=today)
                      .values_list("registration_date", flat=True))
    uploads = recent_upload_counts(start, today, days)
    return [
        {"key": "cases", "label": ("신규 케이스 등록", "New Cases Registered"), "icon": "clipboard-list",
         "color": "text-navy", "count": len(case_dates), "spark": _spark(case_dates, start, days)},
        {"key": "lab", "label": ("검사 데이터 추가", "Lab Data Added"), "icon": "test-tube-diagonal",
         "color": "text-teal", **uploads["lab"]},
        {"key": "genomic", "label": ("유전체 업로드", "Genomic Uploads"), "icon": "dna",
         "color": "text-info", **uploads["genomic"]},
        {"key": "imaging", "label": ("영상 업로드", "Imaging Uploads"), "icon": "image",
         "color": "text-success", **uploads["imaging"]},
    ]


def recent_upload_counts(start: date, end: date, days: int | None = None) -> dict:
    """기간 내 **승인된** 업로드의 종류별 건수와 스파크라인(검토일 기준, 현지 날짜)."""
    from apps.uploads.models import Submission

    days = days or (end - start).days + 1
    tz = timezone.get_current_timezone()
    dates = {"lab": [], "genomic": [], "imaging": []}
    rows = (Submission.objects.filter(status=Submission.Status.APPROVED, domain__in=list(dates),
                                      reviewed_at__date__gte=start, reviewed_at__date__lte=end)
            .values_list("domain", "reviewed_at"))
    for domain, at in rows:
        dates[domain].append(timezone.localtime(at, tz).date())
    return {k: {"count": len(v), "spark": _spark(v, start, days)} for k, v in dates.items()}


ACTIVITY_STYLE = {  # 도메인 → (아이콘, 색 클래스, 동작 문구)
    "clinical": ("clipboard-list", "tone-navy", ("임상 데이터를 등록했습니다", "registered clinical data")),
    "regimen": ("pill", "tone-teal", ("치료 레지멘을 등록했습니다", "registered treatment regimens")),
    "lab": ("test-tube-diagonal", "tone-warning", ("검사 결과를 추가했습니다", "added lab results")),
    "genomic": ("dna", "tone-info", ("유전체 시퀀스를 업로드했습니다", "uploaded genome sequences")),
    "imaging": ("image", "tone-success", ("영상 데이터를 업로드했습니다", "uploaded imaging data")),
}


def recent_upload_activity(limit: int = 5) -> list[dict]:
    """최근 업로드 활동 피드 — 승인된 업로드를 검토일 최신순으로(쿼리 1회).

    반환 형식: [{"icon": str, "org": (ko, en), "action": (ko, en), "at": datetime, "color": str}, …]
    """
    from apps.uploads.models import Submission

    out = []
    for s in (Submission.objects.filter(status=Submission.Status.APPROVED).select_related("submitter")
              .order_by("-reviewed_at", "-id")[:limit]):
        icon, color, action = ACTIVITY_STYLE.get(s.domain, ACTIVITY_STYLE["clinical"])
        org = s.submitter.organization or s.submitter.shown_name
        out.append({"icon": icon, "color": color, "org": (org, data_label_en(org)), "action": action,
                    "at": s.reviewed_at, "upload_id": s.upload_id})
    return out


def data_label_en(value: str) -> str:
    from apps.cases.services import DATA_VALUE_EN

    return DATA_VALUE_EN.get(value, value)


def custom_schema_stats() -> dict:
    """활성(발행) 커스텀 데이터 구조 수와 그 구조들에 입력된 데이터 건수 (apps.schemas DB 집계)."""
    from apps.schemas.services import custom_schema_stats as stats

    return stats()


def sparkline_points(values: list[int], width: int = 64, height: int = 32) -> str:
    """스파크라인 SVG polyline 좌표 (서버 렌더링 — JS 불필요)."""
    if not values:
        return ""
    top = max(values) or 1
    n = len(values)
    step = width / (n - 1) if n > 1 else 0
    pts = [f"{i * step:.1f},{height - 2 - (v / top) * (height - 4):.1f}" for i, v in enumerate(values)]
    return " ".join(pts)
