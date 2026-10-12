#!/usr/bin/env python3
"""Generate schemas from the runtime models; --check reports contract drift."""
import argparse
import json
import tempfile
from pathlib import Path

from agentic_controller.app import create_app
from agentic_controller.models import (
    AutomationIntent,
    BootstrapStart,
    BootstrapStatus,
    Policy,
    provider_schema,
)
from agentic_controller.operations import NetworkPreferences, UpdateStage
from agentic_controller.store import Store
from agentic_controller.work import (
    EvidenceQuery,
    HermesAuth,
    WorkerPreview,
    WorkerQuery,
    WorkerStart,
    WorkerSubmit,
)
from agentic_runner.app import Job, Pair
from agentic_runner.bootstrap import BootstrapJob, BootstrapResult

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache, prefix="contracts-") as temporary:
        store = Store(Path(temporary) / "schema.sqlite3")
        try:
            app = create_app(store=store)
            schemas = {"automation-intent": AutomationIntent.model_json_schema(),
                       "policy": Policy.model_json_schema(), "agent-decision": provider_schema(),
                       "runner-job": Job.model_json_schema(), "runner-pair": Pair.model_json_schema(),
                       "worker-submit-v1": WorkerSubmit.model_json_schema(),
                       "worker-query-v1": WorkerQuery.model_json_schema(),
                       "worker-start-v1": WorkerStart.model_json_schema(),
                       "worker-preview-v1": WorkerPreview.model_json_schema(),
                       "network-preferences-v1": NetworkPreferences.model_json_schema(),
                       "controller-update-v1": UpdateStage.model_json_schema(),
                       "case-evidence-query-v1": EvidenceQuery.model_json_schema(),
                       "hermes-auth": HermesAuth.model_json_schema(),
                       "bootstrap-start-v1": BootstrapStart.model_json_schema(),
                       "bootstrap-status-v1": BootstrapStatus.model_json_schema(),
                       "runner-bootstrap-job-v1": BootstrapJob.model_json_schema(),
                       "runner-bootstrap-result-v1": BootstrapResult.model_json_schema(),
                       "controller.openapi": app.openapi(),
                       "host-controller.openapi": create_app(store=store, host_mode=True).openapi()}
        finally:
            store.close()
    destination = ROOT / "contracts"
    destination.mkdir(exist_ok=True)
    for name, schema in schemas.items():
        path = destination / (name + (".json" if name.endswith("openapi") else ".schema.json"))
        content = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Contract drift: {path.relative_to(ROOT)}")
        else:
            path.write_text(content)
    print(f"{'Checked' if args.check else 'Generated'} {len(schemas)} contracts")


if __name__ == "__main__":
    main()
