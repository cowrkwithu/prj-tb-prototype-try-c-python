from django.contrib import admin

from .models import ManualBlock, ManualSection, ManualTopic


class TopicInline(admin.TabularInline):
    model = ManualTopic
    fields = ("order", "key", "title", "title_en")
    extra = 0


class BlockInline(admin.StackedInline):
    model = ManualBlock
    fields = ("order", "kind", "content")
    extra = 0


@admin.register(ManualSection)
class ManualSectionAdmin(admin.ModelAdmin):
    list_display = ("no", "title", "title_en", "key", "order")
    inlines = [TopicInline]


@admin.register(ManualTopic)
class ManualTopicAdmin(admin.ModelAdmin):
    list_display = ("title", "section", "key", "order")
    list_filter = ("section",)
    search_fields = ("title", "title_en", "search_text", "search_text_en")
    readonly_fields = ("search_text", "search_text_en")
    inlines = [BlockInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        from .services import refresh_search_text

        refresh_search_text(form.instance)  # 블록을 고치면 검색 본문도 갱신
