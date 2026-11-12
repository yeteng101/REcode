"""Translate version-bound REcode exports into the shared first-group contract."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from first_group_client import FirstGroupClientError
from first_group_protocol import require_first_group_request, require_first_group_response
from source_loader import normalize_relative_path


EXPORTS = {
    "get_call_chains": ("call_chains", "external/call-chains.json", "call_chain"),
    "get_resource_flows": ("resource_flow", "external/resource-flow.json", "resource_flow"),
    "get_sync_flows": ("sync_relations", "external/sync-relations.json", "sync_relation"),
}


def validate_source_checkout(review: dict) -> None:
    root = Path(review["repository"]["root"]).resolve(strict=True)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args],
                                       stderr=subprocess.PIPE, timeout=30)
    if git("rev-parse", "HEAD").decode().strip() != review["repository"]["commit_sha"]:
        raise FirstGroupClientError("local source checkout does not match the REcode request head")
    if git("diff", "--name-only", "HEAD", "--") or git("ls-files", "--others", "--exclude-standard"):
        raise FirstGroupClientError("REcode integration requires a clean local head checkout")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class RecodeArtifactsClient:
    """Read immutable native exports. Missing producer facts remain coverage gaps."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve(strict=True)
        self.files: dict[str, str] = {}
        self.manifest = self._read("manifest.json")
        self.graph = self._read(self.manifest.get("outputs", {}).get("graph", "graph.json"))
        self.documents = {}
        for action, (key, fallback, result_type) in EXPORTS.items():
            relative = self.manifest.get("outputs", {}).get(key, fallback)
            path = self._path(relative)
            if path.is_file():
                doc = self._read(relative)
                if doc.get("result_type") != result_type:
                    raise FirstGroupClientError("REcode export has an unexpected result_type: " + relative)
                self.documents[action] = doc
        self.races = None
        relative = self.manifest.get("outputs", {}).get("race_analysis", "external/race-analysis.json")
        if self._path(relative).is_file():
            self.races = self._read(relative)
            if self.races.get("result_type") != "race_analysis":
                raise FirstGroupClientError("REcode raw-access export has an unexpected result_type")
        self._validate_bundle()
        self.snapshot_id = "recode-" + digest(self.files)
        self.omissions = set()
        self.unavailable_evidence = set()
        self.evidence = self._evidence_index()

    def _path(self, relative: str) -> Path:
        relative = normalize_relative_path(relative)
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory) or path.is_symlink():
            raise FirstGroupClientError("REcode artifact path escapes its directory")
        return path

    def _read(self, relative: str) -> dict:
        path = self._path(relative)
        raw = path.read_bytes()
        if len(raw) > 128 * 1024 * 1024:
            raise FirstGroupClientError("REcode JSON artifact exceeds 128 MiB")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise FirstGroupClientError("REcode JSON artifact must be an object")
        self.files[normalize_relative_path(relative)] = hashlib.sha256(raw).hexdigest()
        return value

    def _validate_bundle(self) -> None:
        meta = self.graph.get("meta", {})
        for field in ("repository", "commit", "build_profile", "run_id"):
            expected = self.manifest.get(field)
            actual = self.graph.get(field) if field == "run_id" else meta.get(field)
            if not isinstance(expected, str) or not expected or actual != expected:
                raise FirstGroupClientError("REcode manifest/graph binding mismatch: " + field)
        commit = self.manifest["commit"]
        if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise FirstGroupClientError("REcode requires a full immutable commit, not a local/branch label")
        if self.manifest.get("status") != "succeeded":
            raise FirstGroupClientError("REcode analysis manifest did not succeed")
        for doc in list(self.documents.values()) + ([self.races] if self.races else []):
            if doc.get("schema_version") != "1.0" or doc.get("run_id") != self.manifest["run_id"]:
                raise FirstGroupClientError("REcode export version/run binding mismatch")
            source = doc.get("source", {})
            for field in ("repository", "commit", "build_profile"):
                if source.get(field) != self.manifest[field]:
                    raise FirstGroupClientError("REcode export source binding mismatch: " + field)
            if doc.get("origin", source.get("origin")) != "analyzer":
                raise FirstGroupClientError("REcode integration requires native analyzer exports")
            if doc.get("status") not in {"succeeded", "partial", "not_available"}:
                raise FirstGroupClientError("REcode export failed")

    @staticmethod
    def _location(raw: dict) -> dict:
        location = {"file": normalize_relative_path(raw["file"]),
                    "line": raw.get("line", raw.get("start_line"))}
        if type(location["line"]) is not int or location["line"] < 1:
            raise FirstGroupClientError("REcode source location requires a positive line")
        if raw.get("end_line") is not None:
            location["end_line"] = raw["end_line"]
        if isinstance(raw.get("snippet"), str):
            location["snippet"] = raw["snippet"]
            location["snippet_sha256"] = hashlib.sha256(raw["snippet"].encode("utf-8")).hexdigest()
        return location

    def _evidence_index(self) -> dict[str, dict]:
        result = {}
        docs = [self.graph, *self.documents.values()]
        if self.races:
            docs.append(self.races)
        for doc in docs:
            for raw in doc.get("evidence", []):
                location = self._available_location(raw.get("location", raw), raw["id"])
                if location is None:
                    self.unavailable_evidence.add(raw["id"])
                    continue
                item = {"id": raw["id"], "kind": raw.get("kind", "source_fact"),
                        "location": location, "role": raw.get("role", "supports")}
                prior = result.get(item["id"])
                if prior is not None and prior != item:
                    raise FirstGroupClientError("REcode evidence identity conflict: " + item["id"])
                result[item["id"]] = item
        return result

    def _available_location(self, raw: dict, identity: str) -> dict | None:
        file = raw.get("file", "")
        if PurePosixPath(file).is_absolute() or PureWindowsPath(file).is_absolute():
            self.omissions.add("external_source_record:" + identity)
            return None
        return self._location(raw)

    def _ids(self, raw: dict) -> list[str]:
        ids = raw.get("evidence_ids", [])
        if not isinstance(ids, list) or not ids or any(i not in self.evidence and i not in self.unavailable_evidence for i in ids):
            raise FirstGroupClientError("REcode fact has missing or unknown source evidence")
        return list(dict.fromkeys(i for i in ids if i in self.evidence))

    def _nodes(self) -> dict[str, dict]:
        result = {}
        for raw in self.graph.get("nodes", []):
            node = {"node_id": raw["id"], "name": raw["name"], "kind": raw.get("kind", "function"),
                    "qualified_name": raw["id"].removeprefix("fn:"), "signature": raw.get("signature", "")}
            if raw.get("file") and raw.get("line", 0) > 0:
                location = self._available_location(raw, raw["id"])
                if location is not None:
                    node["location"] = location
                else:
                    node["kind"] = "external"
            if node["node_id"] in result:
                raise FirstGroupClientError("REcode duplicate graph node")
            result[node["node_id"]] = node
        return result

    def _resolve(self, targets: list[dict], nodes: dict) -> tuple[list[dict], list[dict]]:
        resolved, unresolved = [], []
        for target in targets:
            matches = []
            target_kind = target.get("type", target.get("kind"))
            for node in nodes.values():
                by_name = (target.get("qualified_name") or target.get("name")) in {node["name"], node["qualified_name"]}
                matched = target["node_id"] == node["node_id"] if target.get("node_id") else by_name
                file = target.get("location", {}).get("file")
                if target_kind in {"function", "callback", "external"} and matched and (not file or node.get("location", {}).get("file") == file):
                    matches.append(node["node_id"])
            if len(matches) == 1:
                resolved.append({"target_id": target["target_id"], "node_id": matches[0]})
            else:
                unresolved.append({"target_id": target["target_id"], "reason": "ambiguous_native_target" if matches else "native_target_not_found"})
        return resolved, unresolved

    def _calls(self, request: dict, nodes: dict, resolved: list[dict]) -> list[dict]:
        analysis = request.get("analysis") or {}
        direction = analysis.get("direction", "both")
        outward = {"both", "callee", "callees", "forward", "caller_to_callee"}
        inward = {"both", "caller", "callers", "backward", "callee_to_caller"}
        depth = analysis.get("max_depth", 12)
        if direction not in outward | inward or type(depth) is not int or not 0 <= depth <= 64:
            raise FirstGroupClientError("invalid REcode call-subgraph direction/depth")
        adjacency = defaultdict(list)
        edges = []
        for raw in self.graph.get("edges", []):
            if raw["source"] not in nodes or raw["target"] not in nodes:
                raise FirstGroupClientError("REcode call edge references an unknown graph node")
            location = self._available_location(raw["call_site"], raw["id"])
            ids = self._ids(raw)
            if location is None or not ids:
                self.omissions.add("unavailable_call_fact:" + raw["id"])
                continue
            item = {"edge_id": raw["id"], "kind": raw["kind"], "source_node_id": raw["source"],
                    "target_node_id": raw["target"], "call_site": location,
                    "confidence": raw.get("confidence", 0.0), "evidence_ids": ids}
            edges.append(item)
            if direction in outward:
                adjacency[raw["source"]].append(raw["target"])
            if direction in inward:
                adjacency[raw["target"]].append(raw["source"])
        findings = []
        for target in resolved:
            distances = {target["node_id"]: 0}
            queue = deque(distances)
            while queue:
                current = queue.popleft()
                if distances[current] >= depth:
                    continue
                for neighbor in adjacency[current]:
                    if neighbor not in distances:
                        distances[neighbor] = distances[current] + 1
                        queue.append(neighbor)
            selected = [e for e in edges if e["source_node_id"] in distances and e["target_node_id"] in distances]
            findings.append({"id": "recode-call-" + target["target_id"], "kind": "call_chain",
                             "root_node_id": target["node_id"], "target_id": target["target_id"],
                             "nodes": [nodes[key] for key in sorted(distances)], "edges": selected,
                             "evidence_ids": sorted({i for edge in selected for i in edge["evidence_ids"]})})
        return findings

    def _resources(self, doc: dict) -> list[dict]:
        findings = []
        for raw in doc.get("items", []):
            operations = []
            for op in raw.get("operations", []):
                if op["kind"] not in {"acquire", "release", "transfer", "use", "retain", "borrow", "escape", "invalidate"}:
                    continue
                location = self._available_location(op, op["operation_id"])
                ids = self._ids(op)
                if location is None or not ids:
                    self.omissions.add("unavailable_resource_operation:" + op["operation_id"])
                    continue
                operations.append({"operation_id": op["operation_id"], "kind": op["kind"],
                                   "function": op.get("function", ""), "api": op.get("api", ""),
                                   "location": location, "evidence_ids": ids})
            if len(operations) != len(raw.get("operations", [])):
                self.omissions.add("incomplete_resource_flow:" + raw["id"])
                continue
            findings.append({"id": raw["id"], "kind": "resource_flow", "resource_id": raw["id"],
                             "resource_family_id": raw["resource"],
                             "resource_type": raw["resource_type"], "resource_expression": raw.get("resource_expression"),
                             "ownership_model": raw.get("ownership_model"), "operations": operations,
                             "paths": copy.deepcopy(raw.get("paths", [])), "confidence": raw.get("confidence", 0.0),
                             "evidence_ids": self._ids(raw)})
        return findings

    def _sync(self, doc: dict) -> list[dict]:
        objects = {}
        # Consume only raw accesses. Producer race/no_race labels and reasons
        # never become a specialty verdict or a concurrency fact.
        for region in (self.races or {}).get("items", []):
            for raw in region.get("accesses", []):
                if raw.get("access") not in {"read", "write"}:
                    continue
                location = self._available_location(raw, raw["id"])
                ids = self._ids(raw)
                if location is None or not ids:
                    self.omissions.add("unavailable_raw_access:" + raw["id"])
                    continue
                key = (location["file"], raw["memory_location"])
                obj = objects.setdefault(key, {"object_id": "recode-object-" + digest(key)[:20],
                    "name": raw["memory_location"], "type": "", "identity_confidence": 0.35,
                    "declaration": None, "evidence_ids": [], "accesses": {}})
                access = {"access_id": raw["id"], "kind": raw["access"], "function": raw.get("function", ""),
                          "location": location, "execution_context": {}, "must_lockset": [], "may_lockset": [],
                          "other_synchronization": [{"kind": "unresolved_native_protection", "evidence_ids": ids}]
                          if raw.get("protected") or raw.get("atomic") or raw.get("private") else [],
                          "confidence": 0.35, "evidence_ids": ids}
                previous = obj["accesses"].get(access["access_id"])
                if previous is not None and previous != access:
                    raise FirstGroupClientError("REcode conflicting raw access identity")
                obj["accesses"][access["access_id"]] = access
                obj["evidence_ids"] = sorted(set(obj["evidence_ids"]) | set(ids))
        relations = []
        for raw in doc.get("items", []):
            source = self._available_location(raw["source"], raw["id"] + ":source")
            target = self._available_location(raw["target"], raw["id"] + ":target")
            ids = self._ids(raw)
            if source is None or target is None or not ids:
                self.omissions.add("unavailable_sync_relation:" + raw["id"])
                continue
            relations.append({"relation_id": raw["id"], "relation_type": raw["relation_type"],
                              "source": source, "target": target,
                              "order": raw["order"], "confidence": raw.get("confidence", 0.0),
                              "condition": raw.get("condition"), "evidence_ids": ids})
        public_objects = []
        for obj in objects.values():
            obj["accesses"] = list(obj["accesses"].values())
            public_objects.append(obj)
        return [{"id": "recode-native-sync", "kind": "sync_flow", "shared_objects": public_objects,
                 "locks": [], "lock_order_edges": [], "callbacks_and_async_boundaries": [],
                 "observed_relations": relations, "confidence": 0.35,
                 "evidence_ids": sorted({i for obj in public_objects for i in obj["evidence_ids"]}
                                        | {i for rel in relations for i in rel["evidence_ids"]})}]

    def query(self, request: dict) -> dict:
        require_first_group_request(request)
        for relative, expected in self.files.items():
            if hashlib.sha256(self._path(relative).read_bytes()).hexdigest() != expected:
                raise FirstGroupClientError("REcode captured artifact changed: " + relative)
        repository = request["repository"]
        if repository["commit_sha"] != self.manifest["commit"] or repository["name"] != self.manifest["repository"]:
            raise FirstGroupClientError("REcode result does not match the requested repository/head")
        profile = (request.get("analysis") or {}).get("build_profile")
        if profile and profile != self.manifest["build_profile"]:
            raise FirstGroupClientError("REcode result does not match the requested build profile")
        requested_snapshot = repository.get("graph_snapshot_id")
        if requested_snapshot and requested_snapshot != self.snapshot_id:
            raise FirstGroupClientError("REcode result does not match the requested graph snapshot")
        action = request["action"]
        nodes = self._nodes()
        resolved, unresolved = self._resolve(request["targets"], nodes)
        doc = self.documents.get(action)
        gaps = ["recode_native_export_has_no_exhaustive_cfg_alias_or_path_coverage"]
        if not doc:
            findings = []
            gaps.append("recode_export_missing:" + action)
        elif action == "get_call_chains":
            findings = self._calls(request, nodes, resolved)
            gaps.append("recode_bounded_call_subgraph_paths_not_enumerated")
        elif action == "get_resource_flows":
            findings = self._resources(doc)
        else:
            findings = self._sync(doc)
            gaps.extend(["recode_object_declarations_and_aliases_unresolved", "recode_execution_contexts_unresolved",
                         "recode_must_locksets_and_lock_order_unavailable"])
            if not self.races:
                gaps.append("recode_raw_shared_accesses_unavailable")
        if unresolved:
            gaps.append("recode_target_resolution_incomplete")
        if self.omissions:
            gaps.append("recode_external_or_unavailable_source_records_omitted")
        response = {"schema_version": "1.0", "run_id": self.manifest["run_id"] + ":" + action,
            "task_id": request["task_id"], "status": "partial",
            "analysis_context": {"action": action, "repository": copy.deepcopy(repository),
                "commit_sha": repository["commit_sha"], "graph_snapshot_id": self.snapshot_id,
                "build_profile": self.manifest["build_profile"],
                "analyzer": {"name": "REcode-native-export-adapter", "version": "1.0", "formal_evidence": False},
                "recode": {"run_id": self.manifest["run_id"], "artifact_sha256": self.files,
                           "native_provider": (doc or {}).get("source", {}).get("provider")}},
            "target_resolution": {"requested_total": len(request["targets"]), "resolved_total": len(resolved),
                                  "resolved_targets": resolved, "unresolved_targets": unresolved},
            "findings": findings, "evidence": list(self.evidence.values()),
            "coverage": {"complete": False, "reason": "; ".join(gaps), "stopped_because": gaps,
                         "omitted_record_count": len(self.omissions), "omitted_record_ids": sorted(self.omissions)[:100]},
            "warnings": list((doc or {}).get("warnings", [])), "errors": []}
        return require_first_group_response(response, expected_action=action,
                    expected_commit=repository["commit_sha"], expected_task_id=request["task_id"])
