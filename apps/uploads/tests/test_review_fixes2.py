"""2단계 재검증(CONFIRMED 1~3) 재현 테스트."""
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

import pytest
from django.conf import settings as dj_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from apps.uploads.handlers import endpoint_limits
from apps.uploads.models import Submission
from apps.uploads.services import dicom

from .conftest import make_fastq_gz, upload_id_from

pytestmark = pytest.mark.django_db
XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _prefix():
    base = dicom.build_minimal_dicom({(0x0008, 0x0080): "X"})
    return base[:base.find(struct.pack("<HH", 0x0008, 0x0080))]


SQ_OPEN = struct.pack("<HH", 0x0008, 0x1032) + b"SQ\x00\x00" + struct.pack("<I", 0xFFFFFFFF)
ITEM_OPEN = struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFFFF)


# ─── 1. 중첩 SQ 재귀 폭주 ───
@pytest.mark.parametrize("depth", [33, 2000, 5000])
def test_deeply_nested_sequences_unreadable_not_500(depth):
    data = _prefix() + (SQ_OPEN + ITEM_OPEN) * depth
    info = dicom.inspect(io.BytesIO(data))
    assert info["parsed"] is False and "중첩" in info["error"]


def test_nesting_within_limit_still_parsed():
    # 깊이 5 중첩(항목/시퀀스 구분자로 정상 종료) 뒤의 식별 태그까지 판독
    close = struct.pack("<HHI", 0xFFFE, 0xE00D, 0) + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)
    name = b"Kim^A "
    data = _prefix() + (SQ_OPEN + ITEM_OPEN) * 5 + close * 5 + struct.pack("<HH", 0x0010, 0x0010) + b"PN" + struct.pack("<H", len(name)) + name
    info = dicom.inspect(io.BytesIO(data))
    assert info["parsed"] and info["tags"][0]["risky"]


def test_nested_dicom_upload_is_blocked_without_error(client, demo_users):
    client.force_login(demo_users["researcher"])
    data = _prefix() + (SQ_OPEN + ITEM_OPEN) * 5000
    r = client.post("/upload/imaging/files", {"mode": "CT", "condition_id": "X", "study_date": "2025-01-01",
                                              "files": [SimpleUploadedFile("deep.dcm", data, content_type="application/dicom")]})
    assert r.status_code == 302
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    assert sub.pii_blocking and sub.pii_report["detections"][0]["confidence"] == "high"


# ─── 2. 요소 수·판독 시간 상한 ───
def test_many_zero_length_items_hit_element_limit():
    data = _prefix() + SQ_OPEN + struct.pack("<HHI", 0xFFFE, 0xE000, 0) * 1_000_000 + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)
    t = time.monotonic()
    info = dicom.inspect(io.BytesIO(data))
    assert info["parsed"] is False and "요소가 너무 많습니다" in info["error"]
    assert time.monotonic() - t < 2


def test_parse_time_limit(settings):
    settings.DICOM_PARSE_TIMEOUT_SECONDS = 0
    data = _prefix() + SQ_OPEN + struct.pack("<HHI", 0xFFFE, 0xE000, 0) * 5000 + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)
    info = dicom.inspect(io.BytesIO(data))
    assert info["parsed"] is False and "시간" in info["error"]
    settings.DICOM_PARSE_TIMEOUT_SECONDS = 2.0
    assert dicom.inspect(io.BytesIO(data))["parsed"]


# ─── 3. 파일 1개 = 요청 1개, waitress 상한 ───
def _fq(name="a.fq.gz"):
    # 파일명마다 내용을 달리한다 — 같은 업로드 안 동일 내용(SHA-256) 파일은 3c 부터 중복으로 거부된다
    return SimpleUploadedFile(name, make_fastq_gz(f"@{name}\nACGT\n+\nIIII\n"), content_type="application/gzip")


def test_one_file_per_request(client, demo_users):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/genomic/files", {"mode": "individual", "files": [_fq("a.fq.gz"), _fq("b.fq.gz")]})
    assert r.status_code == 302 and not Submission.objects.exists()
    r = client.post("/upload/genomic/files", {"mode": "individual", "files": [_fq("a.fq.gz"), _fq("b.fq.gz")]}, **XHR)
    assert r.status_code == 400 and "1개" in r.json()["message"] and not Submission.objects.exists()


def test_xhr_sequential_upload_json(client, demo_users):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/genomic/files", {"mode": "individual", "action": "add", "files": [_fq("a.fq.gz")]}, **XHR)
    body = r.json()
    assert r.status_code == 200 and body["ok"] and body["upload_id"].startswith("UPL-")
    uid = body["upload_id"]
    r = client.post("/upload/genomic/files", {"id": uid, "files": [_fq("b.fq.gz")]}, **XHR)
    assert r.json()["ok"]
    # 실패 파일: JSON 으로 사유, 다른 파일은 그대로
    r = client.post("/upload/genomic/files", {"id": uid, "files": [SimpleUploadedFile("c.fq.gz", b"not gzip", content_type="application/gzip")]}, **XHR)
    assert r.status_code == 400 and "gzip" in r.json()["message"]
    assert Submission.objects.get(upload_id=uid).files.count() == 2
    page = client.get(f"/upload/genomic?id={uid}&step=1").content.decode()
    assert "fileUploader(" in page and 'data-testid="upload-queue"' in page


def test_xhr_oversized_returns_413(client, demo_users, settings):
    client.force_login(demo_users["researcher"])
    settings.UPLOAD_MAX_FILE_BYTES = 100
    r = client.post("/upload/genomic/files", {"mode": "individual", "files": [SimpleUploadedFile("big.fastq", b"@r\n" + b"A" * 500 + b"\n")]}, **XHR)
    assert r.status_code == 413 and "너무 큽니다" in r.json()["message"]


def test_files_endpoint_limit_is_single_file():
    lim = dj_settings.UPLOAD_LIMITS
    total, per = endpoint_limits("/upload/imaging/files")
    assert per == lim["image_volume"]["max_bytes"] and total == per + 1024 * 1024
    total, per = endpoint_limits("/upload/genomic/files")
    assert total == lim["sequence"]["max_bytes"] + 1024 * 1024


def test_request_body_cap_and_env_override():
    assert dj_settings.UPLOAD_MAX_REQUEST_BYTES == dj_settings.UPLOAD_MAX_FILE_BYTES + 16 * 1024 * 1024
    code = "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');from django.conf import settings;print(settings.UPLOAD_MAX_REQUEST_BYTES)"
    for env, expected in (("1000", 1000), (str(10 ** 13), dj_settings.UPLOAD_MAX_REQUEST_BYTES), ("abc", dj_settings.UPLOAD_MAX_REQUEST_BYTES)):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=dj_settings.BASE_DIR,
                             env={**os.environ, "TB_MAX_REQUEST_BYTES": env}, check=True).stdout.strip()
        assert int(out) == expected, env  # 낮출 수만 있다


def test_serve_passes_body_cap_and_var_tmp(monkeypatch):
    import waitress

    captured = {}
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: captured.update(kw))
    old = tempfile.tempdir
    try:
        call_command("serve", "4399", "--host", "127.0.0.1", stdout=io.StringIO(), stderr=io.StringIO())
        assert captured["max_request_body_size"] == dj_settings.UPLOAD_MAX_REQUEST_BYTES
        assert tempfile.tempdir == str(dj_settings.VAR_DIR / "tmp")
    finally:
        tempfile.tempdir = old


def test_readme_operations_guidance():
    text = (dj_settings.BASE_DIR / "README.md").read_text(encoding="utf-8")
    for needle in ("client_max_body_size", "TB_MAX_REQUEST_BYTES", "condition_id", "DICOM"):
        assert needle in text, needle
    assert json.dumps("ok")
