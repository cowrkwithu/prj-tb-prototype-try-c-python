"""시드 공통 도구: 1회성 마커(SeedLog)와 PK 직접 지정 후 시퀀스 리셋."""
from django.core.management.color import no_style
from django.db import connection

from .models import SeedLog


def is_seeded(dataset: str) -> bool:
    return SeedLog.objects.filter(dataset=dataset).exists()


def mark_seeded(dataset: str, detail: dict | None = None) -> None:
    log, created = SeedLog.objects.get_or_create(dataset=dataset, defaults={"detail": detail or {}})
    if not created:
        log.detail = detail or {}
        log.save(update_fields=["detail", "updated_at"])


def reset_sequences(*models) -> list[str]:
    """PK 를 직접 지정해 넣은 뒤 자동 증가 시퀀스를 MAX(id) 로 맞춘다.

    SQLite 는 AUTOINCREMENT 가 MAX(rowid) 기준이라 빈 목록(no-op)이지만, PostgreSQL 등으로 바꾸면
    Django 가 생성한 setval SQL 이 실행된다.
    """
    sql = connection.ops.sequence_reset_sql(no_style(), list(models))
    if sql:
        with connection.cursor() as cursor:
            for stmt in sql:
                cursor.execute(stmt)
    return sql
