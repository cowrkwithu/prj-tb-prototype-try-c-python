"""환자별 영상 목록 (원본 src/pages/Rap/mockImages.ts generateMockImages 이식).

원본과 같은 규칙으로 환자 DB 필드(cxr/ct/pet_ct 수, CXR 소견, id)에서 촬영 목록을 파생한다.
실제 영상 파일은 static/imaging/ (원본 public/imaging), 자산 목록은 data/seed/rapImaging.json.
영상 DB 모델은 4단계(RAP)에서 도입한다 — 그때 이 함수는 모델 조회로 대체된다.
"""
from __future__ import annotations

import json
from functools import lru_cache

from django.conf import settings

ANNOTATION_COLORS = {
    "결절": "#EF4444", "침윤": "#F59E0B", "공동": "#8B5CF6", "흉수": "#3B82F6", "섬유화": "#10B981",
    "석회화": "#EC4899", "무기폐": "#6366F1", "기관지확장": "#14B8A6",
    "결핵 병변": "#EF4444", "고섭취 병소": "#FACC15",
}
SEVERITIES = ["경미", "중등도", "중증"]


@lru_cache(maxsize=1)
def rap_imaging() -> dict:
    return json.loads((settings.SEED_DIR / "rapImaging.json").read_text(encoding="utf-8"))


def _study_date(base_year: int, month_offset: int, day: int) -> str:
    year = base_year + month_offset // 12
    month = month_offset % 12 + 1
    return f"{year}-{month:02d}-{day:02d}"


def _roi_side(rois) -> str:
    right = any(r["x"] + r["width"] / 2 < 50 for r in rois)
    left = any(r["x"] + r["width"] / 2 >= 50 for r in rois)
    return "양측" if right and left else ("좌폐" if left else "우폐")


def volume_src(modality: str, plane: str, index: int) -> str:
    d = "ct" if modality == "CT" else "petct"
    return f"imaging/{d}/{plane}/{index:03d}.jpg"


def generate_images(patient) -> list[dict]:
    data = rap_imaging()
    images = []
    next_id = 1
    base_year = 2024
    findings_src = list(patient.cxr_findings or [])
    cxr_assets = data["cxr"]

    for i in range(patient.cxr_count):
        asset = cxr_assets[(i + patient.id - 1) % len(cxr_assets)]
        location = _roi_side(asset["rois"])
        findings = [{
            "type": f, "location": location,
            "severity": SEVERITIES[(next_id * 7 + i) % 3],
            "ai_detected": True, "doctor_confirmed": i % 3 != 2,
        } for f in findings_src[:min(i + 1, len(findings_src))]]
        annotations = [{"type": "결핵 병변", "color": ANNOTATION_COLORS["결핵 병변"], **roi} for roi in asset["rois"]]
        images.append({"id": next_id, "type": "CXR", "date": _study_date(base_year, i * 2, 15),
                       "findings": findings, "annotations": annotations,
                       "src": f"imaging/{asset['file']}", "source_id": asset["source"]})
        next_id += 1

    vol = data["volume"]["lesion"]
    side = "우폐" if vol["side"] == "right" else "좌폐"

    def vol_ann(t):
        return {"type": t, "color": ANNOTATION_COLORS[t], **vol["axialBox"]}

    for i in range(patient.ct_count):
        images.append({"id": next_id, "type": "CT", "date": _study_date(base_year, (i + 1) * 3 - 1, 20),
                       "findings": [{"type": "침윤", "location": side, "severity": "중등도",
                                     "ai_detected": True, "doctor_confirmed": True}],
                       "annotations": [vol_ann("고섭취 병소")]})
        next_id += 1

    for i in range(patient.pet_ct_count):
        suv = vol["suvMax"]
        lesion = "공동" if "공동" in findings_src else "침윤"
        images.append({"id": next_id, "type": "PET-CT", "date": _study_date(base_year, 5 + i * 4, 25), "suv_max": suv,
                       "findings": [{"type": lesion, "location": side, "severity": "중증" if suv >= 5 else "경미",
                                     "ai_detected": True, "doctor_confirmed": True}],
                       "annotations": [vol_ann("고섭취 병소")]})
        next_id += 1

    # 촬영일순 정렬 후 ID 재부여 (원본과 동일; Python sort 는 안정 정렬 — JS Array.sort 도 안정)
    images.sort(key=lambda im: im["date"])
    peak = vol["peakSlice"]
    for idx, im in enumerate(images, start=1):
        im["id"] = idx
        if im["type"] != "CXR":
            im["src"] = volume_src(im["type"], "axial", peak)
            im["volume_dir"] = "imaging/ct/axial/" if im["type"] == "CT" else "imaging/petct/axial/"
    return images


def volume_info() -> dict:
    v = rap_imaging()["volume"]
    return {"axial_count": v["axialCount"], "peak": v["lesion"]["peakSlice"],
            "slice_range": v["lesion"]["sliceRange"], "box": v["lesion"]["axialBox"]}
