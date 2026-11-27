"""파일 검증: 확장자 위장, 크기 초과, 경로 조작 파일명, MIME, 업로드 핸들러 상한, 저장 위치·해시."""
import hashlib
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.uploads.models import Submission
from apps.uploads.services import dicom, files

from .conftest import csv_upload, make_fastq_gz, sample_bytes, upload_id_from

pytestmark = pytest.mark.django_db


def _genomic_post(client, *uploads, **extra):
    return client.post("/upload/genomic/files", {"mode": "individual", "files": list(uploads), **extra})


def test_disguised_extensions_rejected(client, demo_users):
    client.force_login(demo_users["researcher"])
    # 텍스트를 .fastq.gz 로 위장
    r = _genomic_post(client, SimpleUploadedFile("fake.fastq.gz", b"@r1\nACGT\n+\nIIII\n", content_type="application/gzip"))
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    assert sub.files.count() == 0
    # gzip 이지만 안은 FASTQ 가 아님
    import gzip, io  # noqa: E401

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(b"MZ\x90\x00 not a fastq")
    client.post("/upload/genomic/files", {"id": sub.upload_id, "files": [SimpleUploadedFile("x.fq.gz", buf.getvalue(), content_type="application/gzip")]})
    # PNG 를 .dcm 으로 위장
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300
    r2 = client.post("/upload/imaging/files", {"mode": "CXR", "condition_id": "X", "study_date": "2025-01-01",
                                               "files": [SimpleUploadedFile("scan.dcm", png, content_type="application/dicom")]})
    img = Submission.objects.get(upload_id=upload_id_from(r2))
    # JPEG 확장자에 PNG 내용
    client.post("/upload/imaging/files", {"id": img.upload_id, "files": [SimpleUploadedFile("scan.jpg", png, content_type="image/jpeg")]})
    # 허용되지 않은 확장자(.exe), 이중 확장자 위장(.fastq.exe)
    for bad in (SimpleUploadedFile("run.exe", b"MZ", content_type="application/octet-stream"),
                SimpleUploadedFile("reads.fastq.exe", b"@r\nA\n+\nI\n", content_type="application/octet-stream")):
        client.post("/upload/genomic/files", {"id": sub.upload_id, "files": [bad]})
    assert sub.files.count() == 0 and img.files.count() == 0
    # 정상 파일은 통과
    client.post("/upload/genomic/files", {"id": sub.upload_id, "files": [SimpleUploadedFile("ok.fq.gz", make_fastq_gz(), content_type="application/gzip")]})
    assert sub.files.count() == 1


def test_csv_disguise_and_mime(client, demo_users):
    client.force_login(demo_users["researcher"])
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("data.csv", b"PK\x03\x04xlsx-zip")})
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    assert sub.file_errors and "CSV" in sub.file_errors[0]["ko"] and sub.files.count() == 0
    client.post("/upload/clinical/csv", {"id": sub.upload_id, "file": csv_upload("data.csv", b"a,b\n1,2\n", content_type="application/x-msdownload")})
    sub.refresh_from_db()
    assert "MIME" in sub.file_errors[0]["ko"]


def test_size_limits(client, demo_users, settings):
    client.force_login(demo_users["researcher"])
    limits = {k: dict(v) for k, v in settings.UPLOAD_LIMITS.items()}
    limits["sequence"]["max_bytes"] = 100
    limits["csv"]["max_bytes"] = 50
    limits["image_cxr"]["max_bytes"] = 100
    settings.UPLOAD_LIMITS = limits
    # 엔드포인트 상한(시퀀스 100B)을 넘으면 수신 단계에서 중단 — draft 도 만들지 않는다
    big = SimpleUploadedFile("big.fastq", b"@r\n" + b"A" * 200 + b"\n+\n" + b"I" * 200 + b"\n", content_type="text/plain")
    r = client.post("/upload/genomic/files", {"mode": "individual", "files": [big]}, follow=True)
    assert "파일이 너무 큽니다" in r.content.decode() and not Submission.objects.exists()
    r = client.post("/upload/clinical/csv", {"mode": "csv", "file": csv_upload("clinical_deidentified.csv")}, follow=True)
    assert "파일이 너무 큽니다" in r.content.decode() and not Submission.objects.exists()
    # 수신은 통과(영상 엔드포인트는 CT 상한까지 받음)하지만 CXR 종류별 상한(100B)은 서비스가 거부
    jpg = SimpleUploadedFile("a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 300, content_type="image/jpeg")
    r = client.post("/upload/imaging/files", {"mode": "CXR", "condition_id": "X", "study_date": "2025-01-01", "files": [jpg]})
    assert Submission.objects.get(upload_id=upload_id_from(r)).files.count() == 0


def test_max_file_count(client, demo_users):
    client.force_login(demo_users["researcher"])
    r = _genomic_post(client, SimpleUploadedFile("s0.fq.gz", make_fastq_gz(), content_type="application/gzip"))
    uid = upload_id_from(r)
    for i in range(1, 6):  # 개별 업로드는 최대 5개 — 6번째는 거부 (내용이 같은 파일은 3c 부터 중복으로 거부되므로 내용을 달리한다)
        client.post("/upload/genomic/files", {"id": uid, "files": [SimpleUploadedFile(
            f"s{i}.fq.gz", make_fastq_gz(f"@r{i}\nACGT\n+\nIIII\n"), content_type="application/gzip")]})
    assert Submission.objects.get(upload_id=uid).files.count() == 5


def test_upload_handler_aborts_oversized_stream(client, demo_users, settings):
    client.force_login(demo_users["researcher"])
    settings.UPLOAD_MAX_FILE_BYTES = 1024
    big = SimpleUploadedFile("big.fastq", b"@r\n" + b"A" * 5000 + b"\n", content_type="text/plain")
    r = client.post("/upload/genomic/files", {"mode": "individual", "files": [big]}, follow=True)
    assert "파일이 너무 큽니다" in r.content.decode()
    assert not Submission.objects.exists()


def test_path_traversal_filename(client, demo_users, _media_tmp):
    client.force_login(demo_users["researcher"])
    evil = SimpleUploadedFile("../../etc/passwd.fastq.gz", make_fastq_gz(), content_type="application/gzip")
    r = _genomic_post(client, evil)
    sub = Submission.objects.get(upload_id=upload_id_from(r))
    f = sub.files.get()
    assert f.original_name == "passwd.fastq.gz"
    stored = Path(f.file.path).resolve()
    assert stored.is_relative_to(Path(_media_tmp).resolve() / "uploads")
    assert stored.name != "passwd.fastq.gz" and stored.name.endswith(".fastq.gz") and len(stored.stem.split(".")[0]) == 32
    assert f.sha256 == hashlib.sha256(make_fastq_gz()).hexdigest() or len(f.sha256) == 64
    assert files.sanitize_name("..\\..\\win\\x<y>.csv") == "xy.csv"
    assert files.sanitize_name("") == "file"


def test_stored_hash_and_chunked_copy(demo_users, _media_tmp):
    sub = Submission.objects.create(upload_id="UPL-20260101-001", submitter=demo_users["researcher"], domain="genomic",
                                    method="form", mode="individual")
    data = sample_bytes("TB-KR-102_SP-102A.fastq.gz")
    up = SimpleUploadedFile("TB-KR-102_SP-102A.fastq.gz", data, content_type="application/gzip")
    name, ext = files.validate_upload(up, "sequence", "sequence")
    sf = files.store(sub, up, "sequence", name, ext)
    assert sf.size == len(data) and sf.sha256 == hashlib.sha256(data).hexdigest()
    assert Path(sf.file.path).read_bytes() == data
    assert sf.file.name.startswith("uploads/")


def test_dicom_magic_required(demo_users):
    good = SimpleUploadedFile("a.dcm", dicom.build_minimal_dicom({}), content_type="application/dicom")
    assert files.validate_upload(good, "image_volume", "image")[1] == ".dcm"
    bad = SimpleUploadedFile("a.dcm", b"\x00" * 200, content_type="application/dicom")
    with pytest.raises(files.FileRejected):
        files.validate_upload(bad, "image_volume", "image")
    # CT 는 DICOM 만 허용(JPG 불가)
    jpg = SimpleUploadedFile("a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100, content_type="image/jpeg")
    with pytest.raises(files.FileRejected):
        files.validate_upload(jpg, "image_volume", "image")
    assert files.validate_upload(jpg, "image_cxr", "image")[1] == ".jpg"


def test_media_not_publicly_served(client, demo_users):
    client.force_login(demo_users["researcher"])
    r = _genomic_post(client, SimpleUploadedFile("ok.fq.gz", make_fastq_gz(), content_type="application/gzip"))
    f = Submission.objects.get(upload_id=upload_id_from(r)).files.get()
    assert client.get("/media/" + f.file.name).status_code == 404
