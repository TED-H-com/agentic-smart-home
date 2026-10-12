"""Run the presentation App without inherited Supervisor credentials or root."""
import os

import uvicorn
from agentic_bridge.app import create_app, load_options

os.umask(0o077)
options = load_options()
# Read Supervisor-managed options before dropping root; keep them only in memory.
for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
    os.environ.pop(key, None)
if os.getuid() == 0:
    os.setgroups([])
    os.setgid(10003)
    os.setuid(10003)
uvicorn.run(create_app(options=options), host="0.0.0.0", port=8099,
            proxy_headers=False, access_log=False, log_level="warning")
