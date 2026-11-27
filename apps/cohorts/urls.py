from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r"^depot/?$", views.depot, name="depot"),
    re_path(r"^depot/cohorts/save/?$", views.cohort_save, name="cohort_save"),
    re_path(r"^depot/cohorts/(?P<pk>[0-9]{1,10})/rename/?$", views.cohort_rename, name="cohort_rename"),
    re_path(r"^depot/cohorts/(?P<pk>[0-9]{1,10})/delete/?$", views.cohort_delete, name="cohort_delete"),
    re_path(r"^depot/cohorts/(?P<pk>[0-9]{1,10})/export\.csv$", views.cohort_export, name="cohort_export"),
]
