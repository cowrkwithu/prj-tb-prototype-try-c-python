from django.apps import AppConfig


class CohortsConfig(AppConfig):
    name = "apps.cohorts"
    label = "cohorts"
    verbose_name = "데이터 탐색(코호트)"

    def ready(self):
        from .lookups import register

        register()
