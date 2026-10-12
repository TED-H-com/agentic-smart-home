# Coding worker containment foundation

Plan v3 workline B. This directory does **not** ship an enabled cloud coding worker
or a Home Worker App. `CodingSandbox.capabilities()` reports both coding workers
blocked; the legacy `home_operator` Runner is unchanged.

`host/agentic_host/coding_sandbox.py` is a trusted-host-broker prototype for offline
Python research/reproducer jobs. No owner HTTP route accepts its `program` argument.
It must not be wired into Hermes worker submission as a usable E1 provider yet.

## Fixed boundary

- Constructor configuration supplies a pinned local image digest and an allowlist of
  local repositories. Jobs specify an existing commit, generated job ID, lease epoch
  and bounded program. They cannot specify Docker arguments, endpoints, UID, mounts,
  credentials or environment variables.
- A trusted root broker reserves a fixed-size disk file, formats and mounts ext4,
  creates an independent Git clone (`--no-local --no-hardlinks`) and removes its
  origin. At most four disk images are retained, and at least 4 GiB of host disk must remain
  after allocation. No live checkout, shared `.git`, profile or provider credentials is mounted.
  Every writable worker directory, including `/tmp`, lies on this bounded disk.
  Capacity is enforced by the filesystem, not a periodic size poll or per-file limit.
- Candidate code runs as UID 10000 without capabilities. Only a fixed `timeout`
  watchdog retains the minimal capabilities needed to drop UID/GID and terminate its
  children. Code cannot signal the root watchdog. The deadline survives broker loss;
  exiting container PID 1 ends all descendants, including detached processes.
- Docker enforces network `none`, read-only system files, seccomp, AppArmor, private
  namespaces, CPU quota, memory without extra swap, PID limits and no new privileges.
  The launcher refuses hosts without these mechanisms. There is no Docker socket,
  Supervisor token, Controller token, owner cookie or provider account inside.
- Cancellation verifies the exact owned container and its stopped state before
  reporting `cancelled`. A lost daemon response remains `stop_unconfirmed` and fences
  new jobs. Restart reconciliation stops and marks interrupted work; it never replays
  candidate code. Exact submission retries preserve the existing handle.
- Artifact export happens after checking that the actual writer stopped. It rejects
  symlinks, hardlinks, special files, unknown types, deep or excessive directory trees,
  binary contents and oversized totals. Only bounded UTF-8 text is exported with a
  SHA-256 digest and an untrusted-content marker. Pattern scanning rejects likely
  credentials but does not guarantee that no private data remains; export is **local**,
  and publication needs separate authorized review.

The Python API is `launch(job_id, lease_epoch, repo_id, base_commit, program)`,
`status(job_id)`, `cancel(job_id)`, `recover()`, `artifacts(job_id)` and
`dispose(job_id)`. `dispose` removes only the stopped container and mount; it retains
its disk image and audit record for a future broker-owned retention/checkpoint policy.
There is no automatic workspace handoff or claim that changing a lease alone stops a
writer.

## What was actually checked

`pytest-disktmp uv run pytest -q tests/test_coding_sandbox.py --containers` runs
unit cases and disposable real Docker negatives using the existing pinned Runner
image. The container tests use **disk-backed** temporary directories and always
remove their containers. They test network and management denial, non-root execution,
read-only system files, actual PID exhaustion, memory allocation killed by the kernel,
cancellation and watchdog process-tree termination.

These tests do **not** mount an ext4 disk on the developer's host or claim the
root-only clone/quota lifecycle is proven. Its host integration remains blocked and
must be exercised in a disposable VM before enabling the launcher. The real Docker
negative harness deliberately uses a temporary bind directory, so those checks alone
do not prove a disk quota. Tests also do not use a cloud account or real HAOS.

## Next enabled slices

1. In a disposable installed Linux VM, verify the fixed-size ext4 lifecycle, ENOSPC,
   crash/unmount recovery, independent checkout, retention and filesystem fencing.
2. Add a network-enforced provider/public-source proxy and a private per-role account
   adapter. `HTTP_PROXY` alone is insufficient. Do not silently enable unrestricted
   Docker bridge networking or copy a developer's existing login profile.
3. Exercise one actual authorized coding CLI and the async Controller/Runner adapter;
   only then advertise the supported research/development modes.
4. Implement and test a separate low-privilege **HAOS App** boundary. Home Assistant's
   App network normally permits communication with other Apps and Core; setting
   `host_network: false` or omitting `homeassistant_api` does not isolate that network.
   Some Supervisor APIs are available without `hassio_api`. No host container result
   can be counted as Home Worker execution. Keep Home coding blocked until direct
   LAN/management access, credentials, limits, cancellation and real App observations
   pass their own negative tests.

Primary references checked 2026-10-11:

- [Docker network none](https://docs.docker.com/engine/network/drivers/none/)
- [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
- [Docker container create options](https://docs.docker.com/reference/cli/docker/container/create/)
- [Home Assistant App configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [Home Assistant App communication](https://developers.home-assistant.io/docs/apps/communication/)
