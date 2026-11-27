from django.contrib import admin

from .models import DictionaryCategory, DictionaryField, DictionaryVersion


@admin.register(DictionaryVersion)
class DictionaryVersionAdmin(admin.ModelAdmin):
    list_display = ("version", "source_file", "is_active", "loaded_at")


@admin.register(DictionaryCategory)
class DictionaryCategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "name_en", "tier", "version")
    list_filter = ("version", "tier")


@admin.register(DictionaryField)
class DictionaryFieldAdmin(admin.ModelAdmin):
    list_display = ("dictionary_id", "display_name", "data_type", "category")
    list_filter = ("version", "category", "data_type")
    search_fields = ("dictionary_id", "display_name", "display_name_en", "field_name")
