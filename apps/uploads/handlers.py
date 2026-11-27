"""업로드 크기 상한을 수신 단계에서 강제하는 업로드 핸들러(가장 앞에 둔다).

- 요청 합계 상한: settings.UPLOAD_MAX_REQUEST_BYTES 와 엔드포인트별 상한 중 작은 값.
  · CSV 단계(/upload/<도메인>/csv): CSV 상한
  · 파일 단계(/upload/genomic|imaging/files): 요청당 파일 1개 → 개별 상한 + 여유
  · 그 밖의 경로: 작은 기본 상한(파일 업로드를 받지 않는 화면)
  Content-Length 가 상한을 넘으면 첫 파일부터 받지 않고(연결 재설정 없이 남은 본문은 버림) 중단한다.
- 파일 1개 상한: 엔드포인트별 개별 상한. 넘는 순간 수신을 멈춘다.
중단되면 request.upload_aborted = True. Django 가 진행 중이던 임시 파일(var/tmp)을 닫아 지운다.
종류별 정확한 상한(CXR 50MB 등)은 서비스(files.validate_upload)가 다시 검사한다.
"""
import re

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload

_SLACK = 1024 * 1024  # 폼 필드·멀티파트 경계 여유
_CSV = re.compile(r"^/upload/(clinical|regimen|lab|genomic|imaging)/csv/?$")
_FILES = re.compile(r"^/upload/(genomic|imaging)/files/?$")
# 커스텀 데이터 구조 입력 검사(CSV 또는 FILE 필드 첨부) · 관리자 데이터사전 엑셀 적재
_SCHEMA_CHECK = re.compile(r"^/schemas/[A-Za-z0-9_-]{1,40}/data/check/?$")
_DICT_XLSX = re.compile(r"^/admin/dictionary/version/upload/?$")
DEFAULT_LIMIT = 10 * 1024 * 1024


def endpoint_limits(path: str) -> tuple[int, int]:
    """(요청 합계 상한, 파일 1개 상한)."""
    lim = settings.UPLOAD_LIMITS
    cap = settings.UPLOAD_MAX_REQUEST_BYTES
    if _CSV.match(path):
        per = lim["csv"]["max_bytes"]
        return min(cap, per + _SLACK), per
    m = _FILES.match(path)
    if m:
        if m.group(1) == "genomic":
            per = lim["sequence"]["max_bytes"]
        else:
            per = max(lim["image_cxr"]["max_bytes"], lim["image_volume"]["max_bytes"])
        return min(cap, per + _SLACK), min(per, settings.UPLOAD_MAX_FILE_BYTES)
    if _SCHEMA_CHECK.match(path):
        per = max(lim["csv"]["max_bytes"], settings.SCHEMA_FILE_MAX_BYTES)
        return min(cap, per * 2 + _SLACK), per  # 폼 한 건에 FILE 필드 여러 개 — 종류별 상한은 서비스가 다시 검사
    if _DICT_XLSX.match(path):
        per = lim["xlsx"]["max_bytes"]
        return min(cap, per + _SLACK), per
    return min(cap, DEFAULT_LIMIT), min(DEFAULT_LIMIT, settings.UPLOAD_MAX_FILE_BYTES)


class FileSizeLimitHandler(FileUploadHandler):
    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        path = getattr(self.request, "path_info", "") if self.request is not None else ""
        self.total_limit, self.file_limit = endpoint_limits(path)
        self.too_big = bool(content_length and content_length > self.total_limit)
        if self.too_big:
            self._abort()
        return None

    def _abort(self):
        if self.request is not None:
            self.request.upload_aborted = True

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self._received = 0
        if getattr(self, "too_big", False):
            raise StopUpload(connection_reset=False)

    def receive_data_chunk(self, raw_data, start):
        self._received = getattr(self, "_received", 0) + len(raw_data)
        limit = getattr(self, "file_limit", settings.UPLOAD_MAX_FILE_BYTES)
        if self._received > min(limit, settings.UPLOAD_MAX_FILE_BYTES):
            self._abort()
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None
