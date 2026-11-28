"""사용자 매뉴얼 (원본 src/pages/Manual/manualContent.ts) — 대주제(section) → 소주제(topic) → 블록.

내용은 data/seed/manual.json(원본 변환 + 교정)을 load_manual 이 멱등 적재한다. 화면(/manual)은 DB 에서 읽는다.
문구는 한국어 컬럼과 *_en 컬럼을 병기한다. 블록 본문은 종류별 구조를 그대로 JSON 으로 둔다(모든 문구는 {"ko", "en"} 쌍).
"""
from django.db import models


class ManualSection(models.Model):
    key = models.SlugField("키", max_length=40, unique=True)  # 원본 id (start, dictionary …) — 목차 앵커 sec-<key>
    no = models.CharField("번호", max_length=4)
    order = models.PositiveSmallIntegerField("순서", default=0)
    icon = models.CharField("아이콘", max_length=40, blank=True)
    title = models.CharField("제목", max_length=200)
    title_en = models.CharField("제목(영문)", max_length=200, blank=True)
    summary = models.CharField("요약", max_length=300, blank=True)
    summary_en = models.CharField("요약(영문)", max_length=300, blank=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "매뉴얼 대주제"
        verbose_name_plural = "매뉴얼 대주제"

    def __str__(self):
        return f"{self.no} {self.title}"


class ManualTopic(models.Model):
    section = models.ForeignKey(ManualSection, on_delete=models.CASCADE, related_name="topics")
    key = models.SlugField("키", max_length=60, unique=True)
    order = models.PositiveSmallIntegerField("순서", default=0)
    title = models.CharField("제목", max_length=200)
    title_en = models.CharField("제목(영문)", max_length=200, blank=True)
    # 검색용 평탄화 본문(원본 blockText — 블록 문구를 이어 붙임). 블록을 바꾸면 services.refresh_search_text 로 갱신한다.
    search_text = models.TextField("검색 본문", blank=True)
    search_text_en = models.TextField("검색 본문(영문)", blank=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "매뉴얼 소주제"
        verbose_name_plural = "매뉴얼 소주제"

    def __str__(self):
        return self.title


class ManualBlock(models.Model):
    class Kind(models.TextChoices):
        PARAGRAPH = "paragraph", "문단"
        STEPS = "steps", "단계"
        LIST = "list", "목록"
        TABLE = "table", "표"
        NOTE = "note", "참고"
        PATH = "path", "경로"
        IMAGE = "image", "이미지"

    topic = models.ForeignKey(ManualTopic, on_delete=models.CASCADE, related_name="blocks")
    order = models.PositiveSmallIntegerField("순서", default=0)
    kind = models.CharField("종류", max_length=12, choices=Kind.choices)
    # 종류별: paragraph/note {text}, steps/list {items}, table {headers, rows}, path {label, value},
    # image {src: "manual/<파일>.png"(static 기준), caption}
    content = models.JSONField("내용", default=dict)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "매뉴얼 블록"
        verbose_name_plural = "매뉴얼 블록"
