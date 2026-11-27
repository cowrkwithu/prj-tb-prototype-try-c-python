"""데이터 업로드·검증 (원본 pages/Upload/*, pages/Admin/ReviewDashboard, DataSharing/MyUploadsTab).

업로드 1건 = Submission. 행 단위 데이터는 SubmissionRecord(JSON payload + 행별 검증 메시지),
파일은 SubmissionFile(var/media/uploads/<yyyy>/<mm>/<재생성 이름>). 상태 변화는 SubmissionEvent 로 기록한다.

상태: draft(임시저장·수정요청) → pending(검증 대기) → approved | rejected (되돌릴 수 없음).
원본의 '수정요청'은 상태를 따로 두지 않고 draft + revision_requested 로 표현한다(제출자가 고쳐 다시 제출).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class Submission(models.Model):
    class Domain(models.TextChoices):
        CLINICAL = "clinical", "임상"
        REGIMEN = "regimen", "레지멘"
        LAB = "lab", "검사실"
        GENOMIC = "genomic", "유전체"
        IMAGING = "imaging", "영상"

    class Method(models.TextChoices):
        FORM = "form", "웹 폼"
        CSV = "csv", "CSV"
        FILE = "file", "파일"

    class Status(models.TextChoices):
        DRAFT = "draft", "임시저장"
        PENDING = "pending", "검증대기"
        APPROVED = "approved", "승인"
        REJECTED = "rejected", "반려"

    upload_id = models.CharField("업로드 ID", max_length=32, unique=True)
    submitter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submissions",
                                  verbose_name="제출자")
    domain = models.CharField("데이터 유형", max_length=20, choices=Domain.choices)
    method = models.CharField("입력 방식", max_length=10, choices=Method.choices)
    # 화면 선택값: clinical/regimen/lab = form|csv, genomic = individual|batch, imaging = CXR|CT|PET-CT
    mode = models.CharField("업로드 방식", max_length=20)
    status = models.CharField("상태", max_length=20, choices=Status.choices, default=Status.DRAFT)
    step = models.PositiveSmallIntegerField("현재 단계", default=1)

    condition_id = models.CharField("대상자 ID", max_length=40, blank=True)
    form_data = models.JSONField("폼 입력값(이어쓰기용)", default=dict, blank=True)
    source_filename = models.CharField("원본 CSV 파일명", max_length=255, blank=True)
    csv_headers = models.JSONField("CSV 헤더", default=list, blank=True)
    file_errors = models.JSONField("파일·형식 오류", default=list, blank=True)

    total_records = models.PositiveIntegerField("전체 행", default=0)
    valid_records = models.PositiveIntegerField("유효 행", default=0)
    warning_records = models.PositiveIntegerField("경고 행", default=0)
    error_records = models.PositiveIntegerField("오류 행", default=0)
    issue_count = models.PositiveIntegerField("검증 메시지 수", default=0)
    ignore_errors = models.BooleanField("오류 무시하고 진행", default=False)

    pii_report = models.JSONField("개인정보 자동 탐지 결과", default=dict, blank=True)
    pii_blocking = models.BooleanField("개인정보 탐지(제출 차단)", default=False)
    deid_checklist = models.JSONField("비식별화 체크리스트", default=list, blank=True)
    deid_signer = models.CharField("기관 담당자 서명", max_length=100, blank=True)
    deid_date = models.DateField("확인일", null=True, blank=True)

    revision_requested = models.BooleanField("수정 요청됨", default=False)
    review_checklist = models.JSONField("검증 체크리스트", default=list, blank=True)
    review_comment = models.TextField("검증자 코멘트/반려 사유", blank=True)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="reviewed_submissions", verbose_name="검토자")
    reviewed_at = models.DateTimeField("검토일시", null=True, blank=True)
    apply_summary = models.JSONField("반영 결과", default=dict, blank=True)
    # 승인 순서(승인 트랜잭션 안에서 환자 잠금 하에 max+1) — 파생 값 접기·삭제 시 재적용(replay)·'최신 레지멘' 판정의 정렬 키.
    # 승인 시각(reviewed_at)은 시계 역전·같은 시각에 취약해 정렬에 쓰지 않는다.
    approval_seq = models.PositiveIntegerField("승인 순서", null=True, blank=True, unique=True, editable=False)
    deletion_requested_at = models.DateTimeField("삭제 요청일시", null=True, blank=True)
    # 삭제 요청 반려(관리자) — 승인되면 업로드 자체가 지워지고 DeletedSubmission 에 기록이 남는다
    deletion_rejected_at = models.DateTimeField("삭제 요청 반려일시", null=True, blank=True)
    deletion_reject_reason = models.TextField("삭제 요청 반려 사유", blank=True)
    deletion_reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name="+", verbose_name="삭제 요청 검토자")
    resubmitted_from = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="resubmissions", verbose_name="재제출 원본")

    submitted_at = models.DateTimeField("제출일시", null=True, blank=True)
    created_at = models.DateTimeField("생성일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "업로드"
        verbose_name_plural = "업로드"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("status", "domain")),
            models.Index(fields=("submitter", "status")),
            models.Index(fields=("submitted_at",)),
            models.Index(fields=("reviewed_at",)),
        ]

    def __str__(self):
        return self.upload_id

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    @property
    def display_status(self) -> str:
        """화면 상태 키: draft | revision | pending | approved | rejected."""
        if self.status == self.Status.DRAFT and self.revision_requested:
            return "revision"
        return self.status

    @property
    def deletion_pending(self) -> bool:
        """삭제 요청이 접수돼 관리자 처리를 기다리는 중(반려되면 False — 다시 요청할 수 있다)."""
        return bool(self.deletion_requested_at) and self.deletion_rejected_at is None

    @property
    def auto_result(self) -> str:
        """자동 검증 결과(원본 ReviewDashboard autoResult): fail | warning | pass."""
        if self.error_records:
            return "fail"
        if self.warning_records or (self.pii_report or {}).get("medium"):
            return "warning"
        return "pass"


class SubmissionRecord(models.Model):
    class RowStatus(models.TextChoices):
        VALID = "valid", "유효"
        WARNING = "warning", "경고"
        ERROR = "error", "오류"

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="records")
    row_no = models.PositiveIntegerField("행")
    payload = models.JSONField("데이터")
    status = models.CharField("검증", max_length=10, choices=RowStatus.choices, default=RowStatus.VALID)
    # [{field, value, severity: error|warning, ko, en}]
    messages = models.JSONField("검증 메시지", default=list, blank=True)

    class Meta:
        verbose_name = "업로드 행"
        verbose_name_plural = "업로드 행"
        ordering = ("submission", "row_no")
        constraints = [models.UniqueConstraint(fields=("submission", "row_no"), name="uniq_submission_row")]

    def __str__(self):
        return f"{self.submission_id}#{self.row_no}"


def _upload_to(instance, filename):  # 실제 이름은 services.files 가 정한다(재생성 이름)
    return filename


class SubmissionFile(models.Model):
    class Kind(models.TextChoices):
        CSV = "csv", "CSV"
        SEQUENCE = "sequence", "시퀀스"
        IMAGE = "image", "영상"

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="files")
    kind = models.CharField("종류", max_length=20, choices=Kind.choices)
    file = models.FileField("파일", upload_to=_upload_to, max_length=255)
    original_name = models.CharField("원래 파일명", max_length=255)
    size = models.BigIntegerField("크기(bytes)")
    sha256 = models.CharField("SHA-256", max_length=64)
    content_type = models.CharField("MIME", max_length=100, blank=True)
    meta = models.JSONField("메타데이터", default=dict, blank=True)  # DICOM 헤더 검사 결과 등
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "업로드 파일"
        verbose_name_plural = "업로드 파일"
        ordering = ("submission", "id")

    def __str__(self):
        return self.original_name


class SubmissionEvent(models.Model):
    class Action(models.TextChoices):
        SUBMITTED = "submitted", "제출"
        APPROVED = "approved", "승인"
        REJECTED = "rejected", "반려"
        REVISION = "revision", "수정 요청"
        DELETION_REQUESTED = "deletion_requested", "삭제 요청"
        DELETION_REJECTED = "deletion_rejected", "삭제 요청 반려"
        DELETION_CLEARED = "deletion_cleared", "삭제 요청 해제"

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    action = models.CharField("작업", max_length=30, choices=Action.choices)
    comment = models.TextField("코멘트", blank=True)
    at = models.DateTimeField("일시", default=timezone.now)

    class Meta:
        verbose_name = "업로드 이력"
        verbose_name_plural = "업로드 이력"
        ordering = ("-at", "-id")
        indexes = [models.Index(fields=("action", "at"))]

    def __str__(self):
        return f"{self.submission_id}:{self.action}"


class OrphanFile(models.Model):
    """지우지 못한 업로드 파일(예: Windows 에서 다른 프로세스가 열고 있어 삭제 실패).

    `manage.py cleanup_media` 가 다시 지우고, 성공하면 이 기록도 지운다.
    """

    path = models.CharField("MEDIA_ROOT 기준 경로", max_length=255, unique=True)
    reason = models.CharField("실패 사유", max_length=255, blank=True)
    attempts = models.PositiveIntegerField("시도 횟수", default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    last_tried_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "삭제 대기 파일"
        verbose_name_plural = "삭제 대기 파일"

    def __str__(self):
        return self.path


class DeletedSubmission(models.Model):
    """삭제 요청이 승인돼 지워진 업로드의 기록(업로드·행·파일·이력은 지워지므로 감사용으로 남긴다).

    summary: 지운 도메인 데이터 건수와, 되돌릴 수 없어 유지한 항목(병합·갱신된 값 등) — services.deletion 참고.
    """

    upload_id = models.CharField("업로드 ID", max_length=32, db_index=True)
    domain = models.CharField("데이터 유형", max_length=20)
    status_at_deletion = models.CharField("삭제 당시 상태", max_length=20)
    submitter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+", verbose_name="제출자")
    submitter_label = models.CharField("제출자 표시", max_length=150, blank=True)
    requested_at = models.DateTimeField("삭제 요청일시", null=True, blank=True)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="+", verbose_name="승인자")
    deleted_at = models.DateTimeField("삭제일시", default=timezone.now)
    summary = models.JSONField("삭제 내역", default=dict, blank=True)

    class Meta:
        verbose_name = "삭제된 업로드 기록"
        verbose_name_plural = "삭제된 업로드 기록"
        ordering = ("-deleted_at", "-id")

    def __str__(self):
        return self.upload_id


class DerivedBaseline(models.Model):
    """업로드가 환자의 파생 값을 **처음** 건드리기 직전의 값(기준 스냅샷) — services.derived 참고.

    - genomic: has_sequence·sra_id·lineage, imaging: has_cxr/has_ct/has_pet_ct·각 수·cxr_findings.
      preexisting_ids = 스냅샷 시점에 이미 있던 시퀀스/영상 행 id(값에 이미 반영됨 — 재계산에서 접지 않는다).
    - biochem: values = {생화학 행 pk 문자열: {reference_range, flag}} — 업로드가 '갱신만' 하기 전 값(행별 최초 1회).
    한 번 저장된 값은 덮어쓰지 않는다. 삭제 승인(재적용) 시 이 값으로 되돌린 뒤 남은 업로드를 승인 순서로 다시 적용한다.
    """

    class Domain(models.TextChoices):
        GENOMIC = "genomic", "유전체"
        IMAGING = "imaging", "영상"
        BIOCHEM = "biochem", "생화학"

    patient = models.ForeignKey("cases.Patient", on_delete=models.CASCADE, related_name="derived_baselines")
    domain = models.CharField("대상", max_length=20, choices=Domain.choices)
    values = models.JSONField("기준 값", default=dict, blank=True)
    preexisting_ids = models.JSONField("스냅샷 시점 기존 행", default=list, blank=True)
    # 스냅샷을 잡을 때 재계산 규칙 이전(recompute 표지 없음)에 승인된 업로드의 효과가 이미 들어 있었다 —
    # 이 기준 값으로는 '남은 업로드만 승인' 상태를 만들 수 없으므로 삭제 시 다시 적용하지 않는다(services.deletion).
    legacy_mixed = models.BooleanField("예전 승인 건 혼재", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "파생 값 기준 스냅샷"
        verbose_name_plural = "파생 값 기준 스냅샷"
        constraints = [models.UniqueConstraint(fields=("patient", "domain"), name="uniq_derived_baseline")]

    def __str__(self):
        return f"{self.patient_id}:{self.domain}"
