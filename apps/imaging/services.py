"""영상 분석(RAP) 서비스 — 공개 샘플 자산·샘플 검사 시드, 화면용 검사 목록, 전문가 소견 권한·검증.

데이터는 ImagingStudy(DB) 다. 시드 환자의 영상 수(cxr/ct/pet_ct_count)만큼 공개 샘플 영상을 가리키는 검사를
만든다(원본 generateMockImages 와 같은 촬영일·소견 규칙 — apps.cases.imaging.generate_images 재사용).
업로드로 들어온 검사는 SubmissionFile 을 권한 검사 뷰(/rap/files/<id>)로 서빙한다 — 제출자·관리자·영상 DAR 승인자만.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.templatetags.static import static
from django.utils import timezone

from apps.cases import imaging as case_imaging
from apps.cases.models import ImagingStudy, Patient
from apps.cases.services import data_label
from apps.core.seeding import is_seeded, mark_seeded

from .models import ExpertAnnotation, ImagingAsset

DATASET = "imaging_samples"
DISPLAY_EXTS = (".jpg", ".jpeg", ".png")
EXPERT_COLOR = "#38BDF8"
AI_COLOR = {"결핵 병변": "#EF4444", "고섭취 병소": "#FACC15"}
MIN_SIZE = 1.5   # 짧은 클릭을 도형으로 보지 않는 최소 크기(%) — 원본 ExpertOverlay MIN_SIZE
TEXT_MAX = 120
NOTE_MAX = 300
MAX_ANNOTATIONS_PER_STUDY = 500

FINDING_KO = {"Infiltrate": "침윤", "Infiltration": "침윤", "Consolidation": "침윤", "Nodule": "결절", "Cavity": "공동",
              "Cavitary lesion": "공동", "Pleural effusion": "흉수", "Fibrosis": "섬유화", "Calcification": "석회화",
              "Atelectasis": "무기폐", "Bronchiectasis": "기관지확장"}
LOCATION_KO = {"Left": "좌폐", "Right": "우폐", "Bilateral": "양측"}
SEVERITY_KO = {"Mild": "경미", "Moderate": "중등도", "Severe": "중증"}
SOURCE_NOTE = {
    "cxr": ("영상: NLM Shenzhen 결핵 CXR 세트 (판독의 합의 ROI) — 기관 내부 시연용",
            "Image: NLM Shenzhen TB CXR Set (consensus ROI) — internal demonstration only"),
    "volume": ("영상: TCIA Lung-PET-CT-Dx (CC BY 4.0) — 폐암 환자 공개 영상, 결핵 영상 아님",
               "Image: TCIA Lung-PET-CT-Dx (CC BY 4.0) — public lung cancer study, not tuberculosis"),
    "upload": ("영상: 승인된 업로드 파일", "Image: approved upload file"),
}


# ═══════════════════════════════════════════════════════════
#  시드
# ═══════════════════════════════════════════════════════════
def load_assets(data: dict) -> int:
    created = 0
    for c in data["cxr"]:
        key = c["file"]
        if ImagingAsset.objects.filter(key=key).exists():
            continue
        ImagingAsset.objects.create(key=key, kind=ImagingAsset.Kind.CXR, modality="CXR", path=f"imaging/{c['file']}",
                                    source_id=c.get("source", ""), source=data.get("sources", {}).get("cxr", ""),
                                    width=c.get("width"), height=c.get("height"), rois=list(c.get("rois") or []))
        created += 1
    vol = data["volume"]
    for key, modality, d in (("volume/ct", "CT", "ct"), ("volume/petct", "PET-CT", "petct")):
        if ImagingAsset.objects.filter(key=key).exists():
            continue
        ImagingAsset.objects.create(key=key, kind=ImagingAsset.Kind.VOLUME, modality=modality, path=f"imaging/{d}",
                                    source_id="Lung_Dx-A0166", source=data.get("sources", {}).get("volume", ""),
                                    meta=dict(vol))
        created += 1
    return created


def _sample_findings(fs: list[dict]) -> list[dict]:
    return [{"finding": f["type"], "location": f["location"], "severity": f["severity"], "description": "",
             "ai_detected": bool(f.get("ai_detected")), "doctor_confirmed": bool(f.get("doctor_confirmed"))}
            for f in fs]


def load_imaging_samples(path: Path | None = None, force: bool = False) -> dict:
    """공개 샘플 영상 자산 + 시드 환자의 샘플 검사를 1회 적재한다(SeedLog, 멱등 — seed_key 로 중복 방지).

    시드 환자(data/seed/patients.json) 중 DB 에 있는 환자만, 모달리티별로 '케이스 영상 수 − 업로드된 실제 검사 수'
    만큼 만든다. 환자 영상 수(파생 값)는 바꾸지 않는다."""
    if is_seeded(DATASET) and not force:
        return {"assets": 0, "studies": 0, "skipped": True, "total": ImagingStudy.objects.count()}
    data = json.loads(Path(path or settings.SEED_DIR / "rapImaging.json").read_text(encoding="utf-8"))
    seed_ids = [p["conditionId"] for p in json.loads((settings.SEED_DIR / "patients.json").read_text(encoding="utf-8"))]
    studies = 0
    with transaction.atomic():
        assets = load_assets(data)
        by_key = {a.key: a for a in ImagingAsset.objects.all()}
        have_keys = set(ImagingStudy.objects.filter(is_public_sample=True).values_list("seed_key", flat=True))
        seed_set = set(seed_ids)
        for p in Patient.objects.filter(Q(cxr_count__gt=0) | Q(ct_count__gt=0) | Q(pet_ct_count__gt=0)).order_by("id"):
            if p.condition_id not in seed_set:
                continue
            uploaded = dict(ImagingStudy.objects.filter(patient=p, is_public_sample=False)
                            .values_list("modality").annotate(n=Count("id")).values_list("modality", "n"))
            shadow = SimpleNamespace(
                id=p.id, cxr_findings=list(p.cxr_findings or []) if isinstance(p.cxr_findings, list) else [],
                cxr_count=max(0, p.cxr_count - uploaded.get("CXR", 0)),
                ct_count=max(0, p.ct_count - uploaded.get("CT", 0)),
                pet_ct_count=max(0, p.pet_ct_count - uploaded.get("PET-CT", 0)))
            seq = {"CXR": 0, "CT": 0, "PET-CT": 0}
            for im in case_imaging.generate_images(shadow):
                seq[im["type"]] += 1
                key = f"sample:{p.condition_id}:{im['type']}:{seq[im['type']]}"
                if key in have_keys:
                    continue
                asset = by_key.get(im["src"][len("imaging/"):]) if im["type"] == "CXR" else \
                    by_key.get("volume/ct" if im["type"] == "CT" else "volume/petct")
                ImagingStudy.objects.create(
                    patient=p, modality=im["type"], study_date=date.fromisoformat(im["date"]),
                    description="공개 샘플 영상 (Public sample image)", findings=_sample_findings(im["findings"]),
                    is_public_sample=True, sample_asset=asset, suv_max=im.get("suv_max"), seed_key=key)
                have_keys.add(key)
                studies += 1
        mark_seeded(DATASET, {"assets": assets, "studies": studies})
    return {"assets": assets, "studies": studies, "skipped": False, "total": ImagingStudy.objects.count()}


# ═══════════════════════════════════════════════════════════
#  권한
# ═══════════════════════════════════════════════════════════
def can_view_files(user, study: ImagingStudy, approved_imaging: bool | None = None) -> bool:
    """영상 픽셀 열람 — 공개 샘플은 로그인 사용자 전체, 업로드 영상은 제출자·관리자·영상 DAR 승인자."""
    if study.is_public_sample:
        return True
    if not (user and user.is_authenticated):
        return False
    if user.is_portal_admin:
        return True
    sub = study.submission
    if sub is not None and sub.submitter_id == user.pk:
        return True
    if approved_imaging is None:
        from apps.datasharing.services import is_approved

        approved_imaging = is_approved(user, "imaging")
    return bool(approved_imaging)


def can_edit_annotation(user, ann: ExpertAnnotation) -> bool:
    return bool(user and user.is_authenticated and (user.is_portal_admin or ann.author_id == user.pk))


def display_files(study: ImagingStudy) -> list:
    return [f for f in study.files.all() if (f.original_name or "").lower().endswith(DISPLAY_EXTS)]


# ═══════════════════════════════════════════════════════════
#  화면용 직렬화
# ═══════════════════════════════════════════════════════════
def _ko(v, table) -> str:
    return table.get(v, v) if isinstance(v, str) else ""


def finding_view(f, lang: str) -> dict | None:
    if not isinstance(f, dict):
        return None
    ftype = _ko(f.get("finding"), FINDING_KO)
    if not ftype:
        return None
    loc, sev = _ko(f.get("location"), LOCATION_KO), _ko(f.get("severity"), SEVERITY_KO)
    return {"type": ftype, "type_l": data_label(ftype, lang), "location": loc, "location_l": data_label(loc, lang),
            "severity": sev, "severity_l": data_label(sev, lang),
            "ai": f.get("ai_detected") if isinstance(f.get("ai_detected"), bool) else None,
            "confirmed": f.get("doctor_confirmed") if isinstance(f.get("doctor_confirmed"), bool) else None,
            "description": f.get("description") if isinstance(f.get("description"), str) else ""}


def volume_meta(asset: ImagingAsset | None) -> dict | None:
    if asset is None or asset.kind != ImagingAsset.Kind.VOLUME:
        return None
    m = asset.meta or {}
    lesion = m.get("lesion") or {}
    planes = ["axial", "coronal", "mip"] if asset.modality == "PET-CT" else ["axial", "coronal"]
    return {
        "dir": static(asset.path + "/"), "planes": planes,
        "counts": {"axial": m.get("axialCount", 1), "coronal": m.get("coronalCount", 1), "mip": m.get("mipFrames", 1)},
        "initial": {"axial": lesion.get("peakSlice", 0),
                    "coronal": (lesion.get("coronalRows") or [0])[len(lesion.get("coronalRows") or [0]) // 2],
                    "mip": 0},
        "slice_range": lesion.get("sliceRange", [0, -1]), "axial_box": lesion.get("axialBox"),
        "coronal_rows": lesion.get("coronalRows", []), "coronal_box": lesion.get("coronalBox"),
        "suv_max": lesion.get("suvMax"), "z_range": m.get("zRangeMm", [0, 0]),
        "thickness": m.get("sliceThicknessMm"),
    }


def plane_count(study: ImagingStudy, plane: str) -> int | None:
    v = volume_meta(study.sample_asset) if study.is_public_sample else None
    if v is None or plane not in v["planes"]:
        return None
    n = v["counts"].get(plane)
    return n if isinstance(n, int) and n > 0 else None


def study_view(study: ImagingStudy, user, lang: str, approved_imaging: bool) -> dict:
    asset = study.sample_asset if study.is_public_sample else None
    out = {"id": study.pk, "type": study.modality, "date": study.study_date.isoformat(), "sample": study.is_public_sample,
           "findings": [x for x in (finding_view(f, lang) for f in (study.findings or [])) if x],
           "suv": study.suv_max, "rois": [], "volume": None, "image": None, "thumb": None, "files": [],
           "can_view": False, "annotatable": False, "kind": "none", "impression": study.impression or "",
           "tb_activity": study.tb_activity or ""}
    if asset is not None and asset.kind == ImagingAsset.Kind.CXR:
        out.update(kind="cxr", image=static(asset.path), thumb=static(asset.path), can_view=True, annotatable=True,
                   rois=[{"x": r.get("x"), "y": r.get("y"), "width": r.get("width"), "height": r.get("height")}
                         for r in (asset.rois or []) if isinstance(r, dict)],
                   source=_pick_note("cxr", lang), ai_label=data_label("결핵 병변", lang), ai_color=AI_COLOR["결핵 병변"])
    elif asset is not None:
        vm = volume_meta(asset)
        out.update(kind="volume", volume=vm, can_view=True, annotatable=True,
                   thumb=f"{vm['dir']}axial/{int(vm['initial']['axial']):03d}.jpg",
                   source=_pick_note("volume", lang), ai_label=data_label("고섭취 병소", lang),
                   ai_color=AI_COLOR["고섭취 병소"])
    else:
        can = can_view_files(user, study, approved_imaging)
        files = list(study.files.all())
        shown = [f for f in files if (f.original_name or "").lower().endswith(DISPLAY_EXTS)]
        # 원래 파일명은 열람 권한이 있을 때만 보인다(API 와 같은 비식별 원칙 — 그 밖에는 개수만)
        out["files"] = [{"name": f.original_name if can else f"file {i + 1}", "id": f.pk, "display": f in shown}
                        for i, f in enumerate(files)]
        out["can_view"] = can
        out["source"] = _pick_note("upload", lang)
        if can and shown:
            url = f"/rap/files/{shown[0].pk}"
            out.update(kind="upload", image=url, thumb=url, annotatable=True)
    return out


def _pick_note(kind, lang):
    ko, en = SOURCE_NOTE[kind]
    return en if lang == "en" else ko


def annotation_view(a: ExpertAnnotation, user, lang: str) -> dict:
    local = timezone.localtime(a.created_at)
    return {"id": a.pk, "study": a.study_id, "shape": a.shape, "x": a.x, "y": a.y, "width": a.width, "height": a.height,
            "plane": a.plane or None, "slice": a.slice_index, "finding_type": a.finding_type,
            "finding_l": data_label(a.finding_type, lang) if a.finding_type else "",
            "location": a.location, "location_l": data_label(a.location, lang) if a.location else "",
            "severity": a.severity, "severity_l": data_label(a.severity, lang) if a.severity else "",
            "text": a.text, "author": a.author_name, "created": f"{local:%Y-%m-%d %H:%M}",
            "can_edit": can_edit_annotation(user, a)}


def imaging_patients():
    """영상 검사가 1건 이상 있는 환자(모달리티별 수 포함)."""
    return (Patient.objects.filter(imaging_studies__isnull=False)
            .annotate(n_cxr=Count("imaging_studies", filter=Q(imaging_studies__modality="CXR")),
                      n_ct=Count("imaging_studies", filter=Q(imaging_studies__modality="CT")),
                      n_pet=Count("imaging_studies", filter=Q(imaging_studies__modality="PET-CT")))
            .order_by("id"))


def default_patient(patients: list):
    return next((p for p in patients if p.n_cxr >= 3 and p.n_ct > 0), patients[0] if patients else None)


def patient_studies(p: Patient) -> list[ImagingStudy]:
    """촬영일순(같은 날이면 id 순) — 썸네일·타임시리즈·비교가 이 순서를 따른다(원본과 같음)."""
    return list(ImagingStudy.objects.filter(patient=p).select_related("sample_asset", "submission")
                .prefetch_related("files").order_by("study_date", "id")[:500])


# ═══════════════════════════════════════════════════════════
#  데이터 조건 탭 — 영상 소견 조건으로 검사·대상자 수(DB)
# ═══════════════════════════════════════════════════════════
def condition_counts(findings: list[str], location: str, severity: str) -> dict:
    want = set(findings)
    n_studies, pats = 0, set()
    qs = ImagingStudy.objects.filter(modality="CXR").values_list("patient_id", "findings")
    for pid, fs in qs.iterator(chunk_size=2000):
        ok = False
        for f in fs or []:
            if not isinstance(f, dict):
                continue
            t = _ko(f.get("finding"), FINDING_KO)
            if want and t not in want:
                continue
            if location and _ko(f.get("location"), LOCATION_KO) != location:
                continue
            if severity and _ko(f.get("severity"), SEVERITY_KO) != severity:
                continue
            ok = True
            break
        if ok or (not want and not location and not severity):
            n_studies += 1
            pats.add(pid)
    return {"studies": n_studies, "patients": len(pats)}
