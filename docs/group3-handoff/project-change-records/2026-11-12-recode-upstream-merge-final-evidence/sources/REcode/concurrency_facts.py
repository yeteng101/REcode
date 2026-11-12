"""Source-backed lexical observations, without CFG or thread-instance claims.

These records retain declarations and API sites for downstream review. A name
match and textual call order cannot establish alias identity, successful thread
creation, overlapping lifetimes, or a must-lockset.
"""
from __future__ import annotations

import re

from .clang_ast import evidence_id, stable_digest
from .race_detector import (
    LOCK_ACQUIRE, LOCK_RELEASE, _balanced_region, _function_ranges,
    _global_declarations, _line_number, _split_top_level,
    _strip_comments_and_literals,
)


def collect_concurrency_facts(text: str, file: str, accesses: list[dict]) -> dict:
    clean = _strip_comments_and_literals(text)
    lines = text.splitlines()
    ranges = _function_ranges(clean)
    evidence = {}

    def site(kind: str, position: int, name: str) -> tuple[dict, list[str]]:
        line = _line_number(clean, position)
        location = {"file": file, "line": line, "snippet": lines[line - 1].strip()}
        identity = evidence_id(kind, file, line, name)
        evidence[identity] = {"id": identity, "kind": kind, "location": location, "role": "supports"}
        return location, [identity]

    def identity(kind: str, *parts) -> str:
        return kind + "_" + stable_digest([file, *parts])[:16]

    objects = []
    globals_ = _global_declarations(clean)
    depth = 0
    position = 0
    for line in clean.splitlines(keepends=True):
        # Only a whole simple top-level declaration is supported. Initializers
        # with braces, pointers, typedef resolution and extern definitions remain gaps.
        match = re.fullmatch(
            r"\s*(?P<type>(?:(?:static|extern|const|volatile|unsigned|signed|long|short)\s+)*"
            r"[A-Za-z_]\w*)\s+(?P<name>[A-Za-z_]\w*)\s*(?:=\s*[^;{}]+)?;\s*", line)
        if depth == 0 and match and match["name"] in globals_:
            name = match["name"]
            location, ids = site("global_declaration", position, name)
            objects.append({"object_id": identity("global", location["line"], name),
                "name": name, "type": match["type"], "declaration": location,
                "linkage": "internal" if "static" in match["type"].split() else "external",
                "identity_basis": "lexical_declaration", "identity_confidence": 0.65,
                "evidence_ids": ids})
        depth = max(0, depth + line.count("{") - line.count("}"))
        position += len(line)

    by_name = {}
    for obj in objects:
        by_name.setdefault(obj["name"], []).append(obj)

    events = []
    operations = []
    APIs = (*LOCK_ACQUIRE, *LOCK_RELEASE, "pthread_create", "pthread_join", "thrd_create", "thrd_join")
    pattern = r"\b(" + "|".join(re.escape(api) for api in APIs) + r")\s*\("
    for match in re.finditer(pattern, clean):
        owner = next((name for name, _, start, end in ranges if start <= match.start() < end), None)
        if owner is None:
            continue
        opening = clean.find("(", match.start())
        _, end = _balanced_region(clean, opening, "(", ")")
        if end <= opening or clean[end - 1] != ")":
            continue
        args = [arg.strip() for arg in _split_top_level(clean[opening + 1:end - 1], ",")]
        api = match[1]
        location, ids = site("synchronization_api", match.start(), api + str(match.start()))
        record = {"id": identity("sync_site", match.start(), api), "api": api,
                  "function": owner, "location": location, "evidence_ids": ids,
                  "analysis_basis": "lexical_api_site"}
        if api in {"pthread_create", "thrd_create", "pthread_join", "thrd_join"}:
            record["kind"] = "create" if api.endswith("create") else "join"
            record["handle_expression"] = args[0] if args else ""
            if record["kind"] == "create":
                target_index = 1 if api == "thrd_create" else 2
                target = args[target_index].strip(" &") if len(args) > target_index else ""
                record["entry_function"] = target if re.fullmatch(r"[A-Za-z_]\w*", target) else None
            events.append(record)
        else:
            expression = args[0] if args else ""
            name = expression.removeprefix("&").strip()
            matches = by_name.get(name, []) if re.fullmatch(r"&\s*[A-Za-z_]\w*", expression) else []
            record.update(kind="acquire" if api in LOCK_ACQUIRE else "release",
                          lock_expression=expression,
                          declaration_candidate_id=matches[0]["object_id"] if len(matches) == 1 else None)
            operations.append(record)

    bindings = []
    for access in accesses:
        function = access.get("function", "")
        name = access["memory_location"]
        # Address-taking and payload accesses are different facts. The original
        # detector's textual name occurrences are retained, with this limitation explicit.
        matches = by_name.get(name, [])
        function_range = next((item for item in ranges if item[0] == function), None)
        shadowed = True
        if function_range:
            _, declaration, start, end = function_range
            scope = clean[declaration:end]
            shadowed = bool(re.search(
                rf"\b(?:[A-Za-z_]\w*)\s+\**\s*{re.escape(name)}\b", scope))
        declaration_id = matches[0]["object_id"] if len(matches) == 1 and not shadowed else None
        creators = [e for e in events if e["kind"] == "create" and e.get("entry_function") == function]
        related_ops = [op for op in operations if op["function"] == function]
        bindings.append({"access_id": access["id"], "declaration_candidate_id": declaration_id,
            "execution_context": {"entry_function": function, "creation_site_ids": [e["id"] for e in creators],
                                  "instance_identity": "unknown", "overlap": "unknown"},
            "lock_operation_ids": [op["id"] for op in related_ops],
            "must_lockset_status": "unknown", "must_lockset": [],
            "coverage_gaps": ["lexical_access_identity_not_ast_bound", "thread_instances_and_overlap_unproved",
                              "path_sensitive_lockset_unavailable"]
                             + (["global_binding_ambiguous_or_shadowed"] if not declaration_id else [])})
    return {"schema_version": "1.0", "analysis_basis": "lexical_source_observations",
            "objects": objects, "thread_events": events, "lock_operations": operations,
            "access_bindings": bindings, "evidence": list(evidence.values()),
            "coverage": {"complete": False, "gaps": ["aliases_and_cross_file_bindings_unresolved",
                "thread_instances_and_overlap_unproved", "path_sensitive_lockset_unavailable"]}}
