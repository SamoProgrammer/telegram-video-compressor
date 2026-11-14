"""Low-cardinality stage timings for Actions logs; never logs tokens or media."""

import json
import time
import uuid
from contextlib import contextmanager


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def stage(name: str, *, bytes: int = 0, started: float | None = None, **fields):
    fields.setdefault("job_id", new_job_id())
    if not fields["job_id"]:
        fields["job_id"] = new_job_id()
    started = time.monotonic() if started is None else started
    status = "ok"
    try:
        yield fields
    except BaseException as exc:
        status = "error"
        fields["error_type"] = type(exc).__name__
        fields["bytes"] = 0
        raise
    finally:
        elapsed = max(0.000_001, time.monotonic() - started)
        event = {"event": "pipeline_stage", "stage": name, "status": status}
        if fields.get("job_id"):
            event["job"] = fields.pop("job_id")
        bytes = fields.pop("bytes", bytes)
        event.update(fields)
        event.update(
            {
                "bytes": bytes,
                "milliseconds": round(elapsed * 1000, 1),
                "mib_s": round(bytes / 1048576 / elapsed, 3) if status == "ok" else 0,
            }
        )
        print(json.dumps(event, separators=(",", ":")), flush=True)
