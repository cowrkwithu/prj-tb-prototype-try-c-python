"""NUL(\\x00) 입력 전역 퍼징 하네스 — 모든 URL 패턴 × GET/POST × 입력 이름 각각에 NUL, 연구원·관리자로 500 이 없다.

PostgreSQL 은 text·jsonb 에 NUL 을 저장·비교하지 못해(DataError) SQLite 에서 드러나지 않던 500 이 난다(5단계 PG 검증).
두 백엔드 모두에서 같은 결과(500 없음)여야 한다.

구조:
  - URL 목록은 URL resolver 에서 자동으로 뽑는다. 정규식 인자(`(?P<uid>…)`)는 실제로 존재하는 객체 값으로 채우고
    (`_kwargs`), 선택지 그룹(`clinical|lab|…`)은 값마다 펼친다. 값 매핑이 없는 새 패턴은 테스트가 실패해 매핑 추가를 요구한다.
  - 입력 이름은 뷰 함수와 그 뷰가 부르는 프로젝트 함수(apps.*, 3단계까지)·사용하는 Form 의 소스에서
    `.get("x")`/`.getlist("x")`/`["x"]` 로 뽑는다(서비스가 POST 를 직접 읽는 경우 포함). f-문자열 이름은 _EXTRA_NAMES.
  - 요청: GET·POST 각각 (1) 모든 이름 NUL (2) 이름 하나만 NUL + 나머지는 그럴듯한 기본값(`BASE`) — 앞 단계 검증에서
    막히지 않고 깊은 경로까지 가게 한다. POST 는 JSON 본문(문자열 값 NUL)도 보낸다.
  - 요청마다 savepoint 로 감싸 되돌린다(앞 요청의 승인·삭제가 뒤 요청의 대상을 없애지 않게).
"""
import inspect
import itertools
import json
import re
from http.cookies import SimpleCookie

import pytest
from django import forms
from django.core.cache import cache
from django.db import transaction
from django.urls import URLPattern, URLResolver, get_resolver

pytestmark = pytest.mark.django_db

NUL = "a\x00b"
# 뷰 소스에서 뽑히지 않는(서비스 깊은 곳에서 읽는) 공통 이름 — 모두 NUL 단계에 함께 넣는다
PARAMS = ("action back cat cat_code category check cohort comment condition_id confirm country data_types dep drug "
          "email fill finding format from gene help id include institution lang location method mode name next note "
          "offset open p page patient pending q query reason record_id rev row run scenario searched severity "
          "status step study submitted tab text type types view visibility link_value payload json").split()
SKIP_VIEWS = {"logout"}  # 세션을 끊는다(별도 테스트)
_NAME_RE = re.compile(r"""\.(?:get|getlist|pop)\(\s*["']([A-Za-z_][\w-]{0,40})["']|\[\s*["']([A-Za-z_][\w-]{0,40})["']\s*\]""")
_GROUP_RE = re.compile(r"\(\?P<(\w+)>((?:[^()]|\([^()]*\))*)\)")


@pytest.fixture(autouse=True)
def _media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()


# ─── URL 패턴 수집 ───
def _patterns():
    out = []

    def walk(patterns, prefix=""):
        for p in patterns:
            route = prefix + str(p.pattern)
            if isinstance(p, URLResolver):
                if not route.startswith("django-admin"):
                    walk(p.url_patterns, route)
            elif isinstance(p, URLPattern) and p.callback.__name__ not in SKIP_VIEWS:
                out.append((route, p.callback))

    walk(get_resolver().url_patterns)
    return out


def _code_names(code) -> set[str]:
    names = set(code.co_names)
    for c in code.co_consts:  # 중첩 함수·컴프리헨션
        if inspect.iscode(c):
            names |= _code_names(c)
    return names


def _view_names(callback, depth: int = 3) -> set[str]:
    """뷰가 읽는 입력 이름 — 뷰와, 뷰가 부르는 프로젝트(apps.*) 함수를 depth 단계까지 따라가며 소스에서 뽑는다
    (`workflow.save_form` → `rows_from_form` 처럼 서비스가 POST 를 직접 읽는 경우 포함). 사용하는 Form 의 필드도 넣는다."""
    names, seen = set(), set()

    def visit(fn, d):
        if fn in seen or d < 0:
            return
        seen.add(fn)
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            return
        for a, b in _NAME_RE.findall(src):
            names.add(a or b)
        used = _code_names(fn.__code__)
        for n in used:
            obj = fn.__globals__.get(n)
            if inspect.isfunction(obj) and obj.__module__.startswith("apps."):
                visit(inspect.unwrap(obj), d - 1)
            elif inspect.ismodule(obj) and obj.__name__.startswith("apps."):
                for m in used:
                    attr = getattr(obj, m, None)
                    if inspect.isfunction(attr) and attr.__module__.startswith("apps."):
                        visit(inspect.unwrap(attr), d - 1)
            elif inspect.isclass(obj) and issubclass(obj, forms.BaseForm):
                names.update(obj.base_fields)

    visit(inspect.unwrap(callback), depth)
    return names


def _expand(route: str, kw: dict) -> list[str]:
    """정규식 라우트 → 실제 경로들. 선택지 그룹(a|b|c)은 값마다 펼친다."""
    groups = _GROUP_RE.findall(route)
    choices = []
    for name, pat in groups:
        if name in kw:
            choices.append([kw[name]])
        elif re.fullmatch(r"[\w-]+(\|[\w-]+)+", pat):
            choices.append(pat.split("|"))
        else:
            raise KeyError(f"URL 인자 값 매핑 없음: {route} ({name}) — _kwargs 에 추가하세요")
    paths = []
    for combo in itertools.product(*choices) if choices else [()]:
        vals = iter(combo)
        s = _GROUP_RE.sub(lambda m: str(next(vals)), route)
        s = re.sub(r"/\?\$$", "", s.lstrip("^")).rstrip("$").replace("\\.", ".")
        paths.append("/" + s)
    return paths


# ─── 실제 객체로 채운 세계 ───
@pytest.fixture
def world(demo_users, seeded_patients, loaded_dictionary, settings):
    from django.core.files.base import ContentFile

    from apps.cases.models import Patient
    from apps.cohorts import services as cohort_services
    from apps.datasharing.tests.conftest import make_dar
    from apps.dictionary.models import DictionaryCategory, DictionaryField, DictionaryVersion
    from apps.genomics.models import Variant
    from apps.genomics.services import load_genomics
    from apps.imaging.models import ExpertAnnotation
    from apps.imaging.services import load_imaging_samples
    from apps.imaging.tests.conftest import make_uploaded_study
    from apps.schemas import services as schema_services
    from apps.schemas.models import PendingEntry, SchemaRecord
    from apps.schemas.tests.conftest import simple_payload
    from apps.uploads.models import Submission, SubmissionFile
    from apps.uploads.services import schemas as up_schemas
    from apps.uploads.services import workflow
    from apps.uploads.tests.test_deletion_invariant import World, lab

    settings.API_RATE_LIMIT_PER_MINUTE = 10 ** 6
    settings.API_AUTH_FAIL_LIMIT_PER_MINUTE = 10 ** 6
    r, a = demo_users["researcher"], demo_users["admin"]
    load_imaging_samples()
    load_genomics()
    schema_services.load_sample_schemas()
    p = Patient.objects.order_by("pk").first()
    st = make_uploaded_study(r, p)
    ann = ExpertAnnotation.objects.create(study=st, shape="text", x=1, y=1, text="t", author_name="x", author=r)
    w = World(demo_users)
    pending = w.build(lab("KR-2024-0002", ("ALT", "80")), 101)                 # 검토 대기(관리자 검토 대상)
    approved = w.build(lab("KR-2024-0003", ("ALT", "81")), 102)
    w.approve(approved)
    deleting = w.build(lab("KR-2024-0004", ("AST", "82")), 103)
    w.approve(deleting)
    Submission.objects.filter(pk=deleting.pk).update(deletion_requested_at=deleting.created_at)
    rejected = w.build(lab("KR-2024-0005", ("ALT", "83")), 104)
    workflow.reject(rejected, a, "보완 필요")
    drafts = {d: workflow.create_draft(r, d, up_schemas.MODES[d][0]) for d in up_schemas.DOMAINS}
    sf = SubmissionFile(submission=drafts["genomic"], kind="sequence", original_name="x.fastq.gz", size=3, sha256="0" * 64)
    sf.file.save("fz.fastq.gz", ContentFile(b"abc"), save=False)
    sf.save()
    own = schema_services.save_definition(r, simple_payload(), action="publish")
    rec = SchemaRecord.objects.filter(schema__schema_id="schema-001").first()
    pe = PendingEntry.objects.create(schema=own, user=r, mode=PendingEntry.Mode.FORM, rows=[], schema_rev=own.structure_rev,
                                     raw={"link_value": "", "values": {}}, errors=[], warnings=[])
    tree = {"op": "AND", "children": [{"field": "demographics.age", "operator": "gte", "value": 10}]}
    cohort = cohort_services.save_cohort(r, tree, None, "퍼징")
    dar = make_dar(r, ["clinical"])
    return {
        "users": demo_users, "patient": p, "study": st, "annotation": ann, "variant": Variant.objects.order_by("pk").first(),
        "pending": pending, "approved": approved, "deleting": deleting, "rejected": rejected, "drafts": drafts, "subfile": sf,
        "schema": own, "sample_schema": "schema-001", "record": rec, "pending_entry": pe, "cohort": cohort, "dar": dar,
        "category": DictionaryCategory.objects.order_by("pk").first(), "field": DictionaryField.objects.order_by("pk").first(),
        "version": DictionaryVersion.objects.order_by("pk").first(),
    }


# pk 인자 — 뷰마다 가리키는 모델이 다르다
_PK = {
    "case_images": lambda w: w["patient"].pk, "case_image_detail": lambda w: w["patient"].pk,
    "category_delete": lambda w: w["category"].pk, "entry_deprecate": lambda w: w["field"].pk,
    "version_activate": lambda w: w["version"].pk,
    "data_review": lambda w: w["pending_entry"].pk, "data_submit": lambda w: w["pending_entry"].pk,
    "data_done": lambda w: w["pending_entry"].pk,
    "cohort_rename": lambda w: w["cohort"].pk, "cohort_delete": lambda w: w["cohort"].pk, "cohort_export": lambda w: w["cohort"].pk,
    "case_detail": lambda w: w["patient"].pk, "image_detail": lambda w: w["study"].pk,
    "variant_detail": lambda w: w["variant"].pk,
    "annotation_update": lambda w: w["annotation"].pk, "annotation_delete": lambda w: w["annotation"].pk,
}
_UID = {"my_delete": "approved", "my_resubmit": "rejected", "deletion_detail": "deleting", "deletion_approve": "deleting",
        "deletion_reject": "deleting"}
_FILE = {"download_file": lambda w: w["subfile"].pk, "remove_file": lambda w: w["subfile"].pk,
         "study_file": lambda w: w["study"].files.first().pk}
# 이름을 f-문자열로 만드는 입력(소스에서 뽑히지 않음)
_EXTRA_NAMES = {
    "data_check": lambda w: {f"f__{f.field_name}" for f in w["schema"].fields.all()} | {"link_value"},
    "record_detail": lambda w: {f"f__{f.field_name}" for f in w["schema"].fields.all()},
}
_SPECIAL = {"not_found": ["/api/zzz", "/api/v1/zzz"], "fallback_redirect": ["/nonexistent-path"]}


def _kwargs(view, w) -> dict:
    name = view.__name__
    kw = {"image_id": w["study"].pk, "dictionary_id": w["field"].dictionary_id, "condition_id": w["patient"].condition_id,
          "schema_id": w["schema"].schema_id, "request_id": w["dar"].request_id, "slug": "patient-cases",
          "uid": w[_UID.get(name, "pending")].upload_id}
    if name in _PK:
        kw["pk"] = _PK[name](w)
    if name in _FILE:
        kw["file_id"] = _FILE[name](w)
    elif name == "download_file":
        kw["file_id"] = 999999
    if name in ("record_detail", "record_delete"):
        kw["schema_id"], kw["record_id"] = w["sample_schema"], w["record"].record_id
    return kw


def _base(w, path) -> dict:
    """한 이름만 NUL 로 바꿀 때 나머지에 넣는 그럴듯한 값 — 검증 앞 단계에서 막히지 않게."""
    m = re.match(r"/upload/(clinical|regimen|lab|genomic|imaging)", path)
    uid = w["drafts"][m.group(1)].upload_id if m else w["pending"].upload_id
    return {"id": uid, "action": "next", "check": ["0", "1", "2", "3", "4"], "condition_id": w["patient"].condition_id,
            "link_value": w["patient"].condition_id, "name": "퍼징", "reason": "사유", "comment": "의견", "types": ["clinical"],
            "data_types": ["clinical"], "query": "country=한국", "lang": "ko", "next": "/", "email": "a@b.co",
            "institution": "기관", "payload": json.dumps({"meta": {"name": "x"}, "fields": []}), "text": "메모",
            "study": str(w["study"].pk), "shape": "rect", "x": "1", "y": "1", "width": "1", "height": "1", "q": "결핵",
            "tab": "dar", "mode": "form", "step": "2", "confirm": "1", "visibility": "public", "status": "published"}


def _requests(names, base):
    """(설명, 데이터) — 모든 이름 NUL, 이름 하나만 NUL(+기본값)."""
    yield "all", {k: NUL for k in names | set(PARAMS)}
    for k in sorted(names):
        d = {n: base.get(n, "1") for n in names if n != k}
        d[k] = NUL
        yield k, d


def _call(client, method, path, data, **extra):
    """요청 1건을 savepoint 안에서 보내고 되돌린다. 500·예외면 그 내용을 돌려준다."""
    cache.clear()
    cookies = SimpleCookie(client.cookies.output(header="", sep=";"))  # 세션 쿠키 — 로그아웃·로그인 요청 뒤 되돌린다
    try:
        with transaction.atomic():
            r = getattr(client, method)(path, data, **extra)
            transaction.set_rollback(True)
        return None if r.status_code < 500 else r.status_code
    except Exception as exc:  # noqa: BLE001 — 어떤 예외든 500 에 해당
        msg = re.sub(r"\s+", " ", str(exc))[:150]
        return f"{type(exc).__name__}: {msg}"
    finally:
        client.cookies = cookies


def test_harness_covers_every_pattern(world):
    """모든 URL 패턴이 실제 경로로 펼쳐진다(새 패턴은 _kwargs 매핑 없으면 여기서 실패)."""
    n = 0
    for route, view in _patterns():
        paths = _SPECIAL.get(view.__name__) or _expand(route, _kwargs(view, world))
        n += len(paths)
    assert n >= 120


@pytest.mark.parametrize("user", ["researcher", "admin"])
def test_no_500_with_nul_everywhere(client, world, user):
    client.raise_request_exception = True
    u = world["users"][user]
    client.force_login(u)
    bad, sent = [], 0
    for route, view in _patterns():
        paths = _SPECIAL.get(view.__name__) or _expand(route, _kwargs(view, world))
        names = _view_names(view) | _EXTRA_NAMES.get(view.__name__, lambda w: set())(world)
        for path in paths:
            base = _base(world, path)
            for method in ("get", "post"):
                for label, data in _requests(names, base):
                    sent += 1
                    err = _call(client, method, path, data)
                    if err:
                        bad.append((method, path, label, err))
            for body in ({**{k: NUL for k in names}, "text": NUL, "name": NUL, "study": world["study"].pk, "shape": "rect",
                          "x": 1, "y": 1, "width": 1, "height": 1,
                          "conditions": [{"field": "demographics.age", "operator": "gte", "value": NUL}]},
                         {"name": NUL, "conditions": [{"field": "demographics.age", "operator": "gte", "value": 10}]}):
                sent += 1
                err = _call(client, "post", path, json.dumps(body), content_type="application/json")
                if err:
                    bad.append(("json", path, sorted(body)[:2], err))
    assert sent > 2000
    assert not bad, "\n".join(map(repr, bad))


def test_nul_in_path_header_and_login(client, demo_users, seeded_patients):
    """경로·인증 헤더·로그인 폼의 NUL."""
    client.raise_request_exception = True
    assert client.get("/foo\x00bar").status_code < 500
    assert client.get("/api/v1/cases", HTTP_AUTHORIZATION="Bearer ab\x00cd").status_code == 401
    assert client.post("/login", {"username": "ad\x00min", "password": "x\x00"}).status_code < 500
    assert client.post("/admin/login", {"username": "ad\x00min", "password": "x"}).status_code < 500
    client.force_login(demo_users["researcher"])
    assert client.get("/login", {"next": "/cases\x00"}).status_code < 500
    assert client.post("/lang", {"lang": "en\x00", "next": "/\x00"}).status_code < 500
    assert client.post("/logout", {"next": "\x00"}).status_code < 500


# ─── 서비스 단위: NUL 을 저장하지 않는다(두 백엔드 같은 결과) ───
@pytest.mark.parametrize("approve", [True, False])
def test_dar_review_text_with_nul_rejected(demo_users, approve):
    from apps.datasharing import services
    from apps.datasharing.tests.conftest import make_dar

    dar = make_dar(demo_users["researcher"], ["clinical"])
    with pytest.raises(services.DarError):
        if approve:
            services.approve_request(dar, demo_users["admin"], ["clinical"], "확인\x00")
        else:
            services.reject_request(dar, demo_users["admin"], "사유\x00")
    dar.refresh_from_db()
    assert dar.status == "pending" and dar.review_comment == ""


def test_entry_issue_values_and_link_stored_without_nul():
    from apps.schemas import validation

    fields = [{"field_name": "note", "data_type": "TEXT", "is_required": False, "constraints": {}}]
    data, link, issues = validation.validate_entry(fields, [], {"note": "a\x00b"}, "K\x00R-1", 1)
    assert link == "KR-1"  # 형식 오류로 이미 거부된 값 — NUL 만 지워 기록
    assert {i["field"] for i in issues} == {"condition_id", "note"}
    assert all("\x00" not in i["value"] for i in issues)


# ─── 자유 텍스트 공통 검사(apps.core.textinput) — 업로드 검토·삭제 요청 반려·데이터 구조 메모 ───
@pytest.mark.parametrize("action", ["approve", "reject", "revise"])
def test_upload_review_text_with_nul_rejected(demo_users, seeded_patients, action):
    from apps.uploads.models import Submission
    from apps.uploads.services import workflow
    from apps.uploads.tests.test_deletion_invariant import CHECKLIST, World, lab

    sub = World(demo_users).build(lab("KR-2024-0002", ("ALT", "80")), 201)
    admin = demo_users["admin"]
    with pytest.raises(workflow.WorkflowError) as ei:
        if action == "approve":
            workflow.approve(sub, admin, CHECKLIST, "의견\x00")
        elif action == "reject":
            workflow.reject(sub, admin, "사유\x00")
        else:
            workflow.request_revision(sub, admin, "사유\x00")
    assert "허용되지 않는 문자" in ei.value.ko
    sub = Submission.objects.get(pk=sub.pk)
    assert sub.status == "pending" and sub.review_comment == ""


def test_deletion_reject_reason_with_nul_rejected(demo_users, seeded_patients):
    from django.utils import timezone

    from apps.uploads.models import Submission
    from apps.uploads.services import deletion, workflow
    from apps.uploads.tests.test_deletion_invariant import World, lab

    w = World(demo_users)
    sub = w.build(lab("KR-2024-0002", ("ALT", "80")), 202)
    w.approve(sub)
    Submission.objects.filter(pk=sub.pk).update(deletion_requested_at=timezone.now())
    with pytest.raises(workflow.WorkflowError):
        deletion.reject_deletion(Submission.objects.get(pk=sub.pk), demo_users["dmanager"], "반려\x00")
    sub = Submission.objects.get(pk=sub.pk)
    assert sub.deletion_rejected_at is None and sub.deletion_reject_reason == ""


def test_upload_review_text_rejected_via_http(client, demo_users, seeded_patients):
    """리뷰 재현: PG 에서는 '겹쳐 처리 못함'으로 영구 실패, SQLite 에서는 저장되던 경로 — 이제 둘 다 안내 후 거부."""
    from apps.uploads.models import Submission
    from apps.uploads.tests.test_deletion_invariant import CHECKLIST, World, lab

    sub = World(demo_users).build(lab("KR-2024-0002", ("ALT", "80")), 203)
    client.force_login(demo_users["admin"])
    r = client.post(f"/admin/review/{sub.upload_id}/reject", {"reason": "사유\x00끝", "check": CHECKLIST}, follow=True)
    msgs = [str(m) for m in r.context["messages"]]
    assert any("허용되지 않는 문자" in m for m in msgs), msgs
    assert Submission.objects.get(pk=sub.pk).status == "pending"


def test_schema_transition_note_with_nul_rejected(demo_users):
    from apps.schemas import services
    from apps.schemas.models import Schema
    from apps.schemas.tests.conftest import simple_payload

    s = services.save_definition(demo_users["researcher"], simple_payload(), action="publish")
    with pytest.raises(services.SchemaError):
        services.transition(s, "archive", demo_users["admin"], note="메모\x00", by_admin=True)
    assert Schema.objects.get(pk=s.pk).status == "published"


def test_free_text_problem_rules():
    from apps.core.textinput import clean_free_text, free_text_problem

    assert clean_free_text("​ 사유 ﻿") == "사유"
    assert free_text_problem("정상", 10) is None
    assert "최대 3자" in free_text_problem("네글자임", 3)[0]
    assert "허용되지 않는 문자" in free_text_problem("a\x00", 10)[0]
