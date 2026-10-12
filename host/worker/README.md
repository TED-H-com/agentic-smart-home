# Isolated coding workers

The enabled Host path is Controller → paired TLS Runner → fixed Unix Host broker → private Codex workspace. The retained home_operator path keeps its original restrictions. See the current [worker guide](../../docs/WORKERS.md), [delivery and network guide](../../docs/UPDATES.md), and [acceptance ledger](../../docs/v3/PROGRESS.md).

The Host broker owns the ext4 disk quota, immutable image, source bundle commit, private clone, deadline and cgroup boundary. A worker has no Docker socket, Live credentials, or provider bearer. Provider and allowlisted public research traffic uses a bounded Unix relay outside the worker's network namespace. Home capabilities remain disabled until the actual HAOS App boundary is proved; an outer container is never inner execution evidence.

The actual pinned CLI tool protocol is tested against an explicit local model transport fixture. The installer VM additionally exercises real root-only disk quota, Codex repair, independent no-network verifier and stopped checkpoint. These are engineering tests, not a real cloud account or household pilot.

Tests: `test_coding_sandbox.py`, `test_worker_execution.py`, `test_runner_lifecycle.py`, `test_worker_controller.py`, `test_preview.py`. `installed_coding_probe.py` is only for the disposable Linux installer VM. All test scratch lives on disk and owned containers are removed.
