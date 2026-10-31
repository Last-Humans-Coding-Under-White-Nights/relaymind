"""Copy published gateway schemas and emit TypeScript wire types (RM36/RM37)."""

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parents[1]
NAMES = {
    "recommendation_view": "RecommendationViewV1",
    "recommendation_result": "RecommendationResultV1",
    "recommendation_event": "RecommendationEventV1",
    "recommendation_readiness": "ReadinessV1",
    "asset_record": "AssetRecordV1",
    "action_evidence": "ActionEvidenceV1",
    "feedback_record": "FeedbackRecordV1",
}
WIDE = {
    "seq",
    "next_after_seq",
    "state_version",
    "catalog_revision",
    "rechecked_catalog_revision",
    "feedback_revision",
    "expected_version",
    "expected_feedback_revision",
}
schemas = {
    name + ".v1.json": json.loads(
        (ROOT / "src/relaymind/store/schemas" / (name + ".v1.json")).read_text()
    )
    for name in NAMES
}
for name in NAMES:
    (PACKAGE / "src/recommendation-schemas" / (name + ".ts")).write_text(
        "// Generated from the published Python schema; "
        "run scripts/sync-recommendation-contract.py.\nexport default "
        + json.dumps(schemas[name + ".v1.json"], indent=2)
        + " as const;\n"
    )
aliases = {name + ".v1.json": alias for name, alias in NAMES.items()}
pending = dict(aliases)


def type_of(s: dict[str, Any], file: str, key: str = "") -> str:
    if "$ref" in s:
        path, _, fragment = s["$ref"].partition("#")
        target = path or file
        ref = target + ("#" + fragment if fragment else "")
        if ref not in pending:
            pending[ref] = aliases[target] + "".join(
                p.title().replace("_", "")
                for p in fragment.split("/")
                if p and not p.startswith("$")
            )
        return pending[ref]
    if "const" in s:
        return json.dumps(s["const"])
    if "enum" in s:
        return " | ".join(json.dumps(v) for v in s["enum"])
    for union in ("anyOf", "oneOf"):
        if union in s and "type" not in s:
            return "(" + " | ".join(type_of(v, file, key) for v in s[union]) + ")"
    t = s.get("type")
    if isinstance(t, list):
        return "(" + " | ".join(type_of(s | {"type": v}, file, key) for v in t) + ")"
    if t == "null":
        return "null"
    if t == "integer":
        return "bigint" if key in WIDE else "number"
    if t in ("number", "string", "boolean"):
        return t
    if t == "array":
        return "Array<" + type_of(s["items"], file, key) + ">"
    if t == "object":
        props = s.get("properties", {})
        if not props:
            return "Record<string, JsonValue>"
        return (
            "{\n"
            + "".join(
                "  "
                + json.dumps(k)
                + ("" if k in s.get("required", []) else "?")
                + ": "
                + type_of(v, file, k)
                + ";\n"
                for k, v in props.items()
            )
            + "}"
        )
    return "JsonValue"


output = [
    "// Generated from the published gateway schemas. Do not edit by hand.",
    "export type JsonValue = null | boolean | number | bigint | string | JsonValue[] "
    "| { [key: string]: JsonValue };",
]
completed = set()
while set(pending) - completed:
    ref = next(r for r in pending if r not in completed)
    file, _, fragment = ref.partition("#")
    schema = schemas[file]
    for part in fragment.split("/"):
        if part:
            schema = schema[int(part)] if isinstance(schema, list) else schema[part]
    if ref == "recommendation_event.v1.json":
        base = schema
        variants = [
            base
            | {
                "properties": base["properties"]
                | {"erased": {"const": True}, "payload": {"type": "null"}}
            }
        ]
        for branch in base["allOf"][0]["else"]["allOf"]:
            props = (
                branch["if"]["properties"]
                | branch["then"]["properties"]
                | {"erased": {"const": False}}
            )
            variants.append(base | {"properties": base["properties"] | props})
        schema = {"oneOf": variants}
    if ref == "asset_record.v1.json":
        base = schema
        conditional = base["allOf"][0]
        schema = {
            "oneOf": [
                base
                | {
                    "properties": base["properties"]
                    | {"kind": {"const": kind}}
                    | conditional[branch]["properties"]
                }
                for kind, branch in [("skill", "then"), ("mcp_tool", "else")]
            ]
        }
    if ref == "recommendation_readiness.v1.json":
        schema = {
            "oneOf": [
                schema | {"properties": schema["properties"] | branch["properties"]}
                for branch in schema["oneOf"]
            ]
        }
    output.append("export type " + pending[ref] + " = " + type_of(schema, file) + ";")
    completed.add(ref)
(PACKAGE / "src/recommendation-types.ts").write_text("\n".join(output) + "\n")
