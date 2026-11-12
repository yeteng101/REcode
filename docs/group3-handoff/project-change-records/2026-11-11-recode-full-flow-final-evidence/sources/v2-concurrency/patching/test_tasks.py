"""Bounded test authoring artifacts; model reports never become executable argv."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from .scope import scope_sha256, _labels
from .behavior import STRATEGY

ALLOWED_FILES = {"driver.c", "test-case.json", "README.md"}
TEST_TEMPLATES = {"redis_bio_c", "c_source"}


def git_guard(path: Path) -> dict:
    path = path.resolve(strict=True)
    def git(*args):
        return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(path), *args], stderr=subprocess.PIPE, timeout=30)
    return {"path": str(path), "head_sha": git("rev-parse", "HEAD").decode().strip(),
            "diff_sha256": hashlib.sha256(git("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")).hexdigest(),
            "status_sha256": hashlib.sha256(git("status", "--porcelain=v1", "--untracked-files=all")).hexdigest()}


def validate_test_policy(policy: dict, repository: dict, source: Path) -> dict:
    if isinstance(policy, dict) and policy.get("schema_version") == "lock-race-test-policy/0.2":
        required = {"schema_version", "image_id", "repetitions"}
        normal_fields = {"normal_repository", "normal_head_sha", "normal_basis"}
        if (not required <= set(policy) or set(policy) - (required | normal_fields | {"template"})
                or not isinstance(policy.get("template", "redis_bio_c"), str)
                or policy.get("template", "redis_bio_c") not in TEST_TEMPLATES
                or type(policy["repetitions"]) is not int or not 3 <= policy["repetitions"] <= 5
                or not isinstance(policy["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", policy["image_id"])):
            raise ValueError("invalid behavior test policy")
        defective = git_guard(source)
        clean = hashlib.sha256(b"").hexdigest()
        if defective["head_sha"] != repository["head_sha"] or any(defective[k] != clean for k in ("diff_sha256", "status_sha256")):
            raise ValueError("behavior reproduction source must be the clean defective head")
        if set(policy) & normal_fields:
            if (not normal_fields <= set(policy) or not isinstance(policy["normal_repository"], str)
                    or not policy["normal_repository"] or not isinstance(policy["normal_head_sha"], str)
                    or not re.fullmatch(r"[0-9a-f]{40}", policy["normal_head_sha"])
                    or not isinstance(policy["normal_basis"], str) or not policy["normal_basis"].strip()):
                raise ValueError("optional normal control requires its version and suitability basis")
            normal = git_guard(Path(policy["normal_repository"]))
            if normal["head_sha"] != policy["normal_head_sha"] or any(normal[k] != clean for k in ("diff_sha256", "status_sha256")):
                raise ValueError("normal control version/cleanliness mismatch")
            return dict(policy, normal_repository=normal["path"])
        return copy.deepcopy(policy)
    if (not isinstance(policy, dict) or set(policy) != {"schema_version", "normal_repository", "normal_head_sha", "image_id"}
            or policy["schema_version"] != "lock-race-test-policy/0.1"
            or not isinstance(policy["normal_repository"], str) or not policy["normal_repository"]
            or not isinstance(policy["normal_head_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", policy["normal_head_sha"])
            or not isinstance(policy["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", policy["image_id"])):
        raise ValueError("invalid host test policy or immutable image ID")
    normal = git_guard(Path(policy["normal_repository"]))
    defective = git_guard(source)
    clean = hashlib.sha256(b"").hexdigest()
    if normal["head_sha"] != policy["normal_head_sha"] or defective["head_sha"] != repository["head_sha"]:
        raise ValueError("test policy normal/defective version binding mismatch")
    if any(g["diff_sha256"] != clean or g["status_sha256"] != clean for g in (normal, defective)):
        raise ValueError("test discrimination repositories must be clean")
    return dict(policy, normal_repository=normal["path"])


def prepare_test_request(review_request: dict, review: dict, scope: dict, assessment: dict,
                         policy: dict, source: Path, workspace: Path, *, attempt=1,
                         feedback: dict | None = None, product_workspace: Path | None = None,
                         test_strategy: str | None = None) -> dict:
    if attempt not in (1, 2) or (attempt == 2) != (feedback is not None):
        raise ValueError("one initial test task and one feedback revision only")
    policy = validate_test_policy(policy, review_request["repository"], source)
    behavior = test_strategy == STRATEGY
    if behavior and (policy["schema_version"] != "lock-race-test-policy/0.2" or product_workspace is not None
                     or scope["selected_protocol"] != "behavior"):
        raise ValueError("behavior authoring requires a technique-neutral scope, v0.2 policy and no product patch")
    workspace = workspace.resolve()
    if attempt == 1:
        if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
            raise ValueError("initial test workspace must be absent or empty")
        workspace.mkdir(parents=True, exist_ok=True)
    guards = [git_guard(source)]
    if policy.get("normal_repository"):
        guards.append(git_guard(Path(policy["normal_repository"])))
    if product_workspace:
        guards.append(git_guard(product_workspace))
    template = policy.get("template", "redis_bio_c")
    files = sorted({u["path"] for u in review_request.get("source_units", [])}
                   | ({"src/atomicvar.h"} if template == "redis_bio_c" else set()))
    include_paths = (["/source/src", "/source/deps/hiredis", "/source/deps/linenoise", "/source/deps/lua/src", "/source/deps/hdr_histogram"]
                     if template == "redis_bio_c" else ["/source"])
    request = {"schema_version": "lock-race-test-request/0.1", "candidate_id": review_request["candidate_id"],
            "repository": copy.deepcopy(review_request["repository"]), "source_repository": str(source.resolve()),
            "candidate_request": copy.deepcopy(review_request), "review": copy.deepcopy(review),
            "verification_scope": copy.deepcopy(scope), "scope_assessment": copy.deepcopy(assessment),
            "policy": policy, "attempt": attempt, "feedback": copy.deepcopy(feedback),
            "execution_template": {"name": template, "compiler": "gcc", "c_standard": "gnu11",
                "flags": ["-O1", "-pthread", "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections"],
                "source_mount": "/source", "test_mount": "/regressions", "binary": "/work/protocol",
                "include_paths": include_paths,
                "prelinked_libraries": [], "execution_timeout_seconds": 10,
                "exit_codes": {"0": "satisfied", "42": "target_assertion", "90": "test_harness_error"}},
            "workspace": {"path": str(workspace)}, "product_guards": guards,
            "constraints": {"allowed_files": sorted(ALLOWED_FILES), "readable_source_files": files,
                            "network_allowed": False, "verification_allowed": False, "commit_allowed": False}}
    if behavior:
        request["test_strategy"] = STRATEGY
        request["behavior_requirements"] = {
            "goal": "Derive an observable correctness assertion from source/contracts and cited evidence; reproduce the original behavior before product repair.",
            "forbidden_oracles": ["requiring a particular lock or lock-call count", "requiring an atomic type/API or source spelling", "branching on repository version/path", "rewriting the product algorithm in the driver"],
            "schedule": "Use bounded, semantics-preserving scheduling. Instrumentation may arrange events but must not assert the repair technique. Compilation errors, unrelated crashes and timeouts are not target reproduction.",
            "independence": "No repair patch is available. A host independently reviews actual assertions and scheduling before execution.",
            "repetitions": policy["repetitions"]}
    if product_workspace:
        raw = subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(product_workspace), "diff", "--binary",
                                       "--no-ext-diff", "--no-textconv", "HEAD", "--"], stderr=subprocess.PIPE, timeout=30)
        text = raw.decode("utf-8", errors="replace")
        request["product_patch"] = {"path": str(product_workspace.resolve()), "expected_head_sha": guards[-1]["head_sha"],
            "diff_sha256": hashlib.sha256(raw).hexdigest(), "current_diff": text[:32000], "current_diff_truncated": len(text) > 32000,
            "readable_source_files": files}
    if len(json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True) + "\n") > 160000:
        raise ValueError("test request exceeds 160000-character delivery budget; host must prepare bounded context")
    return request


def capture_test_artifact(request: dict, report: dict) -> dict:
    if (not isinstance(report, dict) or set(report) != {"schema_version", "candidate_id", "status", "changed_files", "summary", "verification_status"}
            or report["schema_version"] != "lock-race-test-worker-report/0.1"
            or report["candidate_id"] != request["candidate_id"] or report["status"] not in {"authored", "refused"}
            or report["verification_status"] != "not_run" or not isinstance(report["summary"], str) or not report["summary"]):
        raise ValueError("invalid test-worker report")
    for guard in request["product_guards"]:
        if git_guard(Path(guard["path"])) != guard:
            raise ValueError("test worker changed a protected product repository")
    declared_root = Path(request["workspace"]["path"])
    root = declared_root.resolve(strict=True)
    if declared_root.is_symlink() or root != declared_root:
        raise ValueError("test workspace path was redirected")
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink() or path.resolve().parent != root or not path.is_file() or path.name not in ALLOWED_FILES:
            raise ValueError("test worker edited an unapproved path")
        data = path.read_bytes()
        if len(data) > 65536:
            raise ValueError("test artifact exceeds 64 KiB")
        files[path.name] = hashlib.sha256(data).hexdigest()
    declared = report["changed_files"]
    if not isinstance(declared, list) or any(not isinstance(v, str) for v in declared) or len(declared) != len(set(declared)):
        raise ValueError("invalid test changed_files")
    if report["status"] == "refused":
        # Revision refusal can preserve the first attempt's captured files.
        previous = (request.get("feedback") or {}).get("prior_artifact_files", {})
        if declared or files != previous:
            raise ValueError("refused test worker must preserve the prior workspace")
        return {"status": "refused", "candidate_id": request["candidate_id"], "summary": report["summary"]}
    prior_files = (request.get("feedback") or {}).get("prior_artifact_files", {})
    expected_changed = {name for name, value in files.items() if prior_files.get(name) != value} | (set(prior_files) - set(files))
    if set(declared) != expected_changed or not {"driver.c", "test-case.json"} <= set(files):
        raise ValueError("test report does not match actual required files")
    case = json.loads((root / "test-case.json").read_text(encoding="utf-8"))
    if (not isinstance(case, dict) or set(case) != {"schema_version", "candidate_id", "template", "supported_protocols", "covers", "limitations", "rationale"}
            or case["schema_version"] != "lock-race-test-case/0.1" or case["candidate_id"] != request["candidate_id"]
            or case["template"] != request.get("execution_template", {}).get("name", "redis_bio_c")
            or case["template"] not in TEST_TEMPLATES
            or not isinstance(case["rationale"], str) or not case["rationale"]):
        raise ValueError("invalid fixed-template test case")
    _labels(case["supported_protocols"], "supported_protocols", nonempty=True)
    _labels(case["covers"], "covers", nonempty=True)
    if not isinstance(case["limitations"], list) or not case["limitations"] or any(not isinstance(v, str) or not v for v in case["limitations"]):
        raise ValueError("generated test must declare limitations")
    scope = request["verification_scope"]
    behavior = request.get("test_strategy") == STRATEGY
    if behavior and case["supported_protocols"] != ["behavior"]:
        raise ValueError("behavior test cannot restrict a repair technique; use the neutral behavior label")
    if scope["selected_protocol"] not in case["supported_protocols"] or not set(scope["required_coverage"]) <= set(case["covers"]):
        raise ValueError("generated test does not declare the requested protocol/coverage")
    snapshot = root.parent / ("captured-" + str(request["attempt"]))
    if snapshot.exists():
        if snapshot.is_symlink() or not snapshot.is_dir() or snapshot.resolve() != snapshot:
            raise ValueError("test snapshot path was redirected")
        actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in snapshot.iterdir() if p.is_file() and not p.is_symlink()}
        if actual != files or len(list(snapshot.iterdir())) != len(files):
            raise ValueError("existing test snapshot differs; never overwrite historical tests")
    else:
        snapshot.mkdir()
        for name, expected in files.items():
            data = (root / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("test changed during capture")
            (snapshot / name).write_bytes(data)
    if json.loads((snapshot / "test-case.json").read_text(encoding="utf-8")) != case:
        raise ValueError("test metadata changed during capture")
    artifact = {"schema_version": "lock-race-test-artifact/0.1", "status": "captured", "candidate_id": request["candidate_id"],
            "repository": request["repository"], "workspace": str(snapshot), "author_workspace": str(root), "files": files, "test_case": case,
            "artifact_sha256": scope_sha256({"files": files, "candidate_id": request["candidate_id"], "repository": request["repository"]})}
    if behavior:
        artifact.update(test_strategy=STRATEGY, repetitions=request["policy"]["repetitions"])
        artifact["artifact_sha256"] = artifact_digest(artifact)
    return artifact


def artifact_digest(artifact):
    fields = {k: artifact[k] for k in ("files", "candidate_id", "repository")}
    if "test_strategy" in artifact:
        fields.update(test_strategy=artifact["test_strategy"], repetitions=artifact["repetitions"])
    return scope_sha256(fields)


def assert_artifact(artifact: dict) -> None:
    declared_root = Path(artifact["workspace"])
    root = declared_root.resolve(strict=True)
    if declared_root.is_symlink() or root != declared_root:
        raise ValueError("captured test workspace path was redirected")
    actual = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root or path.name not in ALLOWED_FILES:
            raise ValueError("captured test directory changed")
        actual[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != artifact["files"] or artifact["artifact_sha256"] != artifact_digest(artifact):
        raise ValueError("captured test hash changed")
    if "test_strategy" in artifact and (artifact["test_strategy"] != STRATEGY or type(artifact.get("repetitions")) is not int
                                        or not 3 <= artifact["repetitions"] <= 5):
        raise ValueError("invalid frozen behavior test settings")
    if json.loads((root / "test-case.json").read_text(encoding="utf-8")) != artifact["test_case"]:
        raise ValueError("captured metadata differs from its hashed test-case file")


def discriminate_test(artifact_path: Path, policy: dict, source: Path, output: Path) -> dict:
    """The same captured driver must accept normal and assert on defective HEAD."""
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert_artifact(artifact)
    validate_test_policy(policy, artifact["repository"], source)
    output.mkdir(parents=True, exist_ok=True)
    runner = Path(__file__).resolve().parents[1] / "scripts/run_generated_redis_test.py"
    rows = []
    versions = [("defective", source, 42)]
    if policy.get("normal_repository"):
        versions.insert(0, ("normal", Path(policy["normal_repository"]), 0))
    for label, repository, expected in versions:
        destination = output / (label + ".json")
        proc = subprocess.run([sys.executable, str(runner), "--repository", str(repository),
                               "--image", policy["image_id"], "--test-artifact", str(artifact_path),
                                "--output", str(destination)], capture_output=True, text=True, encoding="utf-8", timeout=120 * artifact.get("repetitions", 1))
        data = json.loads(destination.read_text(encoding="utf-8")) if destination.is_file() else {}
        expected_head = policy["normal_head_sha"] if label == "normal" else artifact["repository"]["head_sha"]
        valid = (data.get("exit_code") == expected and type(data.get("exit_code")) is int
                 and data.get("head_sha") == expected_head and data.get("head_sha_after") == expected_head
                 and data.get("image_id") == policy["image_id"] and data.get("driver_unchanged") is True
                 and data.get("diff_unchanged") is True and data.get("timed_out") is False
                 and data.get("test_artifact_sha256") == artifact["artifact_sha256"]
                 and data.get("driver_sha256") == artifact["files"]["driver.c"]
                 and proc.returncode == (0 if expected == 0 else 1))
        if artifact.get("test_strategy") == STRATEGY:
            executions = data.get("executions", [])
            valid = (valid and data.get("stable") is True and len(executions) == policy["repetitions"]
                     and all(e.get("exit_code") == expected and e.get("head_sha") == expected_head
                             and e.get("head_sha_after") == expected_head and e.get("image_id") == policy["image_id"]
                             and e.get("driver_sha256") == artifact["files"]["driver.c"] and e.get("driver_unchanged") is True
                             and e.get("diff_unchanged") is True and e.get("timed_out") is False for e in executions))
        rows.append({"version": label, "expected_exit_code": expected, "actual_exit_code": data.get("exit_code"),
                     "matched": valid, "result_path": str(destination), "runner_exit_code": proc.returncode,
                     "result_sha256": hashlib.sha256(destination.read_bytes()).hexdigest() if destination.is_file() else None,
                     "category": ("none" if valid else "environment_unavailable" if data.get("errors") else
                                  "unknown" if data.get("timed_out") else "test_no_discrimination" if data.get("exit_code") in (0, 42)
                                  else "test_harness_error")})
    assert_artifact(artifact)
    validate_test_policy(policy, artifact["repository"], source)
    return {"schema_version": "lock-race-test-discrimination/0.1", "candidate_id": artifact["candidate_id"],
            "status": "passed" if all(row["matched"] for row in rows) else "failed", "versions": rows,
            "retry_test_allowed": all(row["category"] in {"none", "test_no_discrimination", "test_harness_error"} for row in rows),
            "test_artifact_sha256": artifact["artifact_sha256"],
            "interpretation": "Same frozen test reproduced the target on the defective head; an optional normal control was checked when supplied. Fixed version still requires independent verification."}


def plan_with_generated_test(original_plan: dict, original_scope: dict, artifact_path: Path,
                             image: str, logs: Path) -> tuple[dict, dict]:
    """Replace defect oracles only; preserve all format/build/official commands."""
    from .plan import validate_verification_plan, verification_plan_sha256
    from .scope import validate_scope
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert_artifact(artifact)
    validate_scope(original_scope, original_plan, artifact["candidate_id"], artifact["repository"])
    approved = validate_verification_plan(original_plan)
    checks, declarations = [], []
    for check, declaration in zip(approved["checks"], original_scope["checks"]):
        if declaration["purpose"] != "defect_regression":
            checks.append(copy.deepcopy(check))
            declarations.append(copy.deepcopy(declaration))
    runner = Path(__file__).resolve().parents[1] / "scripts/run_generated_redis_test.py"
    helper = runner.with_name("run_redis_protocol_regression.py")
    name = "generated-defect-regression"
    if any(c["name"] == name for c in checks):
        raise ValueError("generated test check name conflicts with preserved check")
    checks.append({"name": name, "argv": [sys.executable, str(runner), "--repository", ".", "--image", image,
                   "--test-artifact", str(artifact_path.resolve()), "--output", str(logs.resolve() / "fixed.json")], "timeout_seconds": 120 * artifact.get("repetitions", 1)})
    paths = [runner, helper, artifact_path.resolve()] + [Path(artifact["workspace"]) / name for name in artifact["files"]]
    if artifact.get("test_strategy") == STRATEGY:
        paths.extend([Path(__file__).resolve(), Path(__file__).with_name("behavior.py")])
    declarations.append({"name": name, "purpose": "defect_regression",
        "supported_protocols": artifact["test_case"]["supported_protocols"], "covers": artifact["test_case"]["covers"],
        "limitations": artifact["test_case"]["limitations"],
        "artifacts": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths], "failure_codes": {}})
    plan = {"schema_version": "lock-race-verification-plan/0.1", "checks": checks}
    scope = dict(copy.deepcopy(original_scope), checks=declarations, verification_plan_sha256=verification_plan_sha256(plan))
    validate_scope(scope, plan, artifact["candidate_id"], artifact["repository"])
    return plan, scope
