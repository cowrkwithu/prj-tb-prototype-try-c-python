"""커스텀 데이터 구조(스키마) — 원본 src/types/schema.ts 의 CustomSchema / FieldDefinition / DynamicDataRow.

원본은 SchemaProvider(메모리)·sampleSchemas.ts 상수였다. 여기서는 모두 DB 에 영속한다.
"""
from django.conf import settings
from django.db import models

from apps.dictionary.models import DataFieldType


class Schema(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "초안"
        PUBLISHED = "published", "발행됨"
        ARCHIVED = "archived", "보관됨"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "비공개"
        SHARED = "shared", "기관내 공유"
        PUBLIC = "public", "전체 공개"

    class Category(models.TextChoices):
        CLINICAL_EXT = "clinical_ext", "임상 확장"
        GENOMIC_EXT = "genomic_ext", "유전체 확장"
        IMAGING_EXT = "imaging_ext", "영상 확장"
        SURVEY = "survey", "설문조사"
        LAB_EXT = "lab_ext", "검사실 확장"
        OTHER = "other", "기타"

    schema_id = models.CharField("데이터 구조 ID", max_length=40, unique=True)  # schema-001 / SCH-…
    name = models.CharField("이름", max_length=200)
    name_en = models.CharField("이름(영문)", max_length=200, blank=True)
    description = models.TextField("설명", blank=True)
    description_en = models.TextField("설명(영문)", blank=True)
    category = models.CharField("카테고리", max_length=20, choices=Category.choices, default=Category.CLINICAL_EXT)
    version = models.CharField("버전", max_length=20, default="1.0")
    status = models.CharField("상태", max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    visibility = models.CharField("공개 범위", max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE)
    # 작성자: 포털 사용자(FK). 시드 데이터의 가상 작성자(원본 owner.userName)는 FK 없이 이름·기관만 남긴다.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="schemas")
    owner_name = models.CharField("작성자 이름", max_length=100, blank=True)
    owner_name_en = models.CharField("작성자 이름(영문)", max_length=100, blank=True)
    owner_institution = models.CharField("작성자 기관", max_length=200, blank=True)
    # 연결 키 필드 (원본 linkField)
    link_field_name = models.CharField("연결 키 필드", max_length=40, default="condition_id")
    link_display_name = models.CharField("연결 키 표시명", max_length=100, blank=True)
    link_display_name_en = models.CharField("연결 키 표시명(영문)", max_length=100, blank=True)
    link_description = models.CharField("연결 키 설명", max_length=200, blank=True)
    link_description_en = models.CharField("연결 키 설명(영문)", max_length=200, blank=True)
    field_groups = models.JSONField("필드 그룹", default=list, blank=True)  # FieldGroup[]
    validation_rules = models.JSONField("검증 규칙", default=list, blank=True)  # ValidationRule[]
    tags = models.JSONField("태그", default=list, blank=True)
    tags_en = models.JSONField("태그(영문)", default=list, blank=True)
    created_at = models.DateTimeField("생성일")
    updated_at = models.DateTimeField("수정일")
    published_at = models.DateTimeField("발행일", null=True, blank=True)
    # 낙관적 잠금: 빌더 저장·상태 전이는 rev 가 같을 때만 반영(조건부 UPDATE)
    rev = models.PositiveIntegerField("개정 번호", default=0)
    # 입력 검증에 영향을 주는 구조(필드·타입·제약·검증 규칙·연결 키)가 바뀔 때만 오른다.
    # 데이터 제출은 검사 시점과 이 값이 같을 때만 기록한다(공개 범위·이름 같은 메타 변경은 제출을 막지 않음).
    structure_rev = models.PositiveIntegerField("구조 개정 번호", default=0)

    class Meta:
        verbose_name = "데이터 구조"
        verbose_name_plural = "데이터 구조"
        ordering = ("-created_at", "-id")

    def __str__(self):
        return self.name


class SchemaField(models.Model):
    """원본 FieldDefinition. 입력 데이터(SchemaRecord.data)는 field_name 을 키로 쓴다."""

    schema = models.ForeignKey(Schema, on_delete=models.CASCADE, related_name="fields")
    field_key = models.CharField("필드 ID", max_length=60)  # 원본 fieldId (f-ae-001 / uuid)
    field_name = models.CharField("필드명", max_length=64)
    display_name = models.CharField("표시명", max_length=200)
    display_name_en = models.CharField("표시명(영문)", max_length=200, blank=True)
    description = models.TextField("설명", blank=True)
    description_en = models.TextField("설명(영문)", blank=True)
    data_type = models.CharField("데이터 타입", max_length=20, choices=DataFieldType.choices)
    constraints = models.JSONField("제약조건", default=dict, blank=True)  # 원본 FieldConstraints(options 포함)
    default_value = models.JSONField("기본값", null=True, blank=True)
    is_required = models.BooleanField("필수", default=False)
    is_unique = models.BooleanField("고유값", default=False)
    order = models.PositiveIntegerField("순서", default=0)
    group = models.CharField("필드 그룹", max_length=60, blank=True)
    tags = models.JSONField("태그", default=list, blank=True)
    # 표준 데이터사전 참조 (원본 sourceRef). 사전 재적재는 항목을 지우지 않고 폐기 표시만 하므로 PROTECT 와 충돌하지 않는다.
    dictionary_field = models.ForeignKey("dictionary.DictionaryField", null=True, blank=True,
                                         on_delete=models.PROTECT, related_name="schema_fields")
    source_dictionary_id = models.CharField("참조 사전 ID", max_length=60, blank=True)
    source_field_name = models.CharField("참조 사전 필드명", max_length=100, blank=True)
    source_modified = models.BooleanField("원본에서 수정됨", default=False)

    class Meta:
        verbose_name = "데이터 구조 필드"
        verbose_name_plural = "데이터 구조 필드"
        ordering = ("schema", "order", "id")
        constraints = [
            models.UniqueConstraint(fields=("schema", "field_name"), name="uniq_schema_field_name"),
            models.UniqueConstraint(fields=("schema", "field_key"), name="uniq_schema_field_key"),
        ]

    def __str__(self):
        return f"{self.schema_id}:{self.field_name}"

    @property
    def has_source(self) -> bool:
        return bool(self.source_dictionary_id)


class SchemaRecord(models.Model):
    """원본 DynamicDataRow — 스키마별 입력 행. 데이터가 있는 스키마는 삭제할 수 없다(PROTECT)."""

    schema = models.ForeignKey(Schema, on_delete=models.PROTECT, related_name="records")
    record_id = models.CharField("행 ID", max_length=40, unique=True)
    # 연결 키 값 (원본 conditionId — 스키마의 연결 키 필드가 specimen_id 등이어도 이 칸에 담는다)
    condition_id = models.CharField("연결 키 값", max_length=40, db_index=True)
    data = models.JSONField("데이터", default=dict, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="schema_records")
    author_label = models.CharField("작성자", max_length=150, blank=True)  # 사용자명 또는 시드 createdBy
    created_at = models.DateTimeField("등록일")
    updated_at = models.DateTimeField("수정일")
    rev = models.PositiveIntegerField("개정 번호", default=0)

    class Meta:
        verbose_name = "데이터 구조 입력 행"
        verbose_name_plural = "데이터 구조 입력 행"
        ordering = ("schema", "created_at", "id")
        indexes = [models.Index(fields=("schema", "condition_id"))]

    def __str__(self):
        return self.record_id


class SchemaFile(models.Model):
    """FILE 타입 필드의 첨부 파일. 입력 검사 단계에서 저장되고(pending), 제출 시 행에 연결된다."""

    schema = models.ForeignKey(Schema, on_delete=models.CASCADE, related_name="files")
    record = models.ForeignKey(SchemaRecord, null=True, blank=True, on_delete=models.SET_NULL, related_name="files")
    pending = models.ForeignKey("PendingEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="files")
    field_name = models.CharField("필드명", max_length=64)
    file = models.FileField("파일", max_length=300)
    original_name = models.CharField("원래 파일명", max_length=200)
    size = models.BigIntegerField("크기", default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "데이터 구조 첨부 파일"
        verbose_name_plural = "데이터 구조 첨부 파일"


class PendingEntry(models.Model):
    """데이터 입력 4단계(입력 방식 → 입력 → 유효성 검사 → 제출) 중 검사를 마친 입력.

    제출은 consumed=False 인 행만 조건부 UPDATE 로 소비한다 → 같은 입력을 두 번 제출(이중 클릭·재전송)해도 한 번만 반영.
    """

    class Mode(models.TextChoices):
        FORM = "form", "웹 폼 입력"
        CSV = "csv", "CSV 업로드"
        EDIT = "edit", "기존 데이터 수정"

    schema = models.ForeignKey(Schema, on_delete=models.CASCADE, related_name="pending_entries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    mode = models.CharField(max_length=8, choices=Mode.choices)
    record = models.ForeignKey(SchemaRecord, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    record_rev = models.PositiveIntegerField(default=0)
    rows = models.JSONField(default=list)  # [{"condition_id":…, "data":{…}}]  (검사·형 변환을 마친 값)
    raw = models.JSONField(default=dict, blank=True)  # 폼 재입력용 원래 입력값
    errors = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    schema_rev = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    consumed = models.BooleanField(default=False)
    result = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "데이터 입력 대기"
        verbose_name_plural = "데이터 입력 대기"


class SchemaLog(models.Model):
    """버전 이력(원본 VersionHistoryTab)과 관리자 조치 이력(원본 SchemaAdminLog)."""

    class Action(models.TextChoices):
        CREATE = "create", "생성"
        SAVE = "save", "저장"
        IMPORT = "import", "가져오기"
        DUPLICATE = "duplicate", "복제"
        PUBLISH = "publish", "발행"
        ARCHIVE = "archive", "보관"
        RESTORE = "restore", "보관 해제(발행 상태로 복원)"
        TO_DRAFT = "to_draft", "초안으로 되돌림"
        VISIBILITY = "visibility", "공개 범위 변경"

    schema = models.ForeignKey(Schema, on_delete=models.CASCADE, related_name="logs")
    at = models.DateTimeField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=12, choices=Action.choices)
    version = models.CharField(max_length=20, blank=True)
    detail = models.CharField(max_length=500, blank=True)
    detail_en = models.CharField(max_length=500, blank=True)
    note = models.CharField(max_length=500, blank=True)
    by_admin = models.BooleanField(default=False)

    class Meta:
        verbose_name = "데이터 구조 이력"
        verbose_name_plural = "데이터 구조 이력"
        ordering = ("-at", "-id")
