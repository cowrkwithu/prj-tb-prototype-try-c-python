from django.contrib import admin

from .models import ApiKey, DataAccessRequest


@admin.register(DataAccessRequest)
class DataAccessRequestAdmin(admin.ModelAdmin):
    list_display = ("request_id", "applicant", "status", "created_at", "reviewed_at")
    list_filter = ("status",)
    search_fields = ("request_id", "name", "institution", "email")
    raw_id_fields = ("applicant", "reviewer")


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    # 키 원문은 저장하지 않는다(해시만) — 관리 사이트에서도 해시는 보이지 않게 한다
    list_display = ("prefix", "user", "created_at", "revoked_at", "last_used_at")
    exclude = ("key_hash",)
    raw_id_fields = ("user",)
