"""GAP 새 URL 전체 퍼징 — 어떤 입력에도 500 을 내지 않는다(CLAUDE.md, datasharing/tests/test_fuzz.py 패턴)."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.genomics.models import PredictionRun

from .conftest import approve_genomics

pytestmark = pytest.mark.django_db

ATTACKS = {
    "long": "가" * 5000, "nul": "a\x00b", "control": "\r\n=cmd|' /C calc'!A0",
    "html": "<script>alert(1)</script>\"'><img src=x onerror=1>", "unicode-digits": "²٣１", "negative": "-1",
    "huge-int": "9" * 60, "float": "1e308", "nan": "NaN", "json": '{"a": [1, {"b": "\\ud800"}]}', "deep-json": "[" * 5000,
    "path-traversal": "../../etc/passwd", "empty": "", "space": " ", "comma": "1,2,3", "pct": "%00%ff",
}
GET_PARAMS = ("q", "gene", "drug", "aa", "pos_min", "pos_max", "min_freq", "searched", "page", "run", "cohort",
              "method", "p", "next")
GET_URLS = ["/gap", "/gap/search", "/gap/search/export.csv", "/gap/resistance", "/gap/gwas", "/gap/similarity",
            "/gap/variants/1", "/gap/variants/0", "/gap/variants/9999999999"]


def _ok(r, label):
    assert r.status_code < 500, (label, r.status_code)


@pytest.mark.parametrize("name", list(ATTACKS))
def test_get_no_500(client, demo_users, catalog, name):
    v = ATTACKS[name]
    approve_genomics(demo_users["researcher"])
    for user in ("researcher", "admin"):
        client.force_login(demo_users[user])
        for url in GET_URLS:
            _ok(client.get(url, {p: v for p in GET_PARAMS}), (user, url, name))
            _ok(client.get(url, {"searched": "1", "run": "1", "p": [v, "2", v], "drug": v}), (user, url, name, "list"))
        _ok(client.get("/gap", {"q": v}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="gap-variant-table"), name)
        _ok(client.get("/gap/gwas", {"run": "1", "drug": "RIF", "cohort": v}), name)
        _ok(client.get("/gap/similarity", {"run": "1", "method": v, "p": [v] * 60}), name)


@pytest.mark.parametrize("name", list(ATTACKS))
def test_post_no_500(client, demo_users, catalog, name):
    v = ATTACKS[name]
    client.force_login(demo_users["researcher"])
    posts = [
        {"source": v, "patient": v, "algorithms": [v]},
        {"source": "patient", "patient": v, "algorithms": ["tb-profiler", v]},
        {"source": "upload", "algorithms": "tb-profiler", "text": v, "assume_wgs": v},
        {"source": "upload", "algorithms": ["tb-profiler", "mykrobe"], "text": f"mutation\n{v}\nrpoB {v}\n"},
        {"source": "upload", "algorithms": "resfinder", "text": f"##fileformat=VCF\n#CHROM\nX\t{v}\t.\t{v}\t{v}\t.\t{v}\n"},
        {"source": "upload", "algorithms": "tb-profiler", "text": f"position,ref,alt\n{v},{v},{v}\n"},
    ]
    for data in posts:
        _ok(client.post("/gap/resistance", data), (name, data.get("source")))
    for fname in (v[:100] or "x", "a.csv", "a.vcf"):
        f = SimpleUploadedFile(fname, v.encode("utf-8", "surrogatepass")[:5000])
        _ok(client.post("/gap/resistance", {"source": "upload", "algorithms": "tb-profiler", "file": f}), (name, fname))
    for k in ("gene", "drug", "aa", "pos_min", "pos_max", "min_freq"):
        _ok(client.post("/gap/search/save", {k: v}), (name, k))
    assert PredictionRun.objects.count() <= 12


def test_binary_and_odd_uploads(client, demo_users, catalog):
    client.force_login(demo_users["researcher"])
    for raw in (b"\xff\xfe\x00", b"\xed\xa0\x80", bytes(range(256)), b"\n" * 100000, b"a,b\n" + b"\"" * 5000,
                "  \x85".encode() * 1000, b"\t" * 9000):
        f = SimpleUploadedFile("a.csv", raw)
        _ok(client.post("/gap/resistance", {"source": "upload", "algorithms": "tb-profiler", "file": f}), raw[:10])
