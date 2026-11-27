"""업로드 파일 정리: ① 지우지 못한 파일(OrphanFile) 재시도 ② DB 가 참조하지 않는 var/media/uploads 파일 삭제
③ 커스텀 데이터 구조 첨부 중 제출되지 않은 오래된 파일·참조 없는 var/media/schemas 파일 삭제.

두 번째 단계는 업로드 진행 중인 파일을 지우지 않도록 수정된 지 --min-age-minutes(기본 60분) 지난 파일만 본다.
"""
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.uploads.models import OrphanFile, SubmissionFile
from apps.uploads.services.files import delete_path


class Command(BaseCommand):
    help = "지우지 못한 업로드 파일을 다시 지우고, DB 에서 참조하지 않는 업로드 파일을 정리한다."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="지우지 않고 대상만 보여 준다")
        parser.add_argument("--min-age-minutes", type=int, default=60)

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        retried = removed = failed = 0
        for o in list(OrphanFile.objects.all()):
            if dry:
                self.stdout.write(f"[재시도 대상] {o.path} ({o.attempts}회 실패)")
                continue
            if delete_path(o.path):
                retried += 1
            else:
                failed += 1
        root = Path(settings.MEDIA_ROOT)
        uploads = root / "uploads"
        referenced = set(SubmissionFile.objects.values_list("file", flat=True))
        cutoff = time.time() - opts["min_age_minutes"] * 60
        if uploads.exists():
            for f in uploads.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(root).as_posix()
                if rel in referenced or f.stat().st_mtime > cutoff:
                    continue
                if dry:
                    self.stdout.write(f"[참조 없음] {rel}")
                elif delete_path(rel):
                    removed += 1
                else:
                    failed += 1
        removed, failed = self._schema_files(root, cutoff, dry, removed, failed)
        self.stdout.write(self.style.SUCCESS(
            f"재시도 삭제 {retried}건, 참조 없는 파일 삭제 {removed}건, 실패 {failed}건" + (" (dry-run)" if dry else "")))

    def _schema_files(self, root, cutoff, dry, removed, failed):
        """커스텀 데이터 구조(apps.schemas) 첨부: 제출되지 않은(행에 연결되지 않은) 오래된 첨부와
        DB 가 참조하지 않는 var/media/schemas 파일을 지운다."""
        from datetime import datetime, timezone as dt_tz

        from apps.schemas.models import SchemaFile

        cutoff_dt = datetime.fromtimestamp(cutoff, tz=dt_tz.utc)
        stale = SchemaFile.objects.filter(record__isnull=True, uploaded_at__lt=cutoff_dt)
        for sf in list(stale):
            rel = sf.file.name
            if dry:
                self.stdout.write(f"[미제출 첨부] {rel}")
                continue
            sf.delete()
            if rel and delete_path(rel):
                removed += 1
            elif rel:
                failed += 1
        folder = root / "schemas"
        if folder.exists():
            referenced = set(SchemaFile.objects.values_list("file", flat=True))
            for f in folder.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(root).as_posix()
                if rel in referenced or f.stat().st_mtime > cutoff:
                    continue
                if dry:
                    self.stdout.write(f"[참조 없음] {rel}")
                elif delete_path(rel):
                    removed += 1
                else:
                    failed += 1
        return removed, failed
