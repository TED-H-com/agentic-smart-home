"""Fixed appliance power policy; never restart logind or its graphical sessions."""
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .manager import run

LID_PROPERTIES = ("HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked")
SLEEP_TARGETS = ("sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target",
                 "suspend-then-hibernate.target")
POWER_KEYS = {"sleep-inactive-ac-type": "'nothing'", "sleep-inactive-ac-timeout": "0",
              "sleep-inactive-battery-type": "'nothing'", "sleep-inactive-battery-timeout": "0",
              "lid-close-ac-action": "'nothing'", "lid-close-battery-action": "'nothing'"}


def write_policy(path, content, mode=0o644):
    """Replace only changed content, preserving a complete file across interruption."""
    path = Path(path)
    if path.exists() and path.read_text() == content and path.stat().st_mode & 0o777 == mode:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".agentic-")
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def verify_power(runner=run):
    def check(name):
        if name in LID_PROPERTIES:
            result = runner("busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
                            "org.freedesktop.login1.Manager", name, check=False, timeout=3)
            return result.returncode == 0 and result.stdout.strip() == 's "ignore"'
        result = runner("systemctl", "is-enabled", name, check=False, timeout=3)
        # is-enabled deliberately returns nonzero for a masked unit.
        return result.stdout.strip() == "masked"
    names = LID_PROPERTIES + SLEEP_TARGETS
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        checks = dict(zip(names, pool.map(check, names), strict=True))
    return {"verified": all(checks.values()), "checks": checks}


def reconcile_power(profile, root=Path("/"), runner=run, runtime=True):
    if not profile.get("appliance_lid"):
        return {"applicable": False, "changed": False, "verified": None, "checks": {}}
    root = Path(root)
    changed = write_policy(root / "etc/systemd/logind.conf.d/60-agentic-home.conf",
                           "[Login]\n" + "".join(name + "=ignore\n" for name in LID_PROPERTIES))
    desktop_changed = False
    if profile.get("desktop"):
        desktop_changed |= write_policy(root / "etc/dconf/db/local.d/60-agentic-home",
                                        "[org/gnome/settings-daemon/plugins/power]\n" +
                                        "".join(key + "=" + value + "\n" for key, value in POWER_KEYS.items()))
        desktop_changed |= write_policy(root / "etc/dconf/db/local.d/locks/60-agentic-home",
                                        "".join("/org/gnome/settings-daemon/plugins/power/" + key + "\n"
                                                for key in POWER_KEYS))
        path = root / "etc/dconf/profile/user"
        previous = path.read_text() if path.exists() else "user-db:user\n"
        if "system-db:local" not in previous.splitlines():
            desktop_changed |= write_policy(path, previous.rstrip() + "\nsystem-db:local\n")
    changed |= desktop_changed
    if not runtime:
        # Offline staging may mask units, but must never talk to the build host's systemd.
        for target in SLEEP_TARGETS:
            path = root / "etc/systemd/system" / target
            if path.is_symlink() and os.readlink(path) == "/dev/null":
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            path.symlink_to("/dev/null")
            changed = True
        return {"applicable": True, "changed": changed, "verified": None, "checks": {}}
    before = verify_power(runner)
    missing = [target for target in SLEEP_TARGETS if not before["checks"][target]]
    if missing:
        runner("systemctl", "mask", *missing, timeout=15)
    # daemon-reload does not reload logind.conf. HUP leaves the login session intact.
    if changed or not all(before["checks"][name] for name in LID_PROPERTIES):
        runner("systemctl", "kill", "-s", "HUP", "systemd-logind", timeout=10)
    if desktop_changed:
        runner("dconf", "update", timeout=15)
    report = verify_power(runner)
    for _ in range(4):
        if report["verified"]:
            break
        time.sleep(0.1)
        report = verify_power(runner)
    return {"applicable": True, "changed": changed or bool(missing), **report}
