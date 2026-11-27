"""관리 → 데이터사전 관리 (원본 pages/Admin/DictionaryManager.tsx) — 카테고리·항목 CRUD, 버전·엑셀 적재, 변경 이력."""
from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.decorators import admin_required
from apps.core.i18n import get_lang, t
from apps.core.views import render_page

from . import admin_services as svc
from . import viewer
from .admin_services import DictAdminError
from .models import DictionaryCategory, DictionaryChangeLog, DictionaryField, DictionaryVersion
from .services import active_version

ENTRY_PAGE = 20
VIEWS = ("entries", "versions", "history")


def _int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _url(**kw):
    pairs = [(k, v) for k, v in kw.items() if v not in (None, "", 1) or (k == "page" and v not in (None, 1))]
    pairs = [(k, v) for k, v in pairs if not (k == "view" and v == "entries")]
    return "/admin/dictionary" + ("?" + urlencode(pairs) if pairs else "")


def _err(request, e):
    messages.error(request, t(request, e.ko, e.en))


@admin_required
@require_safe
def manager(request):
    lang = get_lang(request)
    view = request.GET.get("view") if request.GET.get("view") in VIEWS else "entries"
    ver = active_version()
    ctx = {"view": view, "ver": ver,
           "tabs": [{"key": "entries", "label": t(request, "카테고리·항목", "Categories & Items"), "url": _url(view="entries")},
                    {"key": "versions", "label": t(request, "버전·엑셀 적재", "Versions & Excel Load"), "url": _url(view="versions")},
                    {"key": "history", "label": t(request, "변경 이력", "Change History"), "url": _url(view="history")}]}
    if view == "entries" and ver is not None:
        cats = svc.categories_with_counts(ver)
        codes = [c.code for c in cats]
        cat_code = request.GET.get("cat") if request.GET.get("cat") in codes else (codes[0] if codes else "")
        show_dep = request.GET.get("dep") == "1"
        cur = next((c for c in cats if c.code == cat_code), None)
        entries = DictionaryField.objects.filter(version=ver, category=cur).order_by("order", "id") if cur else DictionaryField.objects.none()
        if not show_dep:
            entries = entries.filter(is_deprecated=False)
        q = (request.GET.get("q") or "").strip()[:100]
        if q:
            entries = entries.filter(Q(display_name__icontains=q) | Q(display_name_en__icontains=q) |
                                     Q(field_name__icontains=q) | Q(dictionary_id__icontains=q))
        pager = Paginator(entries, ENTRY_PAGE)
        page = pager.get_page(_int(request.GET.get("page"), 1))
        ref_counts = dict(DictionaryField.objects.filter(pk__in=[e.pk for e in page.object_list])
                          .annotate(n=Count("schema_fields")).values_list("pk", "n"))
        base = {"cat": cat_code, "dep": "1" if show_dep else "", "q": q}
        ctx.update({
            "cats": [{"c": c, "name": viewer.category_text(c, lang)["name"],
                      "icon": viewer.CATEGORY_ICON_FILES.get(c.icon, "folder"), "url": _url(cat=c.code, dep=base["dep"]),
                      "active": c.code == cat_code} for c in cats],
            "cur": cur, "cur_text": viewer.category_text(cur, lang) if cur else None,
            "entries": [{"e": e, **viewer.entry_text(e, lang), "type_label": viewer.type_label(e.data_type, lang),
                         "refs": ref_counts.get(e.pk, 0)} for e in page.object_list],
            "page": page, "total": pager.count, "show_dep": show_dep, "q": q,
            "prev_url": _url(**base, page=page.number - 1) if page.has_previous() else None,
            "next_url": _url(**base, page=page.number + 1) if page.has_next() else None,
            "toggle_dep_url": _url(cat=cat_code, dep="" if show_dep else "1", q=q),
        })
    elif view == "versions":
        ctx["versions"] = list(DictionaryVersion.objects.annotate(
            n_fields=Count("fields", filter=Q(fields__is_deprecated=False), distinct=True),
            n_dep=Count("fields", filter=Q(fields__is_deprecated=True), distinct=True),
            n_custom=Count("fields", filter=Q(fields__is_standard=False, fields__is_deprecated=False), distinct=True),
        ).order_by("-loaded_at"))
    elif view == "history":
        logs = DictionaryChangeLog.objects.all()
        pager = Paginator(logs, 30)
        page = pager.get_page(_int(request.GET.get("page"), 1))
        ctx.update({"logs": [{"l": lg, "action": lg.get_action_display() if lang != "en" else ACTION_EN.get(lg.action, lg.action),
                              "detail": _detail_text(lg)} for lg in page.object_list],
                    "page": page,
                    "prev_url": _url(view="history", page=page.number - 1) if page.has_previous() else None,
                    "next_url": _url(view="history", page=page.number + 1) if page.has_next() else None})
    return render_page(request, "dictionary/admin/manager.html", ctx, sidebar="admin")


ACTION_EN = {"category_add": "Category added", "category_edit": "Category edited", "category_delete": "Category deleted",
             "entry_add": "Item added", "entry_edit": "Item edited", "entry_deprecate": "Item deprecated",
             "entry_restore": "Item restored", "version_load": "Excel loaded", "version_activate": "Current version switched"}


def _detail_text(lg) -> str:
    d = lg.detail or {}
    parts = []
    for k, v in d.items():
        if isinstance(v, list) and len(v) == 2:
            parts.append(f"{k}: {v[0]} → {v[1]}")
        else:
            parts.append(f"{k}: {v}")
    return " · ".join(parts)[:400]


# ── 모달 ──
@admin_required
@require_safe
def category_modal(request):
    lang = get_lang(request)
    cat = None
    if request.GET.get("id"):
        cat = get_object_or_404(DictionaryCategory, pk=_int(request.GET.get("id"), 0))
    return render(request, "dictionary/admin/_category_modal.html", {
        "cat": cat, "icons": [(v, en if lang == "en" else ko, viewer.CATEGORY_ICON_FILES.get(v, "folder")) for v, ko, en in svc.ICON_OPTIONS],
        "colors": [(v, en if lang == "en" else ko) for v, ko, en in svc.COLOR_OPTIONS]})


@admin_required
@require_safe
def entry_modal(request):
    lang = get_lang(request)
    entry = cat = None
    if request.GET.get("id"):
        entry = get_object_or_404(DictionaryField.objects.select_related("category"), pk=_int(request.GET.get("id"), 0))
        cat = entry.category
    else:
        cat = get_object_or_404(DictionaryCategory, pk=_int(request.GET.get("cat"), 0))
    c = (entry.constraints or {}) if entry else {}
    return render(request, "dictionary/admin/_entry_modal.html", {
        "entry": entry, "cat": cat, "c": c,
        "options_text": ", ".join(o.get("value", "") for o in (entry.options if entry else [])),
        "types": [(tp, viewer.type_label(tp, lang)) for tp in viewer.DATA_TYPES],
        "current_type": entry.data_type if entry else "TEXT",
        # Alpine 에는 json_script 로만 넘긴다(속성 안에 JSON 을 직접 넣지 않음)
        "type_sets": {"range": sorted(svc.RANGE_TYPES), "length": sorted(svc.LENGTH_TYPES), "option": sorted(svc.OPTION_TYPES)}})


def _back_to(request, cat_code=None):
    return redirect(_url(cat=cat_code or request.POST.get("cat_code") or ""))


@admin_required
@require_POST
def category_save(request):
    try:
        if request.POST.get("id"):
            cat = get_object_or_404(DictionaryCategory, pk=_int(request.POST.get("id"), 0))
            svc.edit_category(request.user, cat, request.POST)
            messages.success(request, t(request, f'"{cat.name}" 카테고리가 수정되었습니다', f'Category "{cat.name}" updated'))
        else:
            cat = svc.add_category(request.user, request.POST)
            messages.success(request, t(request, f'"{cat.name}" 카테고리가 추가되었습니다', f'Category "{cat.name}" added'))
        return _back_to(request, cat.code)
    except DictAdminError as e:
        _err(request, e)
        return _back_to(request)


@admin_required
@require_POST
def category_delete(request, pk):
    cat = get_object_or_404(DictionaryCategory, pk=pk)
    try:
        svc.delete_category(request.user, cat)
        messages.success(request, t(request, f'"{cat.name}" 카테고리가 삭제되었습니다', f'Category "{cat.name}" deleted'))
        return redirect("/admin/dictionary")
    except DictAdminError as e:
        _err(request, e)
        return _back_to(request, cat.code)


@admin_required
@require_POST
def entry_save(request):
    try:
        if request.POST.get("id"):
            f = get_object_or_404(DictionaryField.objects.select_related("category", "version"), pk=_int(request.POST.get("id"), 0))
            svc.edit_entry(request.user, f, request.POST)
            messages.success(request, t(request, f'"{f.display_name}" 항목이 수정되었습니다', f'Item "{f.display_name}" updated'))
        else:
            cat = get_object_or_404(DictionaryCategory, pk=_int(request.POST.get("cat"), 0))
            f = svc.add_entry(request.user, cat, request.POST)
            messages.success(request, t(request, f'"{f.display_name}" 항목이 추가되었습니다', f'Item "{f.display_name}" added'))
        return _back_to(request, f.category.code)
    except DictAdminError as e:
        _err(request, e)
        return _back_to(request)


@admin_required
@require_POST
def entry_deprecate(request, pk):
    f = get_object_or_404(DictionaryField.objects.select_related("category", "version"), pk=pk)
    restore = request.POST.get("restore") == "1"
    if svc.set_deprecated(request.user, f, not restore):
        messages.success(request, t(request, f'"{f.display_name}" 항목을 {"복원" if restore else "폐기"}했습니다',
                                    f'Item "{f.display_name}" {"restored" if restore else "deprecated"}'))
    else:
        messages.info(request, t(request, "이미 처리된 항목입니다", "Already processed"))
    return redirect(_url(cat=f.category.code, dep="1" if restore or request.POST.get("dep") == "1" else ""))


@admin_required
@require_POST
def version_activate(request, pk):
    ver = get_object_or_404(DictionaryVersion, pk=pk)
    if svc.activate_version(request.user, ver):
        messages.success(request, t(request, f"{ver.version} 을(를) 현행 버전으로 전환했습니다", f"{ver.version} is now the current version"))
    return redirect(_url(view="versions"))


@admin_required
@require_POST
def version_upload(request):
    up = request.FILES.get("file")
    if not up:
        messages.error(request, t(request, "엑셀 파일을 선택하세요", "Select an Excel file"))
        return redirect(_url(view="versions"))
    try:
        res = svc.import_workbook(request.user, up)
    except DictAdminError as e:
        _err(request, e)
        return redirect(_url(view="versions"))
    messages.success(request, t(
        request,
        f"{res['version']} 적재 완료 — 추가 {res['created']} · 갱신 {res['updated']} · 폐기 {res['deprecated']} · 사용자 항목 이관 {res.get('carried_custom', 0)}",
        f"Loaded {res['version']} — created {res['created']}, updated {res['updated']}, deprecated {res['deprecated']}, custom carried {res.get('carried_custom', 0)}"))
    return redirect(_url(view="versions"))


_ = HttpResponse
