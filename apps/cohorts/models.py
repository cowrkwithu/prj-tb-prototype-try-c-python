"""저장 코호트(원본 화면 용어: '데이터셋') — 원본 src/types Cohort, contexts/CohortProvider.

- conditions: 조건 트리 JSON. 그룹 {"op": "AND"|"OR", "children": [...]} 과
  조건 {"field": "demographics.country", "operator": "in", "value": [...]} 의 중첩
  (필드·연산자는 apps.cohorts.conditions 의 허용 목록만 저장된다).
- snapshot_ids: 저장 시점의 대상자 Patient.id 목록(원본 patientIds). 화면의 '불러오기'는 조건으로 다시 계산하므로
  이후 업로드 승인으로 생긴 환자도 반영되고, 스냅샷은 저장 당시 인원 표시·비교용으로 남긴다.
- 원본에는 소유자·공개 구분이 없고 모든 사용자가 같은 목록을 본다 → 목록은 로그인 사용자 전체 공개,
  수정(이름 변경)·삭제·CSV 내보내기는 소유자만.
"""
from django.conf import settings
from django.db import models


class Cohort(models.Model):
    name = models.CharField("이름", max_length=200)
    name_en = models.CharField("이름(영문)", max_length=200, blank=True)
    description = models.TextField("설명", blank=True)
    description_en = models.TextField("설명(영문)", blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="cohorts", verbose_name="소유자")
    conditions = models.JSONField("조건 트리", default=dict)
    snapshot_ids = models.JSONField("저장 시점 대상자 ID", default=list, blank=True)
    # 저장 시점 대상자의 국적 요약(원본 카드의 국가 태그, 처음 나온 순서). None 이면 아직 계산 전 → 목록에서 지연 계산.
    snapshot_countries = models.JSONField("저장 시점 국적 요약", null=True, blank=True)
    created_date = models.DateField("생성일")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "저장 데이터셋(코호트)"
        verbose_name_plural = "저장 데이터셋(코호트)"
        # 원본 addCohort: 새 코호트를 목록 맨 위에 추가
        ordering = ("-created_date", "-id")

    def __str__(self):
        return self.name

    @property
    def snapshot_count(self) -> int:
        return len(self.snapshot_ids or [])
