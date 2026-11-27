from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.accounts.services import ensure_demo_accounts
from apps.cases.services import load_patients
from apps.core.home_content import seed_home_content
from apps.dictionary.services import load_dictionary


def describe(res: dict) -> str:
    if res.get("skipped"):
        return "이미 적재됨 — 건너뜀 (재적재는 seed_demo --force / load_dictionary --force)"
    return ", ".join(f"{k} {v}" for k, v in res.items() if k not in ("skipped", "loaded"))


class Command(BaseCommand):
    help = ("migrate → 데모 계정 → 표준 데이터사전 → 환자 시드 → 홈 콘텐츠 → 코호트·데이터 구조 → 변이 카탈로그·샘플 영상. 데이터셋마다 최초 1회만 적재한다"
            "(SeedLog). 여러 번 실행해도 안전하며, 사용자가 지운 레코드를 되살리지 않는다.")

    def handle(self, *args, **opts):
        (settings.VAR_DIR / "media").mkdir(parents=True, exist_ok=True)
        (settings.VAR_DIR / "tmp").mkdir(parents=True, exist_ok=True)
        self.stdout.write("[1/5] migrate")
        call_command("migrate", interactive=False, verbosity=0)
        self.stdout.write("[2/5] 데모 계정: " + describe(ensure_demo_accounts()))
        self.stdout.write("[3/5] 표준 데이터사전: " + describe(load_dictionary(settings.DICTIONARY_XLSX)))
        self.stdout.write("[4/5] 환자 시드: " + describe(load_patients()))
        self.stdout.write("[5/5] 홈 콘텐츠(뉴스·게시판·공지·연구사업·관련 사이트): " + describe(seed_home_content()))
        from apps.cohorts.services import load_cohorts  # 환자·계정 뒤에 적재(소유자·스냅샷이 참조)
        self.stdout.write("[+] 저장 데이터셋(코호트): " + describe(load_cohorts()))
        from apps.schemas.services import load_sample_schemas  # 데이터사전 뒤에 적재(필드의 사전 참조 FK 연결)
        self.stdout.write("[+] 커스텀 데이터 구조(샘플 4종): " + describe(load_sample_schemas()))
        from apps.genomics.services import load_genomics  # 4단계 GAP — 유전자·변이 카탈로그
        self.stdout.write("[+] 유전체 변이 카탈로그: " + describe(load_genomics()))
        from apps.imaging.services import load_imaging_samples  # 4단계 RAP — 환자 시드 뒤(환자 영상 수 기준)
        self.stdout.write("[+] 공개 샘플 영상 검사: " + describe(load_imaging_samples()))
        self.stdout.write(self.style.SUCCESS("bootstrap 완료"))
