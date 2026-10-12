"""Bounded content retention with durable, non-reusable job identities."""
import time

MAX_IDENTITIES = 100000
DAY = 86400
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}


def retained(job, days, now=None):
    now = time.time() if now is None else now
    value = dict(job)
    created = value.get("created", now)
    previous = value.get("retained_until", created + days * DAY)
    value["retained_until"] = min(previous, created + days * DAY)
    if value["retained_until"] > now and not value.get("content_expired"):
        return value
    # A tombstone preserves deduplication and fencing, never model content.
    keep = {"id", "digest", "created", "updated", "provider", "kind", "retained_until",
            "execution_started", "stop_confirmed", "lease_epoch", "runtime_location", "case_id", "worker_id"}
    expired = {key: item for key, item in value.items() if key in keep}
    state = value.get("state")
    expired.update(state="expired" if state in TERMINAL else "stop_unconfirmed", content_expired=True,
                   error="context_expired", stop_confirmed=state in TERMINAL and value.get("stop_confirmed", True))
    return expired
