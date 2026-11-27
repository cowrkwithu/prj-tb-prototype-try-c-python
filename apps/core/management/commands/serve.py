import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def parse_port(raw, source: str) -> int:
    raw = str(raw).strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 65535):
        raise CommandError(f"잘못된 포트 {raw!r} ({source}) — 1~65535 사이의 숫자여야 합니다.")
    return int(raw)


class Command(BaseCommand):
    help = ("waitress 운영 서버 (Windows/Linux 공통, 정적 파일은 whitenoise). manage.py 가 TB_DEBUG 미지정 시 0 으로 실행한다. "
            "포트: 인자 > --port > TB_PORT > 4363, 바인딩: --host > TB_BIND > 0.0.0.0. 내부망 HTTP 전제 — "
            "외부 공개 시 HTTPS 리버스 프록시 뒤에 두고 TB_ALLOWED_HOSTS 를 지정한다.")

    def add_arguments(self, parser):
        parser.add_argument("port_arg", nargs="?", metavar="port", help="포트 (기본 TB_PORT 또는 4363)")
        # 기본값은 문자열로만 두고 검증은 handle() 에서 — 잘못된 TB_PORT 로 --help 까지 실패하지 않게
        parser.add_argument("--host", default=None)
        parser.add_argument("--port", default=None)
        parser.add_argument("--threads", type=int, default=8)

    def handle(self, *args, **opts):
        if opts["port_arg"] is not None:
            port = parse_port(opts["port_arg"], "인자")
        elif opts["port"] is not None:
            port = parse_port(opts["port"], "--port")
        elif os.environ.get("TB_PORT", "").strip():
            port = parse_port(os.environ["TB_PORT"], "TB_PORT")
        else:
            port = 4363
        host = (opts["host"] or os.environ.get("TB_BIND") or "0.0.0.0").strip()

        from waitress import serve

        from config.wsgi import application

        if settings.DEBUG:
            self.stderr.write(self.style.WARNING("경고: DEBUG=True 상태입니다. 운영 시 TB_DEBUG=0 을 설정하세요."))
        if not settings.ALLOWED_HOSTS_FROM_ENV:
            self.stderr.write(self.style.WARNING(
                "경고: TB_ALLOWED_HOSTS 가 설정되지 않아 모든 Host 헤더를 허용합니다(내부망 전제). "
                "외부에 공개한다면 TB_ALLOWED_HOSTS=도메인 과 HTTPS 리버스 프록시를 설정하세요."))
        self.stdout.write(f"waitress: http://{host}:{port}/  (Ctrl+C 종료)")
        # waitress 는 요청 본문을 앱에 넘기기 전에 버퍼링한다. 상한 = 단일 파일 최대(CT 2GB) + 16MB
        # (TB_MAX_REQUEST_BYTES 로 낮출 수 있음). 큰 본문의 임시 버퍼는 시스템 임시 폴더 대신 var/tmp 에 둔다.
        import tempfile

        tmp = settings.VAR_DIR / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(tmp)
        self.stdout.write(f"요청 본문 상한: {settings.UPLOAD_MAX_REQUEST_BYTES:,} bytes · 임시 버퍼: {tmp}")
        serve(application, host=host, port=port, threads=opts["threads"],
              max_request_body_size=settings.UPLOAD_MAX_REQUEST_BYTES)
