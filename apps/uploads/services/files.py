"""업로드 파일 검증·저장.

- 확장자(허용 목록) · MIME(브라우저가 보낸 Content-Type 허용 목록) · 매직바이트(gzip, DICOM 'DICM', JPEG, PNG,
  FASTA/FASTQ 첫 글자, CSV 텍스트) · 크기(settings.UPLOAD_LIMITS) 를 모두 서버에서 검사한다.
- 저장 이름은 재생성(uuid4) — 원래 파일명은 정리(sanitize)해 메타데이터로만 남긴다 → 경로 조작 불가.
- 저장은 UploadedFile.chunks() 로 청크 복사하며 SHA-256 을 함께 계산한다(전체를 메모리에 올리지 않음).
  큰 파일은 Django 가 FILE_UPLOAD_TEMP_DIR(var/tmp)에 먼저 청크로 받아 둔다.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import unicodedata
import uuid
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from ..models import SubmissionFile

MIME_ALLOWED = {
    "csv": {"text/csv", "text/plain", "application/vnd.ms-excel", "application/csv", "text/x-csv",
            "text/tab-separated-values", "application/octet-stream", ""},
    "sequence": {"application/octet-stream", "application/gzip", "application/x-gzip", "application/x-gzip-compressed",
                 "text/plain", "text/x-fasta", "chemical/seq-na-fasta", "application/x-fasta", ""},
    "image": {"application/dicom", "application/octet-stream", "image/jpeg", "image/pjpeg", "image/png", ""},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream",
             "application/zip", "application/x-zip-compressed", ""},
}


class FileRejected(ValueError):
    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en


def sanitize_name(name: str) -> str:
    """원래 파일명 정리: 경로 구분자 앞부분 제거, 제어문자 제거, 길이 제한. 저장 경로에는 쓰지 않는다."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    name = name.strip(" .")
    if not name:
        name = "file"
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:180] + "." + ext[:15]) if dot else name[:200]
    return name


def extension(name: str, allowed: list[str]) -> str | None:
    """허용 목록 중 가장 긴 일치 확장자(.fastq.gz 등). 없으면 None."""
    low = name.lower()
    for ext in sorted(allowed, key=len, reverse=True):
        if low.endswith(ext) and len(low) > len(ext):
            return ext
    return None


def _head(f, n=8192) -> bytes:
    f.seek(0)
    data = f.read(n)
    f.seek(0)
    return data


def _first_text_char(data: bytes) -> str:
    for line in data.splitlines():
        s = line.strip()
        if s:
            return chr(s[0])
    return ""


def check_magic(f, ext: str) -> None:
    head = _head(f, 8192)
    if ext.endswith(".gz"):
        if head[:2] != b"\x1f\x8b":
            raise FileRejected("gzip 파일 형식이 아닙니다(확장자 위장)", "Not a gzip file (disguised extension)")
        f.seek(0)
        try:
            with gzip.GzipFile(fileobj=f) as gz:
                inner = gz.read(4096)
        except (OSError, EOFError):
            raise FileRejected("gzip 압축을 풀 수 없습니다", "Cannot decompress the gzip file") from None
        finally:
            f.seek(0)
        _check_sequence_text(inner, ext[:-3])
    elif ext in (".fasta", ".fa", ".fastq", ".fq"):
        _check_sequence_text(head, ext)
    elif ext in (".dcm", ".dicom"):
        data = _head(f, 132)
        if len(data) < 132 or data[128:132] != b"DICM":
            raise FileRejected("DICOM 파일이 아닙니다(128바이트 뒤 'DICM' 없음)", "Not a DICOM file (no 'DICM' at offset 128)")
    elif ext in (".jpg", ".jpeg"):
        if head[:3] != b"\xff\xd8\xff":
            raise FileRejected("JPEG 파일 형식이 아닙니다", "Not a JPEG file")
    elif ext == ".png":
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            raise FileRejected("PNG 파일 형식이 아닙니다", "Not a PNG file")
    elif ext == ".pdf":
        if head[:5] != b"%PDF-":
            raise FileRejected("PDF 파일 형식이 아닙니다", "Not a PDF file")
    elif ext == ".xlsx":
        if head[:4] != b"PK\x03\x04":
            raise FileRejected("엑셀(xlsx) 파일 형식이 아닙니다", "Not an Excel (xlsx) file")
    elif ext in (".csv", ".tsv"):
        if b"\x00" in head or head[:2] == b"\x1f\x8b" or head[:4] == b"PK\x03\x04":
            raise FileRejected("텍스트 CSV 파일이 아닙니다", "Not a text CSV file")


def _check_sequence_text(data: bytes, ext: str) -> None:
    if b"\x00" in data:
        raise FileRejected("시퀀스 텍스트 파일이 아닙니다", "Not a sequence text file")
    first = _first_text_char(data)
    expect = "@" if ext in (".fastq", ".fq") else ">"
    if first != expect:
        kind = "FASTQ" if expect == "@" else "FASTA"
        raise FileRejected(f"{kind} 형식이 아닙니다(첫 글자 '{expect}' 필요)", f"Not a {kind} file (must start with '{expect}')")


def validate_upload(f, limit_key: str, mime_group: str) -> tuple[str, str]:
    """(정리된 원래 이름, 확장자). 실패하면 FileRejected."""
    limits = settings.UPLOAD_LIMITS[limit_key]
    name = sanitize_name(getattr(f, "name", ""))
    ext = extension(name, limits["exts"])
    if not ext:
        raise FileRejected(f"허용되지 않는 파일 형식입니다 ({', '.join(limits['exts'])})",
                           f"File type not allowed ({', '.join(limits['exts'])})")
    size = getattr(f, "size", None) or 0
    if size <= 0:
        raise FileRejected("빈 파일입니다", "The file is empty")
    if size > limits["max_bytes"]:
        raise FileRejected(f"파일이 너무 큽니다(최대 {format_size(limits['max_bytes'])})",
                           f"File too large (max {format_size(limits['max_bytes'])})")
    ctype = (getattr(f, "content_type", "") or "").split(";")[0].strip().lower()
    if ctype not in MIME_ALLOWED[mime_group]:
        raise FileRejected(f"허용되지 않는 MIME 형식입니다 ({ctype})", f"MIME type not allowed ({ctype})")
    check_magic(f, ext)
    return name, ext


def store(submission, f, kind: str, original_name: str, ext: str, meta: dict | None = None) -> SubmissionFile:
    """var/media/uploads/<yyyy>/<mm>/<uuid><ext> 로 청크 복사하고 SHA-256 을 기록한다."""
    now = timezone.localtime()
    rel = Path("uploads") / f"{now:%Y}" / f"{now:%m}" / f"{uuid.uuid4().hex}{ext}"
    dest = Path(settings.MEDIA_ROOT) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    f.seek(0)
    with open(dest, "xb") as out:
        for chunk in f.chunks():
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    try:
        dest.chmod(0o640)
    except OSError:
        pass
    return SubmissionFile.objects.create(
        submission=submission, kind=kind, file=rel.as_posix(), original_name=original_name, size=size,
        sha256=digest.hexdigest(), content_type=(getattr(f, "content_type", "") or "")[:100], meta=meta or {})


def delete_path(rel: str) -> bool:
    """MEDIA_ROOT 기준 상대 경로의 파일을 지운다. 실패하면 OrphanFile 로 기록하고 False."""
    from ..models import OrphanFile

    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        return False
    try:
        if target.exists():
            target.unlink()
    except OSError as exc:
        obj, created = OrphanFile.objects.get_or_create(path=rel, defaults={"reason": str(exc)[:255]})
        if not created:
            obj.attempts += 1
            obj.reason = str(exc)[:255]
            obj.save(update_fields=["attempts", "reason", "last_tried_at"])
        return False
    OrphanFile.objects.filter(path=rel).delete()
    return True


def delete_stored(sf: SubmissionFile) -> None:
    """DB 행은 지우고, 파일 삭제가 실패하면(Windows 파일 잠금 등) OrphanFile 로 남겨 cleanup_media 가 다시 지운다."""
    rel = sf.file.name
    sf.delete()
    if rel:
        delete_path(rel)


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 ** 3:.1f} GB"
