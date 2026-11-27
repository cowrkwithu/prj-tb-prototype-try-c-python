from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        RESEARCHER = "researcher", "연구원"
        ADMIN = "admin", "관리자"

    role = models.CharField("역할", max_length=20, choices=Role.choices, default=Role.RESEARCHER)
    display_name = models.CharField("이름", max_length=100, blank=True)
    organization = models.CharField("소속 기관", max_length=200, blank=True)

    class Meta:
        verbose_name = "사용자"
        verbose_name_plural = "사용자"

    def __str__(self):
        return self.display_name or self.username

    @property
    def is_portal_admin(self) -> bool:
        """포털 /admin/* 화면 접근 권한. Django 슈퍼유저도 관리자로 본다."""
        return self.role == self.Role.ADMIN or self.is_superuser

    @property
    def shown_name(self) -> str:
        return self.display_name or self.username

    @property
    def initial(self) -> str:
        return (self.shown_name or "?")[0]
