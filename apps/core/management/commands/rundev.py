import os

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from .serve import parse_port

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


class Command(BaseCommand):
    help = ("개발 서버(runserver). 기본 127.0.0.1 바인딩 — 외부 공개는 TB_BIND(예: 0.0.0.0) 로만 선택. "
            "포트: 인자 > --port > TB_PORT > 4363.")

    def add_arguments(self, parser):
        parser.add_argument("port_arg", nargs="?", metavar="port", help="포트 (기본 TB_PORT 또는 4363)")
        parser.add_argument("--bind", default=None)
        parser.add_argument("--port", default=None)

    def handle(self, *args, **opts):
        if opts["port_arg"] is not None:
            port = parse_port(opts["port_arg"], "인자")
        elif opts["port"] is not None:
            port = parse_port(opts["port"], "--port")
        elif os.environ.get("TB_PORT", "").strip():
            port = parse_port(os.environ["TB_PORT"], "TB_PORT")
        else:
            port = 4363
        bind = (opts["bind"] or os.environ.get("TB_BIND") or "127.0.0.1").strip()
        # 자동 리로더의 자식 프로세스(RUN_MAIN)에서는 경고를 반복하지 않는다
        if settings.DEBUG and bind not in LOOPBACK and os.environ.get("RUN_MAIN") != "true":
            self.stderr.write(self.style.WARNING(
                f"경고: DEBUG=True 상태로 {bind} 에 바인딩합니다. 같은 네트워크의 누구나 디버그 화면(설정·경로 노출)을 "
                "볼 수 있습니다. 외부 공개는 운영 모드(run prod)를 쓰세요."))
        host = f"[{bind}]" if ":" in bind and not bind.startswith("[") else bind
        call_command("runserver", f"{host}:{port}")
