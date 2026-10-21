"""Conservative repository job-minute accounting and automatic budget pause."""
import math
import os
from datetime import datetime, timezone
from urllib.parse import urlencode
from .core import ArchiveError, json_request, request, timestamp, utcnow

WORKFLOW = "archive.yml"
# Reserve headroom for teardown, the stop decision, and small concurrent jobs.
HEADROOM = 5


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def job_minutes(job, month_start, current_run, current_attempt, reservation):
    if (str(job["run_id"]) == str(current_run)
            and job.get("run_attempt") == current_attempt
            and job.get("name") == "archive" and job["status"] != "completed"):
        return 0  # The caller reserves this whole job separately.
    if job.get("conclusion") == "skipped":
        return 0
    start, end = job.get("started_at"), job.get("completed_at")
    if job["status"] == "completed" and not start and job.get("conclusion") == "cancelled":
        return 0
    if end and instant(end) <= month_start:
        return 0
    if job["status"] != "completed":
        if job.get("workflow_name") in ("Archive checks", "Archive budget reset"):
            return 5
        if job.get("workflow_name") == "Archive Infrared VODs":
            return reservation
        raise ArchiveError("Unknown active workflow; cannot bound repository runtime")
    if not start or not end:
        raise ArchiveError("Completed job has no usable runtime")
    # Count the full duration of a job crossing a month boundary in both months.
    return max(1, math.ceil((instant(end) - instant(start)).total_seconds() / 60))


def collection(store, path, key, params=None):
    for page in range(1, 21):
        query = urlencode({**(params or {}), "per_page": 100, "page": page})
        payload, _ = json_request(store.api + path + "?" + query, headers=store.headers)
        rows = payload[key]
        yield from rows
        if len(rows) < 100:
            return
    raise ArchiveError("Usage history exceeds bounded scan; budget cannot be verified")


def sync_usage(store, state, config, now=None):
    now = now or utcnow()
    month = now.strftime("%Y-%m")
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cache = state.setdefault("github_usage_cache", {})
    if cache.get("month") != month:
        cache.clear()
        cache.update(month=month, runs={})
    current = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    total = 0
    # Scan run history, not created>=month: reruns of older runs also cost minutes.
    for run in collection(store, "/actions/runs", "workflow_runs"):
        if instant(run["updated_at"]) < start and run["status"] == "completed":
            continue
        key = str(run["id"])
        stamp = [run["updated_at"], run["run_attempt"]]
        cached = cache["runs"].get(key)
        if cached and cached["stamp"] == stamp and run["status"] == "completed":
            minutes = cached["minutes"]
        else:
            minutes = sum(job_minutes(job, start, current, attempt, config["run_reservation_minutes"])
                          for job in collection(store, f"/actions/runs/{run['id']}/jobs", "jobs", {"filter": "all"}))
            if run["status"] == "completed":
                cache["runs"][key] = {"stamp": stamp, "minutes": minutes}
        total += minutes
    ledger = state.setdefault("budget", {}).setdefault(month, {})
    worker_total = sum(v for k, v in ledger.items() if k != "github_other_jobs")
    # Preserve crash reservations; add any usage not already covered by the ledger.
    ledger["github_other_jobs"] = max(0, total - worker_total)
    state["github_usage"] = {"month": month, "observed_or_reserved_job_minutes": total,
                             "checked_at": timestamp()}
    return sum(ledger.values())


def has_room(used, config):
    return used + config["run_reservation_minutes"] + HEADROOM <= config["monthly_runtime_minutes"]


def set_archive_enabled(store, enabled):
    action = "enable" if enabled else "disable"
    code, _, _ = request(store.api + f"/actions/workflows/{WORKFLOW}/{action}",
                         method="PUT", headers=store.headers, data=b"")
    if code != 204:
        raise ArchiveError(f"Cannot {action} archive workflow: HTTP {code}")


def pause(store, state, reason):
    state["budget_pause"] = {"month": utcnow().strftime("%Y-%m"), "reason": reason,
                             "at": timestamp()}
    store.save(state)
    set_archive_enabled(store, False)
    print("Archive schedule paused to protect the 300-minute budget:", reason)


def admit(store, state, config):
    try:
        used = sync_usage(store, state, config)
    except Exception:
        pause(store, state, "GitHub job usage could not be verified; no transfer started")
        raise
    store.save(state)
    if not has_room(used, config):
        pause(store, state, "Insufficient room for a full run plus safety headroom")
        return False
    return True


def reset_month():
    from .state import StateStore
    store = StateStore()
    state = store.load()
    paused = state.get("budget_pause")
    month = utcnow().strftime("%Y-%m")
    if paused and paused["month"] < month:
        set_archive_enabled(store, True)
        del state["budget_pause"]
        store.save(state)
        print("New monthly budget: archive schedule re-enabled")
    else:
        print("No previous-month budget pause to clear")


if __name__ == "__main__":
    reset_month()
