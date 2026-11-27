from django.urls import re_path

from . import admin_views, views

S = r"(?P<schema_id>[A-Za-z0-9_-]{1,40})"

urlpatterns = [
    re_path(r"^schemas/?$", views.explorer, name="schemas"),
    re_path(r"^schemas/create/?$", views.builder, name="schema_create"),
    re_path(r"^schemas/import/?$", views.import_schema, name="schema_import"),
    re_path(r"^schemas/select/?$", views.select_modal, name="schema_select"),
    re_path(r"^schemas/builder/dictionary/?$", views.builder_dictionary, name="schema_builder_dictionary"),
    re_path(r"^schemas/builder/preview/?$", views.builder_preview, name="schema_builder_preview"),
    re_path(r"^schemas/case/(?P<condition_id>[A-Za-z0-9._-]{1,40})/?$", views.case_tab, name="schema_case_tab"),
    re_path(rf"^schemas/{S}/?$", views.detail, name="schema_detail"),
    re_path(rf"^schemas/{S}/edit/?$", views.builder, name="schema_edit"),
    re_path(rf"^schemas/{S}/data/?$", views.data_entry, name="schema_data"),
    re_path(rf"^schemas/{S}/data/check/?$", views.data_check, name="schema_data_check"),
    re_path(rf"^schemas/{S}/data/review/(?P<pk>\d+)/?$", views.data_review, name="schema_data_review"),
    re_path(rf"^schemas/{S}/data/submit/(?P<pk>\d+)/?$", views.data_submit, name="schema_data_submit"),
    re_path(rf"^schemas/{S}/data/done/(?P<pk>\d+)/?$", views.data_done, name="schema_data_done"),
    re_path(rf"^schemas/{S}/records/(?P<record_id>[A-Za-z0-9_-]{{1,40}})/?$", views.record_detail, name="schema_record"),
    re_path(rf"^schemas/{S}/records/(?P<record_id>[A-Za-z0-9_-]{{1,40}})/delete/?$", views.record_delete,
            name="schema_record_delete"),
    re_path(rf"^schemas/{S}/files/(?P<file_id>\d+)/?$", views.download_file, name="schema_file"),
    re_path(rf"^schemas/{S}/records\.csv$", views.export_records, name="schema_export_csv"),
    re_path(rf"^schemas/{S}/template\.csv$", views.template_csv, name="schema_template_csv"),
    re_path(rf"^schemas/{S}/export\.json$", views.export_json, name="schema_export_json"),
    re_path(rf"^schemas/{S}/duplicate/?$", views.duplicate, name="schema_duplicate"),
    re_path(rf"^schemas/{S}/status/?$", views.owner_status, name="schema_status"),
    re_path(rf"^schemas/{S}/delete/?$", views.owner_delete, name="schema_delete"),
    # 관리 → 데이터 구조 관리
    re_path(r"^admin/schemas/?$", admin_views.manager, name="admin_schemas"),
    re_path(rf"^admin/schemas/{S}/?$", admin_views.admin_detail, name="admin_schema_detail"),
    re_path(rf"^admin/schemas/{S}/confirm/?$", admin_views.admin_confirm, name="admin_schema_confirm"),
    re_path(rf"^admin/schemas/{S}/action/?$", admin_views.admin_action, name="admin_schema_action"),
    re_path(rf"^admin/schemas/{S}/visibility/?$", admin_views.admin_visibility, name="admin_schema_visibility"),
    re_path(rf"^admin/schemas/{S}/delete/?$", admin_views.admin_delete, name="admin_schema_delete"),
]
