from django.contrib import admin
from django.urls import include, path, re_path

from apps.core import views as core_views

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.cases.urls")),
    path("", include("apps.dictionary.urls")),
    path("", include("apps.uploads.urls")),
    path("", include("apps.schemas.urls")),
    path("", include("apps.cohorts.urls")),
    path("", include("apps.datasharing.urls")),
    path("", include("apps.core.urls")),
    # 정의되지 않은 주소는 홈으로 (원본: <Route path="*" element={<Navigate to="/" replace />} />)
    re_path(r"^(?!static/|media/).*$", core_views.fallback_redirect),
]
