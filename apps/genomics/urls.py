from django.urls import re_path

from . import views

ID = r"(?P<pk>[0-9]{1,10})"

urlpatterns = [
    re_path(r"^gap/?$", views.browser, name="gap_browser"),
    re_path(rf"^gap/variants/{ID}/?$", views.variant_detail, name="gap_variant_detail"),
    re_path(r"^gap/search/?$", views.search, name="gap_search"),
    re_path(r"^gap/search/export\.csv$", views.search_export, name="gap_search_export"),
    re_path(r"^gap/search/save/?$", views.search_save, name="gap_search_save"),
    re_path(r"^gap/resistance/?$", views.resistance, name="gap_resistance"),
    re_path(r"^gap/gwas/?$", views.gwas, name="gap_gwas"),
    re_path(r"^gap/similarity/?$", views.similarity, name="gap_similarity"),
]
