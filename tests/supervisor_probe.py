"""Run in the official Supervisor image without starting Supervisor or mounting Docker."""
import json
import tarfile
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import yaml
from securetar import atomic_contents_add
from supervisor.apps.app import App
from supervisor.apps.validate import SCHEMA_APP_CONFIG

root = Path("/probe-data")
root.mkdir()
for name in ("controller", "runner"):
    config = yaml.safe_load(Path(f"/source/{name}/config.yaml").read_text())
    SCHEMA_APP_CONFIG(config)
config = yaml.safe_load(Path("/source/runner/config.yaml").read_text())
for name in ("profiles/codex/auth.json", "profiles/claude/nested/credential.json",
             "private/server.key", "work/task/nested/prompt.json", "runner.sqlite3"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture only")
filter_file = partial(App._is_excluded_by_filter, SimpleNamespace(backup_exclude=config["backup_exclude"]), root, "data")
archive = Path("/probe-backup.tar")
with tarfile.open(archive, "w") as tar:
    atomic_contents_add(tar, root, file_filter=filter_file, arcname="data")
with tarfile.open(archive) as tar:
    members = tar.getnames()
assert "data/runner.sqlite3" in members, members
assert not any("auth.json" in x or "credential.json" in x or "server.key" in x or "prompt.json" in x for x in members), members
print(json.dumps({"supervisor": "2026.09.3", "app_config_schema": "passed",
                  "actual_backup_filter": "passed", "archive_members": members,
                  "limitations": "Supervisor daemon, role authorization and full restore not exercised"}))
