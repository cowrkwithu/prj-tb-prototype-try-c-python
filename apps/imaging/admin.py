from django.contrib import admin

from .models import ExpertAnnotation, ImagingAsset


@admin.register(ImagingAsset)
class ImagingAssetAdmin(admin.ModelAdmin):
    list_display = ("key", "kind", "modality", "source_id", "width", "height")
    list_filter = ("kind", "modality")


@admin.register(ExpertAnnotation)
class ExpertAnnotationAdmin(admin.ModelAdmin):
    list_display = ("id", "study", "shape", "finding_type", "location", "severity", "plane", "slice_index", "author_name",
                    "created_at")
    list_filter = ("shape", "finding_type", "plane")
    search_fields = ("text", "author_name")
