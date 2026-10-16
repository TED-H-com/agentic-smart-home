"""Interactive candidate preview: verifier build, bundle reader, and host-only serving.

Most cases use fixtures and a fake toolchain. One case runs the real tsc and vite build;
the --containers case runs it inside the packaged Runner image. None is an installed-VM run.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_controller import VERSION  # noqa: E402
from agentic_controller.models import WorkerPreview as ModelPreview  # noqa: E402
from agentic_controller.operations import Operations  # noqa: E402
from agentic_controller.store import Conflict  # noqa: E402
from agentic_controller.workers import WorkerError  # noqa: E402
from agentic_host.coding_program import (  # noqa: E402
    VERIFY_PROGRAM,
    verification_program,
    web_build_step_seconds,
)
from agentic_host.coding_sandbox import (  # noqa: E402
    Limits,
    SandboxError,
    bundle_relative,
    container_command,
    load_stored_bundle,
    read_artifacts,
    read_web_bundle,
    store_web_bundle,
)
from agentic_host.coding_service import discard_job_content  # noqa: E402
from agentic_host.manager import HostError  # noqa: E402
from agentic_host.preview import Previews, preview_server  # noqa: E402
from test_api import client_for  # noqa: E402
from web_toolchain import sync_web_toolchain  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JOB = "ab" * 16
FIXTURE_KEY = re.compile(r"[a-z0-9/_-]{1,120}\Z")
REQUIRED_FIXTURES = {
    "deployment", "session", "tasks", "status", "host/home-entry", "workers", "worker/jobs",
    "inventory", "automations", "services", "retention", "evidence", "gateway", "gateway/messages",
    "hermes", "runtimes", "host/status", "host/bridge/maintenance", "host/home-worker/maintenance",
    "bootstrap", "network", "updates", "gateway/channels/line/pairing",
}


def _web_touched(patch):
    start = VERIFY_PROGRAM.index("    touched=False\n")
    end = VERIFY_PROGRAM.index("    if code!=0 or not touched:")
    namespace = {}
    exec("def check(patch):\n" + VERIFY_PROGRAM[start:end] + "    return touched\n", namespace)
    return namespace["check"](patch)


def test_quoted_web_paths_count_as_web_changes():
    assert _web_touched(b"diff --git a/web/src/app.tsx b/web/src/app.tsx\n")
    assert _web_touched(b'diff --git "a/web/src/\\346\\226\\207.tsx" "b/web/src/\\346\\226\\207.tsx"\n')
    assert _web_touched(b'--- a/web/src/app.tsx\n')
    assert _web_touched(b'+++ b/web/src/app.tsx\n')
    assert _web_touched(b'--- "a/web/src/app.tsx"\n')
    assert _web_touched(b'+++ "b/web/src/app.tsx"\n')
    assert _web_touched(b"rename from web/src/old.tsx\n")
    assert _web_touched(b"rename to web/src/new.tsx\n")
    assert _web_touched(b'rename from "web/src/old.tsx"\n')
    assert _web_touched(b'rename to "web/src/new.tsx"\n')
    assert _web_touched(b"copy from web/src/old.tsx\n")
    assert _web_touched(b"copy to web/src/new.tsx\n")
    assert _web_touched(b'copy from "web/src/old.tsx"\n')
    assert _web_touched(b'copy to "web/src/new.tsx"\n')
    assert not _web_touched(b"diff --git a/docs/web.md b/docs/web.md\n--- a/docs/web.md\n+++ b/docs/web.md\n")
    assert not _web_touched(b"diff --git a/README b/README\n")


def test_web_build_budget_matches_the_program_copy():
    assert web_build_step_seconds(0) == 120
    assert web_build_step_seconds(100) == 120
    assert web_build_step_seconds(150) == 90
    assert web_build_step_seconds(210) == 30
    assert web_build_step_seconds(210.1) is None
    assert web_build_step_seconds(240) is None
    start = VERIFY_PROGRAM.index("def web_build_step_seconds")
    copied = VERIFY_PROGRAM[start:VERIFY_PROGRAM.index("\ndef ", start)]
    namespace = {}
    exec(copied, namespace)
    for elapsed in (0, 100, 150, 210, 210.1, 240):
        assert namespace["web_build_step_seconds"](elapsed) == web_build_step_seconds(elapsed)
    assert len(verification_program().encode()) <= 65536
    assert len(verification_program("independent_home_verifier").encode()) <= 65536


def _git(source, *args, check=True):
    return subprocess.run(["git", *args], cwd=source, check=check, capture_output=True)


def _repo(source, files):
    source.mkdir(parents=True)
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "demo@example.invalid")
    _git(source, "config", "user.name", "Demo")
    for name, data in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "base")


def _patch(source, mutate):
    mutate()
    _git(source, "add", "-A")
    patch = _git(source, "diff", "--cached").stdout
    _git(source, "reset", "-q", "--hard")
    _git(source, "clean", "-qfd")
    assert patch.startswith(b"diff --git ")
    return patch


def _run_program(tmp_path, *, location, touch, drift=False, budget=False):
    work = tmp_path / "work"
    source = work / "source"
    toolchain = tmp_path / "toolchain"
    pinned = b'{"name":"demo","private":true}\n'
    lock = b'{"name":"demo","lockfileVersion":3}\n'
    fixtures = b'{"session":{"mode":"host","csrf":"preview-csrf"}}\n'
    files = {
        "README": b"base\n",
        "web/package.json": pinned,
        "web/package-lock.json": b'{"name":"demo","lockfileVersion":9}\n' if drift else lock,
        "web/preview/fixtures.json": fixtures,
    }
    _repo(source, files)
    if touch:
        def mutate():
            demo = source / "web/src/demo.tsx"
            demo.parent.mkdir(parents=True, exist_ok=True)
            demo.write_text("export const demo = true\n")
    else:
        def mutate():
            (source / "README").write_text("changed\n")
    patch = _patch(source, mutate)
    (work / "candidate.patch").write_bytes(patch)
    (work / "verification.ini").write_text("[pytest]\n")
    tests = work / "trusted-tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (work / "scratch").mkdir()
    (work / "artifacts").mkdir()
    toolchain.mkdir()
    (toolchain / "package.json").write_bytes(pinned)
    (toolchain / "package-lock.json").write_bytes(lock)
    (toolchain / "node_modules/typescript/bin").mkdir(parents=True)
    (toolchain / "node_modules/vite/bin").mkdir(parents=True)
    node = tmp_path / "node"
    node.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "log = Path(sys.argv[0]).with_suffix('.log')\n"
        "previous = json.loads(log.read_text()) if log.exists() else []\n"
        "previous.append({'argv': sys.argv[1:], 'env': {k: os.environ.get(k) for k in "
        "('HOME','CI','GOMAXPROCS','UV_THREADPOOL_SIZE','NODE_OPTIONS','NO_COLOR','PATH')}})\n"
        "log.write_text(json.dumps(previous))\n"
        "if any('vite' in arg for arg in sys.argv):\n"
        "    out = Path(sys.argv[sys.argv.index('--outDir') + 1])\n"
        "    (out / 'assets').mkdir(parents=True, exist_ok=True)\n"
        "    (out / 'index.html').write_text('<!doctype html><title>demo</title>')\n"
        "    (out / 'assets' / 'app.js').write_text('console.log(1)\\n')\n"
        "raise SystemExit(0)\n")
    node.chmod(0o755)
    program = verification_program(location)
    program = program.replace("'/usr/local/bin/node'", repr(str(node)))
    program = program.replace("/opt/web-toolchain", str(toolchain))
    program = program.replace("/work", str(work))
    if budget:
        program = program.replace("time.monotonic()-_web_started", "240")
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    # The sandbox has no pytest runner options. A leaked --basetemp makes pytest exit 4.
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = "*"
    completed = subprocess.run([sys.executable, "-I", "-c", program], cwd=source, env=env, capture_output=True)
    logged = work / "artifacts" / "verification.log"
    detail = completed.stderr.decode() + completed.stdout.decode()
    if logged.is_file():
        detail += logged.read_text(errors="replace")
    status_path = work / "artifacts" / "web-preview.json"
    assert status_path.is_file(), detail
    status = json.loads(status_path.read_text())
    receipt = json.loads((work / "artifacts" / "verification.json").read_text())
    return completed.returncode, status, receipt, node.with_suffix(".log"), detail


def test_verifier_web_preview_states(tmp_path):
    code, status, receipt, log, detail = _run_program(
        tmp_path / "plain", location="independent_host_verifier", touch=False)
    assert code == 0 and status == {"state": "not_applicable", "reason": None}, detail
    assert receipt["state"] == "passed" and receipt["exit_code"] == 0 and not log.exists()
    code, status, receipt, log, detail = _run_program(
        tmp_path / "home", location="independent_home_verifier", touch=True)
    assert code == 0 and status == {"state": "unavailable", "reason": "host_only"}, detail
    assert receipt["observed_from"] == "independent_home_verifier" and receipt["state"] == "passed"
    assert not log.exists()
    code, status, receipt, log, detail = _run_program(
        tmp_path / "drift", location="independent_host_verifier", touch=True, drift=True)
    assert code == 0 and status == {"state": "failed", "reason": "web_dependencies_changed"}, detail
    assert receipt["state"] == "passed" and receipt["exit_code"] == 0 and not log.exists()
    code, status, receipt, log, detail = _run_program(
        tmp_path / "budget", location="independent_host_verifier", touch=True, budget=True)
    assert code == 0 and status == {"state": "failed", "reason": "web_build_budget"}, detail
    assert receipt["state"] == "passed" and not log.exists()
    code, status, receipt, log, detail = _run_program(
        tmp_path / "built", location="independent_host_verifier", touch=True)
    assert code == 0 and status == {"state": "built", "reason": None}, detail
    assert receipt["state"] == "passed" and receipt["exit_code"] == 0
    assert receipt["trusted_tests"] == "pinned_baseline" and receipt["network"] == "none"
    built = tmp_path / "built" / "work" / "artifacts"
    assert (built / "web-preview" / "index.html").is_file()
    assert (built / "web-fixtures.json").read_bytes().startswith(b'{"session"')
    calls = json.loads(log.read_text())
    assert calls[0]["argv"][1:] == ["-b"]
    assert calls[1]["argv"][1:5] == ["build", "--configLoader", "runner", "--outDir"]
    assert calls[1]["env"]["HOME"].endswith("/scratch")
    assert calls[1]["env"]["CI"] == "1"
    assert calls[1]["env"]["GOMAXPROCS"] == "2"
    assert calls[1]["env"]["UV_THREADPOOL_SIZE"] == "2"
    assert calls[1]["env"]["NODE_OPTIONS"] == "--max-old-space-size=512"
    assert calls[1]["env"]["NO_COLOR"] == "1"
    assert (built / "web-build.log").stat().st_size <= 65536


def _product_web_workspace(tmp_path):
    """Verifier workspace: the product web/ tree plus a patch that adds one typed module."""
    work = tmp_path / "work"
    source = work / "source"
    listed = _git(ROOT, "ls-files", "-z", "web").stdout.decode().split("\0")
    _repo(source, {name: (ROOT / name).read_bytes() for name in listed if name})
    def mutate():
        (source / "web/src/preview-probe.ts").write_text("export const probe: number = 1;\n")
    (work / "candidate.patch").write_bytes(_patch(source, mutate))
    (work / "verification.ini").write_text("[pytest]\n")
    tests = work / "trusted-tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    for name in ("scratch", "artifacts", "profile"):
        (work / name).mkdir()
    return work


def test_real_web_build_never_writes_the_toolchain(tmp_path):
    """Real tsc and vite. The image toolchain is read-only, so the build must not write into it."""
    modules = ROOT / "web" / "node_modules"
    node = shutil.which("node")
    if node is None or not (modules / "vite" / "bin" / "vite.js").is_file():
        pytest.skip("Run npm ci --prefix web before the real web build test")
    toolchain = tmp_path / "toolchain"
    (toolchain / "node_modules").mkdir(parents=True)
    for entry in modules.iterdir():
        if not entry.name.startswith(".vite"):
            (toolchain / "node_modules" / entry.name).symlink_to(entry)
    for name in ("package.json", "package-lock.json"):
        (toolchain / name).write_bytes((ROOT / "web" / name).read_bytes())
    before = sorted(os.listdir(toolchain / "node_modules"))
    work = _product_web_workspace(tmp_path)
    program = verification_program()
    program = program.replace("'/usr/local/bin/node'", repr(node))
    program = program.replace("/opt/web-toolchain", str(toolchain))
    program = program.replace("/work", str(work))
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    completed = subprocess.run([sys.executable, "-I", "-c", program], cwd=work / "source", env=env,
                               capture_output=True, timeout=300)
    log = work / "artifacts" / "web-build.log"
    detail = completed.stderr.decode(errors="replace") + (log.read_text() if log.is_file() else "")
    status = json.loads((work / "artifacts" / "web-preview.json").read_text())
    assert completed.returncode == 0 and status == {"state": "built", "reason": None}, detail
    assert sorted(os.listdir(toolchain / "node_modules")) == before
    assert "\x1b" not in log.read_text()
    files, fixtures = read_web_bundle(work / "artifacts")
    assert "index.html" in files and fixtures == (ROOT / "web" / "preview" / "fixtures.json").read_bytes()


def test_real_runner_image_builds_the_product_web_preview(tmp_path, request):
    if not request.config.getoption("--containers"):
        pytest.skip("requires local packaged Runner image and Docker; foundation only")
    image = subprocess.check_output(["docker", "image", "inspect", f"agentic-home-runner:{VERSION}",
                                     "--format", "{{.Id}}"], text=True).strip()
    work = _product_web_workspace(tmp_path)
    # Plain files without .git: this harness runs as uid 1000, production chowns the workspace to UID 10000.
    shutil.rmtree(work / "source" / ".git")
    for path in (work, *work.rglob("*")):
        if path.is_dir():
            path.chmod(0o777)
    (work / "task.py").write_text(verification_program())
    name = "agentic-coding-" + uuid.uuid4().hex
    limits = Limits(disk_mib=512, memory_mib=768, pids=128, wall_seconds=300)
    try:
        subprocess.run(container_command(name, image, work, limits), check=True, capture_output=True, timeout=60)
        subprocess.run(["docker", "start", name], check=True, capture_output=True, timeout=60)
        code = subprocess.run(["docker", "wait", name], check=True, capture_output=True, text=True,
                              timeout=330).stdout.strip()
        log = work / "artifacts" / "web-build.log"
        detail = {name: (work / name).read_text(errors="replace")[-4000:]
                  for name in ("stderr.txt", "artifacts/verification.log", "artifacts/web-build.log")
                  if (work / name).is_file()}
        status = json.loads((work / "artifacts" / "web-preview.json").read_text())
        assert code == "0" and status == {"state": "built", "reason": None}, detail
        assert "\x1b" not in log.read_text()
        files, fixtures = read_web_bundle(work / "artifacts")
        assert "index.html" in files and fixtures == (ROOT / "web" / "preview" / "fixtures.json").read_bytes()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        # Files written by UID 10000 and root are returned to the test user so tmp cleanup works.
        subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "chown",
                        "--mount", f"type=bind,src={work},dst=/w", image, "-R", f"{os.getuid()}:{os.getgid()}", "/w"],
                       capture_output=True, timeout=60)

def _preview_tree(tmp_path, files, fixtures=b'{"session":{"mode":"host"}}\n'):
    root = tmp_path / "artifacts"
    bundle = root / "web-preview"
    for name, data in files.items():
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "web-fixtures.json").write_bytes(fixtures)
    return root


def test_web_bundle_reader_rejects_unsafe_files(tmp_path):
    ok = _preview_tree(tmp_path / "ok", {"index.html": b"<p>demo</p>", "assets/app.js": b"console.log(1)\n"})
    files, fixtures = read_web_bundle(ok)
    assert set(files) == {"index.html", "assets/app.js"} and b"session" in fixtures
    with pytest.raises(SandboxError, match="entry"):
        read_web_bundle(_preview_tree(tmp_path / "missing", {"assets/app.js": b"console.log(1)\n"}))
    linked = _preview_tree(tmp_path / "link", {"assets/app.js": b"console.log(1)\n"})
    (linked / "web-preview" / "index.html").symlink_to(linked / "web-preview" / "assets" / "app.js")
    with pytest.raises(SandboxError, match="safely opened"):
        read_web_bundle(linked)
    hard = _preview_tree(tmp_path / "hard", {"index.html": b"<p>demo</p>"})
    target = hard / "web-preview" / "assets"
    target.mkdir()
    os.link(hard / "web-preview" / "index.html", target / "copy.html")
    with pytest.raises(SandboxError, match="regular file"):
        read_web_bundle(hard)
    huge = b"x" * (3 * 1024 * 1024 + 1)
    with pytest.raises(SandboxError, match="quota"):
        read_web_bundle(_preview_tree(tmp_path / "size", {"index.html": huge}))
    with pytest.raises(SandboxError, match="path"):
        read_web_bundle(_preview_tree(tmp_path / "suffix", {"index.html": b"<p>demo</p>", "notes.md": b"x"}))
    for name in ("../index.html", "assets/../../index.html", "/etc/passwd", "a/b/c/d/e.html"):
        with pytest.raises(SandboxError, match="path"):
            bundle_relative(name)
    secret = b'<p>password = "not-a-real-secret"</p>'
    with pytest.raises(SandboxError, match="secret"):
        read_web_bundle(_preview_tree(tmp_path / "secret", {"index.html": secret}))
    benign = b"password:!0,range:!0,search:!0,tel:!0,text:!0,time:!0,url:!0,week:!0};function"
    read_web_bundle(_preview_tree(tmp_path / "dom", {"index.html": b"<p>ok</p>", "assets/app.js": benign}))
    glued = b"password:!0,range:!0}sk-abcdefghijklmnop"
    with pytest.raises(SandboxError, match="secret"):
        read_web_bundle(_preview_tree(tmp_path / "glued", {"index.html": glued}))
    skipped = tmp_path / "export"
    nested = skipped / "web-preview"
    nested.mkdir(parents=True)
    (nested / "index.html").write_text("<p>bundle</p>")
    (skipped / "web-fixtures.json").write_text("{}\n")
    (skipped / "verification.json").write_text("{}\n")
    names = {item["name"] for item in read_artifacts(skipped, Limits())}
    assert names == {"verification.json"}
    (skipped / "leak.html").write_text("<p>no</p>")
    exported = {item["name"]: item for item in read_artifacts(skipped, Limits())}
    assert set(exported) == {"verification.json", "skipped.json"}
    assert json.loads(exported["skipped.json"]["content"]) == {
        "skipped": [{"name": "leak.html", "reason": "unsupported_type"}]}
    (skipped / "leak.html").unlink()
    (skipped / "web-fixtures.json").unlink()
    (skipped / "web-fixtures.json").symlink_to(skipped / "verification.json")
    with pytest.raises(SandboxError, match="safely opened"):
        read_artifacts(skipped, Limits())
    (skipped / "web-fixtures.json").unlink()
    nested.rename(skipped / "kept")
    (skipped / "web-preview").symlink_to(skipped / "kept", target_is_directory=True)
    with pytest.raises(SandboxError, match="symlinks"):
        read_artifacts(skipped, Limits())


def test_product_bundle_and_manifest_rehash(tmp_path):
    static = ROOT / "controller" / "static"
    if not (static / "index.html").is_file():
        pytest.skip("Build the web UI before the bundle test")
    allowed = {".html", ".js", ".css", ".svg", ".json", ".txt", ".png", ".webp", ".woff2", ".ico"}
    artifacts = tmp_path / "artifacts"
    for path in static.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(static).as_posix()
        if relative.startswith("downloads/") or path.suffix not in allowed:
            continue
        target = artifacts / "web-preview" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    fixtures = (ROOT / "web" / "preview" / "fixtures.json").read_bytes()
    (artifacts / "web-fixtures.json").write_bytes(fixtures)
    files, raw = read_web_bundle(artifacts)
    assert "index.html" in files and raw == fixtures
    stored = store_web_bundle(tmp_path / "web-previews" / JOB, files, raw)
    assert stored["files"] == len(files) and stored["bytes"] == sum(len(item) for item in files.values())
    bundle = tmp_path / "web-previews" / JOB
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle / "bundle" / "index.html").stat().st_mode) == 0o600
    loaded, parsed, digest = load_stored_bundle(bundle)
    assert digest == stored["artifact_digest"] and parsed["session"]["mode"] == "host"
    assert loaded["index.html"] == files["index.html"]
    index = bundle / "bundle" / "index.html"
    index.chmod(0o600)
    index.write_bytes(index.read_bytes() + b" ")
    with pytest.raises(SandboxError, match="preview_artifact_digest_mismatch"):
        load_stored_bundle(bundle)
    service = Previews(SimpleNamespace(root=tmp_path, run=None), bundles=tmp_path / "web-previews")
    with pytest.raises(HostError, match="preview_artifact_digest_mismatch"):
        service.submit({"id": "interactive-demo", "mode": "interactive", "job_id": JOB})


def test_interactive_server_refuses_host_worker_and_expiry(tmp_path):
    files = {"index.html": b"<!doctype html><title>demo</title>", "assets/app.js": b"console.log(1)\n"}
    fixtures = b'{"session":{"mode":"host","csrf":"preview-csrf"}}\n'
    root = _preview_tree(tmp_path, files, fixtures)
    stored = store_web_bundle(tmp_path / "web-previews" / JOB, *read_web_bundle(root))
    service = Previews(SimpleNamespace(root=tmp_path, run=None), bundles=tmp_path / "web-previews")
    server = preview_server(service, ("127.0.0.2", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        view = service.submit({"id": "interactive-demo", "mode": "interactive", "job_id": JOB})
        assert view["mode"] == "interactive" and view["state"] == "ready"
        assert view["artifact_digest"] == stored["artifact_digest"]
        assert view["interactive_url"] == f"http://127.0.0.2:{server.server_port}/i/{service.records['interactive-demo']['capability']}/"
        assert "capability" not in view
        again = service.submit({"id": "interactive-demo", "mode": "interactive", "job_id": JOB})
        assert again["interactive_url"] == view["interactive_url"]
        cap = service.records["interactive-demo"]["capability"]

        def raw(path, host=None, method="GET", extra=None):
            connection = http.client.HTTPConnection("127.0.0.2", server.server_port, timeout=5)
            headers = {"Host": host or f"127.0.0.2:{server.server_port}"}
            if extra:
                headers.update(extra)
            body = b"{}" if method == "POST" else None
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            value = response.status, dict(response.getheaders()), response.read()
            connection.close()
            return value

        status, headers, body = raw(f"/i/{cap}/")
        assert status == 200 and b"demo" in body
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "frame-ancestors http://localhost:8099 http://127.0.0.1:8099" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
        assert "Set-Cookie" not in headers and "Access-Control-Allow-Origin" not in headers
        assert raw(f"/i/{cap}/api/session?unused=1")[0] == 200
        assert b"preview-csrf" in raw(f"/i/{cap}/api/session")[2]
        missing = raw(f"/i/{cap}/api/missing")
        assert missing[0] == 404 and "預覽沒有這項資料" in missing[2].decode()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            denied = raw(f"/i/{cap}/api/tasks", method=method)
            assert denied[0] == 403 and "預覽不會執行操作" in denied[2].decode(), method
        # An interactive capability never opens the screenshot route.
        assert service.read_png(cap, 1440) is None
        assert raw(f"/p/{cap}/1440.png")[0] == 404
        assert raw(f"/i/{cap}/", host="localhost:8099")[0] == 404
        assert raw(f"/i/{cap}/", extra={"Service-Worker": "script"})[0] == 404
        assert raw(f"/i/{'b' * 43}/")[0] == 404
        service.records["interactive-demo"]["expires_at"] = time.time() - 1
        assert raw(f"/i/{cap}/")[0] == 404
        service.records["interactive-demo"]["expires_at"] = time.time() + 60
        assert raw(f"/i/{cap}/assets/app.js")[0] == 200
        assert service.revoke("interactive-demo")["state"] == "expired"
        assert raw(f"/i/{cap}/")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_discard_job_content_removes_bundle_with_artifacts(tmp_path):
    for name in ("artifacts", "web-previews"):
        path = tmp_path / name / JOB
        path.mkdir(parents=True)
        (path / "kept.txt").write_text("x")
    discard_job_content(tmp_path, JOB)
    assert not (tmp_path / "artifacts" / JOB).exists()
    assert not (tmp_path / "web-previews" / JOB).exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "web-previews" / JOB
    link.symlink_to(outside, target_is_directory=True)
    discard_job_content(tmp_path, JOB)
    assert outside.is_dir() and not link.exists()
    with pytest.raises(ValueError):
        discard_job_content(tmp_path, "../escape")


def test_preview_fixtures_parse_and_cover_shell_gets():
    path = ROOT / "web" / "preview" / "fixtures.json"
    data = path.read_bytes()
    assert len(data) <= 1024 * 1024
    value = json.loads(data)
    assert all(FIXTURE_KEY.fullmatch(key) for key in value)
    assert REQUIRED_FIXTURES <= set(value)
    assert value["session"]["mode"] == "host" and value["session"]["csrf"] == "preview-csrf"
    assert value["deployment"] == {"mode": "host", "authenticated": True}
    assert value["status"]["ha"]["location_name"] == "示範家庭"
    names = {item["name"] for item in value["inventory"]["entities"]}
    assert {"客廳燈", "玄關感應器", "臥室冷氣"} <= names
    assert value["bootstrap"]["state"] == "completed"
    assert value["worker/jobs"]["jobs"][0]["state"] == "succeeded"
    from agentic_controller.models import DiscoverySnapshot, digest
    discovery = DiscoverySnapshot.model_validate(value["discovery"]).model_dump()
    assert discovery["content_digest"] == digest({k: v for k, v in discovery.items() if k != "content_digest"})
    assert "synthetic_fixture_no_network_or_physical_observation" in discovery["limitations"]
    assert {item["name"] for item in discovery["devices"]} == names
    for source, field in (("ha_discovery", "candidates"), ("ha_devices", "devices"), ("ha_integrations", "integrations")):
        observed = next(item for item in discovery["sources"] if item["id"] == source)
        assert observed["content_digest"] == digest(discovery[field])
    text = data.decode()
    assert "password" not in text.lower() and "BEGIN " not in text and "sk-" not in text


def test_web_toolchain_check_detects_drift(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_bytes(b'{"name":"demo"}\n')
    (web / "package-lock.json").write_bytes(b'{"lock":1}\n')
    sync_web_toolchain(tmp_path, check=False)
    sync_web_toolchain(tmp_path, check=True)
    (web / "package-lock.json").write_bytes(b'{"lock":2}\n')
    with pytest.raises(SystemExit, match="drift"):
        sync_web_toolchain(tmp_path, check=True)


def test_preview_model_keeps_screenshot_default():
    assert ModelPreview(mode="interactive").model_dump() == {"mode": "interactive", "artifact_ids": []}
    with pytest.raises(ValidationError):
        ModelPreview(mode="interactive", artifact_ids=[JOB])
    with pytest.raises(ValidationError):
        ModelPreview()
    screenshot = ModelPreview(artifact_ids=[JOB])
    assert screenshot.mode == "screenshot" and screenshot.artifact_ids == [JOB]


class _Jobs:
    def __init__(self, web_state):
        self.job = {"id": "a" * 32, "case_id": "b" * 32, "created_epoch": time.time(),
                    "verification": {"web_preview": {"state": web_state}}}
        self.expired = False
        self.artifact_reads = 0
        body = b"<h1>fixture</h1>"
        self.files = {"c" * 32: {"name": "index.html", "content_base64": base64.b64encode(body).decode(),
                                 "sha256": hashlib.sha256(body).hexdigest()}}

    def authority(self, request):
        return {"owner": None, "web_epoch": None, "expires": time.time() + 60}

    def get(self, identifier):
        return {**self.job, "content_expired": self.expired}

    def context_days(self):
        return 7

    async def artifact(self, identifier, artifact):
        self.artifact_reads += 1
        return self.files[artifact]


async def _broker_missing(method, path, body=None, *, store=None):
    if method == "GET":
        raise HTTPException(404)
    return body


async def test_interactive_preview_is_host_only_and_needs_a_build(store, monkeypatch):
    monkeypatch.setattr("agentic_controller.operations.broker_call", _broker_missing)
    workers = _Jobs("failed")
    request = SimpleNamespace(headers={"idempotency-key": "interactive-unavailable"}, state=SimpleNamespace())
    with pytest.raises(WorkerError) as error:
        await Operations(store, workers, host_mode=True).preview("a" * 32, ModelPreview(mode="interactive"), request)
    assert error.value.code == "web_preview_unavailable" and error.value.status == 409
    workers.job["verification"]["web_preview"]["state"] = "built"
    bridged = SimpleNamespace(headers={"idempotency-key": "interactive-bridge"},
                              state=SimpleNamespace(bridge_identity="bridge"))
    with pytest.raises(WorkerError) as error:
        await Operations(store, workers, host_mode=True).preview("a" * 32, ModelPreview(mode="interactive"), bridged)
    assert error.value.code == "interactive_preview_host_only"
    ingress = SimpleNamespace(headers={"idempotency-key": "interactive-ingress"}, state=SimpleNamespace())
    with pytest.raises(WorkerError) as error:
        await Operations(store, workers).preview("a" * 32, ModelPreview(mode="interactive"), ingress)
    assert error.value.code == "interactive_preview_host_only"


async def test_preview_idempotency_includes_mode(store, monkeypatch):
    calls = []

    async def broker(method, path, body=None, *, store=None):
        calls.append((method, path, body))
        if method == "GET":
            raise HTTPException(404)
        return {"id": body["id"], "state": "queued"}

    monkeypatch.setattr("agentic_controller.operations.broker_call", broker)
    workers = _Jobs("built")
    operations = Operations(store, workers, host_mode=True)
    request = SimpleNamespace(headers={"idempotency-key": "same-preview"}, state=SimpleNamespace())
    await operations.preview("a" * 32, ModelPreview(artifact_ids=["c" * 32]), request)
    with pytest.raises(Conflict):
        await operations.preview("a" * 32, ModelPreview(mode="interactive"), request)
    assert calls[-1][2]["artifact_digest"]
    host = SimpleNamespace(headers={"idempotency-key": "interactive-once"}, state=SimpleNamespace())

    async def accept(method, path, body=None, *, store=None):
        if method == "GET":
            raise HTTPException(404)
        assert set(body) == {"id", "mode", "job_id"}
        assert body["mode"] == "interactive" and body["job_id"] == "a" * 32
        return {"id": body["id"], "mode": "interactive", "state": "ready",
                "interactive_url": "http://127.0.0.2:8100/i/" + "d" * 43 + "/"}

    monkeypatch.setattr("agentic_controller.operations.broker_call", accept)
    before = workers.artifact_reads
    view = await operations.preview("a" * 32, ModelPreview(mode="interactive"), host)
    assert workers.artifact_reads == before
    assert view["interactive_url"].startswith("http://127.0.0.2:8100/i/")
    repeated = await operations.preview("a" * 32, ModelPreview(mode="interactive"), host)
    assert repeated == view


async def test_controller_csp_allows_only_the_preview_frame(store, ha):
    async with await client_for(store, ha) as client:
        response = await client.get("/api/session")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert policy.count("frame-src") == 1
    assert "frame-src http://127.0.0.2:8100" in policy
    assert "frame-ancestors 'self'" in policy


def test_bundle_digest_matches_preview_validate():
    files = {"index.html": b"<p>demo</p>", "assets/app.js": b"console.log(1)\n"}
    from agentic_host.coding_sandbox import bundle_digest
    from agentic_host.preview import canonical
    listing = [{"path": name, "sha256": hashlib.sha256(files[name]).hexdigest()} for name in sorted(files)]
    assert bundle_digest(files) == hashlib.sha256(canonical(listing)).hexdigest()
