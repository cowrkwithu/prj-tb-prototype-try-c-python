#!/bin/sh
# 실행: ./run.sh [포트]        개발 서버 (127.0.0.1 바인딩, 외부 공개는 TB_BIND=0.0.0.0)
#       ./run.sh prod [포트]   운영 모드 waitress (TB_DEBUG=0)
# 포트는 인자 > TB_PORT > 4363 순. 로직은 manage.py rundev / serve 에 있다.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "가상환경이 없습니다. 먼저 ./setup.sh 를 실행하세요." >&2
  exit 1
fi
if [ "$1" = "prod" ]; then
  shift
  export TB_DEBUG="${TB_DEBUG:-0}"
  if [ -n "$1" ]; then exec .venv/bin/python manage.py serve "$1"; fi
  exec .venv/bin/python manage.py serve
fi
if [ -n "$1" ]; then exec .venv/bin/python manage.py rundev "$1"; fi
exec .venv/bin/python manage.py rundev
