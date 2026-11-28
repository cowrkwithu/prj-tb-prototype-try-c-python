"""요청 1건 = 커밋 1번(apps.core.filetx) — 커밋 뒤 정리 실패가 커밋된 결과·응답을 바꾸지 않는다(5단계 2차 리뷰).

재현: 기존 파일 지연 삭제(커밋 후 콜백)에서 파일 삭제가 실패(Windows 파일 잠금)하고 OrphanFile 기록도 실패(DB 잠금)하면,
이전에는 atomic_write 가 이미 커밋된 '새' 파일을 지우고 503 '저장되지 않았습니다'를 돌려줬다(행은 남고 파일은 없음).
세 경로(업로드 CSV 교체, 임시저장 업로드 삭제, 데이터 구조 오래된 입력 정리)를 실제 커밋(transaction=True)으로 확인하고,
남은 파일은 cleanup_media 가 회수하는지 본다.
"""
import io
import pathlib
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import OperationalError, transaction
from django.test import RequestFactory
from django.utils import timezone

from apps.core.filetx import atomic_write, delete_after_commit, register_new_file


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()
    return settings.MEDIA_ROOT


def _break_cleanup(monkeypatch, locked: pathlib.Path):
    """locked 파일 삭제는 PermissionError, OrphanFile 기록은 DB 잠금 오류."""
    from apps.uploads.models import OrphanFile

    real_unlink = pathlib.Path.unlink
    target = locked.resolve()

    def unlink(self, *a, **k):
        if self.resolve() == target:
            raise PermissionError("[WinError 32] 다른 프로세스가 파일을 사용 중")
        return real_unlink(self, *a, **k)

    def busy(*a, **k):
        raise OperationalError("database is locked")

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    monkeypatch.setattr(OrphanFile.objects, "get_or_create", busy)


def _csv(text_from="300mg", text_to="300mg") -> bytes:
    from apps.uploads.services import csvio

    return csvio.template_csv("regimen").lstrip("﻿").replace("TB-KR-051", "KR-2024-0002") \
        .replace(text_from, text_to).encode()


def _csv_file(name, data):
    return SimpleUploadedFile(name, data, content_type="text/csv")


def _cleanup():
    call_command("cleanup_media", "--min-age-minutes", "0", stdout=io.StringIO())


@pytest.mark.django_db(transaction=True)
def test_csv_replace_cleanup_failure_keeps_committed_new_file(client, demo_users, seeded_patients, media, monkeypatch):
    from apps.uploads.models import Submission, SubmissionFile

    client.force_login(demo_users["researcher"])
    client.post("/upload/regimen/csv", {"mode": "csv", "file": _csv_file("a.csv", _csv())})
    sub = Submission.objects.get()
    old_path = media / SubmissionFile.objects.get(submission=sub).file.name
    _break_cleanup(monkeypatch, old_path)
    r = client.post("/upload/regimen/csv", {"id": sub.upload_id, "file": _csv_file("b.csv", _csv("300mg", "301mg"))})
    monkeypatch.undo()
    assert r.status_code == 302  # 커밋된 요청 — 503 이 아니다
    rows = list(SubmissionFile.objects.filter(submission=sub))
    assert [f.original_name for f in rows] == ["b.csv"]
    assert (media / rows[0].file.name).exists()  # 커밋된 새 파일은 지우지 않는다
    assert old_path.exists()  # 지우지 못한 기존 파일(참조 없음) → cleanup_media 가 회수
    _cleanup()
    assert not old_path.exists() and (media / rows[0].file.name).exists()


@pytest.mark.django_db(transaction=True)
def test_draft_delete_cleanup_failure_keeps_success(client, demo_users, seeded_patients, media, monkeypatch):
    from apps.uploads.models import Submission, SubmissionFile

    client.force_login(demo_users["researcher"])
    client.post("/upload/regimen/csv", {"mode": "csv", "file": _csv_file("a.csv", _csv())})
    sub = Submission.objects.get()
    path = media / SubmissionFile.objects.get(submission=sub).file.name
    _break_cleanup(monkeypatch, path)
    r = client.post(f"/upload/mine/{sub.upload_id}/delete")
    monkeypatch.undo()
    assert r.status_code == 302 and not Submission.objects.exists()
    assert path.exists()
    _cleanup()
    assert not path.exists()


@pytest.mark.django_db(transaction=True)
def test_schema_stale_pending_cleanup_failure_keeps_new_attachment(client, demo_users, media, monkeypatch):
    from apps.schemas import services
    from apps.schemas.models import PendingEntry, SchemaFile
    from apps.schemas.tests.conftest import simple_payload

    s = services.save_definition(demo_users["researcher"], simple_payload(fields=[
        {"fieldId": "f", "fieldName": "doc", "displayName": "문서", "dataType": "FILE", "constraints": {}},
        {"fieldId": "n", "fieldName": "n", "displayName": "N", "dataType": "INTEGER", "isRequired": True}]), action="publish")
    client.force_login(demo_users["researcher"])
    url = f"/schemas/{s.schema_id}/data/check"
    client.post(url, {"mode": "form", "link_value": "A1", "f__n": "1", "f__doc": SimpleUploadedFile("a.txt", b"x" * 10)})
    old = SchemaFile.objects.get()
    old_path = media / old.file.name
    PendingEntry.objects.update(created_at=timezone.now() - timedelta(hours=25))  # 24시간 지난 미제출 입력
    _break_cleanup(monkeypatch, old_path)
    r = client.post(url, {"mode": "form", "link_value": "A1", "f__n": "1", "f__doc": SimpleUploadedFile("b.txt", b"y" * 10)})
    monkeypatch.undo()
    assert r.status_code == 302 and "/data/review/" in r["Location"]
    new = SchemaFile.objects.get()
    assert new.original_name == "b.txt" and (media / new.file.name).exists()
    assert old_path.exists()
    bumped = SchemaFile.objects.filter(pk=new.pk).update(uploaded_at=timezone.now() + timedelta(hours=1))
    assert bumped == 1  # 방금 입력(미제출) 첨부는 정리 대상이 아니게(정리 기준 시각 이후)
    _cleanup()
    assert not old_path.exists() and (media / new.file.name).exists()


# ─── atomic_write 단위 ───
def _view_storing(media, name, then=None):
    @atomic_write
    def view(request):
        (media / name).write_bytes(b"x")
        register_new_file(name)
        if then:
            then()
        from django.http import HttpResponse

        return HttpResponse("ok")

    return view


@pytest.mark.django_db(transaction=True)
def test_atomic_write_removes_new_file_only_on_rollback(media):
    def boom():
        raise OperationalError("database is locked")

    with pytest.raises(OperationalError):
        _view_storing(media, "rolled.bin", boom)(RequestFactory().get("/"))
    assert not (media / "rolled.bin").exists()  # 롤백 → 새 파일 정리


@pytest.mark.django_db(transaction=True)
def test_atomic_write_keeps_new_file_when_post_commit_callback_fails(media):
    def failing_hook():
        transaction.on_commit(lambda: (_ for _ in ()).throw(OSError("post-commit failure")))  # robust 아님

    with pytest.raises(OSError):
        _view_storing(media, "kept.bin", failing_hook)(RequestFactory().get("/"))
    assert (media / "kept.bin").exists()  # 커밋된 뒤의 예외 — 새 파일은 지우지 않는다


@pytest.mark.django_db(transaction=True)
def test_delete_after_commit_is_robust(media):
    def failing(p):
        raise OSError("locked")

    with transaction.atomic():
        delete_after_commit(["x.bin"], failing)  # 커밋 뒤 예외가 나도 호출자에게 올라오지 않는다(로그만)
