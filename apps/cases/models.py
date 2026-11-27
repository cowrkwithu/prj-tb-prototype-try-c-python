"""환자 케이스 (원본 src/types/index.ts 의 Patient 를 정규화).

중첩 객체(demographics/clinical/genomics/imaging/lab)는 접두어 없는 평면 컬럼으로 펼치고,
다중값(comorbidities, resistanceMutations, cxrFindings)은 JSONField(list), DST 결과는 하위 테이블.
"""
from django.db import models


class Patient(models.Model):
    class Sex(models.TextChoices):
        M = "M", "남"
        F = "F", "여"

    class DiagnosisType(models.TextChoices):
        DS = "DS-TB", "DS-TB"
        MDR = "MDR-TB", "MDR-TB"
        PRE_XDR = "Pre-XDR-TB", "Pre-XDR-TB"
        XDR = "XDR-TB", "XDR-TB"

    class Outcome(models.TextChoices):
        CURED = "Cured", "Cured"
        COMPLETED = "Completed", "Completed"
        FAILED = "Failed", "Failed"
        DIED = "Died", "Died"
        LTFU = "LTFU", "LTFU"
        ONGOING = "Ongoing", "Ongoing"  # 업로드 화면 선택지(치료 중)

    class SiteOfDisease(models.TextChoices):
        PULMONARY = "Pulmonary", "폐결핵"
        EXTRAPULMONARY = "Extrapulmonary", "폐외결핵"
        BOTH = "Both", "모두"

    # 시드 8개국 + 업로드 화면 선택지(방글라데시, 기타)
    COUNTRIES = ["한국", "몽골", "베트남", "인도", "남아공", "인도네시아", "필리핀", "미얀마", "방글라데시", "기타"]
    COUNTRY_EN = {"한국": "Korea", "몽골": "Mongolia", "베트남": "Vietnam", "인도": "India",
                  "남아공": "South Africa", "인도네시아": "Indonesia", "필리핀": "Philippines", "미얀마": "Myanmar",
                  "방글라데시": "Bangladesh", "기타": "Other"}
    CXR_FINDINGS = ["결절", "침윤", "공동", "흉수", "섬유화", "석회화", "무기폐", "기관지확장"]

    condition_id = models.CharField("케이스 ID", max_length=40, unique=True)
    # demographics
    age = models.PositiveSmallIntegerField("나이")
    sex = models.CharField("성별", max_length=1, choices=Sex.choices)
    country = models.CharField("국가", max_length=30, choices=[(c, c) for c in COUNTRIES])
    site = models.CharField("기관", max_length=100)
    # clinical
    diagnosis_type = models.CharField("진단 유형", max_length=20, choices=DiagnosisType.choices)
    treatment_outcome = models.CharField("치료 결과", max_length=20, choices=Outcome.choices)
    treatment_duration = models.PositiveIntegerField("치료 기간(일)")
    comorbidities = models.JSONField("동반질환", default=list, blank=True)
    # genomics
    has_sequence = models.BooleanField("유전체 보유", default=False)
    sra_id = models.CharField("SRA ID", max_length=40, blank=True, null=True)
    lineage = models.CharField("계통", max_length=40, blank=True, null=True)
    resistance_mutations = models.JSONField("내성 변이", default=list, blank=True)
    # imaging
    has_cxr = models.BooleanField("CXR 보유", default=False)
    has_ct = models.BooleanField("CT 보유", default=False)
    has_pet_ct = models.BooleanField("PET-CT 보유", default=False)
    cxr_count = models.PositiveIntegerField("CXR 수", default=0)
    ct_count = models.PositiveIntegerField("CT 수", default=0)
    pet_ct_count = models.PositiveIntegerField("PET-CT 수", default=0)
    cxr_findings = models.JSONField("CXR 소견", default=list, blank=True)
    # lab
    microscopy = models.CharField("도말", max_length=30, blank=True, null=True)
    culture = models.CharField("배양", max_length=30, blank=True, null=True)

    registration_date = models.DateField("등록일")
    last_update_date = models.DateField("최종 수정일")
    # 임상 데이터 업로드(2단계)로 채워지는 진단 상세 — 시드 환자는 비어 있다
    diagnosis_date = models.DateField("진단일", null=True, blank=True)
    site_of_disease = models.CharField("질병 부위", max_length=20, choices=SiteOfDisease.choices, blank=True)
    previous_treatment = models.BooleanField("이전 치료 여부", null=True, blank=True)
    previous_treatment_count = models.PositiveSmallIntegerField("이전 치료 횟수", null=True, blank=True)
    outcome_date = models.DateField("결과 확정일", null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "환자"
        verbose_name_plural = "환자"
        ordering = ("id",)
        indexes = [
            models.Index(fields=("country",)),
            models.Index(fields=("diagnosis_type",)),
            models.Index(fields=("treatment_outcome",)),
        ]

    def __str__(self):
        return self.condition_id


class DSTResult(models.Model):
    class Result(models.TextChoices):
        S = "S", "Susceptible"
        R = "R", "Resistant"
        I = "I", "Intermediate"  # noqa: E741

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="dst_results")
    drug = models.CharField("약제", max_length=30)
    result = models.CharField("결과", max_length=1, choices=Result.choices)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "약제감수성(DST) 결과"
        verbose_name_plural = "약제감수성(DST) 결과"
        ordering = ("patient", "order")
        constraints = [models.UniqueConstraint(fields=("patient", "drug"), name="uniq_dst_patient_drug")]

    def __str__(self):
        return f"{self.patient_id}:{self.drug}={self.result}"


class BiochemResult(models.Model):
    """혈액·생화학 검사 결과 (원본 CaseDetail 생화학 탭).

    원본은 모든 환자에게 같은 하드코딩 값을 보였다. 여기서는 환자별 DB 레코드만 보이며,
    시드는 없다 — 2단계 검사실 데이터 업로드가 이 테이블을 채운다.
    """

    class Flag(models.TextChoices):
        NORMAL = "", "Normal"
        HIGH = "H", "High"
        LOW = "L", "Low"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="biochem_results")
    test = models.CharField("검사항목", max_length=60)
    value = models.CharField("결과", max_length=60)
    reference_range = models.CharField("참고범위", max_length=60, blank=True)
    flag = models.CharField("판정", max_length=1, choices=Flag.choices, blank=True, default="")
    measured_on = models.DateField("검사일", null=True, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    # 이 행을 만든 업로드(검사실 업로드 승인) — 없으면 업로드 밖에서 생긴 행. 삭제 시 재적용(replay)의 소유 판정에 쓴다.
    submission = models.ForeignKey("uploads.Submission", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="출처 업로드")

    class Meta:
        verbose_name = "생화학 검사 결과"
        verbose_name_plural = "생화학 검사 결과"
        ordering = ("patient", "order", "id")

    def __str__(self):
        return f"{self.patient_id}:{self.test}={self.value}"


# ═══════════════════════════════════════════════════════════
#  업로드 승인으로 채워지는 도메인 테이블 (2단계)
# ═══════════════════════════════════════════════════════════
class Regimen(models.Model):
    """치료 레지멘 1건(업로드 승인 1회 = 환자별 1건). 케이스 상세 레지멘 차트는 가장 최근 것을 쓴다."""

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="regimens")
    submission = models.ForeignKey("uploads.Submission", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="출처 업로드")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "치료 레지멘"
        verbose_name_plural = "치료 레지멘"
        ordering = ("patient", "-created_at", "-id")

    def __str__(self):
        return f"{self.patient_id} regimen #{self.pk}"


class RegimenDrug(models.Model):
    class Route(models.TextChoices):
        ORAL = "Oral", "Oral"
        INJECTABLE = "Injectable", "Injectable"
        IV = "IV", "IV"

    regimen = models.ForeignKey(Regimen, on_delete=models.CASCADE, related_name="drugs")
    drug_name = models.CharField("약물", max_length=60)
    drug_code = models.CharField("약물 코드", max_length=10)
    start_day = models.PositiveIntegerField("시작일")
    end_day = models.PositiveIntegerField("종료일")
    dosage = models.CharField("용량", max_length=40, blank=True)
    route = models.CharField("경로", max_length=20, choices=Route.choices, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "레지멘 약물"
        verbose_name_plural = "레지멘 약물"
        ordering = ("regimen", "order", "id")

    def __str__(self):
        return f"{self.drug_code} {self.start_day}-{self.end_day}"


class SequenceRun(models.Model):
    """유전체 시퀀스 1건(검체 단위 메타데이터 + 시퀀스 파일)."""

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="sequence_runs")
    submission = models.ForeignKey("uploads.Submission", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="출처 업로드")
    specimen_id = models.CharField("검체 ID", max_length=60)
    platform = models.CharField("플랫폼", max_length=40)
    method = models.CharField("방법", max_length=20)
    coverage = models.FloatField("커버리지", null=True, blank=True)
    sra_accession = models.CharField("SRA Accession", max_length=40, blank=True)
    lineage = models.CharField("Lineage", max_length=40, blank=True)
    files = models.ManyToManyField("uploads.SubmissionFile", blank=True, related_name="sequence_runs")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "유전체 시퀀스"
        verbose_name_plural = "유전체 시퀀스"
        ordering = ("patient", "-created_at", "-id")

    def __str__(self):
        return f"{self.patient_id}:{self.specimen_id}"


class ImagingStudy(models.Model):
    """영상 검사 1건(모달리티·촬영일·파일·판독 소견). 4단계 영상 분석(RAP)이 이 모델을 쓴다."""

    class Modality(models.TextChoices):
        CXR = "CXR", "CXR"
        CT = "CT", "CT"
        PET_CT = "PET-CT", "PET-CT"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="imaging_studies")
    submission = models.ForeignKey("uploads.Submission", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="출처 업로드")
    modality = models.CharField("모달리티", max_length=10, choices=Modality.choices)
    study_date = models.DateField("촬영일")
    description = models.CharField("촬영 설명", max_length=200, blank=True)
    tb_activity = models.CharField("TB 활성도", max_length=20, blank=True)
    impression = models.TextField("전체 인상", blank=True)
    # [{finding, location, severity, description}]
    findings = models.JSONField("판독 소견", default=list, blank=True)
    files = models.ManyToManyField("uploads.SubmissionFile", blank=True, related_name="imaging_studies")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "영상 검사"
        verbose_name_plural = "영상 검사"
        ordering = ("patient", "-study_date", "-id")

    def __str__(self):
        return f"{self.patient_id}:{self.modality}@{self.study_date}"
