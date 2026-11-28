"""TB Research Portal (Python Edition) 설정.

경로는 모두 BASE_DIR 기준 pathlib. 런타임 산출물(DB·업로드·비밀키)은 var/ 아래에 둔다.
환경변수:
  TB_DEBUG          1/0 (기본 1 — 개발). run.sh prod 는 0 으로 실행한다.
  TB_SECRET_KEY     지정하지 않으면 var/secret_key 를 자동 생성·재사용한다.
  TB_ALLOWED_HOSTS  쉼표 구분 (기본 "*" — 내부망 HTTP 시범 운영 전제. 외부 공개 시 반드시 지정)
  TB_BIND / TB_PORT 서버 바인딩 주소·포트 (rundev 기본 127.0.0.1, serve 기본 0.0.0.0 / 포트 4363)
  TB_DATABASE_URL   지정하면 PostgreSQL (빈 값이면 SQLite)
var/.env 가 있으면 먼저 읽는다(KEY=VALUE, 이미 설정된 환경변수가 우선 — config/envfile.py 참고).
"""
import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent
VAR_DIR = BASE_DIR / "var"
VAR_DIR.mkdir(exist_ok=True)

from config.envfile import load_env_file  # noqa: E402

ENV_FILE = VAR_DIR / ".env"
ENV_FILE_APPLIED = load_env_file(ENV_FILE)  # 셸/OS 환경변수가 항상 우선(덮어쓰지 않음)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _load_secret_key() -> str:
    env = os.environ.get("TB_SECRET_KEY")
    if env:
        return env
    path = VAR_DIR / "secret_key"
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = get_random_secret_key()
    # 소유자만 읽기/쓰기(0600). Windows 에서는 권한 비트가 대부분 무시되지만 예외 없이 진행한다.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


SECRET_KEY = _load_secret_key()
DEBUG = _env_bool("TB_DEBUG", True)
ALLOWED_HOSTS_FROM_ENV = bool(os.environ.get("TB_ALLOWED_HOSTS", "").strip())
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("TB_ALLOWED_HOSTS", "*").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core",
    "apps.accounts",
    "apps.dictionary",
    "apps.cases",
    "apps.uploads",
    "apps.schemas",
    "apps.cohorts",
    "apps.datasharing",
    "apps.genomics",
    "apps.imaging",
    "apps.manual",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "apps.core.middleware.RejectSurrogatesMiddleware",  # 고립 서로게이트 입력 → 400 (CSRF 뒤: 본문 파싱 시점 유지)
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.DatabaseBusyMiddleware",  # DB 잠금(쓰기 경합) → 503 안내 (맨 끝: process_exception 이 가장 먼저)
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.portal",
            ],
            "builtins": ["apps.core.templatetags.portal"],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# 기본은 SQLite(WAL). ORM 만 사용하므로 PostgreSQL 은 환경변수로만 전환한다(코드 수정 불필요):
#   TB_DATABASE_URL=postgresql://<user>:<password>@<host>:<port>/<db>   (드라이버: requirements-postgres.txt)
#   TB_DB_LOCK_TIMEOUT_MS  잠금 대기 상한(기본 5000, 1 이상 — SQLite busy_timeout 과 같은 값, 넘으면 503 안내)
#   URL 쿼리 문자열은 libpq 옵션 sslmode·sslrootcert·sslcert·sslkey·connect_timeout·application_name 만(그 밖은 오류)
# URL 쿼리 문자열로 받는 libpq 접속 옵션(그 밖의 이름은 오타·미지원으로 보고 오류)
_PG_URL_OPTIONS = ("sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout", "application_name")


def _database_from_url(url: str) -> dict:
    from urllib.parse import parse_qsl, unquote, urlsplit

    u = urlsplit(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"TB_DATABASE_URL: 지원하지 않는 형식입니다 ({u.scheme!r}) — postgresql://… 만 지원")
    lock_ms = os.environ.get("TB_DB_LOCK_TIMEOUT_MS", "5000").strip()
    if not lock_ms.isdigit() or int(lock_ms) < 1:
        # 0 은 PostgreSQL 에서 '무제한 대기'라 503 안내가 동작하지 않는다
        raise ValueError("TB_DB_LOCK_TIMEOUT_MS 는 1 이상의 정수(밀리초)여야 합니다")
    options = {"options": f"-c lock_timeout={int(lock_ms)}"}
    try:
        pairs = parse_qsl(u.query, keep_blank_values=True, strict_parsing=bool(u.query))
    except ValueError:
        raise ValueError("TB_DATABASE_URL: 쿼리 문자열 형식이 올바르지 않습니다(이름=값&…)") from None
    for k, v in pairs:
        if k not in _PG_URL_OPTIONS:
            raise ValueError(f"TB_DATABASE_URL: 지원하지 않는 접속 옵션입니다 ({k!r}) — 지원: {', '.join(_PG_URL_OPTIONS)}")
        if k in options:
            raise ValueError(f"TB_DATABASE_URL: 접속 옵션이 두 번 지정되었습니다 ({k!r})")
        options[k] = v
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(u.path.lstrip("/")),
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),
        "HOST": u.hostname or "",
        "PORT": str(u.port or ""),
        "OPTIONS": options,
    }


_DB_URL = os.environ.get("TB_DATABASE_URL", "").strip()
DATABASES = {
    "default": _database_from_url(_DB_URL) if _DB_URL else {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": VAR_DIR / "db.sqlite3",
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

AUTH_PASSWORD_VALIDATORS = []  # 데모 계정(admin1234 등)을 그대로 쓰기 위해 비활성

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = False  # 다국어는 세션 기반 t() 헬퍼로 처리(gettext 미사용)
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = VAR_DIR / "staticfiles"
STATIC_ROOT.mkdir(exist_ok=True)  # whitenoise 가 없는 디렉터리를 경고하지 않도록
# collectstatic 없이도 waitress(prod)에서 static/ 을 바로 서빙한다.
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG

MEDIA_URL = "/media/"  # 업로드 파일은 공개 URL 로 서빙하지 않는다(권한 검사 뷰로만 내려받음)
MEDIA_ROOT = VAR_DIR / "media"

# ── 업로드 (2단계) ──
# 메모리에 2.5MB 까지만 두고, 그보다 큰 파일은 FILE_UPLOAD_TEMP_DIR 에 청크 단위로 기록된다(전체 메모리 적재 없음).
FILE_UPLOAD_TEMP_DIR = VAR_DIR / "tmp"
FILE_UPLOAD_TEMP_DIR.mkdir(exist_ok=True)
FILE_UPLOAD_MAX_MEMORY_SIZE = 2_621_440
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024     # 파일을 제외한 폼 필드 합계
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000
FILE_UPLOAD_PERMISSIONS = 0o640
FILE_UPLOAD_HANDLERS = [
    "apps.uploads.handlers.FileSizeLimitHandler",   # 파일 하나가 상한을 넘으면 수신을 중단한다
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]
_MB = 1024 * 1024
# 원본 FileDropzone 제한값: CSV 50MB(기본값)·1개, 시퀀스 500MB·개별 5개/일괄 50개,
# CXR 50MB·20개, CT/PET-CT 2GB·100개.
UPLOAD_LIMITS = {
    "csv": {"exts": [".csv", ".tsv"], "max_bytes": 50 * _MB, "max_files": 1},
    "sequence": {"exts": [".fasta", ".fa", ".fastq", ".fq", ".fasta.gz", ".fa.gz", ".fastq.gz", ".fq.gz"],
                 "max_bytes": 500 * _MB, "max_files": {"individual": 5, "batch": 50}},
    "image_cxr": {"exts": [".dcm", ".dicom", ".jpg", ".jpeg", ".png"], "max_bytes": 50 * _MB, "max_files": 20},
    "image_volume": {"exts": [".dcm", ".dicom"], "max_bytes": 2048 * _MB, "max_files": 100},
    # 관리자 데이터사전 엑셀 적재(/admin/dictionary) — 통합 데이터사전 v3.2.0 은 약 1MB
    "xlsx": {"exts": [".xlsx"], "max_bytes": 20 * _MB, "max_files": 1},
}
# 커스텀 데이터 구조 FILE 필드 첨부 상한(필드 제약 maxFileSizeMB 가 더 작으면 그것을 따른다)
SCHEMA_FILE_MAX_BYTES = 50 * _MB
# 사용자 1명이 제출 전(검사 단계) 상태로 쌓아 둘 수 있는 첨부 총량 — 디스크 소진 방지
SCHEMA_PENDING_MAX_BYTES = 200 * _MB
# 사용자 정의 정규식(데이터 구조 제약조건) 실행 시간 상한(초) — regex 패키지 timeout
SCHEMA_REGEX_TIMEOUT = 0.1
# 검사 1회(폼·CSV·수정·제출 재검증·미리보기·정의 저장) 단위 사용자 정규식 총 실행 시간 예산(초)
SCHEMA_REGEX_BUDGET_SECONDS = 2.0
# 업로드 핸들러가 적용하는 파일 1개 절대 상한(가장 큰 종류 기준). 종류별 상한은 서비스에서 다시 검사한다.
UPLOAD_MAX_FILE_BYTES = max(v["max_bytes"] for v in UPLOAD_LIMITS.values())
# 요청 1건의 본문 합계 상한 = 단일 파일 최대(CT 2GB) + 여유 16MB. 파일 업로드는 요청당 1개(화면이 순차 전송).
# serve(waitress)의 max_request_body_size 도 이 값 — TB_MAX_REQUEST_BYTES 로 낮출 수 있다(높이지는 못함).
UPLOAD_MAX_REQUEST_BYTES = UPLOAD_MAX_FILE_BYTES + 16 * _MB
_req_env = os.environ.get("TB_MAX_REQUEST_BYTES", "").strip()
if _req_env.isdigit() and 0 < int(_req_env) < UPLOAD_MAX_REQUEST_BYTES:
    UPLOAD_MAX_REQUEST_BYTES = int(_req_env)
UPLOAD_CSV_MAX_ROWS = 10_000
DICOM_PARSE_TIMEOUT_SECONDS = 2.0  # DICOM 헤더 판독 시간 상한(파일당) — 넘으면 판독 불가(제출 차단)

# ── 데이터 공유 (3c) ──
# 엑셀 내보내기 행 상한(엑셀 시트 최대 1,048,576행) — 넘으면 CSV(스트리밍)를 쓰도록 안내한다
DATASHARING_XLSX_MAX_ROWS = 1_000_000
# REST API(/api/v1) 키당 분당 요청 수 — 프로세스 메모리 캐시 기반(단일 프로세스 waitress 전제, services.rate_limit_hit 참고)
API_RATE_LIMIT_PER_MINUTE = 60
# 키 없음/잘못된 키 요청의 클라이언트(REMOTE_ADDR)당 분당 허용 수 — 넘으면 그 창 동안 429(services.auth_fail_hit)
API_AUTH_FAIL_LIMIT_PER_MINUTE = 30
# POST /api/v1/cohorts 본문 상한
API_MAX_BODY_BYTES = 64 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# DEBUG 오류 화면에서 DB 접속 URL(비밀번호) 등 환경변수를 가린다(apps/core/debug.py)
DEFAULT_EXCEPTION_REPORTER_FILTER = "apps.core.debug.PortalExceptionReporterFilter"

# 시드 원본
SEED_DIR = BASE_DIR / "data" / "seed"
DICTIONARY_XLSX = SEED_DIR / "261031-4개원천데이터사전_통합데이터사전_v3.2.0.xlsx"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO" if DEBUG else "WARNING"},
}
