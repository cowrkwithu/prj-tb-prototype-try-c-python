import gzip
import io
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile

SAMPLES = Path(settings.BASE_DIR) / "static" / "samples"


@pytest.fixture(autouse=True)
def _media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()
    return settings.MEDIA_ROOT


def sample_bytes(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def csv_upload(name: str, data: bytes | None = None, content_type="text/csv"):
    return SimpleUploadedFile(name, data if data is not None else sample_bytes(name), content_type=content_type)


def fastq_gz(name="TB-KR-101_SP-101A.fastq.gz"):
    return SimpleUploadedFile(name, sample_bytes(name), content_type="application/gzip")


def make_fastq_gz(text="@r1\nACGT\n+\nIIII\n"):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(text.encode())
    return buf.getvalue()


def upload_id_from(resp) -> str:
    m = re.search(r"id=(UPL-\d{8}-\d{3,})", resp["Location"])
    assert m, resp["Location"]
    return m.group(1)


@pytest.fixture
def other_researcher(db):
    from apps.accounts.models import User

    u = User.objects.create_user("other", password="x", role="researcher", organization="몽골국립의료원")
    return u
