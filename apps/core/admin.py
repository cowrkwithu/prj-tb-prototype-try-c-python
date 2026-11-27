from django.contrib import admin
from django.db import models

from .models import BoardPost, NewsItem, Notice, RelatedSite, ResearchProgram, SeedLog


@admin.register(SeedLog)
class SeedLogAdmin(admin.ModelAdmin):
    list_display = ("dataset", "loaded_at", "updated_at")
    readonly_fields = ("dataset", "loaded_at", "updated_at", "detail")


@admin.register(NewsItem)
class NewsItemAdmin(admin.ModelAdmin):
    list_display = ("title", "tag", "date", "is_published", "order")
    list_filter = ("tag", "is_published")
    list_editable = ("is_published", "order")
    search_fields = ("title", "title_en")


@admin.register(BoardPost)
class BoardPostAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "tag", "date", "is_published")
    list_filter = ("category", "tag", "is_published")
    list_editable = ("is_published",)
    search_fields = ("title", "title_en")
    date_hierarchy = "date"


@admin.register(Notice)
class NoticeAdmin(admin.ModelAdmin):
    list_display = ("title", "date", "is_published", "order")
    list_editable = ("is_published", "order")
    search_fields = ("title", "title_en")


@admin.register(ResearchProgram)
class ResearchProgramAdmin(admin.ModelAdmin):
    list_display = ("title", "icon", "color", "is_published", "order")
    list_editable = ("is_published", "order")


@admin.register(RelatedSite)
class RelatedSiteAdmin(admin.ModelAdmin):
    formfield_overrides = {models.URLField: {"assume_scheme": "https"}}
    list_display = ("name", "url", "is_published", "order")
    list_editable = ("is_published", "order")
