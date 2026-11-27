from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.core.management.commands.bootstrap import describe
from apps.dictionary.services import load_dictionary


class Command(BaseCommand):
    help = ("통합 데이터사전 엑셀(v3.2.0)을 DB 에 적재한다. 이미 적재했으면 건너뛴다. "
            "--force 는 UDE 기준으로 갱신하고(삭제 없음), 엑셀에서 사라진 항목은 폐기(is_deprecated) 표시한다.")

    def add_arguments(self, parser):
        parser.add_argument("--path", default=str(settings.DICTIONARY_XLSX), help="엑셀 경로")
        parser.add_argument("--force", action="store_true", help="적재 기록이 있어도 다시 반영")

    def handle(self, *args, **opts):
        self.stdout.write(describe(load_dictionary(Path(opts["path"]), force=opts["force"])))
