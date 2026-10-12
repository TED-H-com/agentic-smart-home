"""Actual prepare() DAC regression; kernel quota is covered by the installed VM."""
import json
import subprocess
import uuid
from pathlib import Path

import pytest


def test_actual_prepare_umask077_trusted_tests_are_readable_but_not_writable(request):
    if not request.config.getoption('--containers'):
        pytest.skip('Actual pinned worker requires --containers')
    root = Path(__file__).parents[1]
    name = 'agentic-prepare-dac-' + uuid.uuid4().hex[:12]
    script = r'''
import json,os,subprocess
from pathlib import Path
from types import SimpleNamespace
from agentic_host.coding_program import VERIFY_PROGRAM
from agentic_host.coding_sandbox import CodingSandbox,Limits,command
os.umask(0o077)
repo=Path('/fixture');repo.mkdir()
(repo/'calculator.py').write_text('def add(a,b): return a-b\n')
(repo/'tests').mkdir()
(repo/'tests/conftest.py').write_text('import sys\nsys.path.insert(0,"/work/source")\n')
(repo/'tests/test_engine.py').write_text('from calculator import add\ndef test_add(): assert add(4,2)==6\n')
(repo/'tests/test_api.py').write_text('from calculator import add\ndef test_other(): assert add(4,-2)==2\n')
command('git','init','-q',str(repo))
command('git','-C',str(repo),'add','.')
command('git','-C',str(repo),'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','baseline')
commit=command('git','-C',str(repo),'rev-parse','HEAD').stdout.strip()
state=Path('/state');state.mkdir()
patch=b'diff --git a/calculator.py b/calculator.py\n--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1 @@\n-def add(a,b): return a-b\n+def add(a,b): return a+b\n'
def execute(*args,**kwargs):
 if args[0] in {'mount','mkfs.ext4'}:return SimpleNamespace(returncode=0,stdout='')
 return command(*args,**kwargs)
sandbox=CodingSandbox(state,'sha256:'+'1'*64,{'agentic-home':repo},limits=Limits(disk_mib=64),
                      inputs={'candidate.patch':patch},execute=execute)
# Mount/quota plumbing alone is substituted; actual prepare Git, UID, mkdir,
# chmod and verifier processes run in this disposable nonprivileged container.
original_mount,original_statvfs=os.path.ismount,os.statvfs
os.path.ismount=lambda _:True
os.statvfs=lambda _:SimpleNamespace(f_blocks=16384,f_frsize=4096)
try:
 mount=sandbox.prepare({'job_id':'permissions','repo_id':'agentic-home','base_commit':commit},VERIFY_PROGRAM)
finally:
 os.path.ismount,os.statvfs=original_mount,original_statvfs
# Match the production bind mount destination, without mounting anything.
mount.rename('/work')
tests=Path('/work/trusted-tests')
assert tests.stat().st_uid==0 and tests.stat().st_mode&0o777==0o555
assert all(p.stat().st_uid==0 and p.stat().st_mode&0o777==0o444 for p in tests.iterdir())
permission=r"""
import json
from pathlib import Path
p=Path('/work/trusted-tests')
assert len(list(p.iterdir()))==3
assert (p/'test_engine.py').read_text()
for target in (p/'injected.py',p/'test_engine.py'):
 try:target.write_text('untrusted')
 except PermissionError:pass
 else:raise AssertionError('trusted test was writable')
print(json.dumps({'uid10000_can_read':True,'directory_and_files_not_writable':True}))
"""
env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/work/scratch','TMPDIR':'/work/scratch'}
checked=subprocess.run(['python','-I','-c',permission],user=10000,group=10000,extra_groups=[],env=env,
                       capture_output=True,text=True,timeout=10)
assert checked.returncode==0,checked.stderr
result=subprocess.run(['python','-I','/work/task.py'],cwd='/work/source',user=10000,group=10000,
                      extra_groups=[],env=env,capture_output=True,text=True,timeout=30)
assert result.returncode==0,(result.returncode,result.stderr,Path('/work/artifacts/verification.log').read_text())
report=json.loads(Path('/work/artifacts/verification.json').read_text())
assert report['completion_verified'] is True and report['tests_executed']==2 and report['state']=='passed'
print(json.dumps({'observed_from':'actual_pinned_runner_container','prepare':'actual_product_method','umask':'0077',
 'trusted_directory_mode':'0555','trusted_owner_uid':0,'kernel_quota':'separate_installed_vm_evidence',
 'uid10000_can_read':True,'directory_and_files_not_writable':True,'verifier_tests_executed':2}))
'''
    try:
        result = subprocess.run(['docker', 'run', '--rm', '--name', name, '--network', 'none', '--memory', '768m',
            '--memory-swap', '768m', '--pids-limit', '128', '--cpus', '1', '--mount',
            f'type=bind,src={root / "host"},dst=/host,readonly', '--env', 'PYTHONPATH=/host', '--entrypoint', 'python',
            'agentic-home-runner:0.6.1', '-c', script], capture_output=True, text=True, timeout=50)
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout)
        assert evidence['verifier_tests_executed'] == 2
        (root / 'evidence/coding-prepare-permissions.json').write_text(json.dumps(evidence, indent=2) + '\n')
    finally:
        subprocess.run(['docker', 'rm', '-fv', name], capture_output=True, check=False)
