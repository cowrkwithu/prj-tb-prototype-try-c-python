from django.contrib import admin

from .models import DSTResult, Patient


class DSTInline(admin.TabularInline):
    model = DSTResult
    extra = 0


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ("condition_id", "age", "sex", "country", "site", "diagnosis_type", "treatment_outcome")
    list_filter = ("country", "diagnosis_type", "treatment_outcome", "sex")
    search_fields = ("condition_id", "site", "sra_id")
    inlines = [DSTInline]
