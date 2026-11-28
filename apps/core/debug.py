"""DEBUG 오류 화면(technical_500)·오류 메일에서 숨길 값.

Django 기본 필터는 이름에 API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE 가 든 설정·META 만 가린다.
개발 서버(runserver)는 프로세스 환경변수 전체를 요청 META 에 복사하므로 var/.env 로 읽은 TB_DATABASE_URL
(비밀번호 포함)이 오류 화면에 그대로 보인다 — DB 접속 정보 계열 이름을 더 가린다.
"""
import re

from django.views.debug import SafeExceptionReporterFilter


class PortalExceptionReporterFilter(SafeExceptionReporterFilter):
    hidden_settings = re.compile(
        "API|AUTH|TOKEN|KEY|SECRET|PASS|PWD|PASSWD|SIGNATURE|HTTP_COOKIE|DATABASE|URL|DSN|PASSWORD|CREDENTIAL|CONN",
        flags=re.IGNORECASE)
