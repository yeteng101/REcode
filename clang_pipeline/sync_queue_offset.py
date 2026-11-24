"""Verify the source operands of JSON AST OffsetOfExpr nodes."""
from __future__ import annotations

import re
from pathlib import Path

from .sync_field_bindings import _pointee, _pointer_expression


IDENTIFIER = rb"[A-Za-z_]\w*"
TYPE = rb"(?:struct\s+)?" + IDENTIFIER
OFFSET = re.compile(rb"(?:__builtin_offsetof|offsetof)\s*\(\s*(" + TYPE + rb")\s*,\s*(" + IDENTIFIER + rb")\s*\)")
RECOVER_CALL = re.compile(rb"(" + IDENTIFIER + rb")\s*\(\s*(" + IDENTIFIER + rb")\s*,\s*(" + TYPE
                          + rb")\s*,\s*(" + IDENTIFIER + rb")\s*\)")
MACRO = re.compile(rb"^[ \t]*#[ \t]*define[ \t]+(" + IDENTIFIER + rb")\(([^\n)]*)\)[ \t]*"
                   rb"((?:[^\n\\]|\\\r?\n|\\[^\n])*)", re.MULTILINE)


def _begin(node):
    return node.get("range", {}).get("begin", {})


def _macro_at(data, offset):
    if offset is None or offset >= len(data):
        return None
    # The supported macros fit in a short logical source line.
    start = data.rfind(b"\n", 0, max(0, offset - 2048)) + 1
    for macro in MACRO.finditer(data[start:offset + 2048]):
        if start + macro.start(3) <= offset < start + macro.end(3):
            parameters = tuple(part.strip() for part in macro[2].split(b","))
            if all(re.fullmatch(IDENTIFIER, name) for name in parameters) and len(set(parameters)) == len(parameters):
                body = re.sub(rb"\\\r?\n|\s+", b"", macro[3])
                return macro[1], parameters, body
    return None


def _spelling_data(analyzer, site, local_file, *, compiler_header=False):
    path = Path(site.get("file", local_file))
    if not path.is_absolute():
        path = analyzer.root / path
    try:
        path = path.resolve()
        relative = path.relative_to(analyzer.root).as_posix()
    except ValueError:
        if not compiler_header or path.name not in {"stddef.h", "__stddef_offsetof.h"}:
            return b""
        if (path.parent.name != "include" or not re.fullmatch(r"\d+(?:\.\d+)*", path.parent.parent.name)
                or path.parent.parent.parent.name != "clang"):
            return b""
    else:
        if relative in analyzer.sources:
            return analyzer.sources[relative]
    try:
        if path.is_file() and path.stat().st_size <= 128 * 1024:
            return path.read_bytes()
    except OSError:
        pass
    return b""


def _forwarded_offset(analyzer, offset_node, file):
    spelling = _begin(offset_node).get("spellingLoc", {})
    if not spelling:
        return False
    data = _spelling_data(analyzer, spelling, file, compiler_header=True)
    macro = _macro_at(data, spelling.get("offset"))
    if not macro or macro[0] != b"offsetof" or len(macro[1]) != 2:
        return False
    owner, member = macro[1]
    return macro[2] == b"__builtin_offsetof(" + owner + b"," + member + b")"


def recovery_parts(expression, shapes):
    """Return the node reference, owner declaration, and selected member name."""
    cast = _pointer_expression(expression)
    if cast.get("kind") != "CStyleCastExpr" or cast.get("castKind") != "BitCast" or len(cast.get("inner", [])) != 1:
        return None
    result_record = shapes.record(_pointee(cast))
    subtract = _pointer_expression(cast["inner"][0])
    if not result_record or subtract.get("kind") != "BinaryOperator" or subtract.get("opcode") != "-":
        return None
    operands = subtract.get("inner", [])
    if len(operands) != 2:
        return None
    left, offset = map(_pointer_expression, operands)
    if (left.get("kind") != "CStyleCastExpr" or left.get("castKind") != "BitCast" or _pointee(left) != "char"
            or len(left.get("inner", [])) != 1 or offset.get("kind") != "OffsetOfExpr"):
        return None
    node_ref = _pointer_expression(left["inner"][0])
    if node_ref.get("kind") != "DeclRefExpr":
        return None
    location = shapes.location(cast)
    if not location:
        return None
    file = location["file"]
    data = shapes.analyzer.sources[file]
    start = _begin(cast)
    offset_start = _begin(offset)
    if "expansionLoc" in start:
        at = start["expansionLoc"].get("offset")
        spelling = start.get("spellingLoc", {})
        macro_data = _spelling_data(shapes.analyzer, spelling, file)
        macro = _macro_at(macro_data, spelling.get("offset"))
        call = RECOVER_CALL.match(data[at:at + 512]) if at is not None else None
        if not macro or not call or call[1] != macro[0] or len(macro[1]) != 3:
            return None
        pointer, owner, member = macro[1]
        before = b"((" + owner + b"*)((char*)"
        after = b"(" + owner + b"," + member + b")))"
        for arg in (pointer, b"(" + pointer + b")"):
            if macro[2] == before + arg + b"-__builtin_offsetof" + after:
                break
            if macro[2] == before + arg + b"-offsetof" + after and _forwarded_offset(shapes.analyzer, offset, file):
                break
        else:
            return None
        if call[2].decode() != node_ref.get("referencedDecl", {}).get("name"):
            return None
        owner, member = call[3], call[4]
    else:
        at = offset_start.get("expansionLoc", offset_start).get("offset")
        call = OFFSET.match(data[at:at + 512]) if at is not None else None
        if not call:
            return None
        if "expansionLoc" in offset_start and not _forwarded_offset(shapes.analyzer, offset, file):
            return None
        owner, member = call[1], call[2]
    if shapes.record(" ".join(owner.decode().split())) != result_record:
        return None
    return node_ref, result_record, member.decode(), location
