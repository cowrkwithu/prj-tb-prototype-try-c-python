"""var/.env 로더 — 표준 라이브러리만 사용(외부 dotenv 패키지 없음).

형식(한 줄에 하나):
    # 주석(줄 맨 앞, 앞 공백 허용)
    KEY=VALUE
    KEY="VALUE"   또는   KEY='VALUE'   (따옴표는 벗겨내고 안쪽은 그대로 — 이스케이프 처리 없음)
    KEY=          (빈 값)
규칙:
  - 이미 설정된 환경변수는 덮어쓰지 않는다(빈 값으로 설정된 것도 '설정됨' — 예: TB_DATABASE_URL= 로 SQLite 강제).
  - KEY 는 영문·숫자·밑줄(숫자로 시작 불가). 앞의 `export ` 는 허용(셸 파일과 겸용).
  - 따옴표 없는 값의 `#` 은 값의 일부다(줄 중간 주석 없음). 앞뒤 공백은 잘라낸다.
  - 형식이 틀린 줄은 무시하지 않고 줄 번호와 함께 EnvFileError — 값(비밀번호 등)은 메시지에 넣지 않는다.
  - 파일은 UTF-8(BOM 허용 — Windows 메모장 저장본).
"""
import os
import re
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EnvFileError(ValueError):
    pass


def parse_env_file(path: Path) -> dict:
    """파일을 읽어 {KEY: VALUE}. 파일이 없으면 빈 dict."""
    path = Path(path)
    if not path.is_file():
        return {}
    result = {}
    text = path.read_text(encoding="utf-8-sig")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep:
            raise EnvFileError(f"{path}:{lineno}: KEY=VALUE 형식이 아닙니다('=' 없음)")
        if not _KEY.fullmatch(key):
            raise EnvFileError(f"{path}:{lineno}: 변수 이름이 올바르지 않습니다(영문·숫자·밑줄만, 숫자로 시작 불가)")
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote):
                raise EnvFileError(f"{path}:{lineno}: 따옴표가 닫히지 않았습니다({key})")
            inner = value[1:-1]
            if quote in inner:
                raise EnvFileError(f"{path}:{lineno}: 따옴표 뒤에 다른 내용이 있습니다({key})")
            value = inner
        if key in result:
            raise EnvFileError(f"{path}:{lineno}: 같은 변수가 두 번 지정되었습니다({key})")
        result[key] = value
    return result


def load_env_file(path: Path, environ=None) -> list:
    """파일 값을 environ(기본 os.environ)에 넣되 이미 있는 키는 건드리지 않는다. 실제로 넣은 키 목록을 돌려준다."""
    environ = os.environ if environ is None else environ
    applied = []
    for key, value in parse_env_file(path).items():
        if key in environ:
            continue
        environ[key] = value
        applied.append(key)
    return applied
