"""Read-only SES configuration preview. Never loads plugins or opens a database.

This is a deliberately restricted SES dialect, not a general SES/MB simulator.
The model base below describes future module contracts, NOT installed handlers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


class ConfigurationError(ValueError):
    pass


# Attributes are bounded data, never Python expressions or import paths.
MODEL_BASE = {
    "core.policy.v1": {},
    "lifecycle.manual.v1": {},
    "lifecycle.deadlines.v1": {"appeal_days": (int, 0, 365)},
    "archive.disabled.v1": {},
    "archive.local_tar.v1": {"delay_days": (int, 0, 365), "retention_days": (int, 1, 3650)},
    "reports.disabled.v1": {},
    "reports.csv.v1": {"score_basis": ("latest", "best", "instructor_confirmed")},
    "reports.html_csv.v1": {"score_basis": ("latest", "best", "instructor_confirmed")},
    "support.disabled.v1": {},
    "support.rules.v1": {
        "minimum_graded_assignments": (int, 3, 100),
        "consecutive_missed_deadlines": (int, 2, 20),
        "low_score_percent": (int, 1, 99),
    },
    "notifications.disabled.v1": {},
    "notifications.instructor_inbox.v1": {},
    "scheduler.manual.v1": {},
    "scheduler.pyjevsim.v1": {"interval_seconds": (int, 60, 86400)},
}
SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SAFE_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
SCHEMA = "autograde.management.ses.v1"
PROFILE = "autograde.management.profile.v1"


def require(condition, message):
    if not condition:
        raise ConfigurationError(message)


def _keys(value, expected, context):
    require(isinstance(value, dict) and set(value) == set(expected),
            f"{context}: fields must be {', '.join(sorted(expected))}")


def validate_model(model):
    _keys(model, {"schema_version", "root"}, "model")
    require(model["schema_version"] == SCHEMA, "unsupported SES schema")
    labels, decisions, modules = set(), {}, []
    replicas = 0

    def walk(node, parent=None, depth=0, inside_courses=False):
        nonlocal replicas
        require(depth <= 24 and len(labels) < 256, "model exceeds node/depth limit")
        require(isinstance(node, dict), "node must be an object")
        kind = node.get("type")
        require(isinstance(kind, str) and kind in {"entity", "aspect", "specialization", "multi_aspect"}, "unsupported node type")
        children = node.get("children")
        require(isinstance(children, list), "children must be an array")
        fields = {"label", "type", "children"}
        if kind == "specialization":
            fields.add("selection")
        if kind == "multi_aspect":
            fields.add("multiplicity")
        if kind == "entity" and not children:
            fields.add("module_id")
        _keys(node, fields, "node")
        label = node["label"]
        require(isinstance(label, str) and SAFE_LABEL.fullmatch(label), "invalid node label")
        # Globally unique template labels enforce strict hierarchy/valid brothers
        # and avoid ambiguous uniformity; repeated entities use multi_aspect.
        require(label not in labels, "duplicate template label")
        labels.add(label)
        require((kind == "entity") != (parent == "entity"), "SES nodes must alternate entity and relation")
        if kind == "entity":
            require(len(children) <= 1, "this dialect supports one decomposition per entity")
            if not children:
                module = node["module_id"]
                require(isinstance(module, str) and module in MODEL_BASE, "unknown model-base module")
                modules.append(module)
                require((module == "core.policy.v1") != inside_courses,
                        "core belongs outside course instances; optional modules belong inside")
        elif kind == "multi_aspect":
            require(not inside_courses and len(children) == 1 and node["multiplicity"] == "courses",
                    "only one course-template replication is supported")
            replicas += 1
        elif kind == "specialization":
            choice = node["selection"]
            require(inside_courses and isinstance(choice, str) and SAFE_LABEL.fullmatch(choice),
                    "specialization needs a course selection")
            require(choice not in decisions and len(children) >= 2, "invalid/duplicate specialization")
            require(all(isinstance(c, dict) and isinstance(c.get("label"), str) for c in children),
                    "specialization children need labels")
            decisions[choice] = {c["label"] for c in children}
        else:
            require(bool(children), "aspect must decompose into entities")
        for child in children:
            walk(child, kind, depth + 1, inside_courses or kind == "multi_aspect")

    require(isinstance(model["root"], dict) and model["root"].get("type") == "entity", "root must be entity")
    walk(model["root"])
    require(replicas == 1 and modules.count("core.policy.v1") == 1, "one course fleet and mandatory core required")
    require(len(modules) == len(set(modules)), "model-base leaves must be unique in this dialect")
    return decisions


def _validate_attributes(module, value):
    spec = MODEL_BASE[module]
    _keys(value, spec, module)
    for key, rule in spec.items():
        actual = value[key]
        if rule[0] is int:
            require(type(actual) is int and rule[1] <= actual <= rule[2],
                    f"{module}.{key}: integer required in [{rule[1]}, {rule[2]}]")
        else:
            require(isinstance(actual, str) and actual in rule, f"{module}.{key}: invalid choice")


def _semantic_checks(modules):
    selected = set(modules)
    require(len(modules) == 6 and {m.split(".")[0] for m in modules} ==
            {"lifecycle", "archive", "reports", "support", "notifications", "scheduler"},
            "each course needs exactly one variant of each management module")
    if "lifecycle.deadlines.v1" in selected:
        require("scheduler.pyjevsim.v1" in selected, "deadline lifecycle requires periodic scheduler")
    if "support.rules.v1" in selected:
        require(bool(selected & {"reports.csv.v1", "reports.html_csv.v1"}),
                "learning support requires report snapshot capability")
    if "notifications.instructor_inbox.v1" in selected:
        require("support.rules.v1" in selected, "support inbox requires learning-support rules")
    if "archive.local_tar.v1" in selected:
        require("lifecycle.deadlines.v1" in selected, "archive requires distinct deadline-window policy")


def compile_plan(model, profile):
    """Return a deterministic PES preview; no I/O or changes to inputs."""
    decisions = validate_model(model)
    _keys(profile, {"schema_version", "courses"}, "profile")
    require(profile["schema_version"] == PROFILE, "unsupported profile schema")
    courses = profile["courses"]
    require(isinstance(courses, list) and 1 <= len(courses) <= 100, "1..100 course profiles required")
    seen, compiled = set(), []
    for course in courses:
        _keys(course, {"course_key", "selections", "parameters"}, "course profile")
        key = course["course_key"]
        require(isinstance(key, str) and SAFE_KEY.fullmatch(key), "invalid course key")
        require(key not in seen, "duplicate course key")
        seen.add(key)
        _keys(course["selections"], decisions, "selections")
        for decision, options in decisions.items():
            value = course["selections"][decision]
            require(isinstance(value, str) and value in options, f"{decision}: unknown variant")
        require(isinstance(course["parameters"], dict), "parameters must be an object")

    def prune(node, course=None):
        kind = node["type"]
        if kind == "multi_aspect":
            instances = []
            for binding in sorted(courses, key=lambda c: c["course_key"]):
                instance = prune(node["children"][0], binding)
                instance["course_key"] = binding["course_key"]
                leaves = []

                def collect(current):
                    if "module_id" in current:
                        leaves.append(current)
                    for child in current["children"]:
                        collect(child)

                collect(instance)
                modules = [leaf["module_id"] for leaf in leaves]
                require(len(modules) == len(set(modules)), "duplicate selected module")
                _semantic_checks(modules)
                expected = {m for m in modules if MODEL_BASE[m]}
                _keys(binding["parameters"], expected, "selected module parameters")
                for leaf in leaves:
                    module = leaf["module_id"]
                    attrs = binding["parameters"].get(module, {})
                    _validate_attributes(module, attrs)
                    leaf["attributes"] = dict(attrs)
                compiled.append({"course_key": binding["course_key"], "modules": modules})
                instances.append(instance)
            return {"label": node["label"], "type": "aspect", "children": instances}
        if kind == "specialization":
            choice = course["selections"][node["selection"]]
            return prune(next(child for child in node["children"] if child["label"] == choice), course)
        label = node["label"] + ("." + course["course_key"] if course else "")
        if kind == "entity" and node["children"] and node["children"][0]["type"] == "specialization":
            result = prune(node["children"][0], course)
            result["specialized_as"] = result["label"]
            result["label"] = label
            return result
        result = {"label": label, "type": kind,
                  "children": [prune(child, course) for child in node["children"]]}
        if "module_id" in node:
            result["module_id"] = node["module_id"]
            result["implementation_status"] = "contract_only"
        return result

    tree = prune(model["root"])
    payload = {"schema_version": "autograde.management.pes-preview.v1",
               "runtime_ready": False, "side_effects": "none",
               "protected_policy": {"automatic_deletion": False, "student_auto_contact": False,
                                    "human_review_required": True, "course_scope_required": True},
               "courses": compiled, "root": tree}
    # Hash the complete source model too: changes to unselected alternatives are auditable.
    payload["model_sha256"] = _digest(model)
    payload["plan_sha256"] = _digest(payload)
    return payload


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ConfigurationError("non-finite JSON number")

    with Path(path).open("rb") as source:
        data = source.read(1024 * 1024 + 1)
    require(len(data) <= 1024 * 1024, "configuration exceeds 1 MiB")
    return json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only SES -> PES preview; does not apply policies.")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = compile_plan(load_json(args.model), load_json(args.profile))
    except (ValueError, OSError, RecursionError) as error:
        # No raw file contents, path details or student data in error output.
        message = str(error) if isinstance(error, ConfigurationError) else "cannot read valid configuration"
        print(json.dumps({"ok": False, "error": "invalid_management_configuration", "message": message}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "plan": plan}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
