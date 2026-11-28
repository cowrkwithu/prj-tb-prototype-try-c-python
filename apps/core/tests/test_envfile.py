"""var/.env 로더(config/envfile.py) — 표준 라이브러리 KEY=VALUE 파서."""
import pytest

from config.envfile import EnvFileError, load_env_file, parse_env_file


def _write(tmp_path, text, name=".env", bom=False):
    p = tmp_path / name
    p.write_bytes((("﻿" if bom else "") + text).encode("utf-8"))
    return p


def test_missing_file_is_noop(tmp_path):
    env = {}
    assert parse_env_file(tmp_path / "nope.env") == {}
    assert load_env_file(tmp_path / "nope.env", env) == []
    assert env == {}


def test_comments_blank_lines_and_quotes(tmp_path):
    p = _write(tmp_path, (
        "# 로컬 전용 설정\n"
        "\n"
        "   # 들여쓴 주석\n"
        "A=plain value  \n"
        'B="double # not comment"\n'
        "C='single \"inner\"'\n"
        "D=\n"
        "export E=exported\r\n"
        "F=x=y=z\n"
        "G=pass#word\n"
        "H=한글값\n"
    ), bom=True)
    assert parse_env_file(p) == {
        "A": "plain value", "B": "double # not comment", "C": 'single "inner"', "D": "",
        "E": "exported", "F": "x=y=z", "G": "pass#word", "H": "한글값",
    }


def test_does_not_override_existing(tmp_path):
    p = _write(tmp_path, "TB_X=from_file\nTB_Y=from_file\nTB_Z=from_file\n")
    env = {"TB_X": "from_shell", "TB_Z": ""}  # 빈 값도 '설정됨' — 파일이 덮어쓰지 않는다(SQLite 강제용)
    applied = load_env_file(p, env)
    assert env == {"TB_X": "from_shell", "TB_Y": "from_file", "TB_Z": ""}
    assert applied == ["TB_Y"]


def test_defaults_to_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("TB_ENVFILE_TEST", raising=False)
    p = _write(tmp_path, "TB_ENVFILE_TEST=1\n")
    load_env_file(p)
    import os
    try:
        assert os.environ["TB_ENVFILE_TEST"] == "1"
    finally:
        os.environ.pop("TB_ENVFILE_TEST", None)


@pytest.mark.parametrize("line, fragment", [
    ("JUSTTEXT", "'=' 없음"),
    ("1BAD=x", "변수 이름"),
    ("BAD KEY=x", "변수 이름"),
    ("=x", "변수 이름"),
    ('Q="unterminated', "닫히지"),
    ("Q='", "닫히지"),
    ('Q="a"b"', "따옴표 뒤"),
])
def test_malformed_lines_raise_with_line_number(tmp_path, line, fragment):
    p = _write(tmp_path, "OK=1\n" + line + "\n")
    with pytest.raises(EnvFileError) as ei:
        parse_env_file(p)
    msg = str(ei.value)
    assert ":2:" in msg and fragment in msg


def test_error_message_does_not_leak_value(tmp_path):
    p = _write(tmp_path, 'TB_DATABASE_URL="postgresql://u:s3cr3t@h/db\n')
    with pytest.raises(EnvFileError) as ei:
        parse_env_file(p)
    assert "s3cr3t" not in str(ei.value)


def test_duplicate_key_rejected(tmp_path):
    p = _write(tmp_path, "A=1\nA=2\n")
    with pytest.raises(EnvFileError, match="두 번"):
        parse_env_file(p)


def test_settings_uses_var_env_path():
    from django.conf import settings

    assert settings.ENV_FILE == settings.BASE_DIR / "var" / ".env"
