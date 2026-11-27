"""이미 적재된 샘플 입력 행(원본 sampleSchemas 의 row-*)의 연결 키를 시드 환자 형식으로 바꾼다.

COND-2024-KR-0001 → KR-2024-0001 (국가-연도-번호). 사용자가 입력한 행(REC-…)은 건드리지 않는다.
"""
import re

from django.db import migrations

COND = re.compile(r"^COND-(\d{4})-([A-Z]{2})-(\d+)$")


def forward(apps, schema_editor):
    SchemaRecord = apps.get_model("schemas", "SchemaRecord")
    for rec in SchemaRecord.objects.filter(record_id__startswith="row-", condition_id__startswith="COND-"):
        m = COND.match(rec.condition_id)
        if m:
            rec.condition_id = f"{m.group(2)}-{m.group(1)}-{m.group(3)}"
            rec.save(update_fields=["condition_id"])


class Migration(migrations.Migration):
    dependencies = [("schemas", "0001_initial")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
