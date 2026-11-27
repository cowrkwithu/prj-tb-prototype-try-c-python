"""유전자 분석(GAP) — 원본 src/data/genomics.ts(resistanceGenes, variants)를 DB 로.

- ResistanceGene: 약제내성 유전자 10종(H37Rv 좌표·가닥 포함 — 유전체 브라우저 트랙).
- Variant: 내성 변이 카탈로그(원본 20건). 원본의 frequency·sampleCount 는 시드 숫자를 쓰지 않고
  환자 resistance_mutations 에서 집계한다(services.variant_stats).
- PredictionRun: 약제내성 예측 실행 기록(카탈로그 규칙 엔진 결과). 본인 것만 조회(관리자는 전체).
"""
from django.conf import settings
from django.db import models


class ResistanceGene(models.Model):
    class Strand(models.TextChoices):
        PLUS = "+", "+"
        MINUS = "-", "-"

    gene = models.CharField("유전자", max_length=20, unique=True)
    full_name = models.CharField("전체 이름", max_length=120)
    associated_drugs = models.JSONField("연관 약제", default=list)
    # 원본 knownVariants — 문헌상 알려진 변이 수(참고값). DB 집계가 아니므로 화면에 '참고' 로 표시한다.
    known_variants = models.PositiveIntegerField("알려진 변이 수(참고)", default=0)
    start = models.PositiveIntegerField("시작(H37Rv)")
    end = models.PositiveIntegerField("끝(H37Rv)")
    strand = models.CharField("가닥", max_length=1, choices=Strand.choices, default=Strand.PLUS)
    order = models.PositiveSmallIntegerField("표시 순서", default=0)

    class Meta:
        verbose_name = "약제내성 유전자"
        verbose_name_plural = "약제내성 유전자"
        ordering = ("order", "id")

    def __str__(self):
        return self.gene


class Variant(models.Model):
    class Confidence(models.TextChoices):
        HIGH = "high", "높음"
        MODERATE = "moderate", "중간"
        UNCERTAIN = "uncertain", "불확실"

    gene = models.ForeignKey(ResistanceGene, on_delete=models.PROTECT, related_name="variants", verbose_name="유전자")
    position = models.PositiveIntegerField("유전체 위치(H37Rv)")
    ref_allele = models.CharField("참조 염기", max_length=50)
    alt_allele = models.CharField("대립 염기", max_length=50)
    amino_acid_change = models.CharField("아미노산 변이(Codon No.)", max_length=60, blank=True)
    drug_resistance = models.CharField("연관 약제(대표)", max_length=40)  # 원본 drugResistance — 표에 먼저 보이는 약제
    # 이 변이가 내성을 주는 약제 전체(대표 약제 포함, 원본 순서). 예측·검색·API 의 약제 판정은 이 목록을 쓴다.
    # 대표 외 추가 연관은 services.EXTRA_VARIANT_DRUGS(근거 주석) — 0002 마이그레이션이 기존 행을 보정한다.
    drugs = models.JSONField("연관 약제 목록", default=list, blank=True)
    annotation = models.CharField("주석", max_length=300, blank=True)
    # 환자 변이 표기('rpoB S450L', 'inhA c-15t')와 대조하는 정규화 라벨 — services.mutation_key
    label = models.CharField("변이 라벨", max_length=100, unique=True)
    # 카탈로그 근거 등급(시드 지정 — 원본 주석 기반). 약제내성 예측의 Mykrobe 규칙이 'high' 만 쓴다.
    confidence = models.CharField("근거 등급", max_length=10, choices=Confidence.choices, default=Confidence.MODERATE)
    order = models.PositiveSmallIntegerField("표시 순서", default=0)

    class Meta:
        verbose_name = "내성 변이"
        verbose_name_plural = "내성 변이"
        ordering = ("order", "id")
        indexes = [models.Index(fields=("position",)), models.Index(fields=("drug_resistance",))]

    def __str__(self):
        return self.label


class PredictionRun(models.Model):
    class Source(models.TextChoices):
        PATIENT = "patient", "대상자 선택"
        UPLOAD = "upload", "변이 목록 업로드"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="prediction_runs",
                             verbose_name="실행자")
    source = models.CharField("입력", max_length=10, choices=Source.choices)
    patient = models.ForeignKey("cases.Patient", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+", verbose_name="대상자")
    sample_name = models.CharField("샘플", max_length=200)
    input_format = models.CharField("입력 형식", max_length=10, blank=True)  # vcf | csv | tsv | patient
    algorithms = models.JSONField("알고리즘", default=list)
    assume_wgs = models.BooleanField("전장 유전체 가정", default=True)
    lineage = models.CharField("Lineage", max_length=40, blank=True)
    # {"observed": [...], "tested_genes": [...], "skipped": {...}}
    inputs = models.JSONField("입력 요약", default=dict)
    # {"drugs": [{"drug", "calls": {algo: {"call", "reason"}}, "consensus"}], ...}
    results = models.JSONField("결과", default=dict)
    created_at = models.DateTimeField("실행일시", auto_now_add=True)

    class Meta:
        verbose_name = "약제내성 예측 실행"
        verbose_name_plural = "약제내성 예측 실행"
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("user", "created_at"))]

    def __str__(self):
        return f"#{self.pk} {self.sample_name}"
