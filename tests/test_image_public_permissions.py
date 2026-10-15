"""Read shipped public files as their unprivileged consumers, without service startup."""
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

CASES = {
    "controller": {"uid": 1000, "files": ["/app/start.py", "/app/static/index.html",
        "/app/static/downloads/agentic-runner.zip", "/app/source/agentic-home.bundle"],
        "assets": "/app/static/assets", "imports": ["agentic_controller.models"]},
    "runner": {"uid": 10000, "files": ["/app/agentic_runner/app.py", "/app/agentic_runner/codex-models.json",
        "/app/agentic_host/coding_sandbox.py", "/app/agentic_host/coding_service.py", "/opt/agents/package.json"],
        "imports": ["agentic_host.coding_sandbox", "agentic_runner.home_launcher"],
        "executables": ["/usr/local/bin/codex", "/usr/local/bin/claude", "/usr/local/bin/grok", "/usr/local/bin/agy"],
        "private": {"/opt/agentic-worker/source": 0o700, "/opt/agentic-worker/staging/relay.sock": 0o600}},
    "hermes": {"uid": 10003, "files": ["/app/compatibility_probe.py", "/app/agentic_hermes/app.py",
        "/opt/hermes/.agentic-commit", "/opt/hermes/run_agent.py"], "imports": ["agentic_hermes.models"]},
    "bridge": {"uid": 10003, "files": ["/app/start.py", "/app/static/index.html"],
        "assets": "/app/static/assets", "imports": ["agentic_bridge.app"]},
    "linejs": {"uid": 1000, "files": ["/opt/start.mjs", "/opt/linejs/package.json", "/opt/linejs/dist/main.js"]},
    "preview": {"uid": 10004, "files": ["/render.py", "/requirements.lock"], "imports": ["playwright.sync_api"]},
}

PYTHON_PROBE = r'''
import importlib,json,os,stat,sys
from pathlib import Path
spec=json.loads(sys.argv[1])
checks=[]
def check(label,action):
    try:
        action()
        checks.append({'check':label,'status':'passed'})
    except Exception as error:
        checks.append({'check':label,'status':'failed','error_type':type(error).__name__ if type(error).__name__ in
            {'PermissionError','FileNotFoundError','ImportError','ModuleNotFoundError','AssertionError','OSError'} else 'OtherError'})
def require(condition):
    assert condition
def public_file(path):
    value=Path(path);info=value.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_gid==0 and info.st_mode&0o022==0)
    with value.open('rb') as stream: require(bool(stream.read(1)))
def assets():
    root=Path(spec['assets'])
    for suffix in ('*.js','*.css'):
        files=list(root.glob(suffix));require(bool(files))
        for path in files: public_file(path)
def private(path,mode):
    info=Path(path).stat()
    require(info.st_uid==0 and info.st_gid==0 and info.st_mode&0o777==mode)
    require(not os.access(path,os.R_OK) and not os.access(path,os.W_OK))
    if stat.S_ISDIR(info.st_mode): require(not os.access(path,os.X_OK))
def boundary():
    require(os.getuid()==spec['uid'] and os.getgid()==spec['uid'])
    process=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    require(int(process['CapEff'].strip(),16)==0 and process['NoNewPrivs'].strip()=='1')
check('unprivileged_without_capabilities',boundary)
for index,path in enumerate(spec['files']): check('public_file_'+str(index),lambda path=path:public_file(path))
if 'assets' in spec: check('javascript_and_stylesheet_assets',assets)
for index,module in enumerate(spec.get('imports',[])):
    check('module_import_'+str(index),lambda module=module:importlib.import_module(module))
for index,path in enumerate(spec.get('executables',[])):
    check('public_executable_'+str(index),lambda path=path:require(os.access(path,os.R_OK|os.X_OK)))
for index,(path,mode) in enumerate(spec.get('private',{}).items()):
    check('private_boundary_'+str(index),lambda path=path,mode=mode:private(path,mode))
passed=all(item['status']=='passed' for item in checks)
print(json.dumps({'status':'passed' if passed else 'failed','uid':os.getuid(),'checks':checks}))
raise SystemExit(0 if passed else 1)
'''

NODE_PROBE = r'''
const fs=require('node:fs');
const spec=JSON.parse(process.argv[1]);const checks=[];
function check(label,fn){try{fn();checks.push({check:label,status:'passed'});}catch(error){
 checks.push({check:label,status:'failed',error_type:['EACCES','ENOENT','EPERM'].includes(error.code)?error.code:'OtherError'});}}
function requireTrue(value){if(!value)throw Error('check_failed');}
check('unprivileged_without_capabilities',()=>{
 requireTrue(process.getuid()===spec.uid&&process.getgid()===spec.uid);
 const status=fs.readFileSync('/proc/self/status','utf8');
 requireTrue(/^CapEff:\s+0+$/m.test(status)&&/^NoNewPrivs:\s+1$/m.test(status));
});
spec.files.forEach((path,index)=>check('public_file_'+index,()=>{
 const info=fs.statSync(path);requireTrue(info.isFile()&&info.uid===0&&info.gid===0&&(info.mode&0o022)===0);
 requireTrue(fs.readFileSync(path).length>0);
}));
check('package_metadata',()=>requireTrue(typeof JSON.parse(fs.readFileSync('/opt/linejs/package.json','utf8')).name==='string'));
const passed=checks.every(item=>item.status==='passed');
console.log(JSON.stringify({status:passed?'passed':'failed',uid:process.getuid(),checks}));process.exit(passed?0:1);
'''


def cleanup(name):
    """Cleanup uncertainty must not silently leave an owned container behind."""
    result = {"remove": "unknown", "absent": False}
    try:
        removed = subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=30)
        result["remove"] = "passed" if removed.returncode == 0 else "not_removed"
    except (OSError, subprocess.TimeoutExpired) as error:
        result["remove"] = type(error).__name__
    try:
        observed = subprocess.run(["docker", "ps", "-a", "--filter", "name=^/" + name + "$",
                                   "--format", "{{.ID}}"], capture_output=True, timeout=10)
        result["absent"] = observed.returncode == 0 and not observed.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


@pytest.mark.parametrize("app", CASES)
def test_packaged_public_files_are_readable_as_runtime_uid(request, app):
    if not request.config.getoption("--containers"):
        pytest.skip("Use --containers after building the six product images")
    spec = CASES[app]
    image = os.environ.get("AGENTIC_IMAGE_PERMISSION_TEST_" + app.upper(), f"agentic-home-{app}:0.9.0")
    name = "agentic-lifecycle-test-permissions-" + uuid.uuid4().hex[:12]
    report = {"app": app, "reader_uid": spec["uid"], "status": "failed",
              "scope": "Actual packaged public-file DAC and imports; no service startup or real-account acceptance"}
    try:
        identity = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                  capture_output=True, text=True, check=True, timeout=10)
        report["image_id"] = identity.stdout.strip()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", report["image_id"])
        entrypoint, program = ("node", NODE_PROBE) if app == "linejs" else ("python", PYTHON_PROBE)
        result = subprocess.run(["docker", "run", "--pull=never", "--rm", "--name", name, "--read-only", "--network", "none",
            "--user", f"{spec['uid']}:{spec['uid']}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "256m", "--pids-limit", "32", "--cpus", "1", "--entrypoint", entrypoint,
            report["image_id"], "-e" if app == "linejs" else "-c", program, json.dumps(spec)],
            capture_output=True, text=True, timeout=45)
        observed = json.loads(result.stdout)
        assert observed["uid"] == spec["uid"]
        report.update(observed)
        assert result.returncode == 0 and observed["status"] == "passed", observed
    except Exception as error:
        report["error_type"] = type(error).__name__ if type(error).__name__ in {
            "AssertionError", "TimeoutExpired", "CalledProcessError", "JSONDecodeError", "OSError"} else "OtherError"
        raise
    finally:
        primary = sys.exc_info()[1]
        report["cleanup"] = cleanup(name)
        if not report["cleanup"]["absent"]:
            report["status"] = "failed"
        path = Path(__file__).parents[1] / "evidence" / ("image-public-permissions-" + app + ".json")
        try:
            path.write_text(json.dumps(report, indent=2) + "\n")
        except OSError:
            if primary is None:
                raise
            primary.add_note("Image permission receipt could not be persisted")
        if not report["cleanup"]["absent"]:
            if primary is not None:
                primary.add_note("Owned image permission check container absence unconfirmed")
            else:
                raise AssertionError("Owned image permission check container absence unconfirmed")
