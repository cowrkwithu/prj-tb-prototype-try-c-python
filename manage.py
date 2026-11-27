#!/usr/bin/env python
"""Django 관리 명령 진입점."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # 운영 서버(serve)는 TB_DEBUG 를 따로 지정하지 않으면 DEBUG=0 으로 띄운다(설정 로드 전에 결정)
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        os.environ.setdefault("TB_DEBUG", "0")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
