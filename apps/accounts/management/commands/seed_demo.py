from django.core.management.base import BaseCommand

from apps.accounts.services import ensure_demo_accounts
from apps.cases.services import load_patients
from apps.core.management.commands.bootstrap import describe


class Command(BaseCommand):
    help = ("데모 계정 3개와 환자 시드(data/seed/patients.json)를 적재한다. 이미 적재된 데이터셋은 건너뛰며, "
            "--force 면 없는 레코드만 다시 만든다(기존 레코드는 수정하지 않음).")

    def add_arguments(self, parser):
        parser.add_argument("--accounts-only", action="store_true", help="계정만")
        parser.add_argument("--force", action="store_true", help="적재 기록이 있어도 없는 레코드를 다시 생성")

    def handle(self, *args, **opts):
        self.stdout.write("데모 계정: " + describe(ensure_demo_accounts(force=opts["force"])))
        if not opts["accounts_only"]:
            self.stdout.write("환자: " + describe(load_patients(force=opts["force"])))
