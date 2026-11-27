from django.contrib import admin

from .models import Cohort


@admin.register(Cohort)
class CohortAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "owner", "created_date", "snapshot_count")
    search_fields = ("name", "name_en")
    raw_id_fields = ("owner",)
