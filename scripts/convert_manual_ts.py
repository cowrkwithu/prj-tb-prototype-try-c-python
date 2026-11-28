#!/usr/bin/env python3
"""원본 사용자 매뉴얼(src/pages/Manual/manualContent.ts)을 data/seed/manual.json 으로 변환하는 일회성 스크립트.

Node.js 없이 scripts/convert_mock_ts.py 의 TypeScript 리터럴 파서로 MANUAL_SECTIONS 를 읽는다.
그 뒤 data/seed/manual_overrides.json 의 수정(Python 판의 실제 동작과 다른 원본 문구 교정)을 적용해 저장한다.
각 수정은 원본 한국어 문구의 앞부분(expect_ko)을 확인한 뒤 적용한다 — 원본이 바뀌어 위치가 어긋나면 중단한다.

사용법:
    python scripts/convert_manual_ts.py [--src <원본 저장소 루트>] [--check]

--check: 파일을 쓰지 않고, 지금 data/seed/manual.json 이 변환 결과와 같은지만 확인한다(같으면 종료 코드 0).
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.convert_mock_ts import DEFAULT_SRC, TsLiteralParser, TsParseError, _skip_type_annotation  # noqa: E402

OUT = ROOT / "data" / "seed" / "manual.json"
OVERRIDES = ROOT / "data" / "seed" / "manual_overrides.json"
KINDS = {"paragraph", "steps", "list", "table", "note", "path", "image"}


def extract_export(text: str, name: str):
    """`export const NAME(: Type)? = <literal>` 하나만 읽는다(다른 export 의 `as const` 등은 건드리지 않는다)."""
    m = re.search(rf"export\s+const\s+{re.escape(name)}\b\s*", text)
    if not m:
        raise TsParseError(f"{name} 를 찾지 못함")
    i = m.end()
    if text[i:i + 1] == ":":
        i = _skip_type_annotation(text, i + 1)
    if text[i:i + 1] != "=":
        raise TsParseError(f"{name}: '=' 필요 @ {i}")
    return TsLiteralParser(text, i + 1).value()


def _is_l(v) -> bool:
    return isinstance(v, dict) and set(v) == {"ko", "en"} and all(isinstance(x, str) for x in v.values())


def validate(sections) -> dict:
    """구조 검사 — 모든 문구는 {ko, en} 쌍, 블록 종류는 허용 목록. 건수를 돌려준다."""
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a non-empty list")
    n_topics = n_blocks = 0
    keys = set()
    for s in sections:
        for k in ("id", "no", "icon"):
            if not isinstance(s.get(k), str) or not s[k]:
                raise ValueError(f"section {k}: {s.get('id')}")
        if not (_is_l(s.get("title")) and _is_l(s.get("summary"))):
            raise ValueError(f"section title/summary: {s['id']}")
        for t in s.get("topics") or []:
            if not isinstance(t.get("id"), str) or t["id"] in keys or not _is_l(t.get("title")):
                raise ValueError(f"topic: {t.get('id')}")
            keys.add(t["id"])
            n_topics += 1
            for b in t.get("blocks") or []:
                n_blocks += 1
                kind = b.get("kind")
                if kind not in KINDS:
                    raise ValueError(f"block kind {kind!r} in {t['id']}")
                ok = {
                    "paragraph": lambda: _is_l(b.get("text")),
                    "note": lambda: _is_l(b.get("text")),
                    "steps": lambda: isinstance(b.get("items"), list) and all(map(_is_l, b["items"])),
                    "list": lambda: isinstance(b.get("items"), list) and all(map(_is_l, b["items"])),
                    "table": lambda: isinstance(b.get("headers"), list) and all(map(_is_l, b["headers"]))
                    and isinstance(b.get("rows"), list)
                    and all(isinstance(r, list) and all(map(_is_l, r)) for r in b["rows"]),
                    "path": lambda: _is_l(b.get("label")) and _is_l(b.get("value")),
                    "image": lambda: isinstance(b.get("src"), str) and b["src"].startswith("/manual/")
                    and _is_l(b.get("caption")),
                }[kind]()
                if not ok:
                    raise ValueError(f"block {kind} malformed in {t['id']}")
    return {"sections": len(sections), "topics": n_topics, "blocks": n_blocks}


def _topic(sections, key):
    for s in sections:
        for t in s["topics"]:
            if t["id"] == key:
                return t
    raise KeyError(f"topic {key}")


def _section(sections, key):
    for s in sections:
        if s["id"] == key:
            return s
    raise KeyError(f"section {key}")


def _get(node, path):
    for p in path:
        node = node[p]
    return node


def _set(node, path, value):
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value


def apply_overrides(sections, overrides: list[dict]) -> list:
    """수정 적용. set(문구 교체)을 먼저, 그다음 블록 delete/insert_after 를 블록 번호 내림차순으로(원본 번호 기준)."""
    out = copy.deepcopy(sections)
    structural = []
    for i, ov in enumerate(overrides):
        op = ov["op"]
        if op == "set":
            target = _section(out, ov["section"]) if "section" in ov else _topic(out, ov["topic"])["blocks"][ov["block"]]
            if "topic" in ov and "block" not in ov:
                raise ValueError(f"override {i}: block required")
            cur = _get(target, ov["path"])
            exp = ov.get("expect_ko", "")
            if not (_is_l(cur) and cur["ko"].startswith(exp)):
                raise ValueError(f"override {i} ({ov.get('id')}): 원본 문구가 예상과 다름 — {cur!r:.120}")
            if not _is_l(ov["value"]):
                raise ValueError(f"override {i}: value must be {{ko, en}}")
            _set(target, ov["path"], ov["value"])
        elif op in ("delete", "insert_after"):
            structural.append(ov)
        else:
            raise ValueError(f"override {i}: unknown op {op}")
    for ov in sorted(structural, key=lambda o: (o["topic"], -o["block"], o["op"] == "delete")):
        blocks = _topic(out, ov["topic"])["blocks"]
        if ov["op"] == "delete":
            b = blocks[ov["block"]]
            text = json.dumps(b, ensure_ascii=False)
            if ov.get("expect_ko", "") not in text:
                raise ValueError(f"delete ({ov.get('id')}): 원본 블록이 예상과 다름")
            del blocks[ov["block"]]
        else:
            blocks.insert(ov["block"] + 1, ov["value"])
    return out


def build(src: Path) -> tuple[dict, dict]:
    ts = (src / "src" / "pages" / "Manual" / "manualContent.ts").read_text(encoding="utf-8")
    sections = extract_export(ts, "MANUAL_SECTIONS")
    counts = validate(sections)
    # 원본 건수 교차 확인(파서 무증상 오변환 방지)
    expect = {"sections": len(re.findall(r"\n\s*no:\s*'\d+'", ts)),
              "topics": len(re.findall(r"\n\s*id:\s*'[a-z0-9-]+',\s*\n\s*title:", ts)),
              "blocks": len(re.findall(r"kind:\s*'\w+',", ts))}  # 타입 정의(kind: '…';)는 제외
    if counts != expect:
        raise ValueError(f"건수 불일치: 변환 {counts} / 원본 {expect}")
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    fixed = apply_overrides(sections, overrides["overrides"])
    final_counts = validate(fixed)
    data = {"source": "prj-tb-prototype-try-b/src/pages/Manual/manualContent.ts",
            "note": "원본 매뉴얼 + data/seed/manual_overrides.json 교정(scripts/convert_manual_ts.py 로 생성 — 직접 고치지 말 것)",
            "sections": fixed}
    return data, {"original": counts, "final": final_counts, "overrides": len(overrides["overrides"])}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="원본 저장소 루트")
    ap.add_argument("--check", action="store_true", help="쓰지 않고 현재 파일과 비교만")
    args = ap.parse_args(argv)
    data, info = build(Path(args.src))
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if args.check:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("manual.json 최신" if same else "manual.json 이 변환 결과와 다릅니다 — 스크립트를 다시 실행하세요")
        sys.exit(0 if same else 1)
    OUT.write_text(text, encoding="utf-8")
    print(f"변환 결과 → {OUT}")
    print(f"  원본 {info['original']} → 교정 {info['overrides']}건 적용 → {info['final']}")


if __name__ == "__main__":
    main()
