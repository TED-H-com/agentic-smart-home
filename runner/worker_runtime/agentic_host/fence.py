"""Fixed broker-owned container termination, including Docker uncertainty."""
import json
import re

from .manager import HostError


def fence(run, name, label):
    if not re.fullmatch(r"agentic-(?:preview|update-probe)-[a-f0-9]{32}", name):
        raise HostError("container_identity_invalid")
    def inspect():
        result = run("docker", "inspect", name, timeout=10, check=False)
        if result.returncode:
            if run("docker", "ps", "-aq", "--filter", "name=^/" + name + "$", timeout=10).stdout.strip():
                raise HostError("container_stop_unconfirmed")
            return None
        values = json.loads(result.stdout)
        if len(values) != 1 or values[0].get("Config", {}).get("Labels", {}).get(label) != name:
            raise HostError("container_ownership_unconfirmed")
        return values[0]
    value = inspect()
    if value is None:
        return
    if value["State"].get("Running") or value["State"].get("Paused"):
        run("docker", "kill", value["Id"], timeout=15, check=False)
        value = inspect()
        if value and (value["State"].get("Running") or value["State"].get("Paused")):
            raise HostError("container_stop_unconfirmed")
    if value:
        run("docker", "rm", value["Id"], timeout=15)
