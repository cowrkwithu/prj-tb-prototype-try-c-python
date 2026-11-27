"""관리 → 데이터 구조 관리 — 원본 pages/Admin/SchemaManager.tsx."""
from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import admin_required
from apps.core.i18n import get_lang, t
from apps.core.views import render_page

from . import catalog, services, ui
from .catalog import pick
from .models import Schema, SchemaLog, SchemaRecord
from .services import SchemaError


class State:
    def __init__(self, g):
        self.q = (g.get("q") or "").strip()[:100]
        self.status = g.get("status") if g.get("status") in catalog.STATUS else ""
        self.category = g.get("category") if g.get("category") in catalog.CATEGORY else ""
        self.visibility = g.get("visibility") if g.get("visibility") in catalog.VISIBILITY else ""

    def url(self, **over):
        d = {"q": self.q, "status": self.status, "category": self.category, "visibility": self.visibility}
        d.update(over)
        pairs = [(k, v) for k, v in d.items() if v]
        return "/admin/schemas" + ("?" + urlencode(pairs) if pairs else "")


def _admin_row(s, lang):
    card = ui.schema_card(s, lang)
    card["status"] = catalog.status_info(s.status, lang, admin=True)
    card["visibility_label"] = catalog.visibility_label(s.visibility, lang, "admin")
    card["actions"] = services.actions_for(s, s.data_count)
    return card


ACTION_BTN = {
    "publish": ("send", "btn-teal", ("발행", "Publish"),
                ("초안을 발행해 연구자가 데이터 입력에 쓸 수 있게 합니다.", "Publish this draft so researchers can enter data with it.")),
    "archive": ("layers", "btn-amber", ("보관", "Archive"),
                ("보관하면 새 데이터 입력이 막히고, 입력된 데이터와 구조 정의는 그대로 남습니다.",
                 "Archiving blocks new data entry; existing data and the definition are kept.")),
    "restore": ("refresh-cw", "btn-outline-teal", ("복원", "Restore"), ("보관을 풀고 발행 상태로 되돌립니다.", "Unarchive and return to published.")),
    "to_draft": ("arrow-left", "btn-secondary", ("초안으로", "To draft"),
                 ("입력된 데이터가 없는 구조를 초안으로 되돌려 작성자가 다시 고칠 수 있게 합니다.",
                  "Return a schema with no data to draft so the owner can revise it.")),
}


def _buttons(actions, lang):
    return [{"action": a, "icon": ACTION_BTN[a][0], "cls": ACTION_BTN[a][1], "label": pick(lang, *ACTION_BTN[a][2]),
             "tip": pick(lang, *ACTION_BTN[a][3])} for a in actions]


@admin_required
@require_safe
def manager(request):
    lang = get_lang(request)
    st = State(request.GET)
    counts = Schema.objects.aggregate(total=Count("id"), published=Count("id", filter=Q(status="published")),
                                      draft=Count("id", filter=Q(status="draft")),
                                      archived=Count("id", filter=Q(status="archived")))
    counts["records"] = SchemaRecord.objects.count()
    qs = services.with_counts(Schema.objects.all())
    if st.status:
        qs = qs.filter(status=st.status)
    if st.category:
        qs = qs.filter(category=st.category)
    if st.visibility:
        qs = qs.filter(visibility=st.visibility)
    items = services.search_schemas(qs, st.q)
    items.sort(key=lambda s: s.updated_at, reverse=True)
    rows = []
    for s in items:
        r = _admin_row(s, lang)
        r["buttons"] = _buttons(r["actions"], lang)
        rows.append(r)
    ctx = {"state": st, "rows": rows, "counts": counts,
           "summary": [
               {"key": "", "label": t(request, "전체 구조", "All Schemas"), "value": counts["total"], "cls": "sum-blue", "url": st.url(status="")},
               {"key": "published", "label": t(request, "발행됨", "Published"), "value": counts["published"], "cls": "sum-green", "url": st.url(status="published")},
               {"key": "draft", "label": t(request, "초안(발행 대기)", "Draft (pending)"), "value": counts["draft"], "cls": "sum-gray", "url": st.url(status="draft")},
               {"key": "archived", "label": t(request, "보관됨", "Archived"), "value": counts["archived"], "cls": "sum-amber", "url": st.url(status="archived")},
           ],
           "categories": [(c[0], c[2] if lang == "en" else c[1]) for c in catalog.CATEGORIES],
           "statuses": [(k, catalog.status_info(k, lang)["label"]) for k in ("draft", "published", "archived")],
           "visibilities": [(k, catalog.visibility_label(k, lang, "admin")) for k in catalog.VISIBILITY]}
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "schema-admin-list":
        return render(request, "schemas/admin/_list.html", ctx)
    return render_page(request, "schemas/admin/manager.html", ctx, sidebar="admin")


@admin_required
@require_safe
def admin_detail(request, schema_id):
    lang = get_lang(request)
    s = get_object_or_404(services.with_counts(Schema.objects.all()), schema_id=schema_id)
    fields = list(s.fields.all())
    groups = {g["groupName"]: pick(lang, g.get("displayName") or g["groupName"], g.get("displayNameEn")) for g in s.field_groups}
    row = _admin_row(s, lang)
    history = []
    for lg in s.logs.filter(by_admin=True)[:50]:
        detail = ""
        if lg.action == "visibility" and "→" in lg.detail:
            a, b = lg.detail.split("→", 1)
            detail = f"{catalog.visibility_label(a, lang, 'admin')} → {catalog.visibility_label(b, lang, 'admin')}"
        history.append({"at": timezone.localtime(lg.at).date().isoformat(), "by": lg.actor_label,
                        "action": pick(lang, *services.ACTION_LABEL.get(lg.action, (lg.action, lg.action))),
                        "detail": detail, "note": lg.note})
    return render(request, "schemas/admin/_detail.html", {
        "s": s, "row": row, "buttons": _buttons(row["actions"], lang),
        "fields": [{"f": f, "display": pick(lang, f.display_name, f.display_name_en),
                    "type": catalog.type_label(f.data_type, lang), "group": groups.get(f.group, "—") if f.group else "—"}
                   for f in fields],
        "dict_count": sum(1 for f in fields if f.source_dictionary_id),
        "rules": [pick(lang, r.get("name", ""), r.get("nameEn")) for r in s.validation_rules],
        "history": history, "visibilities": [(k, catalog.visibility_label(k, lang, "admin")) for k in catalog.VISIBILITY],
        "link": f"{pick(lang, s.link_display_name or s.link_field_name, s.link_display_name_en)} ({s.link_field_name})"})


@admin_required
@require_safe
def admin_confirm(request, schema_id):
    """처리 확인 모달(메모 입력)."""
    lang = get_lang(request)
    s = get_object_or_404(services.with_counts(Schema.objects.all()), schema_id=schema_id)
    action = request.GET.get("action")
    if action not in services.TRANSITIONS and action != "delete":
        return redirect("/admin/schemas")
    label = pick(lang, *services.ACTION_LABEL[action]) if action != "delete" else t(request, "삭제", "Delete")
    return render(request, "schemas/admin/_confirm.html", {"s": s, "action": action, "label": label,
                                                           "name": pick(lang, s.name, s.name_en),
                                                           "label_ko": services.ACTION_LABEL.get(action, ("삭제",))[0],
                                                           "back": request.GET.get("back") or "/admin/schemas"})


def _back(request):
    b = request.POST.get("back") or "/admin/schemas"
    return b if b.startswith("/admin/schemas") and "//" not in b else "/admin/schemas"


@admin_required
@require_POST
def admin_action(request, schema_id):
    s = get_object_or_404(Schema, schema_id=schema_id)
    action = request.POST.get("action")
    lang = get_lang(request)
    try:
        services.transition(s, action, request.user, note=(request.POST.get("note") or "").strip()[:500], by_admin=True)
        ko, en = services.ACTION_LABEL[action]
        messages.success(request, t(request, f"'{s.name}' — {ko} 처리되었습니다", f"'{pick('en', s.name, s.name_en)}' — {en}"))
    except SchemaError as e:
        messages.error(request, t(request, e.ko, e.en))
    _ = lang
    return redirect(_back(request))


@admin_required
@require_POST
def admin_visibility(request, schema_id):
    s = get_object_or_404(Schema, schema_id=schema_id)
    v = request.POST.get("visibility")
    try:
        if services.change_visibility(s, v, request.user, by_admin=True):
            messages.success(request, t(request, f"'{s.name}' 공개 범위를 '{catalog.visibility_label(v, 'ko', 'admin')}'(으)로 바꿨습니다",
                                        f"Visibility of '{pick('en', s.name, s.name_en)}' set to {catalog.visibility_label(v, 'en', 'admin')}"))
    except SchemaError as e:
        messages.error(request, t(request, e.ko, e.en))
    return redirect(_back(request))


@admin_required
@require_POST
def admin_delete(request, schema_id):
    s = get_object_or_404(Schema, schema_id=schema_id)
    try:
        services.delete_schema(s)
        messages.success(request, t(request, f"'{s.name}' 데이터 구조를 삭제했습니다", f"Deleted data schema '{pick('en', s.name, s.name_en)}'"))
    except SchemaError as e:
        messages.error(request, t(request, e.ko, e.en))
    return redirect(_back(request))


_ = SchemaLog
