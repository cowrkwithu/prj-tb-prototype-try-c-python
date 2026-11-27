from django.db import models


class SeedLog(models.Model):
    """데이터셋별 최초 시드 적재 기록.

    한 번 적재된 데이터셋은 bootstrap 이 다시 넣지 않는다 — 사용자가 지운 레코드가 되살아나지 않게.
    다시 넣으려면 각 명령의 --force 를 쓴다.
    """

    dataset = models.CharField("데이터셋", max_length=50, unique=True)
    loaded_at = models.DateTimeField("최초 적재", auto_now_add=True)
    updated_at = models.DateTimeField("최근 적재", auto_now=True)
    detail = models.JSONField("결과", default=dict, blank=True)

    class Meta:
        verbose_name = "시드 적재 기록"
        verbose_name_plural = "시드 적재 기록"

    def __str__(self):
        return self.dataset


# ═══════════════════════════════════════════════════════════
#  홈 편집 콘텐츠 (원본 pages/Home/index.tsx 상수 → DB, /django-admin 에서 편집)
# ═══════════════════════════════════════════════════════════
class PublishedQuerySet(models.QuerySet):
    def published(self):
        return self.filter(is_published=True)


class HomeContent(models.Model):
    order = models.PositiveSmallIntegerField("표시 순서", default=0)
    is_published = models.BooleanField("게시", default=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    objects = PublishedQuerySet.as_manager()

    class Meta:
        abstract = True


class NewsItem(HomeContent):
    """뉴스·주요성과 카드."""

    class Tag(models.TextChoices):
        NEWS = "뉴스", "뉴스"
        ACHIEVEMENT = "성과", "성과"

    COLORS = [("bg-teal", "teal"), ("bg-navy", "navy"), ("bg-info", "info"), ("bg-success", "success")]

    tag = models.CharField("분류", max_length=10, choices=Tag.choices)
    date = models.DateField("게시일")
    color = models.CharField("색상", max_length=20, choices=COLORS, default="bg-teal")
    title = models.CharField("제목", max_length=200)
    title_en = models.CharField("제목(영문)", max_length=200, blank=True)
    desc = models.TextField("요약", blank=True)
    desc_en = models.TextField("요약(영문)", blank=True)

    class Meta:
        verbose_name = "홈 뉴스·주요성과"
        verbose_name_plural = "홈 뉴스·주요성과"
        ordering = ("-date", "order", "id")

    def __str__(self):
        return self.title


class BoardPost(HomeContent):
    """게시판(공지·공고 / 채용 / 보도자료). '전체' 탭은 분류 구분 없이 최신순."""

    class Category(models.TextChoices):
        NOTICE = "notice", "공지·공고"
        CAREERS = "careers", "채용"
        PRESS = "press", "보도자료"

    class Tag(models.TextChoices):
        NOTICE = "공지", "공지"
        ANNOUNCEMENT = "공고", "공고"
        CAREERS = "채용", "채용"
        PRESS = "보도", "보도"

    category = models.CharField("게시판", max_length=10, choices=Category.choices)
    tag = models.CharField("말머리", max_length=10, choices=Tag.choices)
    date = models.DateField("게시일")
    title = models.CharField("제목", max_length=200)
    title_en = models.CharField("제목(영문)", max_length=200, blank=True)

    class Meta:
        verbose_name = "홈 게시판 글"
        verbose_name_plural = "홈 게시판 글"
        ordering = ("-date", "order", "id")
        indexes = [models.Index(fields=("is_published", "category", "-date"))]

    def __str__(self):
        return self.title


class Notice(HomeContent):
    """공지사항 카드."""

    date = models.DateField("게시일")
    title = models.CharField("제목", max_length=200)
    title_en = models.CharField("제목(영문)", max_length=200, blank=True)

    class Meta:
        verbose_name = "홈 공지사항"
        verbose_name_plural = "홈 공지사항"
        ordering = ("-date", "order", "id")

    def __str__(self):
        return self.title


class ResearchProgram(HomeContent):
    """주요 연구사업 카드."""

    icon = models.CharField("아이콘(lucide 이름)", max_length=40)
    color = models.CharField("색상 클래스", max_length=20, default="tone-teal")
    title = models.CharField("제목", max_length=100)
    title_en = models.CharField("제목(영문)", max_length=100, blank=True)
    desc = models.CharField("설명", max_length=200, blank=True)
    desc_en = models.CharField("설명(영문)", max_length=200, blank=True)

    class Meta:
        verbose_name = "홈 주요 연구사업"
        verbose_name_plural = "홈 주요 연구사업"
        ordering = ("order", "id")

    def __str__(self):
        return self.title


class RelatedSite(HomeContent):
    """관련 사이트 타일."""

    icon = models.CharField("아이콘(lucide 이름)", max_length=40)
    name = models.CharField("이름", max_length=100)
    name_en = models.CharField("이름(영문)", max_length=100, blank=True)
    desc = models.CharField("설명", max_length=200, blank=True)
    desc_en = models.CharField("설명(영문)", max_length=200, blank=True)
    url = models.URLField("주소", blank=True)

    class Meta:
        verbose_name = "홈 관련 사이트"
        verbose_name_plural = "홈 관련 사이트"
        ordering = ("order", "id")

    def __str__(self):
        return self.name
