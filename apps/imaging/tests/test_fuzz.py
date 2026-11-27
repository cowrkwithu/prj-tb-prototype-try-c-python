"""RAP 새 URL 전체 퍼징 — 어떤 입력에도 500 을 내지 않는다."""
import pytest

from apps.cases.models import ImagingStudy
from apps.imaging.models import ExpertAnnotation

pytestmark = pytest.mark.django_db

ATTACKS = {
    "long": "가" * 5000, "nul": "a\x00b", "control": "\r\n=cmd|' /C calc'!A0",
    "html": "<script>alert(1)</script>\"'><img src=x onerror=1>", "unicode-digits": "²٣１", "negative": "-1",
    "huge-int": "9" * 60, "float": "1e308", "nan": "NaN", "inf": "-Infinity", "json": '{"a": [1, {"b": "\\ud800"}]}',
    "deep-json": "[" * 5000, "path-traversal": "../../etc/passwd", "empty": "", "space": " ",
}
FIELDS = ("study", "shape", "x", "y", "width", "height", "plane", "slice", "finding_type", "location", "severity", "text")


def _ok(r, label):
    assert r.status_code < 500, (label, r.status_code)


@pytest.mark.parametrize("name", list(ATTACKS))
def test_no_500(client, demo_users, samples, name):
    v = ATTACKS[name]
    st = ImagingStudy.objects.filter(modality="CXR").first()
    ct = ImagingStudy.objects.filter(modality="PET-CT").first()
    for user in ("researcher", "admin"):
        client.force_login(demo_users[user])
        for url in ("/rap", "/rap/timeseries", "/rap/annotation"):
            _ok(client.get(url, {"patient": v, "study": v}), (user, url, name))
        _ok(client.get("/rap/conditions", {"finding": [v, "공동", v], "location": v, "severity": v}), name)
        _ok(client.get(f"/rap/files/{9999999999}"), name)
        good = {"study": st.pk, "shape": "rect", "x": "1", "y": "1", "width": "5", "height": "5", "finding_type": "결절"}
        for f in FIELDS:
            _ok(client.post("/rap/annotations", {**good, f: v}), (user, f, name))
            _ok(client.post("/rap/annotations", {**good, "study": ct.pk, "plane": "axial", "slice": "1", f: v}), (f, name))
        a = ExpertAnnotation.objects.create(study=st, shape="rect", x=1, y=1, width=5, height=5, finding_type="결절",
                                            location="양측", severity="경미", author=demo_users[user], author_name="x")
        for f in ("finding_type", "location", "severity", "text"):
            _ok(client.post(f"/rap/annotations/{a.pk}", {f: v}), (user, "update", f, name))
        _ok(client.post(f"/rap/annotations/{a.pk}/delete", {"x": v}), name)
    assert ExpertAnnotation.objects.count() <= 2 * 2 * len(FIELDS) + 2
