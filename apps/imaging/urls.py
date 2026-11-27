from django.urls import re_path

from . import views

ID = r"(?P<pk>[0-9]{1,10})"

urlpatterns = [
    re_path(r"^rap/?$", views.rap, name="rap"),
    re_path(r"^rap/timeseries/?$", views.rap, name="rap_timeseries"),
    re_path(r"^rap/annotation/?$", views.rap, name="rap_annotation"),
    re_path(r"^rap/annotations/?$", views.annotation_create, name="rap_annotation_create"),
    re_path(rf"^rap/annotations/{ID}/?$", views.annotation_update, name="rap_annotation_update"),
    re_path(rf"^rap/annotations/{ID}/delete/?$", views.annotation_delete, name="rap_annotation_delete"),
    re_path(r"^rap/files/(?P<file_id>[0-9]{1,10})/?$", views.study_file, name="rap_file"),
    re_path(r"^rap/conditions/?$", views.conditions, name="rap_conditions"),
]
