"""시드 patients.json 을 직접 읽어 조건 트리를 독립적으로 평가한다(서비스 코드를 쓰지 않는 기대값 계산기)."""
import json

from django.conf import settings

SEED = json.loads((settings.SEED_DIR / "patients.json").read_text(encoding="utf-8"))

PATHS = {
    "demographics.country": ("demographics", "country"), "demographics.age": ("demographics", "age"),
    "demographics.sex": ("demographics", "sex"), "clinical.diagnosisType": ("clinical", "diagnosisType"),
    "clinical.treatmentOutcome": ("clinical", "treatmentOutcome"),
    "clinical.comorbidities": ("clinical", "comorbidities"), "genomics.hasSequence": ("genomics", "hasSequence"),
    "genomics.lineage": ("genomics", "lineage"), "genomics.resistanceMutations": ("genomics", "resistanceMutations"),
    "imaging.hasCxr": ("imaging", "hasCxr"), "imaging.hasCt": ("imaging", "hasCt"),
    "imaging.hasPetCt": ("imaging", "hasPetCt"), "imaging.cxrCount": ("imaging", "cxrCount"),
    "imaging.ctCount": ("imaging", "ctCount"), "imaging.petCtCount": ("imaging", "petCtCount"),
    "imaging.cxrFindings": ("imaging", "cxrFindings"),
}


def leaf(p, n):
    if n["field"] == "genomics.hasMutation":
        return (len(p["genomics"]["resistanceMutations"]) > 0) == n["value"]
    a, b = PATHS[n["field"]]
    v = p[a][b]
    op, want = n["operator"], n["value"]
    if isinstance(v, list):  # 다중값: 하나라도 포함
        return any(x in v for x in want)
    if op == "eq":
        return v == want
    if op == "in":
        return v in want
    if op == "gte":
        return v >= want
    if op == "lte":
        return v <= want
    if op == "between":
        return want[0] <= v <= want[1]
    raise AssertionError(op)


def match(p, node):
    if isinstance(node, list):
        node = {"op": "AND", "children": node}
    if "op" not in node:
        return leaf(p, node)
    if not node["children"]:
        return True
    res = [match(p, c) for c in node["children"]]
    return all(res) if node["op"] == "AND" else any(res)


def expected_ids(tree) -> list[int]:
    return sorted(p["id"] for p in SEED if match(p, tree))


def condition_ids(tree) -> list[str]:
    return [p["conditionId"] for p in sorted(SEED, key=lambda x: x["id"]) if match(p, tree)]
