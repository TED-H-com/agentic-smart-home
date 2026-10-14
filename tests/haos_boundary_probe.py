"""Disposable HAOS App only: report actual namespace/cgroup permissions without secrets."""
import ctypes
import json
import os
import subprocess
import time
from pathlib import Path

root = Path('/data/boundary-probe')
root.mkdir(exist_ok=True)
result = {"observed_from": "haos_app", "supervisor_token_present": bool(os.environ.get("SUPERVISOR_TOKEN")),
          "docker_socket_present": Path('/var/run/docker.sock').exists(), "fuse_device": Path('/dev/fuse').exists(),
          "cgroup_namespace": os.stat("/proc/self/ns/cgroup").st_ino,
          "cgroup_membership": Path("/proc/self/cgroup").read_text().strip(),
          "cap_eff": next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))}
for name in ('memory.max', 'pids.max', 'cpu.max', 'cgroup.controllers'):
    path = Path('/sys/fs/cgroup') / name
    result[name] = path.read_text().strip() if path.exists() else None
libc = ctypes.CDLL(None, use_errno=True)
probe = subprocess.run(['python', '-c',
    'import ctypes,os; os._exit(0 if ctypes.CDLL(None).unshare(0x10000000|0x40000000)==0 else 1)'],
                       user=10000, group=10000, extra_groups=[], capture_output=True, timeout=60)
result['unprivileged_user_net_namespace'] = probe.returncode == 0
script = r'''
import ctypes,json,os,subprocess
from pathlib import Path
p=Path('/data/boundary-probe/cgroup');p.mkdir(exist_ok=True)
libc=ctypes.CDLL(None,use_errno=True)
result={'private_cgroup_root':Path('/proc/self/cgroup').read_text().strip()=='0::/'}
if not result['private_cgroup_root']:
 print(json.dumps(result));raise SystemExit
result['new_mount_namespace']=libc.unshare(0x00020000)==0
if not result['new_mount_namespace']:
 result['errno']=ctypes.get_errno();print(json.dumps(result));raise SystemExit
mounted=libc.mount(b'none',str(p).encode(),b'cgroup2',0,None)==0
result['new_cgroup_mount']=mounted;result['mount_errno']=ctypes.get_errno() if not mounted else None
try:
 if mounted:
  broker=p/'broker';broker.mkdir()
  for pid in (p/'cgroup.procs').read_text().splitlines():(broker/'cgroup.procs').write_text(pid)
  (p/'cgroup.subtree_control').write_text('+memory +pids +cpu')
  worker=p/'worker';worker.mkdir()
  (worker/'memory.max').write_text(str(64*1024*1024));(worker/'memory.swap.max').write_text('0')
  (worker/'pids.max').write_text('16');(worker/'cpu.max').write_text('100000 100000')
  def enter():(worker/'cgroup.procs').write_text(str(os.getpid()))
  child=subprocess.run(['python','-c','x=bytearray(160*1024*1024)'],preexec_fn=enter,timeout=60)
  result.update(delegated_limits=True,memory_oom_kill=child.returncode==-9,
    memory_events=(worker/'memory.events').read_text().strip())
  worker.rmdir()
except OSError as e:result.update(delegated_limits=False,delegation_errno=e.errno)
finally:
 if mounted:libc.umount2(str(p).encode(),0)
print(json.dumps(result))
'''
probe = subprocess.run(['python', '-c', script], capture_output=True, text=True, timeout=180)
try:
    result['delegation'] = json.loads(probe.stdout)
except ValueError:
    result['delegation'] = {'probe_exit': probe.returncode}
print('AGENTIC_BOUNDARY=' + json.dumps(result), flush=True)
time.sleep(300)
