"""통합 데이터사전 엑셀 → DB 적재.

원본 scripts/gen_dict.py 의 추출·매핑 규칙을 그대로 옮겼다(출력만 .ts 대신 DB).
  · 02_카테고리마스터 → DictionaryCategory (19개)
  · 03_통합데이터사전 → DictionaryField (카테고리별 UDE 순 전량)
  · 12_값세트정의(v3.1.0+) 가 있으면 값 세트 추론보다 우선
  · v3.2.0 한·영 병기 컬럼(설명, 통합 항목명(영문), 설명(영문), 제약조건(영문), 카테고리명(영문) …) 반영
"""
from __future__ import annotations

import collections
import re
from pathlib import Path

from django.db import transaction

from apps.core.seeding import is_seeded, mark_seeded

ICONS = {
    "PAT": "Users", "SOC": "Cigarette", "SCR": "ClipboardList", "DX": "Stethoscope",
    "LAB": "Microscope", "CUL": "TestTube", "DST": "ShieldAlert", "BIO": "Droplet",
    "TX": "Pill", "IMG": "ScanLine", "TPT": "ShieldCheck", "GEN": "Dna",
    "AI": "Brain", "ETC": "Settings", "SAF": "AlertTriangle", "ADH": "CalendarCheck",
    "PKA": "Syringe", "PRO": "MessageSquare", "FUN": "HeartPulse",
}
COLORS = {
    "PAT": "blue", "SOC": "orange", "SCR": "sky", "DX": "rose",
    "LAB": "emerald", "CUL": "lime", "DST": "red", "BIO": "teal",
    "TX": "amber", "IMG": "cyan", "TPT": "green", "GEN": "violet",
    "AI": "indigo", "ETC": "slate", "SAF": "pink", "ADH": "fuchsia",
    "PKA": "purple", "PRO": "yellow", "FUN": "stone",
}
# 카테고리 한국어 설명은 엑셀에 없어 원본의 수작업 설명을 유지한다.
DESCRIPTIONS = {
    "PAT": "인구통계, 인적사항, 신체계측, 활력징후",
    "SOC": "흡연·음주, 기저질환, 사회경제 지표, 취약성평가",
    "SCR": "결핵 증상, 선별 문진",
    "DX": "결핵 진단·분류, 결핵력, 환자 구분",
    "LAB": "검체 채취, 도말(AFB), 분자검사, 조직·병리",
    "CUL": "고체·액체 배양, TTP, 균 동정",
    "DST": "표현형·유전형 감수성, MIC, 내성 분류",
    "BIO": "혈액·뇨 생화학, 혈액학, 연구용 검체",
    "TX": "항결핵제 투여, 병용약물, 치료 결과 판정",
    "IMG": "흉부 X선·CT 등 영상 소견",
    "TPT": "LTBI 검사·예방치료",
    "GEN": "염기서열, 내성 변이, 계통(lineage)",
    "AI": "AI 판독 점수, 예측 확률",
    "ETC": "연구·행정 관리, 의뢰·전원, 시스템 설정",
    "SAF": "이상사례(AE), 중대한 이상사례(SAE)",
    "ADH": "복약 순응도(%), 복용 중단 사유",
    "PKA": "혈중 약물농도, AUC, Cmax, VAMS 채혈",
    "PRO": "PHQ-9, EQ-5D-5L, BIPQ, SGRQ",
    "FUN": "폐기능검사(PFT), 심전도(ECG)",
}
FACET_COLUMNS = ["측정지표", "측정단위", "방문차수", "해부부위", "세부구분", "대상구분"]


class DictionaryFormatError(ValueError):
    pass


def _g(row, H, key):
    """헤더 인덱스 맵 H로 셀 값을 읽어 공백/무의미 값을 None으로 정규화한다."""
    i = H.get(key)
    if i is None or i >= len(row):
        return None
    v = row[i]
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s == "-" or s.lower() in ("nan", "none"):
        return None
    return s


def _int_or_none(v):
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError, OverflowError):  # inf·1e400
        return None


def parse_options(raw):
    """`raw`가 열거형 코드 목록으로 보이면 [(value, label)]을 반환 (원본 parse_options 그대로)."""
    if not raw:
        return None
    seg = raw.split("／")[0].strip()
    seg = re.sub(r"^\[[^\]]*\]\s*", "", seg).strip()
    if not seg:
        return None

    if "|" in seg and ":" in seg:
        opts = []
        for tok in seg.split("|"):
            tok = tok.strip()
            if ":" not in tok:
                return None
            v, _, lab = tok.partition(":")
            v, lab = v.strip(), lab.strip()
            if not v or not lab or len(lab) > 30:
                return None
            opts.append((v, lab))
        return opts if 2 <= len(opts) <= 30 else None

    if "|" in seg and ":" not in seg and "[" not in seg:
        toks = [t.strip() for t in seg.split("|") if t.strip()]
        if 2 <= len(toks) <= 15 and all(len(t) <= 30 for t in toks):
            return [(t, t) for t in toks]

    if "/" in seg and ":" not in seg and "(" not in seg and "|" not in seg:
        toks = [t.strip() for t in seg.split("/") if t.strip()]
        if 2 <= len(toks) <= 12 and all(len(t) <= 25 for t in toks):
            return [(t, t) for t in toks]

    return None


def map_type(row, DH, valuesets):
    """→ (DataFieldType, constraints dict) — 원본 map_type 그대로."""
    raw_type = _g(row, DH, "데이터 타입") or ""
    valdef = _g(row, DH, "원천 값 정의")
    valset = _g(row, DH, "값 세트(원천 선택지)")
    c = {}

    defined = valuesets.get(_g(row, DH, "UDE"))
    if defined:
        if len(defined) == 1:
            opts = [(v, lab) for v, lab in next(iter(defined.values())) if v and lab]
            if opts:
                c["options"] = opts
                return "SELECT", c
        else:
            c["codeSystem"] = ("한정자별 값세트 " + str(len(defined)) + "벌 ("
                               + " / ".join(sorted(k for k in defined if k))
                               + ") — 12_값세트정의 참조")[:120]
            return "CODED", c
    tokens = {t.strip().lower() for t in raw_type.split("|") if t.strip()}

    if any("boolean" in t for t in tokens):
        return "BOOLEAN", c

    if any(t.startswith("date") or t in ("period", "timestamp") for t in tokens):
        if any("datetime" in t or "timestamp" in t for t in tokens):
            return "DATETIME", c
        return "DATE", c

    num_int = tokens & {"integer", "int", "bigint", "smallint"}
    num_dec = tokens & {"numeric", "double precision", "float", "real", "decimal"}
    if num_int and not num_dec:
        return "INTEGER", c
    if num_dec:
        c["precision"] = 1
        return "DECIMAL", c

    if tokens & {"codes", "coding", "category"}:
        opts = parse_options(valdef) or parse_options(valset)
        if opts:
            c["options"] = opts
            return "SELECT", c
        c["codeSystem"] = (valdef or valset or "TB Unified Code System v3.0")[:120]
        return "CODED", c

    lengths = [int(mm.group(1)) for mm in
               (re.search(r"character varying\((\d+)\)", t) for t in tokens) if mm]
    if lengths:
        c["maxLength"] = max(lengths)
        return "TEXT", c

    if any(t == "text" for t in tokens):
        if valdef and ("서술" in valdef or "자유" in valdef):
            return "TEXTAREA", c
        c["maxLength"] = 500
        return "TEXT", c

    c["maxLength"] = 200
    return "TEXT", c


def to_snake(s):
    s = re.sub(r"\[.*?\]|\(.*?\)", " ", s)
    s = s.replace("%", " pct ").replace("#", " no ").replace("+", " plus ")
    s = re.sub(r"[^0-9A-Za-z]+", "_", s)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s


def english_constraints(row, DH, cons):
    """`제약조건(영문)` → 선택지 영문 라벨·코드체계 영문. 순서가 어긋나면 적재를 중단한다."""
    text = _g(row, DH, "제약조건(영문)")
    if not text:
        return {}
    extra = {}
    ude = _g(row, DH, "UDE")
    for part in text.split(" ; "):
        key, _, body = part.partition(": ")
        if key == "Options" and "options" in cons:
            pairs = body.split(" | ")
            if len(pairs) != len(cons["options"]):
                raise DictionaryFormatError(f"{ude}: 영문 선택지 수 불일치")
            labels = []
            for (v, _), pair in zip(cons["options"], pairs):
                if not pair.startswith(f"{v}="):
                    raise DictionaryFormatError(f"{ude}: 영문 선택지 값 불일치 {v!r} ↔ {pair!r}")
                labels.append(pair[len(v) + 1:])
            extra["optionLabelsEn"] = labels
        elif key == "Code system" and "codeSystem" in cons:
            extra["codeSystemEn"] = body
    return extra


def build_description(row, DH):
    curated = _g(row, DH, "설명")
    if curated:
        return curated
    name = _g(row, DH, "통합 항목명") or _g(row, DH, "UDE")
    valdef = _g(row, DH, "원천 값 정의")
    share = _g(row, DH, "수록 사전")
    stype = _g(row, DH, "공유 유형")
    desc = name
    if valdef:
        desc += f" — {valdef}"
    meta = []
    if share:
        meta.append(f"수록: {share}")
    if stype:
        meta.append(stype)
    if meta:
        desc += f' ({" · ".join(meta)})'
    facets = [f"{axis}: {_g(row, DH, f'{axis} 한정자')}"
              for axis in FACET_COLUMNS if _g(row, DH, f"{axis} 한정자")]
    if facets:
        desc += f' [{" · ".join(facets)}]'
    note = _g(row, DH, "개정 비고")
    if note:
        desc += f" ※ {note}"
    return desc


def _clean(s):
    # 원본 ts() 는 줄바꿈을 공백으로 바꿔 내보냈다 — 같은 결과를 내기 위해 동일하게 처리
    return None if s is None else str(s).replace("\n", " ")


def parse_workbook(path: Path) -> dict:
    """엑셀을 읽어 {version, categories:[…], fields:[…]} 로 돌려준다 (DB 비의존)."""
    import openpyxl

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"통합 데이터사전 엑셀을 찾을 수 없습니다: {path}")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        version = "v3.0.0"
        for row in wb["00_개요"].iter_rows(values_only=True):
            if row and str(row[0] or "").strip() == "버전":
                version = str(row[1] or "").strip() or version
                break

        cm = list(wb["02_카테고리마스터"].iter_rows(values_only=True))
        CH = {h: i for i, h in enumerate(cm[0])}
        cats = []
        for r in cm[1:]:
            if not _g(r, CH, "CategoryCode"):
                continue
            cats.append({
                "code": _g(r, CH, "CategoryCode"), "num": _g(r, CH, "번호") or "",
                "name": _g(r, CH, "카테고리명"), "tier": _g(r, CH, "계층") or "",
                "sdtm": _g(r, CH, "CDISC SDTM 대응") or "",
                "unified": _int_or_none(_g(r, CH, "통합항목 수")),
                "source": _int_or_none(_g(r, CH, "원천항목 수")),
                "nameEn": _g(r, CH, "카테고리명(영문)"),
                "descriptionEn": _g(r, CH, "카테고리 설명(영문)"),
            })

        dd = list(wb["03_통합데이터사전"].iter_rows(values_only=True))
        DH = {h: i for i, h in enumerate(dd[0])}
        rows = [r for r in dd[1:] if _g(r, DH, "UDE")]

        valuesets = collections.defaultdict(lambda: collections.defaultdict(list))
        if "12_값세트정의" in wb.sheetnames:
            vs = list(wb["12_값세트정의"].iter_rows(values_only=True))
            VH = {h: i for i, h in enumerate(vs[0])}
            for r in vs[1:]:
                ude = _g(r, VH, "UDE")
                if ude:
                    valuesets[ude][_g(r, VH, "한정자 조건") or ""].append(
                        (_g(r, VH, "값 코드"), _g(r, VH, "값 라벨")))
    finally:
        wb.close()

    by_cat = collections.defaultdict(list)
    for r in rows:
        by_cat[_g(r, DH, "CategoryCode")].append(r)

    categories, fields, used_names = [], [], set()
    for order, cat in enumerate(cats):
        code = cat["code"]
        picked = sorted(by_cat[code], key=lambda e: _g(e, DH, "UDE"))
        categories.append({
            "code": code.lower(), "num": cat["num"],
            "name": _clean(f"{cat['num']} {cat['name']}"),
            "name_en": _clean(f"{cat['num']} {cat['nameEn']}") if cat["nameEn"] else "",
            "description": _clean(DESCRIPTIONS.get(code, cat["name"])),
            "description_en": _clean(cat["descriptionEn"]) or "",
            "icon": ICONS.get(code, ""), "color": COLORS.get(code, ""),
            "tier": cat["tier"], "sdtm": cat["sdtm"],
            "unified_count": cat["unified"], "source_count": cat["source"],
            "order": order, "field_count": len(picked),
        })
        for i, r in enumerate(picked):
            ude = _g(r, DH, "UDE")
            dtype, cons = map_type(r, DH, valuesets)
            fn = to_snake(ude)
            base, n = fn, 2
            while fn in used_names:
                fn = f"{base}_{n}"
                n += 1
            used_names.add(fn)

            en = english_constraints(r, DH, cons)
            options = []
            if "options" in cons:
                labels_en = en.get("optionLabelsEn") or [None] * len(cons["options"])
                for (ov, ol), oe in zip(cons.pop("options"), labels_en):
                    opt = {"value": _clean(ov), "label": _clean(ol)}
                    if oe is not None:
                        opt["labelEn"] = _clean(oe)
                    options.append(opt)
            if "codeSystemEn" in en:
                cons["codeSystemEn"] = en["codeSystemEn"]
            cons = {k: (_clean(v) if isinstance(v, str) else v) for k, v in cons.items()}

            fields.append({
                "category": code.lower(), "dictionary_id": ude, "field_name": fn,
                "display_name": _clean(_g(r, DH, "통합 항목명") or ude),
                "display_name_en": _clean(_g(r, DH, "통합 항목명(영문)")) or "",
                "description": _clean(build_description(r, DH)),
                "description_en": _clean(_g(r, DH, "설명(영문)")) or "",
                "data_type": dtype, "constraints": cons, "options": options,
                "cdisc_mapping": _clean(_g(r, DH, "SDTM 도메인(참고)")) or "",
                "order": i,
            })
    return {"version": version, "source_file": path.name, "categories": categories, "fields": fields}


DATASET = "dictionary"
FIELD_UPDATE_COLS = ("category", "field_name", "display_name", "display_name_en", "description", "description_en",
                     "data_type", "constraints", "options", "cdisc_mapping", "order", "is_standard", "is_deprecated")


def load_dictionary(path: Path, force: bool = False) -> dict:
    """엑셀을 DB 에 적재한다.

    - 적재 기록(SeedLog)이 있으면 건너뛴다(멱등, 관리자 편집 보존). force=True 면 다시 반영한다.
    - 같은 버전을 다시 반영할 때는 지우고 새로 만들지 않고 UDE 기준으로 갱신한다(update_or_create).
      엑셀에서 사라진 항목은 삭제하지 않고 is_deprecated=True — 이후 단계의 FK 참조가 CASCADE 로 지워지지 않게.
    - 다른 버전의 엑셀이면 새 DictionaryVersion 을 만들고 현행(is_active)으로 바꾼다. 이전 버전 행은 그대로 둔다.
    """
    from .models import DictionaryCategory, DictionaryField, DictionaryVersion

    if is_seeded(DATASET) and not force:
        ver = active_version()
        return {"version": ver.version if ver else None, "loaded": False, "skipped": True,
                "categories": ver.categories.count() if ver else 0,
                "fields": ver.fields.filter(is_deprecated=False).count() if ver else 0}

    parsed = parse_workbook(Path(path))
    with transaction.atomic():
        previous = active_version()
        ver, ver_created = DictionaryVersion.objects.get_or_create(version=parsed["version"],
                                                                   defaults={"source_file": parsed["source_file"]})
        ver.source_file = parsed["source_file"]
        ver.is_active = True
        ver.save()
        DictionaryVersion.objects.exclude(pk=ver.pk).update(is_active=False)

        cat_objs = {}
        for c in parsed["categories"]:
            data = {k: v for k, v in c.items() if k not in ("field_count", "code")}
            cat_objs[c["code"]], _ = DictionaryCategory.objects.update_or_create(
                version=ver, code=c["code"], defaults=data)

        existing = {f.dictionary_id: f for f in DictionaryField.objects.filter(version=ver)}
        seen, to_create, to_update = set(), [], []
        for f in parsed["fields"]:
            values = {k: v for k, v in f.items() if k not in ("category", "dictionary_id")}
            values.update(category=cat_objs[f["category"]], is_standard=True, is_deprecated=False)
            seen.add(f["dictionary_id"])
            obj = existing.get(f["dictionary_id"])
            if obj is None:
                to_create.append(DictionaryField(version=ver, dictionary_id=f["dictionary_id"], **values))
            else:
                for k, v in values.items():
                    setattr(obj, k, v)
                to_update.append(obj)
        gone = [obj for ude, obj in existing.items() if ude not in seen and obj.is_standard]
        for obj in gone:
            obj.is_deprecated = True
        # field_name 고유 제약과 충돌하지 않도록 갱신 → 생성 순으로 반영
        DictionaryField.objects.bulk_update(to_update + gone, FIELD_UPDATE_COLS, batch_size=500)
        DictionaryField.objects.bulk_create(to_create, batch_size=500)
        carried = 0
        if ver_created and previous is not None and previous.pk != ver.pk:
            carried = carry_over_custom(previous, ver)

        result = {"version": ver.version, "loaded": True, "skipped": False,
                  "categories": len(parsed["categories"]), "fields": len(parsed["fields"]),
                  "created": len(to_create), "updated": len(to_update), "deprecated": len(gone),
                  "carried_custom": carried}
        mark_seeded(DATASET, {k: result[k] for k in ("version", "created", "updated", "deprecated")})
    return result


def carry_over_custom(previous, ver) -> int:
    """새 버전 적재 시 관리자가 직접 추가한 항목(is_standard=False)과 엑셀에 없는 카테고리를 새 현행 버전으로 옮겨 적는다.

    이전 버전의 행은 그대로 둔다(스키마 필드의 FK 참조 보존). 같은 필드명·UDE 가 새 버전에 이미 있으면 건너뛴다.
    """
    from .models import DictionaryCategory, DictionaryField

    cats = {c.code: c for c in DictionaryCategory.objects.filter(version=ver)}
    next_order = max([c.order for c in cats.values()], default=-1) + 1
    for c in DictionaryCategory.objects.filter(version=previous).order_by("order"):
        if c.code in cats:
            continue
        cats[c.code] = DictionaryCategory.objects.create(
            version=ver, code=c.code, num=c.num, name=c.name, name_en=c.name_en, description=c.description,
            description_en=c.description_en, icon=c.icon, color=c.color, tier=c.tier, sdtm=c.sdtm, order=next_order)
        next_order += 1
    taken_names = set(DictionaryField.objects.filter(version=ver).values_list("field_name", flat=True))
    taken_ids = set(DictionaryField.objects.filter(version=ver).values_list("dictionary_id", flat=True))
    objs = []
    for f in DictionaryField.objects.filter(version=previous, is_standard=False, is_deprecated=False).select_related("category"):
        if f.field_name in taken_names or f.dictionary_id in taken_ids:
            continue
        objs.append(DictionaryField(
            version=ver, category=cats[f.category.code], dictionary_id=f.dictionary_id, field_name=f.field_name,
            display_name=f.display_name, display_name_en=f.display_name_en, description=f.description,
            description_en=f.description_en, data_type=f.data_type, constraints=f.constraints, options=f.options,
            is_standard=False, hl7_fhir_mapping=f.hl7_fhir_mapping, cdisc_mapping=f.cdisc_mapping, order=f.order))
    DictionaryField.objects.bulk_create(objs)
    return len(objs)


def active_version():
    from .models import DictionaryVersion

    return DictionaryVersion.objects.filter(is_active=True).order_by("-loaded_at").first()
