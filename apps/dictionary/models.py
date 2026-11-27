"""표준 데이터사전 (원본 src/types/schema.ts 의 DictionaryCategory / DictionaryEntry)."""
from django.db import models


class DataFieldType(models.TextChoices):
    TEXT = "TEXT", "텍스트"
    TEXTAREA = "TEXTAREA", "장문 텍스트"
    INTEGER = "INTEGER", "정수"
    DECIMAL = "DECIMAL", "소수"
    BOOLEAN = "BOOLEAN", "예/아니오"
    DATE = "DATE", "날짜"
    DATETIME = "DATETIME", "날짜·시간"
    SELECT = "SELECT", "단일 선택"
    MULTISELECT = "MULTISELECT", "다중 선택"
    NUMERIC_RANGE = "NUMERIC_RANGE", "수치 범위"
    FILE = "FILE", "파일"
    REFERENCE = "REFERENCE", "참조"
    CALCULATED = "CALCULATED", "계산"
    CODED = "CODED", "코드"


class DictionaryVersion(models.Model):
    version = models.CharField("버전", max_length=40, unique=True)
    source_file = models.CharField("원천 파일", max_length=300, blank=True)
    is_active = models.BooleanField("현행", default=True)
    loaded_at = models.DateTimeField("적재 시각", auto_now_add=True)

    class Meta:
        verbose_name = "데이터사전 버전"
        verbose_name_plural = "데이터사전 버전"
        ordering = ("-loaded_at",)

    def __str__(self):
        return self.version


class DictionaryCategory(models.Model):
    version = models.ForeignKey(DictionaryVersion, on_delete=models.CASCADE, related_name="categories")
    code = models.CharField("코드", max_length=20)  # 원본 id: 'pat', 'soc', …
    num = models.CharField("번호", max_length=10, blank=True)  # ① …
    name = models.CharField("이름", max_length=200)  # '① 환자 기본정보'
    name_en = models.CharField("이름(영문)", max_length=200, blank=True)
    description = models.TextField("설명", blank=True)
    description_en = models.TextField("설명(영문)", blank=True)
    icon = models.CharField("아이콘", max_length=50, blank=True)
    color = models.CharField("색상", max_length=30, blank=True)
    tier = models.CharField("계층", max_length=30, blank=True)  # Core / Extension
    sdtm = models.CharField("CDISC SDTM 대응", max_length=200, blank=True)
    unified_count = models.IntegerField("통합항목 수(엑셀 표기)", null=True, blank=True)
    source_count = models.IntegerField("원천항목 수(엑셀 표기)", null=True, blank=True)
    order = models.PositiveIntegerField("순서", default=0)

    class Meta:
        verbose_name = "데이터사전 카테고리"
        verbose_name_plural = "데이터사전 카테고리"
        ordering = ("version", "order")
        constraints = [models.UniqueConstraint(fields=("version", "code"), name="uniq_dict_category_code")]

    def __str__(self):
        return self.name

    @property
    def field_count(self) -> int:
        return self.fields.count()


class DictionaryField(models.Model):
    version = models.ForeignKey(DictionaryVersion, on_delete=models.CASCADE, related_name="fields")
    category = models.ForeignKey(DictionaryCategory, on_delete=models.CASCADE, related_name="fields")
    dictionary_id = models.CharField("UDE", max_length=60)  # TB.U.PAT.001
    field_name = models.CharField("필드명", max_length=100)  # tb_u_pat_001
    display_name = models.CharField("표시명", max_length=300)
    display_name_en = models.CharField("표시명(영문)", max_length=300, blank=True)
    description = models.TextField("설명", blank=True)
    description_en = models.TextField("설명(영문)", blank=True)
    data_type = models.CharField("데이터 타입", max_length=20, choices=DataFieldType.choices)
    # FieldConstraints 중 options 를 뺀 나머지 (maxLength, precision, codeSystem, codeSystemEn, …)
    constraints = models.JSONField("제약조건", default=dict, blank=True)
    # SelectOption[]: [{"value":…, "label":…, "labelEn":…}]
    options = models.JSONField("선택지", default=list, blank=True)
    is_standard = models.BooleanField("표준 항목", default=True)
    hl7_fhir_mapping = models.CharField("HL7 FHIR 매핑", max_length=200, blank=True)
    cdisc_mapping = models.CharField("CDISC 매핑", max_length=200, blank=True)
    order = models.PositiveIntegerField("순서", default=0)
    # 재적재(--force) 시 엑셀에서 사라진 항목은 지우지 않고 표시만 한다(다른 테이블의 FK 참조 보존).
    is_deprecated = models.BooleanField("폐기됨", default=False)

    class Meta:
        verbose_name = "데이터사전 항목"
        verbose_name_plural = "데이터사전 항목"
        ordering = ("category__order", "order")
        constraints = [
            models.UniqueConstraint(fields=("version", "dictionary_id"), name="uniq_dict_field_ude"),
            models.UniqueConstraint(fields=("version", "field_name"), name="uniq_dict_field_name"),
        ]
        indexes = [models.Index(fields=("data_type",))]

    def __str__(self):
        return f"{self.dictionary_id} {self.display_name}"

    @property
    def full_constraints(self) -> dict:
        """원본 FieldConstraints 형태(options 포함)."""
        c = dict(self.constraints or {})
        if self.options:
            c["options"] = self.options
        return c


class DictionaryChangeLog(models.Model):
    """관리자 데이터사전 관리(/admin/dictionary)의 변경 이력 — 항목·카테고리 추가/수정/폐기, 엑셀 적재, 현행 버전 전환."""

    class Action(models.TextChoices):
        CATEGORY_ADD = "category_add", "카테고리 추가"
        CATEGORY_EDIT = "category_edit", "카테고리 수정"
        CATEGORY_DELETE = "category_delete", "카테고리 삭제"
        ENTRY_ADD = "entry_add", "항목 추가"
        ENTRY_EDIT = "entry_edit", "항목 수정"
        ENTRY_DEPRECATE = "entry_deprecate", "항목 폐기"
        ENTRY_RESTORE = "entry_restore", "항목 복원"
        VERSION_LOAD = "version_load", "엑셀 적재"
        VERSION_ACTIVATE = "version_activate", "현행 버전 전환"

    at = models.DateTimeField("시각", auto_now_add=True)
    actor = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_label = models.CharField("처리자", max_length=150, blank=True)
    action = models.CharField("조치", max_length=20, choices=Action.choices)
    version = models.CharField("버전", max_length=40, blank=True)
    target = models.CharField("대상", max_length=120, blank=True)  # UDE / 카테고리 코드 / 파일명
    detail = models.JSONField("상세", default=dict, blank=True)  # 바뀐 필드 {"display_name": [전, 후]} 등

    class Meta:
        verbose_name = "데이터사전 변경 이력"
        verbose_name_plural = "데이터사전 변경 이력"
        ordering = ("-at", "-id")
