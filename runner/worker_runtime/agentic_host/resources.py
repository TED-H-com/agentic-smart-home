"""Reject launches when kernel limits or remaining host headroom are absent."""
import json
from pathlib import Path

from .coding_sandbox import SandboxError, validate_docker_security
from .manager import HostError


def ready(run, minimum_mib):
    try:
        validate_docker_security(json.loads(run("docker", "info", "--format", "{{json .}}", timeout=10).stdout))
        entries = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        if int(entries["MemAvailable"].split()[0]) < minimum_mib * 1024:
            raise HostError("host_memory_reserve_required")
    except HostError:
        raise
    except (OSError, ValueError, KeyError, SandboxError):
        raise HostError("host_resource_limits_unavailable") from None
