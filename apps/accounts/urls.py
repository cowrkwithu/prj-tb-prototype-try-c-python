from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r"^login/?$", views.login_view, name="login"),
    re_path(r"^admin/login/?$", views.admin_login_view, name="admin_login"),
    re_path(r"^logout/?$", views.logout_view, name="logout"),
    re_path(r"^profile/?$", views.profile_view, name="profile"),
]
