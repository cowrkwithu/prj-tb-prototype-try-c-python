"""영상 분석(RAP) — 원본 src/data/rapImaging.json · pages/Rap/expertAnnotations.ts 를 DB 로.

- ImagingAsset: static/imaging 아래 공개 샘플 영상(NLM Shenzhen CXR 5장, TCIA CT/PET-CT 볼륨 1건)과
  판독의 합의 ROI·병변 메타데이터. 시드 환자의 공개 샘플 ImagingStudy 가 이 자산을 가리킨다.
- ExpertAnnotation: 전문가 소견(원본 localStorage) — 검사·영상/평면/단면·도형·% 좌표·소견 유형·메모·작성자.
"""
from django.conf import settings
from django.db import models


class ImagingAsset(models.Model):
    class Kind(models.TextChoices):
        CXR = "cxr", "CXR"
        VOLUME = "volume", "CT/PET-CT 볼륨"

    key = models.CharField("자산 키", max_length=100, unique=True)  # cxr/CHNCXR_0549_1.jpg · volume/ct · volume/petct
    kind = models.CharField("종류", max_length=10, choices=Kind.choices)
    modality = models.CharField("모달리티", max_length=10)  # CXR | CT | PET-CT
    path = models.CharField("정적 경로", max_length=200)  # CXR: imaging/cxr/…jpg, 볼륨: imaging/ct (평면/번호.jpg)
    source_id = models.CharField("원천 식별자", max_length=100, blank=True)
    source = models.TextField("출처", blank=True)
    width = models.PositiveIntegerField("폭(px)", null=True, blank=True)
    height = models.PositiveIntegerField("높이(px)", null=True, blank=True)
    # CXR: 판독의 합의 ROI [{x,y,width,height}] (% 좌표)
    rois = models.JSONField("ROI", default=list, blank=True)
    # 볼륨: axialCount·coronalCount·mipFrames·크기·lesion(sliceRange, peakSlice, axialBox, coronalRows, coronalBox, suvMax, side)…
    meta = models.JSONField("메타데이터", default=dict, blank=True)

    class Meta:
        verbose_name = "공개 샘플 영상 자산"
        verbose_name_plural = "공개 샘플 영상 자산"
        ordering = ("kind", "id")

    def __str__(self):
        return self.key


class ExpertAnnotation(models.Model):
    class Shape(models.TextChoices):
        RECT = "rect", "네모"
        ELLIPSE = "ellipse", "동그라미"
        TEXT = "text", "텍스트"

    class Plane(models.TextChoices):
        NONE = "", "-"
        AXIAL = "axial", "축상"
        CORONAL = "coronal", "관상"
        MIP = "mip", "MIP"

    FINDING_OPTIONS = ["결절", "침윤", "공동", "흉수", "섬유화", "석회화", "무기폐", "기관지확장"]
    LOCATIONS = ["우폐", "좌폐", "양측"]
    SEVERITIES = ["경미", "중등도", "중증"]

    study = models.ForeignKey("cases.ImagingStudy", on_delete=models.CASCADE, related_name="expert_annotations",
                              verbose_name="영상 검사")
    shape = models.CharField("도형", max_length=10, choices=Shape.choices)
    x = models.FloatField("x(%)")
    y = models.FloatField("y(%)")
    width = models.FloatField("폭(%)", default=0)
    height = models.FloatField("높이(%)", default=0)
    plane = models.CharField("평면", max_length=10, choices=Plane.choices, blank=True, default="")
    slice_index = models.PositiveSmallIntegerField("단면", null=True, blank=True)
    finding_type = models.CharField("소견 유형", max_length=20, blank=True)
    location = models.CharField("위치", max_length=10, blank=True)
    severity = models.CharField("중증도", max_length=10, blank=True)
    text = models.CharField("텍스트/메모", max_length=300, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="expert_annotations", verbose_name="작성자")
    author_name = models.CharField("작성자 이름", max_length=150)
    created_at = models.DateTimeField("작성일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "전문가 소견"
        verbose_name_plural = "전문가 소견"
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("study", "created_at"))]

    def __str__(self):
        return f"{self.study_id}:{self.shape}:{self.finding_type or self.text[:20]}"
