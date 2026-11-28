"""커스텀 데이터 구조 서비스 — 권한·저장·상태 전이·데이터 입력·내보내기·시드.

원본 규칙 출처
- 상태 전이(SchemaManager.actionsFor): 초안→발행 / 발행됨→보관(데이터 0건이면 초안으로 되돌리기도) / 보관됨→복원(발행)
- 삭제(SchemaManager): 입력 데이터가 있으면 삭제 불가(보관 권장)
- 데이터 입력(SchemaCard): 발행된 구조만
- 공개 범위(manualContent): 비공개/기관 공유/전체 공개에 따라 다른 연구자에게 노출되는 범위가 달라진다
- 버전(sampleSchemas·SchemaDetail 버전 이력, 복제 시 1.0): 발행된 구조를 고쳐 저장하면 버전을 올린다
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Sum
from django.db.models.fields.json import KeyTransform
from django.utils import timezone

from apps.core.filetx import delete_after_commit, register_new_file
from apps.core.safejson import has_bad_chars, loads_user_json
from apps.core.seeding import is_seeded, mark_seeded
from apps.uploads.services import csvio, files as upload_files

from . import validation
from .catalog import LINK_FIELD
from .models import PendingEntry, Schema, SchemaField, SchemaFile, SchemaLog, SchemaRecord

DATASET = "schemas"
SEED_FILE = "sampleSchemas.json"
PENDING_TTL = timedelta(hours=24)


class SchemaError(Exception):
    def __init__(self, ko: str, en: str, errors: list | None = None):
        super().__init__(ko)
        self.ko, self.en = ko, en
        self.errors = errors or []


# ═══════════════════════════════════════════════════════════
#  권한
# ═══════════════════════════════════════════════════════════
def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and getattr(user, "is_portal_admin", False))


def visible_q(user) -> Q:
    """열람 범위: 관리자 전체 / 작성자 본인 / 전체 공개 / 기관 공유(같은 소속 기관)."""
    if is_admin(user):
        return Q()
    q = Q(visibility=Schema.Visibility.PUBLIC) | Q(owner=user)
    org = (getattr(user, "organization", "") or "").strip()
    if org:
        q |= Q(visibility=Schema.Visibility.SHARED, owner_institution=org)
    return q


def visible_schemas(user):
    return Schema.objects.filter(visible_q(user))


def can_view(user, schema: Schema) -> bool:
    return visible_schemas(user).filter(pk=schema.pk).exists()


def is_owner(user, schema: Schema) -> bool:
    return bool(user and user.is_authenticated and schema.owner_id == user.pk)


def can_manage(user, schema: Schema) -> bool:
    """구조 편집·보관·삭제: 작성자·관리자."""
    return is_owner(user, schema) or is_admin(user)


def can_edit_structure(user, schema: Schema) -> bool:
    return can_manage(user, schema) and schema.status != Schema.Status.ARCHIVED


def can_enter_data(user, schema: Schema) -> bool:
    return schema.status == Schema.Status.PUBLISHED and can_view(user, schema)


def can_modify_record(user, record: SchemaRecord) -> bool:
    """행 수정·삭제: 행 작성자, 데이터 구조 작성자, 관리자 (발행 상태에서만)."""
    if record.schema.status != Schema.Status.PUBLISHED:
        return False
    if is_admin(user) or is_owner(user, record.schema):
        return True
    return bool(user and user.is_authenticated and record.author_id == user.pk)


def modifiable_records(user, schema: Schema):
    qs = schema.records.all()
    if is_admin(user) or is_owner(user, schema):
        return qs
    return qs.filter(author=user)


# ═══════════════════════════════════════════════════════════
#  목록·집계
# ═══════════════════════════════════════════════════════════
def with_counts(qs):
    """필드 수·데이터 건수·사용자 수(입력한 서로 다른 작성자 수)를 한 쿼리로 (N+1 방지)."""
    fields = SchemaField.objects.filter(schema=OuterRef("pk")).order_by().values("schema").annotate(n=Count("id")).values("n")
    recs = SchemaRecord.objects.filter(schema=OuterRef("pk")).order_by().values("schema").annotate(n=Count("id")).values("n")
    users = (SchemaRecord.objects.filter(schema=OuterRef("pk")).order_by().values("schema")
             .annotate(n=Count("author_label", distinct=True)).values("n"))
    dict_fields = (SchemaField.objects.filter(schema=OuterRef("pk")).exclude(source_dictionary_id="").order_by()
                   .values("schema").annotate(n=Count("id")).values("n"))
    from django.db.models import IntegerField, Subquery
    from django.db.models.functions import Coalesce

    return qs.annotate(
        field_count=Coalesce(Subquery(fields, output_field=IntegerField()), 0),
        data_count=Coalesce(Subquery(recs, output_field=IntegerField()), 0),
        usage_count=Coalesce(Subquery(users, output_field=IntegerField()), 0),
        dict_field_count=Coalesce(Subquery(dict_fields, output_field=IntegerField()), 0),
    )


def search_schemas(qs, q: str):
    """원본 SchemaExplorer — 화면 언어와 상관없이 한·영 이름·설명·태그에서 찾는다(대소문자 무시)."""
    q = (q or "").strip().lower()
    if not q:
        return list(qs)
    out = []
    for s in qs:
        texts = [s.name, s.name_en, s.description, s.description_en, *s.tags, *s.tags_en,
                 s.owner_name, s.owner_name_en, s.owner_institution]
        if any(q in (t or "").lower() for t in texts):
            out.append(s)
    return out


def custom_schema_stats() -> dict:
    """홈·관리자 대시보드: 활성(발행) 데이터 구조 수와 그 입력 데이터 건수."""
    active = Schema.objects.filter(status=Schema.Status.PUBLISHED)
    return {"active": active.count(),
            "records": SchemaRecord.objects.filter(schema__status=Schema.Status.PUBLISHED).count()}


# ═══════════════════════════════════════════════════════════
#  구조 저장 (빌더)
# ═══════════════════════════════════════════════════════════
def new_schema_id() -> str:
    return f"SCH-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def bump_version(v: str) -> str:
    """'1.2' → '1.3', '2' → '2.1'."""
    m = re.match(r"^(\d+)(?:\.(\d+))?", v or "1.0")
    if not m:
        return "1.1"
    return f"{m.group(1)}.{int(m.group(2) or 0) + 1}"


def _actor(user):
    return (getattr(user, "shown_name", None) or getattr(user, "username", "")) if user else ""


def log(schema, action, user=None, *, version="", detail="", detail_en="", note="", by_admin=False, at=None):
    return SchemaLog.objects.create(schema=schema, at=at or timezone.now(), actor=user if user and user.pk else None,
                                    actor_label=_actor(user), action=action, version=version or schema.version,
                                    detail=detail[:500], detail_en=detail_en[:500], note=note[:500], by_admin=by_admin)


def resolve_dictionary_field(dictionary_id: str, fallback=None):
    from apps.dictionary.models import DictionaryField
    from apps.dictionary.services import active_version

    if not dictionary_id:
        return None
    ver = active_version()
    if ver:
        hit = DictionaryField.objects.filter(version=ver, dictionary_id=dictionary_id).first()
        if hit:
            return hit
    if fallback is not None and fallback.dictionary_id == dictionary_id:
        return fallback
    return DictionaryField.objects.filter(dictionary_id=dictionary_id).order_by("-version__loaded_at").first()


def _source_modified(f: dict, dfield) -> bool:
    if dfield is None:
        return f["source_modified"]
    base = dict(dfield.constraints or {})
    if dfield.options:
        base["options"] = dfield.options
    return (f["source_modified"] or f["data_type"] != dfield.data_type or f["display_name"] != dfield.display_name
            or f["field_name"] != dfield.field_name
            or {k: v for k, v in f["constraints"].items()} != {k: v for k, v in base.items() if v not in (None, "", [])})


def fields_with_data(schema: Schema) -> set[str]:
    """입력 데이터에 값이 하나라도 있는 필드명."""
    names = set(schema.fields.values_list("field_name", flat=True))
    return {n for n in names if schema.records.filter(data__has_key=n).exists()}


def _derive_groups(meta_groups: list, fields: list, previous: list) -> list:
    known = {g["groupName"]: g for g in (previous or [])}
    known.update({g["groupName"]: g for g in meta_groups})
    out, seen = [], set()
    for f in fields:
        g = f["group"]
        if g and g not in seen:
            seen.add(g)
            base = dict(known.get(g) or {"groupName": g, "displayName": g})
            base["order"] = len(out) + 1
            out.append(base)
    return out


def _structure_signature(fields, rules, link_field) -> str:
    """입력 검증에 영향을 주는 구조(필드·타입·제약·필수·고유·기본값·검증 규칙·연결 키)의 지문.
    이름·설명·공개 범위·태그 같은 메타 변경은 포함하지 않는다(진행 중인 입력을 무효화하지 않음)."""
    rows = []
    for f in fields:
        g = (lambda k: f[k]) if isinstance(f, dict) else (lambda k: getattr(f, k))
        rows.append([g("field_key"), g("field_name"), g("data_type"), g("constraints"), g("is_required"),
                     g("is_unique"), g("default_value")])
    rows.sort(key=lambda r: str(r[0]))
    return json.dumps([rows, rules or [], link_field], sort_keys=True, ensure_ascii=False, default=str)


@transaction.atomic
def save_definition(user, payload: dict, *, schema: Schema | None = None, action: str = "save",
                    expected_rev: int | None = None, ctx: validation.CheckContext | None = None) -> Schema:
    """빌더 저장. action: draft(임시저장) | save(저장) | publish(발행). 실패하면 SchemaError(errors 포함).

    임시저장(너그러운 검증)은 신규 또는 초안 상태 구조에만 적용된다. 이미 발행된 구조에 action=draft 로 저장해도
    발행 수준(strict) 검증을 거치고 상태는 그대로 둔다(발행 상태에서 미완성 정의가 저장되는 우회 방지)."""
    strict = action != "draft" or (schema is not None and schema.status != Schema.Status.DRAFT)
    # 이미 저장돼 있던 필드명은 새 이름 규칙(연속·끝 밑줄 금지) 이전에 만들어졌을 수 있다 — 그대로 둘 수 있게 한다
    legacy = set(schema.fields.values_list("field_name", flat=True)) if schema is not None else set()
    # ctx: 호출 측이 같은 정의를 이미 검사했다면(가져오기 확정) 그 검사의 CheckContext 를 넘겨 정규식 판정·예산을 재사용한다
    meta, fields, errors = validation.clean_definition(payload, strict=strict, ctx=ctx or validation.CheckContext(),
                                                       legacy_names=legacy)
    if errors:
        raise SchemaError("입력 내용을 확인하세요", "Please check your input", errors)
    now = timezone.now()
    link = LINK_FIELD[meta["link_field_name"]]
    common = dict(name=meta["name"], name_en=meta["name_en"], description=meta["description"],
                  description_en=meta["description_en"], category=meta["category"], visibility=meta["visibility"],
                  link_field_name=link[0], link_display_name=link[3], link_display_name_en=link[4],
                  link_description=link[5], link_description_en=link[6], tags=meta["tags"], tags_en=meta["tags_en"],
                  validation_rules=meta["validation_rules"])
    if schema is None:
        schema = Schema.objects.create(
            schema_id=new_schema_id(), owner=user, owner_name=_actor(user),
            owner_institution=(getattr(user, "organization", "") or ""), created_at=now, updated_at=now,
            field_groups=_derive_groups(meta["field_groups"], fields, []), **common)
        old_fields = {}
        log(schema, SchemaLog.Action.CREATE, user, detail=f"데이터 구조 생성 (필드 {len(fields)}개)",
            detail_en=f"Created data schema ({len(fields)} fields)")
    else:
        if schema.status == Schema.Status.ARCHIVED:
            raise SchemaError("보관된 데이터 구조는 수정할 수 없습니다. 먼저 복원하세요.",
                              "Archived data schemas cannot be edited. Restore it first.")
        old_fields = {f.field_key: f for f in schema.fields.select_related("dictionary_field")}
        old_by_name = {f.field_name: f for f in old_fields.values()}
        # 먼저 rev 조건부 UPDATE 로 행을 잡고(같은 트랜잭션), 그다음 입력 데이터를 다시 확인한다 —
        # 확인과 필드 삭제 사이에 데이터가 들어오는 경합을 막는다(제출은 rev 가 같을 때만 기록).
        was_published = schema.status == Schema.Status.PUBLISHED
        new_version = bump_version(schema.version) if was_published else schema.version
        rev = schema.rev if expected_rev is None else expected_rev
        structure_changed = _structure_signature(old_fields.values(), schema.validation_rules, schema.link_field_name) != \
            _structure_signature(fields, meta["validation_rules"], link[0])
        updated = Schema.objects.filter(pk=schema.pk, rev=rev, status=schema.status).update(
            updated_at=now, version=new_version, rev=F("rev") + 1,
            structure_rev=F("structure_rev") + (1 if structure_changed else 0),
            field_groups=_derive_groups(meta["field_groups"], fields, schema.field_groups), **common)
        if not updated:
            raise SchemaError("다른 사용자가 먼저 이 데이터 구조를 수정했습니다. 새로고침한 뒤 다시 시도하세요.",
                              "Someone else modified this data schema first. Reload and try again.")
        with_data = fields_with_data(schema)
        blocked = []
        new_by_key = {f["field_key"]: f for f in fields}
        for name in sorted(with_data):
            old = old_by_name[name]
            new = new_by_key.get(old.field_key)
            if new is None:
                blocked.append(validation._msg(f"{name}: 입력된 데이터가 있는 필드는 삭제할 수 없습니다",
                                               f"{name}: fields with entered data cannot be deleted"))
            elif new["field_name"] != old.field_name:
                blocked.append(validation._msg(f"{name}: 입력된 데이터가 있는 필드의 필드명은 바꿀 수 없습니다",
                                               f"{name}: cannot rename a field that has data"))
            elif new["data_type"] != old.data_type:
                blocked.append(validation._msg(f"{name}: 입력된 데이터가 있는 필드의 데이터 타입은 바꿀 수 없습니다",
                                               f"{name}: cannot change the data type of a field that has data"))
        if blocked:
            raise SchemaError("입력된 데이터와 맞지 않는 구조 변경입니다", "The change conflicts with entered data", blocked)
        schema.refresh_from_db()
        log(schema, SchemaLog.Action.SAVE, user,
            detail=(f"버전 {new_version} 저장 (필드 {len(fields)}개)" if was_published else f"초안 저장 (필드 {len(fields)}개)"),
            detail_en=(f"Saved version {new_version} ({len(fields)} fields)" if was_published else f"Draft saved ({len(fields)} fields)"))
    # 필드는 field_key 기준으로 다시 만든다(입력 데이터는 field_name 을 키로 쓰므로 영향 없음)
    schema.fields.all().delete()
    objs = []
    for f in fields:
        prev = old_fields.get(f["field_key"])
        dfield = resolve_dictionary_field(f["source_dictionary_id"], prev.dictionary_field if prev else None)
        if f["source_dictionary_id"] and dfield is not None:
            f["source_modified"] = _source_modified(f, dfield)
            f["source_field_name"] = f["source_field_name"] or dfield.field_name
        objs.append(SchemaField(schema=schema, dictionary_field=dfield, **f))
    SchemaField.objects.bulk_create(objs)
    if action == "publish" and schema.status == Schema.Status.DRAFT:
        transition(schema, "publish", user, checked=True)  # 위에서 같은 strict 검증을 마쳤다
        schema.refresh_from_db()
    return schema


# ═══════════════════════════════════════════════════════════
#  상태 전이 (원본 SchemaManager)
# ═══════════════════════════════════════════════════════════
TRANSITIONS = {
    "publish": (Schema.Status.DRAFT, Schema.Status.PUBLISHED),
    "archive": (Schema.Status.PUBLISHED, Schema.Status.ARCHIVED),
    "restore": (Schema.Status.ARCHIVED, Schema.Status.PUBLISHED),
    "to_draft": (Schema.Status.PUBLISHED, Schema.Status.DRAFT),
}
ACTION_LABEL = {
    "publish": ("발행", "Published"), "archive": ("보관", "Archived"),
    "restore": ("보관 해제(발행 상태로 복원)", "Restored to published"), "to_draft": ("초안으로 되돌림", "Returned to draft"),
    "visibility": ("공개 범위 변경", "Visibility changed"),
}


def actions_for(schema: Schema, data_count: int | None = None) -> list[str]:
    if data_count is None:
        data_count = getattr(schema, "data_count", None)
        if data_count is None:
            data_count = schema.records.count()
    if schema.status == Schema.Status.DRAFT:
        return ["publish"]
    if schema.status == Schema.Status.PUBLISHED:
        return ["archive"] if data_count > 0 else ["archive", "to_draft"]
    return ["restore"]


def publish_problems(schema: Schema) -> list[dict]:
    """저장된 구조를 빌더 '발행'과 같은 strict 검증으로 다시 검사한다(임시저장은 너그럽게 저장되므로).
    이미 저장된 필드명은 새 이름 규칙의 예외로 둔다."""
    fields = list(schema.fields.all())
    _, _, errors = validation.clean_definition(
        schema_to_json(schema, fields), strict=True, ctx=validation.CheckContext(),
        legacy_names={f.field_name for f in fields})
    return errors


@transaction.atomic
def transition(schema: Schema, action: str, user, note: str = "", by_admin: bool = False, *,
               checked: bool = False) -> Schema:
    """조건부 UPDATE — 현재 상태가 기대와 같을 때만 바꾼다(동시 처리 시 한 번만 반영).
    결과 상태가 발행인 전이(publish·restore)는 strict 검증을 통과해야 한다(checked=True: 호출 측이 방금 같은 검증을 마침)."""
    if action not in TRANSITIONS:
        raise SchemaError("알 수 없는 조치입니다", "Unknown action")
    from apps.core.textinput import clean_free_text, free_text_problem

    note = clean_free_text(note)
    problem = free_text_problem(note, 500)  # 자유 텍스트 공통 검사(NUL·서로게이트·길이) — 상태를 바꾸기 전에
    if problem:
        raise SchemaError(*problem)
    src, dst = TRANSITIONS[action]
    # 결과 상태가 발행(published)인 모든 전이(발행·보관 복원)는 같은 strict 검증을 거친다
    to_published = dst == Schema.Status.PUBLISHED
    if to_published and not schema.fields.exists():
        raise SchemaError("필드가 없는 데이터 구조는 발행할 수 없습니다", "A data schema without fields cannot be published")
    now = timezone.now()
    qs = Schema.objects.filter(pk=schema.pk, status=src)
    if to_published and not checked:
        fresh = Schema.objects.filter(pk=schema.pk).first()
        errors = publish_problems(fresh) if fresh is not None else []
        if errors:
            verb_ko, verb_en = ("복원", "restore") if action == "restore" else ("발행", "publish")
            raise SchemaError(
                f"{verb_ko}할 수 없습니다 — 데이터 구조 정의를 먼저 고치세요: " + "; ".join(e["ko"] for e in errors[:5]),
                f"Cannot {verb_en} — fix the data schema definition first: " + "; ".join(e["en"] for e in errors[:5]),
                errors)
        if fresh is not None:
            qs = qs.filter(rev=fresh.rev)  # 검사한 그 구조가 그대로일 때만 발행(검사 뒤 동시 수정 방지)
    if action == "to_draft":
        qs = qs.filter(~Exists(SchemaRecord.objects.filter(schema=OuterRef("pk"))))
    extra = {}
    if action in ("publish", "restore"):
        extra["published_at"] = schema.published_at or now
    if action == "to_draft":
        extra["published_at"] = None
    if not qs.update(status=dst, updated_at=now, rev=F("rev") + 1, **extra):
        schema.refresh_from_db()
        if action == "to_draft" and schema.status == src:
            raise SchemaError("입력된 데이터가 있어 초안으로 되돌릴 수 없습니다",
                              "Cannot return to draft: data has been entered")
        raise SchemaError("이미 다른 사용자가 상태를 바꿨습니다. 새로고침한 뒤 다시 확인하세요.",
                          "The status was already changed by someone else. Reload and check again.")
    schema.refresh_from_db()
    ko, en = ACTION_LABEL[action]
    log(schema, action, user, detail=ko, detail_en=en, note=note, by_admin=by_admin)
    return schema


@transaction.atomic
def change_visibility(schema: Schema, visibility: str, user, by_admin: bool = False) -> bool:
    if visibility not in Schema.Visibility.values:
        raise SchemaError("공개 범위가 올바르지 않습니다", "Invalid visibility")
    old = schema.visibility
    if old == visibility:
        return False
    if not Schema.objects.filter(pk=schema.pk, visibility=old).update(visibility=visibility, updated_at=timezone.now(),
                                                                       rev=F("rev") + 1):
        raise SchemaError("이미 다른 사용자가 공개 범위를 바꿨습니다", "Visibility was already changed by someone else")
    schema.refresh_from_db()
    log(schema, SchemaLog.Action.VISIBILITY, user, detail=f"{old}→{visibility}", detail_en=f"{old}→{visibility}",
        by_admin=by_admin)
    return True


@transaction.atomic
def delete_schema(schema: Schema) -> None:
    """입력 데이터가 있으면 삭제 불가(원본 SchemaManager). 조건부 DELETE + SchemaRecord.schema PROTECT 이중 방어."""
    paths = list(SchemaFile.objects.filter(schema=schema).values_list("file", flat=True))
    deleted, _ = (Schema.objects.filter(pk=schema.pk)
                  .filter(~Exists(SchemaRecord.objects.filter(schema=OuterRef("pk")))).delete())
    if not deleted:
        if Schema.objects.filter(pk=schema.pk).exists():
            raise SchemaError("입력된 데이터가 있어 삭제할 수 없습니다. 더 이상 쓰지 않으려면 보관하세요.",
                              "Cannot delete: data has been entered. Archive it instead.")
        raise SchemaError("이미 삭제된 데이터 구조입니다", "The data schema was already deleted")
    delete_after_commit(paths, upload_files.delete_path)  # 커밋 뒤, 실패해도 응답 불변(robust)


@transaction.atomic
def duplicate(schema: Schema, user) -> Schema:
    """원본 SchemaExplorer.handleDuplicate — 초안·1.0·비공개 사본, 이름에 (복제)/(Copy)."""
    now = timezone.now()
    copy = Schema.objects.create(
        schema_id=new_schema_id(), name=f"{schema.name} (복제)"[:200],
        name_en=f"{schema.name_en or schema.name} (Copy)"[:200], description=schema.description,
        description_en=schema.description_en, category=schema.category, version="1.0", status=Schema.Status.DRAFT,
        visibility=Schema.Visibility.PRIVATE, owner=user, owner_name=_actor(user),
        owner_institution=getattr(user, "organization", "") or "", link_field_name=schema.link_field_name,
        link_display_name=schema.link_display_name, link_display_name_en=schema.link_display_name_en,
        link_description=schema.link_description, link_description_en=schema.link_description_en,
        field_groups=schema.field_groups, validation_rules=schema.validation_rules, tags=schema.tags,
        tags_en=schema.tags_en, created_at=now, updated_at=now)
    SchemaField.objects.bulk_create([
        SchemaField(schema=copy, **{k: getattr(f, k) for k in (
            "field_key", "field_name", "display_name", "display_name_en", "description", "description_en",
            "data_type", "constraints", "default_value", "is_required", "is_unique", "order", "group", "tags",
            "dictionary_field_id", "source_dictionary_id", "source_field_name", "source_modified")})
        for f in schema.fields.all()])
    log(copy, SchemaLog.Action.DUPLICATE, user, detail=f"'{schema.name}' 에서 복제", detail_en=f"Duplicated from '{schema.name_en or schema.name}'")
    return copy


# ═══════════════════════════════════════════════════════════
#  JSON 내보내기·가져오기 (원본 SchemaDetail.handleJsonExport, SchemaImportModal)
# ═══════════════════════════════════════════════════════════
def field_to_json(f: SchemaField) -> dict:
    d = {"fieldId": f.field_key, "fieldName": f.field_name, "displayName": f.display_name,
         "description": f.description, "dataType": f.data_type, "constraints": f.constraints,
         "isRequired": f.is_required, "isUnique": f.is_unique, "order": f.order}
    if f.display_name_en:
        d["displayNameEn"] = f.display_name_en
    if f.description_en:
        d["descriptionEn"] = f.description_en
    if f.group:
        d["group"] = f.group
    if f.default_value is not None:
        d["defaultValue"] = f.default_value
    if f.tags:
        d["tags"] = f.tags
    if f.source_dictionary_id:
        d["sourceRef"] = {"dictionaryId": f.source_dictionary_id, "fieldId": f.source_dictionary_id,
                          "fieldName": f.source_field_name, "isModified": f.source_modified}
    return d


def schema_to_json(schema: Schema, fields=None) -> dict:
    """내보내기 — 원본처럼 usageCount·dataCount·owner 는 뺀다."""
    fields = list(schema.fields.all()) if fields is None else fields
    d = {"schemaId": schema.schema_id, "name": schema.name, "description": schema.description,
         "category": schema.category, "version": schema.version, "status": schema.status,
         "visibility": schema.visibility,
         "linkField": {"fieldName": schema.link_field_name, "displayName": schema.link_display_name,
                       "description": schema.link_description, "displayNameEn": schema.link_display_name_en,
                       "descriptionEn": schema.link_description_en},
         "fields": [field_to_json(f) for f in fields], "fieldGroups": schema.field_groups,
         "createdAt": timezone.localtime(schema.created_at).date().isoformat(),
         "updatedAt": timezone.localtime(schema.updated_at).date().isoformat(), "tags": schema.tags}
    if schema.name_en:
        d["nameEn"] = schema.name_en
    if schema.description_en:
        d["descriptionEn"] = schema.description_en
    if schema.validation_rules:
        d["validationRules"] = schema.validation_rules
    if schema.published_at:
        d["publishedAt"] = timezone.localtime(schema.published_at).date().isoformat()
    if schema.tags_en:
        d["tagsEn"] = schema.tags_en
    return d


MAX_IMPORT_BYTES = 1024 * 1024


def parse_import(uploaded) -> dict:
    """업로드된 JSON 을 읽어 원본 validateSchemaJson 규칙 + 서버 구조 검증을 통과하면 dict 로 돌려준다."""
    name = upload_files.sanitize_name(getattr(uploaded, "name", ""))
    if not name.lower().endswith(".json"):
        raise SchemaError("JSON(.json) 파일만 가져올 수 있습니다", "Only .json files can be imported")
    if (getattr(uploaded, "size", 0) or 0) > MAX_IMPORT_BYTES:
        raise SchemaError("파일이 너무 큽니다(최대 1MB)", "File too large (max 1MB)")
    raw = uploaded.read(MAX_IMPORT_BYTES + 1)
    return parse_import_text(raw)


def parse_import_text(raw, ctx: validation.CheckContext | None = None) -> dict:
    """ctx: 확정 단계에서 import_definition 에 같은 CheckContext 를 넘기면 정의 검사(정규식 컴파일·탐침)를 다시 하지 않는다."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_IMPORT_BYTES:
            raise SchemaError("파일이 너무 큽니다(최대 1MB)", "File too large (max 1MB)")
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise SchemaError("UTF-8 로 저장된 JSON 이 아닙니다", "The JSON is not UTF-8 encoded") from None
    try:
        obj = loads_user_json(raw)
    except (ValueError, RecursionError):
        raise SchemaError("올바른 JSON 형식이 아니거나 허용되지 않는 문자(서로게이트·NUL)가 있습니다",
                          "Not valid JSON, or it contains disallowed characters (surrogates/NUL)") from None
    if not isinstance(obj, dict):
        raise SchemaError("JSON 객체가 아닙니다", "Not a JSON object")
    if not isinstance(obj.get("name"), str) or not obj.get("name"):
        raise SchemaError("name 필드가 필요합니다 (문자열)", "A name field is required (string)")
    if not isinstance(obj.get("fields"), list):
        raise SchemaError("fields 배열이 필요합니다", "A fields array is required")
    _, _, errors = validation.clean_definition(obj, strict=False, ctx=ctx)
    if errors:
        raise SchemaError("데이터 구조 정의에 오류가 있습니다", "The data schema definition has errors", errors)
    return obj


@transaction.atomic
def import_definition(user, obj: dict, ctx: validation.CheckContext | None = None) -> Schema:
    payload = dict(obj)
    # clean_definition 은 meta 객체가 있으면 그쪽을 읽는다 — 가져온 구조는 어느 쪽이든 항상 비공개로 만든다
    target = dict(payload["meta"]) if isinstance(payload.get("meta"), dict) else payload
    # 연결 키: 빌더 형식(meta.linkFieldName)과 내보내기 형식(linkField 객체) 모두 보존 — 빌더 형식이 우선
    target["linkFieldName"] = (target.get("linkFieldName") or validation.link_field_name_of(target.get("linkField"))
                               or "condition_id")
    target["visibility"] = Schema.Visibility.PRIVATE.value
    if target is not payload:
        payload["meta"] = target
    schema = save_definition(user, payload, action="draft", ctx=ctx)
    log(schema, SchemaLog.Action.IMPORT, user, detail="JSON 가져오기", detail_en="Imported from JSON")
    return schema


# ═══════════════════════════════════════════════════════════
#  데이터 입력 (원본 SchemaData 4단계: 입력 방식 → 데이터 입력 → 유효성 검사 → 제출 완료)
# ═══════════════════════════════════════════════════════════
def new_record_id() -> str:
    return f"REC-{uuid.uuid4().hex[:10].upper()}"


def link_label(schema: Schema):
    return (schema.link_display_name or "케이스 ID", schema.link_display_name_en or schema.link_display_name or "Case ID")


def _patient_exists(values: set[str]) -> set[str]:
    from apps.cases.models import Patient

    return set(Patient.objects.filter(condition_id__in=values).values_list("condition_id", flat=True))


def _check_link_patients(schema, rows, issues):
    """연결 키가 condition_id 인데 등록되지 않은 환자면 경고(저장은 가능)."""
    if schema.link_field_name != "condition_id":
        return
    ids = {r["condition_id"] for r in rows if r.get("condition_id")}
    known = _patient_exists(ids)
    for i, r in enumerate(rows):
        cid = r.get("condition_id")
        if cid and cid not in known:
            issues.append({"row": r.get("_row", i + 1), "field": "condition_id", "value": cid, "severity": "warning",
                           "ko": "등록된 환자 케이스에서 찾을 수 없는 케이스 ID 입니다",
                           "en": "Case ID not found among registered patient cases"})


def unique_key(v):
    """고유값 비교 키. 숫자(int·float)는 Decimal 로 정규화해 2 와 2.0 을 같게 본다.
    불리언은 숫자와 구분한다(true ≠ 1). 그 밖의 값은 JSON 문자열 그대로."""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return ("f", repr(v))
        return ("n", Decimal(v) if isinstance(v, int) else Decimal(repr(v)))
    return ("j", json.dumps(v, sort_keys=True))


def _check_unique(schema, fields, rows, issues, exclude_pk=None):
    for f in fields:
        if not f.is_unique:
            continue
        seen = {}
        for i, r in enumerate(rows):
            v = r["data"].get(f.field_name)
            if v is None or isinstance(v, (dict, list)):
                continue
            key = unique_key(v)
            if key in seen:
                issues.append({"row": r.get("_row", i + 1), "field": f.field_name, "value": str(v), "severity": "error",
                               "ko": f"고유값 필드입니다 — 행 {seen[key]} 과 값이 같습니다",
                               "en": f"Unique field — same value as row {seen[key]}"})
            else:
                seen[key] = r.get("_row", i + 1)
        values = [r["data"][f.field_name] for r in rows
                  if f.field_name in r["data"] and not isinstance(r["data"][f.field_name], (dict, list))]
        if not values:
            continue
        # 숫자는 정수·실수 표기를 모두 조회한다(JSON 비교는 DB 마다 2 와 2.0 을 다르게 볼 수 있음) — 최종 판정은 unique_key
        extra = []
        for v in values:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                if isinstance(v, float) and v.is_integer() and abs(v) <= 2 ** 53:
                    extra.append(int(v))
                elif isinstance(v, int) and abs(v) <= 2 ** 53:
                    extra.append(float(v))
        values = values + extra
        # 키를 표현식으로 지정한다 — `data__{name}__in` 은 이름 속 `__`·조회명(contains·keys 등)이 조회 구분자로
        # 쪼개진다. KeyTransform 의 __in 은 기존과 같은 JSON 값 비교(타입별)를 SQLite·PostgreSQL 모두 ORM 으로 한다.
        qs = (SchemaRecord.objects.filter(schema=schema)
              .annotate(_unique_value=KeyTransform(f.field_name, "data"))
              .filter(_unique_value__in=values))
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        taken = {unique_key(d.get(f.field_name)) for d in qs.values_list("data", flat=True)
                 if isinstance(d, dict) and d.get(f.field_name) is not None}
        for i, r in enumerate(rows):
            v = r["data"].get(f.field_name)
            if v is not None and not isinstance(v, (dict, list)) and unique_key(v) in taken:
                issues.append({"row": r.get("_row", i + 1), "field": f.field_name, "value": str(v), "severity": "error",
                               "ko": "고유값 필드입니다 — 이미 등록된 값입니다", "en": "Unique field — value already exists"})


def collect_form_values(fields, post, existing: dict | None = None) -> dict:
    """웹 폼 POST → {field_name: 원시값}. 다중 선택은 리스트, 수치 범위는 {min,max}, 불리언은 'true'/'false'/''."""
    raw = {}
    for f in fields:
        key = f"f__{f.field_name}"
        if f.data_type == "MULTISELECT":
            raw[f.field_name] = [v for v in post.getlist(key) if v != ""]
        elif f.data_type == "NUMERIC_RANGE":
            lo, hi = post.get(key + "__min", "").strip(), post.get(key + "__max", "").strip()
            raw[f.field_name] = {"min": lo, "max": hi} if (lo or hi) else ""
        elif f.data_type == "FILE":
            if post.get(key + "__keep") == "1" and existing and isinstance(existing.get(f.field_name), dict):
                raw[f.field_name] = existing[f.field_name]
            else:
                raw[f.field_name] = ""
        elif f.data_type == "CALCULATED":
            continue
        else:
            raw[f.field_name] = post.get(key, "")
    return raw


def _clean_pending(user):
    """오래된(24시간) 미제출 입력과 첨부 파일 정리."""
    cutoff = timezone.now() - PENDING_TTL
    old = PendingEntry.objects.filter(user=user, created_at__lt=cutoff)
    paths = list(SchemaFile.objects.filter(pending__in=old, record__isnull=True).values_list("file", flat=True))
    SchemaFile.objects.filter(pending__in=old, record__isnull=True).delete()
    old.delete()
    delete_after_commit(paths, upload_files.delete_path)  # 행 삭제가 커밋된 뒤에 파일을 지운다


# files.check_magic 이 내용(매직바이트)까지 확인하는 형식
MAGIC_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".csv", ".tsv", ".xlsx", ".dcm", ".dicom",
              ".fasta", ".fa", ".fastq", ".fq", ".fasta.gz", ".fa.gz", ".fastq.gz", ".fq.gz"]


def store_field_file(schema, field: SchemaField, uploaded, user) -> tuple[SchemaFile | None, dict | None]:
    """FILE 필드 첨부: 확장자(allowedFileTypes)·크기(maxFileSizeMB, 전역 상한) 서버 검사, 이름 재생성 저장."""
    c = field.constraints or {}
    name = upload_files.sanitize_name(getattr(uploaded, "name", ""))
    allowed = c.get("allowedFileTypes") or []
    if allowed and not upload_files.extension(name, allowed):
        return None, validation._msg(f"허용되지 않는 파일 형식입니다 ({', '.join(allowed)})",
                                     f"File type not allowed ({', '.join(allowed)})")
    size = getattr(uploaded, "size", 0) or 0
    cap = settings.SCHEMA_FILE_MAX_BYTES
    if c.get("maxFileSizeMB"):
        cap = min(cap, int(float(c["maxFileSizeMB"]) * 1024 * 1024))
    if size <= 0:
        return None, validation._msg("빈 파일입니다", "The file is empty")
    if size > cap:
        return None, validation._msg(f"파일이 너무 큽니다(최대 {upload_files.format_size(cap)})",
                                     f"File too large (max {upload_files.format_size(cap)})")
    pending_total = (SchemaFile.objects.filter(uploaded_by=user, record__isnull=True)
                     .aggregate(n=Sum("size"))["n"] or 0)
    if pending_total + size > settings.SCHEMA_PENDING_MAX_BYTES:
        return None, validation._msg(
            f"제출하지 않은 첨부가 너무 많습니다(최대 {upload_files.format_size(settings.SCHEMA_PENDING_MAX_BYTES)}). 먼저 제출하거나 잠시 뒤 다시 시도하세요",
            f"Too many unsubmitted attachments (max {upload_files.format_size(settings.SCHEMA_PENDING_MAX_BYTES)}). Submit first or retry later")
    # 내용 검사: 2단계 업로드 서비스의 매직바이트 검사를 지원 형식에 재사용(그 밖의 형식은 확장자 검사만)
    magic_ext = upload_files.extension(name, MAGIC_EXTS)
    if magic_ext:
        try:
            upload_files.check_magic(uploaded, magic_ext)
        except upload_files.FileRejected as exc:
            return None, validation._msg(exc.ko, exc.en)
    ext = Path(name).suffix.lower()[:12] if re.match(r"^\.[a-z0-9]{1,10}$", Path(name).suffix.lower()) else ""
    now = timezone.localtime()
    rel = Path("schemas") / f"{now:%Y}" / f"{now:%m}" / f"{uuid.uuid4().hex}{ext}"
    dest = Path(settings.MEDIA_ROOT) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    register_new_file(rel.as_posix())  # 요청 트랜잭션이 롤백되면 지운다(apps.core.filetx)
    with open(dest, "xb") as out:
        for chunk in uploaded.chunks():
            digest.update(chunk)
            written += len(chunk)
            out.write(chunk)
    sf = SchemaFile.objects.create(schema=schema, field_name=field.field_name, file=rel.as_posix(), original_name=name,
                                   size=written, sha256=digest.hexdigest(), uploaded_by=user)
    return sf, None


def check_form(schema: Schema, user, post, files, *, record: SchemaRecord | None = None) -> PendingEntry:
    """웹 폼(신규·수정) 한 건 검사 → PendingEntry. 오류가 있어도 결과 화면을 보여 주기 위해 저장한다."""
    _clean_pending(user)
    fields = list(schema.fields.all())
    raw = collect_form_values(fields, post, existing=record.data if record else None)
    link_value = (post.get("link_value") or "").strip()
    issues, new_files = [], []
    for f in fields:
        if f.data_type != "FILE":
            continue
        up = files.get(f"f__{f.field_name}")
        if up:
            sf, err = store_field_file(schema, f, up, user)
            if err:
                issues.append({"row": 1, "field": f.field_name, "value": upload_files.sanitize_name(up.name),
                               "severity": "error", **err})
            else:
                new_files.append(sf)
                raw[f.field_name] = {"file_id": sf.pk, "name": sf.original_name, "size": sf.size}
    data, link, more = validation.validate_entry(fields, schema.validation_rules, raw, link_value, 1,
                                                 link_label=link_label(schema), ctx=validation.CheckContext())
    issues += [dict(i, row=1) for i in more]
    rows = [{"condition_id": link, "data": data, "_row": 1}]
    if not any(i["severity"] == "error" for i in issues):
        _check_unique(schema, fields, rows, issues, exclude_pk=record.pk if record else None)
    _check_link_patients(schema, rows, issues)
    if new_files and any(i["severity"] == "error" for i in issues):
        # 검사에 실패한 요청의 첨부는 바로 지운다(제출될 수 없으므로 디스크에 남기지 않음)
        dropped = {sf.pk for sf in new_files}
        _drop_files(dropped, immediate=True)
        for k, v in list(raw.items()):
            if isinstance(v, dict) and v.get("file_id") in dropped:
                raw[k] = ""
        for r in rows:
            r["data"] = {k: v for k, v in r["data"].items() if not (isinstance(v, dict) and v.get("file_id") in dropped)}
        new_files = []
    pending = PendingEntry.objects.create(
        schema=schema, user=user, mode=PendingEntry.Mode.EDIT if record else PendingEntry.Mode.FORM, record=record,
        record_rev=record.rev if record else 0, rows=rows, schema_rev=schema.structure_rev,
        raw={"link_value": "" if has_bad_chars(link_value) else link_value, "values": _jsonable_raw(raw)},
        errors=[i for i in issues if i["severity"] == "error"], warnings=[i for i in issues if i["severity"] == "warning"])
    if new_files:
        SchemaFile.objects.filter(pk__in=[f.pk for f in new_files]).update(pending=pending)
    return pending


def _jsonable_raw(raw: dict) -> dict:
    """폼 재표시용 원래 입력값 — 서로게이트·NUL 이 든 값은 빈 값으로 남긴다(오류는 검사 결과에 이미 기록)."""
    out = {}
    for k, v in raw.items():
        if not (isinstance(v, (str, list, dict, int, float, bool)) or v is None):
            continue
        out[k] = "" if validation._contains_bad_chars(v) else v
    return out


def _csv_header_map(schema, fields, headers):
    """CSV 헤더 → 필드. 필드명(fieldName)이 기본이고, 표시명(한·영)도 받아 준다. 연결 키는 링크 필드명 또는 condition_id."""
    by = {}
    for f in fields:
        for k in (f.field_name, f.display_name, f.display_name_en):
            if k:
                by.setdefault(k.strip().lower(), f)
    link_keys = {schema.link_field_name.lower(), "condition_id", (schema.link_display_name or "").lower(),
                 (schema.link_display_name_en or "").lower()} - {""}
    mapping, link_col, unknown = {}, None, []
    for h in headers:
        key = h.strip().lower()
        if key in link_keys and link_col is None:
            link_col = h
        elif key in by and by[key].field_name not in mapping.values():
            mapping[h] = by[key].field_name
        else:
            unknown.append(h)
    return mapping, link_col, unknown


def check_csv(schema: Schema, user, uploaded) -> PendingEntry:
    """CSV 업로드 검사 — 파일 검증은 업로드 서비스(files.validate_upload), 파싱은 csvio.parse 를 재사용."""
    _clean_pending(user)
    try:
        upload_files.validate_upload(uploaded, "csv", "csv")
    except upload_files.FileRejected as exc:
        raise SchemaError(exc.ko, exc.en) from None
    uploaded.seek(0)
    data_bytes = uploaded.read()
    try:
        parsed = csvio.parse(data_bytes, delimiter="\t" if uploaded.name.lower().endswith(".tsv") else ",")
    except csvio.CsvError as exc:
        raise SchemaError(exc.ko, exc.en) from None
    if not parsed["rows"]:
        raise SchemaError("CSV 파일에 헤더와 최소 1행의 데이터가 필요합니다", "The CSV file needs a header and at least 1 data row")
    fields = list(schema.fields.all())
    mapping, link_col, unknown = _csv_header_map(schema, fields, parsed["headers"])
    issues = []
    if link_col is None:
        issues.append({"row": None, "field": schema.link_field_name, "value": "", "severity": "error",
                       "ko": f"연결 키 컬럼({schema.link_field_name})이 없습니다",
                       "en": f"Link key column ({schema.link_field_name}) is missing"})
    for h in unknown:
        issues.append({"row": None, "field": h, "value": "", "severity": "warning",
                       "ko": "데이터 구조에 없는 컬럼이라 무시합니다", "en": "Column not in the data schema — ignored"})
    rows = []
    ctx = validation.CheckContext()  # CSV 전체(검사 1회)에 정규식 시간 예산 하나
    for i, r in enumerate(parsed["rows"], start=1):
        raw = {mapping[h]: v for h, v in r.items() if h in mapping}
        data, link, more = validation.validate_entry(fields, schema.validation_rules, raw, r.get(link_col) if link_col else "",
                                                     i, link_label=link_label(schema), ctx=ctx)
        issues += more
        rows.append({"condition_id": link, "data": data, "_row": i})
    if not any(x["severity"] == "error" for x in issues):
        _check_unique(schema, fields, rows, issues)
    _check_link_patients(schema, rows, issues)
    errors = [x for x in issues if x["severity"] == "error"]
    return PendingEntry.objects.create(
        schema=schema, user=user, mode=PendingEntry.Mode.CSV, rows=rows if not errors else rows[:50],
        schema_rev=schema.structure_rev, raw={"file": upload_files.sanitize_name(uploaded.name), "count": len(rows),
                                    "preview": [{k: v.replace("\x00", "") for k, v in r.items()} for r in parsed["rows"][:5]],
                                    "headers": [h.replace("\x00", "") for h in parsed["headers"][:60]]},
        errors=errors[:1000], warnings=[x for x in issues if x["severity"] == "warning"][:1000])


@transaction.atomic
def submit_pending(pending_id: int, user) -> PendingEntry:
    """검사를 통과한 입력을 반영. 조건부 UPDATE 로 PendingEntry 를 한 번만 소비하고, 서버에서 다시 검사한다."""
    pending = PendingEntry.objects.select_related("schema", "record").filter(pk=pending_id, user=user).first()
    if pending is None:
        raise SchemaError("입력 내역을 찾을 수 없습니다", "Entry not found")
    if pending.errors:
        raise SchemaError("오류를 먼저 수정하세요", "Fix errors first")
    if not PendingEntry.objects.filter(pk=pending.pk, consumed=False).update(consumed=True):
        raise SchemaError("이미 제출된 입력입니다", "This entry was already submitted")
    schema = pending.schema
    # 상태·구조 확인 겸 행 잠금: 발행 상태이고 검사 시점과 구조(rev)가 같을 때만 기록한다.
    # (그 사이 구조가 저장되면 — 예: 필드 삭제 — 다시 검사하도록 돌려보낸다)
    if not Schema.objects.filter(pk=schema.pk, status=Schema.Status.PUBLISHED,
                                 structure_rev=pending.schema_rev).update(updated_at=F("updated_at")):
        if Schema.objects.filter(pk=schema.pk, status=Schema.Status.PUBLISHED).exists():
            raise SchemaError("검사한 뒤 데이터 구조가 바뀌었습니다. 입력 화면에서 다시 검사하세요.",
                              "The data schema changed after validation. Go back and validate again.")
        raise SchemaError("발행된 데이터 구조에서만 데이터를 입력할 수 있습니다", "Data entry is available only for published schemas")
    schema.refresh_from_db()
    if not can_view(user, schema):
        raise SchemaError("이 데이터 구조에 접근할 권한이 없습니다", "You do not have access to this data schema")
    fields = list(schema.fields.all())
    issues, rows = [], []
    ctx = validation.CheckContext()  # 제출 재검증(검사 1회) 단위 정규식 시간 예산
    for r in pending.rows:
        data, link, more = validation.validate_entry(fields, schema.validation_rules, r["data"], r["condition_id"],
                                                     r.get("_row"), link_label=link_label(schema), ctx=ctx)
        issues += more
        rows.append({"condition_id": link, "data": data, "_row": r.get("_row")})
    _check_unique(schema, fields, rows, issues, exclude_pk=pending.record_id)
    file_ids = {v["file_id"] for r in rows for v in r["data"].values() if isinstance(v, dict) and "file_id" in v}
    allowed_files = set(SchemaFile.objects.filter(pk__in=file_ids, schema=schema).filter(
        Q(pending=pending) | Q(record=pending.record) if pending.record_id else Q(pending=pending)).values_list("pk", flat=True))
    if file_ids - allowed_files:
        issues.append({"row": 1, "field": "file", "value": "", "severity": "error",
                       "ko": "첨부 파일 정보가 올바르지 않습니다", "en": "Invalid attachment reference"})
    errors = [i for i in issues if i["severity"] == "error"]
    if errors:
        transaction.set_rollback(True)
        raise SchemaError("데이터 구조가 바뀌어 다시 검사한 결과 오류가 있습니다. 입력을 다시 확인하세요.",
                          "The data schema changed and re-validation found errors. Please review your input.", errors)
    now = timezone.now()
    ids = []
    if pending.mode == PendingEntry.Mode.EDIT:
        rec = pending.record
        if rec is None or not can_modify_record(user, rec):
            raise SchemaError("이 데이터를 수정할 권한이 없습니다", "You cannot modify this record")
        r = rows[0]
        old_files = {v["file_id"] for v in rec.data.values() if isinstance(v, dict) and "file_id" in v}
        if not SchemaRecord.objects.filter(pk=rec.pk, rev=pending.record_rev).update(
                condition_id=r["condition_id"], data=r["data"], updated_at=now, rev=F("rev") + 1):
            raise SchemaError("다른 사용자가 먼저 이 데이터를 수정했습니다. 다시 불러와 수정하세요.",
                              "Someone else modified this record first. Reload and edit again.")
        ids.append(rec.record_id)
        SchemaFile.objects.filter(pk__in=file_ids).update(record=rec, pending=None)
        _drop_files(old_files - file_ids)
    else:
        objs = [SchemaRecord(schema=schema, record_id=new_record_id(), condition_id=r["condition_id"], data=r["data"],
                             author=user, author_label=user.username, created_at=now, updated_at=now) for r in rows]
        SchemaRecord.objects.bulk_create(objs, batch_size=500)
        ids = [o.record_id for o in objs]
        if file_ids:
            rec = SchemaRecord.objects.get(record_id=ids[0])
            SchemaFile.objects.filter(pk__in=file_ids).update(record=rec, pending=None)
    Schema.objects.filter(pk=schema.pk).update(updated_at=now)
    pending.result = {"count": len(ids), "record_ids": ids[:20], "at": now.isoformat()}
    pending.rows = []
    pending.save(update_fields=["result", "rows"])
    return pending


def _drop_files(ids, immediate: bool = False):
    """첨부 행·파일 삭제. 트랜잭션 안에서는 커밋 뒤에 파일을 지운다(immediate=True 면 바로)."""
    if not ids:
        return
    paths = list(SchemaFile.objects.filter(pk__in=ids).values_list("file", flat=True))
    SchemaFile.objects.filter(pk__in=ids).delete()
    if immediate:
        for p in paths:
            if p:
                upload_files.delete_path(p)
    else:
        delete_after_commit(paths, upload_files.delete_path)  # 커밋 뒤, 실패해도 응답 불변(robust)


@transaction.atomic
def delete_record(record: SchemaRecord, user) -> None:
    if not can_modify_record(user, record):
        raise SchemaError("이 데이터를 삭제할 권한이 없습니다", "You cannot delete this record")
    file_ids = list(record.files.values_list("pk", flat=True))
    deleted, _ = SchemaRecord.objects.filter(pk=record.pk, schema__status=Schema.Status.PUBLISHED).delete()
    if not deleted:
        raise SchemaError("이미 삭제되었거나 발행 상태가 아닌 데이터 구조입니다", "Already deleted or the schema is not published")
    _drop_files(file_ids)


# ═══════════════════════════════════════════════════════════
#  조회·CSV
# ═══════════════════════════════════════════════════════════
def sample_value(f: SchemaField) -> str:
    """원본 SchemaData.getSampleValue."""
    opts = (f.constraints or {}).get("options") or []
    return {
        "TEXT": "sample_text", "TEXTAREA": "sample description", "INTEGER": "0", "DECIMAL": "0.0", "BOOLEAN": "true",
        "DATE": "2026-01-01", "DATETIME": "2026-01-01T00:00", "SELECT": opts[0]["value"] if opts else "option1",
        "MULTISELECT": opts[0]["value"] if opts else "option1", "NUMERIC_RANGE": "0~1", "FILE": "",
        "REFERENCE": "ref_001", "CALCULATED": "", "CODED": "CODE-001",
    }.get(f.data_type, "")


def _csv(rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow([csvio.safe_cell(v) for v in r])
    return "﻿" + buf.getvalue()


def template_csv(schema: Schema) -> str:
    fields = [f for f in schema.fields.all() if f.data_type not in ("CALCULATED", "FILE")]
    return _csv([[schema.link_field_name] + [f.field_name for f in fields],
                 ["COND-0001" if schema.link_field_name == "condition_id" else "ID-0001"] + [sample_value(f) for f in fields]])


def export_csv(schema: Schema, records) -> str:
    """입력 데이터 전체 내보내기 — 모든 칸에 수식 주입 방어(csvio.safe_cell)."""
    fields = list(schema.fields.all())
    head = ["record_id", schema.link_field_name] + [f.field_name for f in fields] + ["created_by", "created_at", "updated_at"]
    rows = [head]
    for r in records:
        rows.append([r.record_id, r.condition_id] + [validation.csv_value(f, r.data.get(f.field_name)) for f in fields]
                    + [r.author_label, timezone.localtime(r.created_at).strftime("%Y-%m-%d %H:%M"),
                       timezone.localtime(r.updated_at).strftime("%Y-%m-%d %H:%M")])
    return _csv(rows)


def search_records(records: list, fields, q: str, lang: str) -> list:
    q = (q or "").strip().lower()
    if not q:
        return records
    out = []
    for r in records:
        texts = [r.condition_id] + [validation.display_value(f, r.data.get(f.field_name), lang) for f in fields]
        if any(q in (t or "").lower() for t in texts):
            out.append(r)
    return out


def sort_records(records: list, fields, key: str | None, direction: str | None) -> list:
    if not key:
        return records
    if key == "condition_id":
        return sorted(records, key=lambda r: r.condition_id, reverse=direction == "desc")
    f = next((x for x in fields if x.field_name == key), None)
    if f is None:
        return records

    def k(r):
        v = r.data.get(key)
        if v is None:
            return (1, 0, "")
        if isinstance(v, bool):
            return (0, int(v), "")
        if isinstance(v, (int, float)):
            return (0, v, "")
        return (0, 0, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))

    present = [r for r in records if r.data.get(key) is not None]
    missing = [r for r in records if r.data.get(key) is None]
    return sorted(present, key=k, reverse=direction == "desc") + missing


def case_custom_data(user, condition_id: str) -> list[dict]:
    """케이스 상세 '커스텀' 탭 — 볼 수 있는 발행 데이터 구조와, 그중 이 condition_id 로 입력된 행."""
    schemas = list(with_counts(visible_schemas(user).filter(status=Schema.Status.PUBLISHED)).order_by("name"))
    recs = (SchemaRecord.objects.filter(schema__in=[s.pk for s in schemas], condition_id=condition_id)
            .order_by("created_at"))
    by_schema = {}
    for r in recs:
        by_schema.setdefault(r.schema_id, []).append(r)
    fields = {}
    for f in SchemaField.objects.filter(schema__in=[s.pk for s in schemas if s.pk in by_schema]).order_by("order"):
        fields.setdefault(f.schema_id, []).append(f)
    out = []
    for s in schemas:
        out.append({"schema": s, "records": by_schema.get(s.pk, []), "fields": fields.get(s.pk, [])})
    out.sort(key=lambda x: (not x["records"], x["schema"].name))
    return out


# ═══════════════════════════════════════════════════════════
#  시드 (data/seed/sampleSchemas.json — 원본 data/sampleSchemas.ts 변환본: 스키마 4·필드 48·행 14)
# ═══════════════════════════════════════════════════════════
def _aware(d: str | None, h: int = 9):
    if not d:
        return None
    dt = datetime.fromisoformat(d) if "T" in d else datetime.combine(datetime.fromisoformat(d).date(), time(h, 0))
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


SEED_COND_RE = re.compile(r"^COND-(\d{4})-([A-Z]{2})-(\d+)$")


def convert_seed_condition_id(cid: str) -> str:
    """원본 Mock 의 'COND-2024-KR-0001' 형식을 시드 환자 형식 'KR-2024-0001'(국가-연도-번호)로 바꾼다."""
    m = SEED_COND_RE.match(cid or "")
    return f"{m.group(2)}-{m.group(1)}-{m.group(3)}" if m else cid


def load_sample_schemas(force: bool = False) -> dict:
    if is_seeded(DATASET) and not force:
        return {"skipped": True}
    path = Path(settings.SEED_DIR) / SEED_FILE
    src = json.loads(path.read_text(encoding="utf-8"))
    created = {"schemas": 0, "fields": 0, "records": 0}
    with transaction.atomic():
        for s in src["SAMPLE_SCHEMAS"]:
            if Schema.objects.filter(schema_id=s["schemaId"]).exists():
                continue  # --force 라도 기존 레코드는 건드리지 않는다(사용자 편집 보존)
            link = s.get("linkField") or {}
            schema = Schema.objects.create(
                schema_id=s["schemaId"], name=s["name"], name_en=s.get("nameEn", ""), description=s.get("description", ""),
                description_en=s.get("descriptionEn", ""), category=s["category"], version=s.get("version", "1.0"),
                status=s["status"], visibility=s["visibility"], owner=None,
                owner_name=s["owner"].get("userName", ""), owner_name_en=s["owner"].get("userNameEn", ""),
                owner_institution=s["owner"].get("institution", ""),
                link_field_name=link.get("fieldName", "condition_id"), link_display_name=link.get("displayName", ""),
                link_display_name_en=link.get("displayNameEn", ""), link_description=link.get("description", ""),
                link_description_en=link.get("descriptionEn", ""), field_groups=s.get("fieldGroups", []),
                validation_rules=s.get("validationRules", []), tags=s.get("tags", []), tags_en=s.get("tagsEn", []),
                created_at=_aware(s["createdAt"]), updated_at=_aware(s["updatedAt"]),
                published_at=_aware(s.get("publishedAt")))
            created["schemas"] += 1
            for f in s["fields"]:
                src_ref = f.get("sourceRef") or {}
                SchemaField.objects.create(
                    schema=schema, field_key=f["fieldId"], field_name=f["fieldName"], display_name=f["displayName"],
                    display_name_en=f.get("displayNameEn", ""), description=f.get("description", ""),
                    description_en=f.get("descriptionEn", ""), data_type=f["dataType"], constraints=f.get("constraints", {}),
                    default_value=f.get("defaultValue"), is_required=f.get("isRequired", False),
                    is_unique=f.get("isUnique", False), order=f.get("order", 0), group=f.get("group", ""),
                    tags=f.get("tags", []), dictionary_field=resolve_dictionary_field(src_ref.get("dictionaryId", "")),
                    source_dictionary_id=src_ref.get("dictionaryId", ""), source_field_name=src_ref.get("fieldName", ""),
                    source_modified=src_ref.get("isModified", False))
                created["fields"] += 1
            owner_label = s["owner"].get("userName", "")
            SchemaLog.objects.create(schema=schema, at=schema.created_at, actor_label=owner_label, action="create",
                                     version="1.0", detail="데이터 구조 초기 생성", detail_en="Initial creation of data schema")
            if schema.published_at:
                SchemaLog.objects.create(schema=schema, at=schema.published_at, actor_label=owner_label, action="publish",
                                         version=schema.version if schema.version == "1.0" else "1.0",
                                         detail="발행", detail_en="Published")
            if schema.version != "1.0":
                SchemaLog.objects.create(schema=schema, at=schema.updated_at, actor_label=owner_label, action="save",
                                         version=schema.version, detail=f"버전 {schema.version} 저장",
                                         detail_en=f"Saved version {schema.version}")
        known = {s.schema_id: s for s in Schema.objects.filter(schema_id__in=[x["schemaId"] for x in src["SAMPLE_SCHEMAS"]])}
        for r in src["SAMPLE_DYNAMIC_DATA"]:
            schema = known.get(r["schemaId"])
            if schema is None or SchemaRecord.objects.filter(record_id=r["rowId"]).exists():
                continue
            SchemaRecord.objects.create(schema=schema, record_id=r["rowId"], condition_id=convert_seed_condition_id(r["conditionId"]),
                                        data=r["data"],
                                        author=None, author_label=r.get("createdBy", ""),
                                        created_at=_aware(r["createdAt"]), updated_at=_aware(r["updatedAt"]))
            created["records"] += 1
        from apps.cases.models import Patient

        seeded = list(SchemaRecord.objects.filter(record_id__in=[r["rowId"] for r in src["SAMPLE_DYNAMIC_DATA"]])
                      .values_list("condition_id", flat=True))
        known = set(Patient.objects.filter(condition_id__in=seeded).values_list("condition_id", flat=True))
        created["link_unmatched"] = sum(1 for c in seeded if c not in known)
        mark_seeded(DATASET, created)
    return {"skipped": False, **created}
