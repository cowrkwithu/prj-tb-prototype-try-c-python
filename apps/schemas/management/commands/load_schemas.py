from django.core.management.base import BaseCommand

from apps.schemas.services import load_sample_schemas


class Command(BaseCommand):
    help = "샘플 커스텀 데이터 구조(data/seed/sampleSchemas.json: 구조 4·필드 48·입력 행 14)를 1회 적재한다(SeedLog)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="적재 기록이 있어도 없는 구조·행만 다시 넣는다")

    def handle(self, *args, **opts):
        res = load_sample_schemas(force=opts["force"])
        if res.get("skipped"):
            self.stdout.write("이미 적재됨 — 건너뜀 (--force 로 없는 구조·행만 다시 적재)")
        else:
            self.stdout.write(self.style.SUCCESS(f"구조 {res['schemas']}, 필드 {res['fields']}, 입력 행 {res['records']} 적재"))
            self.stdout.write(f"연결 키 변환: COND-YYYY-CC-NNNN → CC-YYYY-NNNN (시드 환자와 연결되지 않은 행 {res['link_unmatched']}건)")
