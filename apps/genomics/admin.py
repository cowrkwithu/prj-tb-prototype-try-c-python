from django.contrib import admin

from .models import PredictionRun, ResistanceGene, Variant


@admin.register(ResistanceGene)
class ResistanceGeneAdmin(admin.ModelAdmin):
    list_display = ("gene", "full_name", "start", "end", "strand", "known_variants", "order")
    ordering = ("order", "id")


@admin.register(Variant)
class VariantAdmin(admin.ModelAdmin):
    list_display = ("label", "gene", "position", "ref_allele", "alt_allele", "drug_resistance", "confidence", "order")
    list_filter = ("gene", "drug_resistance", "confidence")
    search_fields = ("label", "amino_acid_change", "annotation")


@admin.register(PredictionRun)
class PredictionRunAdmin(admin.ModelAdmin):
    list_display = ("id", "sample_name", "source", "input_format", "user", "created_at")
    list_filter = ("source", "input_format")
    readonly_fields = ("inputs", "results", "created_at")
