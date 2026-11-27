from django.urls import path, re_path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    re_path(r"^lang/?$", views.set_language, name="set_language"),
]

# 아직 구현되지 않은 화면의 자리표시(커스텀 데이터 구조는 apps.schemas 가 구현)
urlpatterns = [
    re_path(rf"^{key}/?$", view, name=f"page_{key.replace('/', '_')}")
    for key, view in views.PLACEHOLDER_VIEWS.items()
] + urlpatterns
