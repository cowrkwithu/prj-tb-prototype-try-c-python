from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "display_name", "role", "organization", "email", "is_active")
    list_filter = ("role", "is_active", "is_staff")
    fieldsets = BaseUserAdmin.fieldsets + (("포털", {"fields": ("role", "display_name", "organization")}),)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (("포털", {"fields": ("role", "display_name", "organization")}),)
