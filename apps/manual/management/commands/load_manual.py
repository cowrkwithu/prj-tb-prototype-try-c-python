from django.core.management.base import BaseCommand

from apps.manual.services import load_manual


class Command(BaseCommand):
    help = ("사용자 매뉴얼(data/seed/manual.json — 원본 manualContent.ts 변환 + 교정)을 1회 적재한다(SeedLog). "
            "--force: 시드 내용으로 다시 맞춘다(관리 사이트에서 고친 시드 소주제는 시드 값으로 덮어씀).")

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="적재 기록이 있어도 시드 내용으로 다시 맞춘다")

    def handle(self, *args, **opts):
        res = load_manual(force=opts["force"])
        if res.get("skipped"):
            self.stdout.write("이미 적재됨 — 건너뜀 (--force 로 시드 내용에 다시 맞춤)")
        else:
            self.stdout.write(self.style.SUCCESS(
                f"새 대주제 {res['sections']}, 새 소주제 {res['topics']}, 블록 {res['blocks']} 적재"))
