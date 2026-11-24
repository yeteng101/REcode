"""Source-site observations without object or execution-context binding."""
from __future__ import annotations

from .clang_ast import stable_digest


LIMITS = {"member_accesses": 2048, "atomic_operations": 512}
MAX_PER_FILE_PER_KIND = 128
MEMORY_ORDERS = dict(zip("012345", ("relaxed", "consume", "acquire", "release", "acq_rel", "seq_cst")))
ATOMIC_OPERATIONS = {
    **dict.fromkeys(("load", "load_n"), "load"),
    **dict.fromkeys(("store", "store_n"), "store"),
    **dict.fromkeys(("exchange", "exchange_n"), "exchange"),
    **dict.fromkeys(("compare_exchange", "compare_exchange_n", "compare_exchange_strong",
                     "compare_exchange_weak"), "compare_exchange"),
    **{name: name for name in ("fetch_add", "fetch_sub", "fetch_and", "fetch_or", "fetch_xor",
                               "fetch_nand", "fetch_min", "fetch_max", "add_fetch", "sub_fetch",
                               "and_fetch", "or_fetch", "xor_fetch", "nand_fetch", "min_fetch", "max_fetch")},
}


def _member_access(node, parents):
    while parents:
        parent, parents = parents
        kind, opcode = parent.get("kind"), parent.get("opcode")
        if kind in {"ParenExpr", "ExprWithCleanups", "ConstantExpr"}:
            node = parent
            continue
        if kind == "ImplicitCastExpr" and parent.get("castKind") == "LValueToRValue":
            return "read"
        if kind == "UnaryOperator" and opcode == "&":
            return "address_taken"
        if kind == "UnaryOperator" and opcode in {"++", "--"}:
            return "read_write"
        if parent["inner"][0] is node:
            if kind == "CompoundAssignOperator":
                return "read_write"
            if kind == "BinaryOperator" and opcode == "=":
                return "write"
        break
    return "reference"


def _order_value(node, enum_values):
    while node.get("kind") == "ParenExpr" or (
        node.get("kind") == "ImplicitCastExpr" and node.get("type", {}).get("qualType") == "int"
    ):
        children = node.get("inner", [])
        if len(children) != 1:
            return None
        node = children[0]
    if node.get("kind") in {"IntegerLiteral", "ConstantExpr"}:
        return node.get("value")
    reference = node.get("referencedDecl", {})
    if node.get("kind") == "DeclRefExpr" and reference.get("kind") == "EnumConstantDecl":
        return enum_values.get(reference.get("id"))
    return None


def _atomic_operation(node, enum_values, location, sources):
    operator = node.get("name")
    if not operator:
        # Older JSON dumps omit the opcode. Use a directly written compiler
        # builtin if available, and leave macro opcodes unknown.
        begin = node.get("range", {}).get("begin", {})
        offset, length = begin.get("offset"), begin.get("tokLen")
        if offset is not None and length and "expansionLoc" not in begin:
            data = sources[location["file"]]
            token = data[offset:offset + length].decode("ascii", errors="replace")
            if token.startswith(("__c11_atomic_", "__atomic_")):
                operator = token
    operation = None
    for prefix in ("__c11_atomic_", "__atomic_"):
        if operator and operator.startswith(prefix):
            operation = ATOMIC_OPERATIONS.get(operator[len(prefix):])
            break

    children = node.get("inner", [])
    order = MEMORY_ORDERS.get(_order_value(children[1], enum_values)) if operation and len(children) > 1 else None
    observation = {"operator": operator, "operation": operation, "memory_order": order}
    if operation == "compare_exchange":
        failure = _order_value(children[3], enum_values) if len(children) > 3 else None
        observation["failure_memory_order"] = MEMORY_ORDERS.get(failure)
    return observation


def collect_source_observations(asts, location_for, sources):
    records = {kind: {} for kind in LIMITS}
    counts = {}
    truncated = set()
    for tu, ast in asts:
        if not tu.endswith(".c"):
            continue
        enum_values = {}
        stack = [(ast, None, None)]
        while stack:
            node, function, parents = stack.pop()
            kind = node.get("kind")
            children = node.get("inner", [])
            if kind == "EnumConstantDecl" and children and children[0].get("kind") == "ConstantExpr":
                enum_values[node["id"]] = children[0].get("value")
            if kind == "FunctionDecl":
                body = next((child for child in children if child.get("kind") == "CompoundStmt"), None)
                if body is not None and location_for(node, tu):
                    stack.append((body, node.get("name"), None))
                continue
            stack.extend((child, function, (node, parents)) for child in reversed(children))
            if not function or kind not in {"MemberExpr", "AtomicExpr"}:
                continue
            location = location_for(node, tu)
            if not location:
                continue
            if kind == "MemberExpr":
                if not node.get("name"):
                    continue
                key = "member_accesses"
                details = {"member": node["name"], "access": _member_access(node, parents)}
            else:
                key = "atomic_operations"
                details = _atomic_operation(node, enum_values, location, sources)
            site = {name: location[name] for name in ("file", "line", "column")}
            digest = stable_digest([key, function, site, details])[:20]
            if digest in records[key]:
                continue
            file_key = (key, location["file"])
            count = counts.get(file_key, 0)
            if count >= MAX_PER_FILE_PER_KIND or len(records[key]) >= LIMITS[key]:
                truncated.add(key)
                continue
            counts[file_key] = count + 1
            records[key][digest] = dict(details, id="sync_site_" + digest, function=function, location=site,
                                        evidence_ids=["ev_ast_sync_site_" + digest])

    evidence = [
        {"id": item["evidence_ids"][0], "kind": evidence_kind, "location": item["location"], "role": "supports"}
        for key, evidence_kind in (("member_accesses", "ast_member_access_site"),
                                   ("atomic_operations", "ast_atomic_operation_site"))
        for item in records[key].values()
    ]
    return {"schema_version": "sync-source-observations/1", "analysis_basis": "clang-json-ast-sites",
            **{key: list(items.values()) for key, items in records.items()}, "evidence": evidence,
            "coverage": {"scope": "C function bodies in available translation units",
                         "truncated_kinds": sorted(truncated), "limits": {**LIMITS, "per_file_per_kind": MAX_PER_FILE_PER_KIND}}}
