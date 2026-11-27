from django.urls import re_path

from . import admin_views, api, views

SLUG = r"(?P<slug>[a-z0-9-]{1,40})"
SCHEMA = r"(?P<schema_id>[A-Za-z0-9_-]{1,40})"
DAR_ID = rf"(?P<request_id>{admin_views.REQUEST_ID_RE})"
ID = r"(?P<pk>[0-9]{1,10})"

urlpatterns = [
    # 데이터신청 화면 (원본 /data — 탭은 ?tab=datasets|api|dar|uploads)
    re_path(r"^data/?$", views.data_sharing, name="data_sharing"),
    re_path(r"^data/dar/?$", views.dar_submit, name="dar_submit"),
    re_path(r"^data/api/key/?$", views.api_key_issue, name="api_key_issue"),
    re_path(r"^data/api/key/revoke/?$", views.api_key_revoke, name="api_key_revoke"),
    re_path(rf"^data/datasets/{SLUG}/download\.(?P<ext>csv|xlsx)$", views.dataset_download, name="dataset_download"),
    re_path(rf"^data/datasets/{SLUG}/schema/?$", views.dataset_schema, name="dataset_schema"),
    re_path(rf"^data/custom/{SCHEMA}/schema/?$", views.custom_schema, name="custom_dataset_schema"),
    re_path(rf"^data/custom/{SCHEMA}/template\.csv$", views.custom_template, name="custom_dataset_template"),
    # 포털 관리자 — 데이터 접근 신청 검토
    re_path(r"^admin/dar/?$", admin_views.dar_list, name="admin_dar"),
    re_path(rf"^admin/dar/{DAR_ID}/?$", admin_views.dar_detail, name="admin_dar_detail"),
    re_path(rf"^admin/dar/{DAR_ID}/approve/?$", admin_views.dar_approve, name="admin_dar_approve"),
    re_path(rf"^admin/dar/{DAR_ID}/reject/?$", admin_views.dar_reject, name="admin_dar_reject"),
    # REST API (API 키 인증 — 세션·CSRF 미사용)
    re_path(r"^api/v1/cases/?$", api.cases, name="api_cases"),
    re_path(rf"^api/v1/cases/{ID}/?$", api.case_detail, name="api_case_detail"),
    re_path(rf"^api/v1/images/{ID}/?$", api.image_detail, name="api_image_detail"),
    re_path(r"^api/v1/cohorts/?$", api.cohorts, name="api_cohorts"),
    re_path(r"^api/v1/genomics/variants/?$", api.variants, name="api_variants"),
    re_path(r"^api(?:/.*)?$", api.not_found),  # 정의되지 않은 API 주소는 홈 리다이렉트 대신 JSON 404
]
