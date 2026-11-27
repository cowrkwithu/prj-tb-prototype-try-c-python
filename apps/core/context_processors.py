from .i18n import get_lang
from .navigation import BETA_SAMPLE_DOWNLOADS, build_nav


def portal(request):
    lang = get_lang(request)
    user = getattr(request, "user", None)
    nav = build_nav(request.path, lang, user)
    return {
        "lang": lang,
        "is_en": lang == "en",
        "nav_items": nav["items"],
        "nav_manual": nav["manual"],
        "beta_samples": [
            {"href": d["href"], "file": d["file"], "label": d["label"][1] if lang == "en" else d["label"][0]}
            for d in BETA_SAMPLE_DOWNLOADS
        ],
        "is_portal_admin": bool(user and user.is_authenticated and getattr(user, "is_portal_admin", False)),
    }
