from django.urls import re_path

from . import admin_views, views

urlpatterns = [
    re_path(r"^dictionary/?$", views.dictionary_viewer, name="dictionary"),
    re_path(r"^dictionary/field/(?P<dictionary_id>[A-Za-z0-9._-]+)/?$", views.field_detail, name="dictionary_field"),
    re_path(r"^dictionary/assistant/?$", views.assistant, name="dictionary_assistant"),
    # 관리 → 데이터사전 관리
    re_path(r"^admin/dictionary/?$", admin_views.manager, name="admin_dictionary"),
    re_path(r"^admin/dictionary/category/modal/?$", admin_views.category_modal, name="admin_dict_category_modal"),
    re_path(r"^admin/dictionary/category/save/?$", admin_views.category_save, name="admin_dict_category_save"),
    re_path(r"^admin/dictionary/category/(?P<pk>\d+)/delete/?$", admin_views.category_delete, name="admin_dict_category_delete"),
    re_path(r"^admin/dictionary/entry/modal/?$", admin_views.entry_modal, name="admin_dict_entry_modal"),
    re_path(r"^admin/dictionary/entry/save/?$", admin_views.entry_save, name="admin_dict_entry_save"),
    re_path(r"^admin/dictionary/entry/(?P<pk>\d+)/deprecate/?$", admin_views.entry_deprecate, name="admin_dict_entry_deprecate"),
    re_path(r"^admin/dictionary/version/(?P<pk>\d+)/activate/?$", admin_views.version_activate, name="admin_dict_version_activate"),
    re_path(r"^admin/dictionary/version/upload/?$", admin_views.version_upload, name="admin_dict_version_upload"),
]
