#!/bin/sh
# 설치: .venv 생성(없으면) → 의존성 설치 → DB 마이그레이션·시드(bootstrap). 여러 번 실행해도 안전.
set -e
cd "$(dirname "$0")"
PIP_VERSION="26.3"   # pip 자체 버전 고정 (requirements.txt 와 별도)
PY="${PYTHON:-python3}"
VERSION_CHECK='import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'

if [ ! -x .venv/bin/python ]; then
  if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[setup] 오류: '$PY' 를 찾을 수 없습니다. Python 3.10 이상을 설치하거나 PYTHON=/경로/python3 으로 지정하세요." >&2
    exit 1
  fi
  if ! "$PY" -c "$VERSION_CHECK"; then
    echo "[setup] 오류: Python 3.10 이상이 필요합니다 (현재: $("$PY" -V 2>&1)). PYTHON=/경로/python3.x 로 지정하세요." >&2
    exit 1
  fi
  echo "[setup] 가상환경 생성 (.venv, $("$PY" -V 2>&1))"
  "$PY" -m venv .venv
fi
if ! .venv/bin/python -c "$VERSION_CHECK"; then
  echo "[setup] 오류: .venv 의 Python 이 3.10 미만입니다 ($(.venv/bin/python -V 2>&1)). .venv 를 지우고 다시 실행하세요." >&2
  exit 1
fi
.venv/bin/python -m pip install "pip==$PIP_VERSION"
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py bootstrap
echo "[setup] 완료 — 실행: ./run.sh [포트]  (운영 모드: ./run.sh prod [포트])"
