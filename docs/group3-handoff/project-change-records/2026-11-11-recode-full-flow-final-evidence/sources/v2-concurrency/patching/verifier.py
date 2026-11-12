"""Deterministically execute a predeclared verification plan for one captured patch."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .pipeline import PATCH_WHITESPACE, PatchPipelineError, validate_verification_mode_binding
from .scope import assess_scope, scope_sha256
from .diagnostics import classify_failure, summarize_diagnostics
from .plan import (
    VerificationPlanError,
    validate_verification_plan,
    verification_plan_sha256,
)


MAX_OUTPUT_CHARS = 16000


def _current_diff(repository: Path) -> bytes:
    try:
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-C",
                str(repository),
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                "--",
            ],
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise PatchPipelineError("unable to read patch worktree diff") from exc


def _trim(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_OUTPUT_CHARS:
        return value, False
    marker = "\n... verification output omitted ...\n"
    head_size = (MAX_OUTPUT_CHARS - len(marker)) // 2
    tail_size = MAX_OUTPUT_CHARS - len(marker) - head_size
    return value[:head_size] + marker + value[-tail_size:], True


def verify_patch(
    patch_request: Mapping[str, Any],
    patch_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Run argv-only checks and fail if they mutate the captured tracked-file diff."""

    if patch_result.get("status") != "captured":
        raise PatchPipelineError("only a captured patch can be verified")
    if patch_result.get("candidate_id") != patch_request.get("candidate_id"):
        raise PatchPipelineError("patch result candidate does not match patch request")
    repository = Path(str(patch_request.get("workspace", {}).get("path") or "")).resolve()
    if not repository.is_dir():
        raise PatchPipelineError("patch workspace does not exist")
    patch_path = Path(str(patch_result.get("patch_path") or "")).resolve()
    if not patch_path.is_file():
        raise PatchPipelineError("captured patch file does not exist")
    expected_sha = str(patch_result.get("diff_sha256") or "")
    if hashlib.sha256(patch_path.read_bytes()).hexdigest() != expected_sha:
        raise PatchPipelineError("captured patch file hash does not match patch result")
    if hashlib.sha256(_current_diff(repository)).hexdigest() != expected_sha:
        raise PatchPipelineError("worktree diff changed before verification")

    raw_plan = patch_request.get("verification_plan")
    if not isinstance(raw_plan, Mapping):
        raise PatchPipelineError("patch request has no host-approved verification plan")
    try:
        plan = validate_verification_plan(raw_plan)
        plan_sha256 = verification_plan_sha256(plan)
    except VerificationPlanError as exc:
        raise PatchPipelineError("patch request verification plan is invalid") from exc
    if patch_request.get("verification_plan_sha256") != plan_sha256:
        raise PatchPipelineError("patch request verification plan hash does not match")
    scope = patch_request.get("verification_scope")
    if ("verification_scope" in patch_request) != ("verification_scope_sha256" in patch_request):
        raise PatchPipelineError("verification scope and hash must be supplied together")
    if "verification_scope" in patch_request and not isinstance(scope, Mapping):
        raise PatchPipelineError("verification scope must be an object")
    validate_verification_mode_binding(patch_request)
    if patch_request.get("test_strategy") == "behavior_before_patch":
        from .behavior import validate_certificate, validate_behavior_plan
        try:
            validate_certificate(patch_request.get("behavior_test"), patch_request["candidate_id"], patch_request["repository"])
            validate_behavior_plan(patch_request["behavior_test"], plan, scope)
        except (ValueError, OSError, KeyError) as exc:
            raise PatchPipelineError("frozen behavior reproduction changed or is invalid") from exc
    assessment = None
    if scope is not None:
        if scope_sha256(scope) != patch_request.get("verification_scope_sha256"):
            raise PatchPipelineError("verification scope hash does not match")
        try:
            assessment = assess_scope(scope, plan, patch_request["candidate_id"], patch_request["repository"])
        except VerificationPlanError as exc:
            raise PatchPipelineError("verification scope is invalid") from exc
    checks = plan["checks"]
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # CRLF source files retain real trailing-space checks. This configuration
    # applies only to verification subprocesses, preserving repository settings.
    configuration_index = int(environment.get("GIT_CONFIG_COUNT", "0"))
    environment["GIT_CONFIG_KEY_" + str(configuration_index)] = "core.whitespace"
    environment["GIT_CONFIG_VALUE_" + str(configuration_index)] = PATCH_WHITESPACE
    environment["GIT_CONFIG_COUNT"] = str(configuration_index + 1)
    results: list[dict[str, Any]] = []
    all_passed = assessment is None or assessment["status"] == "applicable"
    stopped = not all_passed
    for check in checks:
        if stopped:
            results.append(
                {
                    "name": check["name"],
                    "argv": check["argv"],
                    "timeout_seconds": check["timeout_seconds"],
                    "status": "not_run",
                    "exit_code": None,
                    "timed_out": False,
                    "stdout": "",
                    "stderr": "",
                    "output_truncated": False,
                    "reason": "verification scope needs review" if assessment and assessment["status"] != "applicable" else "a previous verification check failed",
                }
            )
            continue
        try:
            completed = subprocess.run(
                check["argv"],
                cwd=str(repository),
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=check["timeout_seconds"],
                check=False,
            )
            stdout, stdout_truncated = _trim(completed.stdout)
            stderr, stderr_truncated = _trim(completed.stderr)
            passed = completed.returncode == 0
            results.append(
                {
                    "name": check["name"],
                    "argv": check["argv"],
                    "timeout_seconds": check["timeout_seconds"],
                    "status": "passed" if passed else "failed",
                    "exit_code": completed.returncode,
                    "timed_out": False,
                    "stdout": stdout,
                    "stderr": stderr,
                    "output_truncated": stdout_truncated or stderr_truncated,
                }
            )
            all_passed = all_passed and passed
            if not passed:
                # Repeated behavior executions can exceed the display limit.
                # Classify the complete runner JSON before trimming it for storage.
                declaration = scope["checks"][len(results) - 1] if scope else None
                results[-1]["failure_classification"] = classify_failure(
                    dict(results[-1], stdout=completed.stdout), declaration, dict(patch_request)
                )
                stopped = True
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stdout, stdout_truncated = _trim(stdout)
            stderr, stderr_truncated = _trim(stderr)
            results.append(
                {
                    "name": check["name"],
                    "argv": check["argv"],
                    "timeout_seconds": check["timeout_seconds"],
                    "status": "failed",
                    "exit_code": None,
                    "timed_out": True,
                    "stdout": stdout,
                    "stderr": stderr,
                    "output_truncated": stdout_truncated or stderr_truncated,
                }
            )
            all_passed = False
            stopped = True
        except OSError as exc:
            results.append(
                {
                    "name": check["name"],
                    "argv": check["argv"],
                    "timeout_seconds": check["timeout_seconds"],
                    "status": "not_run",
                    "exit_code": None,
                    "timed_out": False,
                    "stdout": "",
                    "stderr": "",
                    "output_truncated": False,
                    "reason": "unable to start verification command: {}".format(exc),
                    "failure_classification": {"category": "environment_unavailable", "basis": "verification_command_could_not_start"},
                }
            )
            all_passed = False
            stopped = True

    for index, result in enumerate(results):
        if result["status"] == "failed" and "failure_classification" not in result:
            declaration = scope["checks"][index] if scope else None
            result["failure_classification"] = classify_failure(result, declaration, dict(patch_request))
    if assessment and assessment["status"] == "applicable":
        # Detect a test/runner file changed by any check as well as before it.
        assessment = assess_scope(scope, plan, patch_request["candidate_id"], patch_request["repository"])
        if assessment["status"] != "applicable":
            all_passed = False
    diff_unchanged = hashlib.sha256(_current_diff(repository)).hexdigest() == expected_sha
    if not diff_unchanged:
        all_passed = False
    return {
        "schema_version": "lock-race-patch-verification/0.1",
        "candidate_id": patch_request["candidate_id"],
        "repository": dict(patch_request["repository"]),
        "status": "passed" if all_passed else "failed",
        "patch_diff_sha256": expected_sha,
        "verification_plan_sha256": plan_sha256,
        "diff_unchanged": diff_unchanged,
        "checks": results,
        "diagnostics": summarize_diagnostics(results, assessment, diff_unchanged=diff_unchanged),
        **({"scope_assessment": assessment} if assessment else {}),
        "errors": [] if diff_unchanged else ["verification checks changed the tracked-file patch"],
    }
