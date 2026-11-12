"""Exercise REcode native analysis, its real HTTP job service and specialty handoffs."""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from pr_adapter import build_review_input


ROOT = Path(__file__).resolve().parents[1]
NAME = "cbug-integration/recode-contract"
PROFILE = "recode-linux-clang"
SOURCES = {
    "workers.c": """#include <pthread.h>
#include "state.h"
int counter = 0;
pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
void *writer_a(void *unused) {
    (void)unused;
    pthread_mutex_lock(&mutex);
    counter++;
    pthread_mutex_unlock(&mutex);
    return 0;
}
void *writer_b(void *unused) {
    (void)unused;
    pthread_mutex_lock(&mutex);
    counter++;
    pthread_mutex_unlock(&mutex);
    return 0;
}
int main(void) {
    pthread_t a, b;
    pthread_create(&a, 0, writer_a, 0);
    pthread_create(&b, 0, writer_b, 0);
    pthread_join(a, 0);
    pthread_join(b, 0);
    return observe(counter);
}
""",
    "state.h": "#ifndef STATE_H\n#define STATE_H\nint observe(int value);\nvoid allocate_temporary(void);\n#endif\n",
    "observe.c": "#include \"state.h\"\nint observe(int value) {\n    allocate_temporary();\n    return value;\n}\n",
    "resource.c": "#include <stdlib.h>\nvoid allocate_temporary(void) {\n    char *buffer = malloc(32);\n    free(buffer);\n}\n",
}

NATIVE_DRIVER = '''import json
from pathlib import Path
from clang_pipeline.pipeline import run_pipeline
from clang_pipeline.repo_agent import bundle_artifacts, relativize_artifact_paths, generic_compile_commands
settings=json.loads(Path('/integration/native-settings.json').read_text())
source=Path('/source')
workspace=Path('/work/pipeline')
output=Path('/artifacts')
result=run_pipeline(source_root=source,workspace=workspace,run_id=settings['run_id'],repository=settings['repository'],commit=settings['head_sha'],build_profile=settings['profile'],compile_commands=generic_compile_commands(source))
bundle_artifacts(workspace,output,repository_url=settings['repository_url'],commit=settings['head_sha'],run_id=settings['run_id'],build_profile=settings['profile'])
relativize_artifact_paths(output,source)
Path('/artifacts/native-result.json').write_text(json.dumps(result,indent=2))
print(json.dumps({'pipeline_status':result['results'],'native':result['native']}))
'''


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def git(repository: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(repository), *args],
                                   text=True, encoding="utf-8", stderr=subprocess.PIPE).strip()


def run_cli(argv: list[str], output: Path) -> dict:
    proc = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=600)
    output.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode not in {0, 2}:
        raise RuntimeError("integration CLI failed, see " + str(output))
    return {"exit_code": proc.returncode, "log": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recode-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--lock-variant", choices=("common", "distinct", "missing"), default="common")
    args = parser.parse_args()
    recode = args.recode_root.resolve()
    run = args.output.resolve()
    run.mkdir(parents=True, exist_ok=False)
    source = run / "repository"
    source.mkdir()
    for name, content in SOURCES.items():
        (source / name).write_text(content, encoding="utf-8")
    git(source, "init", "-q")
    git(source, "add", ".")
    git(source, "-c", "user.name=CBUG integration", "-c", "user.email=integration@example.invalid", "commit", "-qm", "baseline")
    base = git(source, "rev-parse", "HEAD")
    text = SOURCES["workers.c"].replace("    counter++;", "    counter += 2;", 1)
    if args.lock_variant != "common":
        before, worker = text.split("void *writer_b", 1)
        if args.lock_variant == "distinct":
            before = before.replace("void *writer_a", "pthread_mutex_t second_mutex = PTHREAD_MUTEX_INITIALIZER;\nvoid *writer_a")
            worker = worker.replace("&mutex", "&second_mutex")
        else:
            worker = worker.replace("    pthread_mutex_lock(&mutex);\n", "").replace("    pthread_mutex_unlock(&mutex);\n", "")
        text = before + "void *writer_b" + worker
    (source / "workers.c").write_text(text, encoding="utf-8")
    (source / "observe.c").write_text(SOURCES["observe.c"].replace("return value;", "return value + 1;"), encoding="utf-8")
    git(source, "add", ".")
    git(source, "-c", "user.name=CBUG integration", "-c", "user.email=integration@example.invalid", "commit", "-qm", "change two functions")
    head = git(source, "rev-parse", "HEAD")
    settings = {"run_id": "recode-cbug-integration-" + head[:12], "repository": NAME,
                "repository_url": "https://github.com/" + NAME + ".git", "head_sha": head, "profile": PROFILE}
    write(run / "native-settings.json", settings)
    (run / "native-driver.py").write_text(NATIVE_DRIVER, encoding="utf-8")
    image = subprocess.check_output(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True).strip()
    invocations = []
    def native_runner(repository_url, *, output_dir, **options):
        if repository_url != settings["repository_url"] or options.get("commit") != head:
            raise ValueError("local integration runner source/version mismatch")
        argv = ["docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--cpus", "2", "--memory", "2g", "--pids-limit", "128",
                "--tmpfs", "/tmp:rw,size=128m,mode=1777", "--tmpfs", "/work:rw,size=768m,mode=1777",
                "--mount", "type=bind,src=" + str(recode) + ",dst=/opt/recode,readonly",
                "--mount", "type=bind,src=" + str(source) + ",dst=/source,readonly",
                "--mount", "type=bind,src=" + str(run) + ",dst=/integration,readonly",
                "--mount", "type=bind,src=" + str(output_dir) + ",dst=/artifacts",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/opt/recode",
                "--workdir", "/opt/recode", image, "python3", "/integration/native-driver.py"]
        invocations.append(argv)
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        (run / "native-analysis.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode:
            raise RuntimeError("native REcode analysis failed, see native-analysis.log")
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        return {"status": "succeeded", "repository_url": repository_url, "repository": NAME, "commit": head,
                "run_id": manifest["run_id"], "build_profile": PROFILE, "output_dir": str(output_dir)}
    sys.path.insert(0, str(recode))
    from clang_pipeline import agent_server
    from clang_pipeline.agent_jobs import AnalysisJobManager
    manager = AnalysisJobManager(run / "service", max_workers=1, runner=native_runner, max_attempts=1)
    previous = agent_server._JOB_MANAGER
    agent_server._JOB_MANAGER = manager
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.perf_counter()
    try:
        metadata = {"base": {"sha": base}, "head": {"sha": head}, "repository": {"name": NAME},
                    "number": "local-integration", "pr_url": "local:two-commit-integration"}
        review = build_review_input(metadata, source, PROFILE)
        write(run / "review-input.json", review)
        http = run_cli(["v2-common/review_pipeline.py", "--review", str(run / "review-input.json"),
                       "--first-group-provider", "recode", "--first-group-url", f"http://127.0.0.1:{server.server_port}",
                       "--recode-job-timeout", "360", "--output-dir", str(run / "http")], run / "http.log")
        coordination = json.loads((run / "http/coordination-report.json").read_text(encoding="utf-8"))
        if coordination["status"] != "prepared" or len(coordination["handoffs"]) != 2:
            raise RuntimeError("both specialty handoffs were not prepared")
        cache = next((run / "http/recode-artifacts").iterdir())
        offline = run_cli(["v2-common/review_pipeline.py", "--review", str(run / "review-input.json"),
                          "--first-group-provider", "recode", "--recode-artifacts", str(cache),
                          "--output-dir", str(run / "artifact")], run / "artifact.log")
        specialty = run_cli(["v2-concurrency/concurrency_subagent.py", "--context-bundle", str(run / "http/concurrency-context-bundle.json"),
                            "--prepare-dir", str(run / "concurrency"), "--output", str(run / "concurrency-result.json")], run / "concurrency.log")
        result = json.loads((run / "concurrency-result.json").read_text(encoding="utf-8"))
        if result["candidate_count"] < 1:
            raise RuntimeError("native access facts did not reach candidate preparation")
        candidates = json.loads(Path(result["outputs"]["candidate_bundle"]).read_text(encoding="utf-8"))
        assert all(c["candidate_status"] == "needs_more_context" for c in candidates["candidates"])
        assert any(c["shared_object"]["declaration"] for c in candidates["candidates"])
        context = json.loads((run / "http/concurrency-context-bundle.json").read_text(encoding="utf-8"))
        sync = context["sync_facts"]
        observations = sync["findings"][0]["source_observations"]
        assert len(observations["thread_events"]) == 4
        assert len(observations["lock_operations"]) == (2 if args.lock_variant == "missing" else 4)
        expressions = {op["lock_expression"] for op in observations["lock_operations"] if op["kind"] == "acquire"}
        assert expressions == ({"&mutex", "&second_mutex"} if args.lock_variant == "distinct" else {"&mutex"})
        assert len(invocations) == 1
        summary = {"status": "passed", "recode_commit": git(recode, "rev-parse", "HEAD"), "image_id": image,
                   "repository": settings, "base_sha": base, "head_sha": head, "source_files": len(SOURCES),
                   "changed_files": review["diff"]["changed_files"], "analysis_invocations": len(invocations),
                   "http": http, "artifact": offline, "specialty": specialty, "candidate_count": result["candidate_count"],
                   "candidate_status": "needs_more_context", "coverage": result["coverage"],
                   "source_observations": {key: len(value) for key, value in observations.items()},
                   "lock_variant": args.lock_variant,
                   "scope": "Actual upstream HTTP handler/SQLite queue/object store with a local integration runner executing real REcode Clang/native analysis in Docker. Production GitHub-download sandbox runner is not exercised. Synthetic source is explicitly supplied; facts are analyzer-generated, with no hand-authored first-group responses.",
                   "seconds": round(time.perf_counter() - started, 3)}
        write(run / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        agent_server._JOB_MANAGER = previous
        manager.close()


if __name__ == "__main__":
    main()
