"""데이터 공유 (원본 pages/DataSharing/*).

- DataAccessRequest: 데이터 접근 신청(DAR). 원본은 제출 3초 뒤 자동 승인하는 시뮬레이션이었다 →
  여기서는 DB 에 저장하고 관리자가 승인/반려한다. 승인은 **사용자 × 데이터 유형** 단위로 판정한다
  (승인된 신청의 approved_types 에 든 유형이 그 사용자에게 열린다).
- ApiKey: 승인된 DAR 이 있는 사용자가 발급하는 REST API 키. 원문은 저장하지 않고 SHA-256 해시만 둔다
  (발급 직후 1회만 화면에 보인다). 사용자당 유효 키는 1개 — 재발급하면 이전 키는 폐기된다.
  키가 허용하는 데이터 유형은 발급 시점이 아니라 **요청 시점의 DAR 승인 상태**로 판정한다.
"""
from django.conf import settings
from django.db import models


class DataAccessRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "검토중"
        APPROVED = "approved", "승인"
        REJECTED = "rejected", "반려"

    request_id = models.CharField("신청번호", max_length=20, unique=True)  # DAR-2026-001
    applicant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="access_requests",
                                  verbose_name="신청자 계정")
    # 원본 DarTab 입력 항목 — 연구자 정보
    name = models.CharField("이름", max_length=100)
    institution = models.CharField("소속기관", max_length=200)
    email = models.EmailField("이메일", max_length=254)
    orcid = models.CharField("ORCID", max_length=19, blank=True)
    # 승인권자 정보
    approver_name = models.CharField("승인권자 이름", max_length=100)
    approver_title = models.CharField("승인권자 직책", max_length=100)
    approver_email = models.EmailField("승인권자 이메일", max_length=254)
    # 요청 데이터(clinical/genomics/imaging 중 1개 이상) · 연구 목적 · DUA 동의
    data_types = models.JSONField("요청 데이터 유형", default=list)
    purpose = models.TextField("연구 목적")
    dua_agreed_at = models.DateTimeField("DUA 동의 일시")

    status = models.CharField("상태", max_length=10, choices=Status.choices, default=Status.PENDING)
    approved_types = models.JSONField("승인된 데이터 유형", default=list, blank=True)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="reviewed_access_requests", verbose_name="검토자")
    review_comment = models.TextField("검토 의견/반려 사유", blank=True)
    reviewed_at = models.DateTimeField("검토일시", null=True, blank=True)

    created_at = models.DateTimeField("신청일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "데이터 접근 신청(DAR)"
        verbose_name_plural = "데이터 접근 신청(DAR)"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("applicant", "status")),
            models.Index(fields=("status", "created_at")),
        ]

    def __str__(self):
        return self.request_id


class ApiKey(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_keys",
                             verbose_name="사용자")
    prefix = models.CharField("키 앞부분(표시용)", max_length=16)
    key_hash = models.CharField("SHA-256 해시", max_length=64, unique=True)
    # 원본 발급 요청 폼의 이메일·소속기관
    email = models.EmailField("이메일", max_length=254)
    institution = models.CharField("소속기관", max_length=200)
    created_at = models.DateTimeField("발급일시", auto_now_add=True)
    revoked_at = models.DateTimeField("폐기일시", null=True, blank=True)
    last_used_at = models.DateTimeField("마지막 사용", null=True, blank=True)

    class Meta:
        verbose_name = "API 키"
        verbose_name_plural = "API 키"
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("user", "revoked_at"))]

    def __str__(self):
        return f"{self.prefix}… ({self.user_id})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
