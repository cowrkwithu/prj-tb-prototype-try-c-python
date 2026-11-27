from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r"^cases/?$", views.case_editor, name="cases"),
    re_path(r"^cases/(?P<pk>\d+)/images/?$", views.case_images, name="case_images"),
    re_path(r"^cases/(?P<pk>\d+)/images/(?P<image_id>\d+)/?$", views.case_image_detail, name="case_image_detail"),
]
