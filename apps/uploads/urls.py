from django.urls import re_path

from . import admin_views, deletion_views, views

D = r"(?P<domain>clinical|regimen|lab|genomic|imaging)"
U = rf"(?P<uid>{views.UPLOAD_ID_RE})"

urlpatterns = [
    re_path(r"^upload/?$", views.hub, name="upload_hub"),
    re_path(r"^upload/mine/?$", views.my_uploads, name="my_uploads"),
    re_path(rf"^upload/mine/{U}/delete/?$", views.my_delete, name="my_upload_delete"),
    re_path(rf"^upload/mine/{U}/resubmit/?$", views.my_resubmit, name="my_upload_resubmit"),
    re_path(rf"^upload/report/{U}\.csv$", views.report_csv, name="upload_report"),
    re_path(r"^upload/file/(?P<file_id>\d+)/?$", views.download_file, name="upload_file"),
    re_path(rf"^upload/{D}/?$", views.wizard, name="upload_wizard"),
    re_path(rf"^upload/{D}/template\.csv$", views.template_csv, name="upload_template"),
    re_path(rf"^upload/{D}/mode/?$", views.choose_mode, name="upload_mode"),
    re_path(rf"^upload/{D}/data/?$", views.save_data, name="upload_data"),
    re_path(rf"^upload/{D}/csv/?$", views.upload_csv, name="upload_csv"),
    re_path(rf"^upload/{D}/proceed/?$", views.proceed, name="upload_proceed"),
    re_path(rf"^upload/{D}/files/?$", views.upload_files, name="upload_files"),
    re_path(rf"^upload/{D}/files/(?P<file_id>\d+)/remove/?$", views.remove_file, name="upload_file_remove"),
    re_path(rf"^upload/{D}/submit/?$", views.submit, name="upload_submit"),
    # 포털 관리자
    re_path(r"^admin/?$", admin_views.admin_dashboard, name="admin_dashboard"),
    re_path(r"^admin/review/?$", admin_views.review_queue, name="admin_review"),
    re_path(rf"^admin/review/{U}/?$", admin_views.review_detail, name="admin_review_detail"),
    re_path(rf"^admin/review/{U}/approve/?$", admin_views.review_approve, name="admin_review_approve"),
    re_path(rf"^admin/review/{U}/reject/?$", admin_views.review_reject, name="admin_review_reject"),
    re_path(rf"^admin/review/{U}/revise/?$", admin_views.review_revise, name="admin_review_revise"),
    # 삭제 요청 처리(2단계 이월분)
    re_path(r"^admin/deletions/?$", deletion_views.deletion_list, name="admin_deletions"),
    re_path(rf"^admin/deletions/{U}/?$", deletion_views.deletion_detail, name="admin_deletion_detail"),
    re_path(rf"^admin/deletions/{U}/approve/?$", deletion_views.deletion_approve, name="admin_deletion_approve"),
    re_path(rf"^admin/deletions/{U}/reject/?$", deletion_views.deletion_reject, name="admin_deletion_reject"),
]
