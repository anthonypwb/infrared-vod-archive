"""One bounded archive attempt per run; scheduled runs use no language models."""
import argparse
import json
import os
import signal
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from .core import ArchiveError, read_config, reserve, settle, timestamp, upload_metadata, utcnow
from .source import (acquire, discover_kick, eligible_date, ensure_media_tools, get_text,
                     parse_playlist, read_kick_source, resolve)
from .state import StateStore
from .youtube import YouTube


def discover(config):
    return discover_kick(config)


def merge_inventory(state, rows):
    # A feed migration establishes a baseline, not a historical bulk backfill.
    # A same-day recording remains eligible under monitoring_started_at below.
    already_monitoring_kick = bool(state.get("kick_monitoring_started_at"))
    for row in rows:
        metadata = {key: value for key, value in row.items() if key != "master_url"}
        if row["id"] not in state["vods"]:
            state["vods"][row["id"]] = {**metadata, "status": "discovered", "attempts": 0,
                "first_seen": timestamp(), "new_since_monitoring": already_monitoring_kick}
        else:
            existing = state["vods"][row["id"]]
            old_source = existing.get("source_url", "")
            if old_source.startswith("https://infrared-transcripts.pages.dev/"):
                existing.setdefault("transcript_url", old_source)
            existing.update(metadata)
    state.setdefault("kick_monitoring_started_at", timestamp())


def automatic_candidates(state, eligible, config=None):
    if config and config.get("backfill_available_vods"):
        return sorted(eligible, key=lambda v: (v["started_at"], v["id"]))
    start_date = datetime.fromisoformat(state["monitoring_started_at"]).date().isoformat()
    return [v for v in eligible if v["date"] >= start_date
            or v.get("new_since_monitoring") or v.get("attempts", 0)]


def can_attempt(vod, config, now=None):
    now = now or utcnow()
    if vod["status"] not in ("discovered", "retry", "downloading"):
        return False
    if vod.get("attempts", 0) >= config["max_transfer_attempts"]:
        return False
    if not eligible_date(vod.get("date"), config["max_source_age_days"], now.date()):
        return False
    return not vod.get("retry_after") or datetime.fromisoformat(vod["retry_after"]) <= now


def weekly_count(state, now=None):
    cutoff = (now or utcnow()) - timedelta(days=7)
    return sum(bool(v.get("upload_started_at")) and datetime.fromisoformat(v["upload_started_at"]) >= cutoff
               for v in state["vods"].values())


def reconcile(state, youtube, store):
    for vod in state["vods"].values():
        if vod["status"] in ("uploading", "upload_unknown") and not vod.get("youtube_id"):
            match = youtube.recent_match(vod["id"])
            if match:
                vod["youtube_id"] = match
                vod["status"] = "uploaded_pending"
                store.save(state)
            else:
                # No match is not proof that YouTube did not receive the upload.
                vod["status"] = "upload_unknown"
                vod["note"] = "Upload outcome unknown; automatic duplicate upload suppressed"
        if vod["status"] == "uploaded_pending":
            status, message = youtube.verify_video(vod)
            vod["status"] = "uploaded_pending" if status == "pending" else status
            vod["note"] = message
            if status == "archived":
                vod["archived_at"] = timestamp()
                if vod.get("is_pilot"):
                    state["pilot_verified"] = True
                    state["pilot_video_id"] = vod["youtube_id"]
            store.save(state)


def run(mode, vod_id=None):
    config = read_config()
    if mode == "discover":
        rows = discover(config)
        report = {"discovery_source": "kick", "entries": len(rows),
                  "dated_entries": sum(bool(v["date"]) for v in rows),
                  "recent_entries": sum(eligible_date(v["date"], config["max_source_age_days"]) for v in rows),
                  "examples": [{k: v for k, v in row.items() if k != "master_url"} for row in rows[:5]],
                  "media_downloaded": False}
        recent = [v for v in rows if eligible_date(v["date"], config["max_source_age_days"])]
        if recent:
            latest = sorted(recent, key=lambda v: (v["started_at"], v["id"]), reverse=True)[0]
            try:
                variant, playlist = resolve(read_kick_source(latest, rows))
                segments = parse_playlist(playlist, variant)
                report["source_preflight"] = {"id": latest["id"], "status": "accessible_complete_playlist",
                    "segments": len(segments), "duration_seconds": sum(s["duration"] for s in segments)}
            except Exception as exc:
                report["source_preflight"] = {"id": latest["id"], "status": "blocked",
                    "reason": str(exc) if isinstance(exc, ArchiveError) else type(exc).__name__}
        print(json.dumps(report, indent=2))
        return
    store = StateStore()
    state = store.load()
    started = time.monotonic()
    key = os.environ.get("GITHUB_RUN_ID", "local") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    month = reserve(state, key, config["run_reservation_minutes"], config["monthly_runtime_minutes"])
    store.save(state)  # A crash leaves the full reservation charged.

    def gate():
        if time.monotonic() - started > (config["run_reservation_minutes"] - 5) * 60:
            raise ArchiveError("Run time allowance reached")

    def alarm_handler(signum, frame):
        raise ArchiveError("Run time allowance reached")

    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm((config["run_reservation_minutes"] - 5) * 60)
    try:
        youtube = YouTube()
        youtube.verify_channel(os.environ.get("YOUTUBE_CHANNEL_ID"), config["destination_handle"])
        reconcile(state, youtube, store)
        if mode == "verify":
            return
        if mode == "auto" and not state["pilot_verified"]:
            retry_pilots = [v for v in state["vods"].values() if v.get("is_pilot") and can_attempt(v, config)]
            if retry_pilots:
                mode, vod_id = "pilot", retry_pilots[0]["id"]
            else:
                print("Waiting for a successful pilot upload; automatic transfers have not started.")
                return
        if mode == "pilot" and state["pilot_verified"]:
            print("Pilot already verified; use the recurring workflow.")
            return
        if any(v.get("is_pilot") and v["status"] in ("uploading", "upload_unknown", "uploaded_pending", "blocked")
               for v in state["vods"].values()) and not state["pilot_verified"]:
            print("Existing pilot requires verification or reconciliation; another pilot was not uploaded.")
            return
        if weekly_count(state) >= config["max_new_uploads_per_week"]:
            print("Weekly upload limit reached; remaining recordings stay queued.")
            return
        rows = discover(config)
        merge_inventory(state, rows)
        if "monitoring_started_at" not in state:
            state["monitoring_started_at"] = timestamp()
        store.save(state)
        current_ids = {row["id"] for row in rows}
        eligible = [v for v in state["vods"].values() if v["id"] in current_ids and can_attempt(v, config)]
        if vod_id:
            eligible = [v for v in eligible if v["id"] == vod_id]
            if not eligible:
                raise ArchiveError("Requested VOD is absent, stale, undated, or already processed")
        elif mode == "auto":
            eligible = automatic_candidates(state, eligible, config)
        if not eligible:
            print("No eligible available VOD; no media transferred.")
            return
        # The pilot chooses newest first; routine archival rescues the oldest available first.
        vod = sorted(eligible, key=lambda v: (v["started_at"], v["id"]), reverse=mode == "pilot")[0]
        existing = youtube.recent_match(vod["id"])
        if existing:
            vod.update(status="existing_archive", youtube_id=existing,
                       note="Matching source ID already exists on the archive channel; no duplicate uploaded")
            store.save(state)
            return
        vod["attempts"] += 1
        vod["status"] = "downloading"
        vod["is_pilot"] = mode == "pilot"
        store.save(state)
        try:
            master = read_kick_source(vod, rows)
            if not eligible_date(vod.get("date"), config["max_source_age_days"]):
                raise ArchiveError("Source date is missing or outside the allowed age")
            variant, playlist = resolve(master)
            segments = parse_playlist(playlist, variant)
            if sum(s["duration"] for s in segments) > config["max_source_duration_seconds"]:
                raise ArchiveError("Source exceeds the full-upload duration limit")
            # Completed source must remain unchanged across a short settling period.
            time.sleep(config["source_settle_seconds"])
            gate()
            if get_text(variant, media=True) != playlist:
                raise ArchiveError("Source is still changing; retry later")
            ensure_media_tools()
            gate()
            with tempfile.TemporaryDirectory(prefix="infrared-archive-") as tmp:
                output = Path(tmp) / "vod.mp4"
                vod["media"] = acquire(segments, output, config, gate)
                gate()
                vod["status"] = "uploading"
                vod["upload_started_at"] = timestamp()
                store.save(state)  # Durable barrier before a non-idempotent YouTube request.
                vod["youtube_id"] = youtube.upload(output, upload_metadata(vod), gate)
                vod["status"] = "uploaded_pending"
                store.save(state)
                reconcile(state, youtube, store)
                print("Uploaded:", "https://www.youtube.com/watch?v=" + vod["youtube_id"])
        except Exception as exc:
            if vod["status"] == "downloading":
                vod["status"] = "retry"
                vod["retry_after"] = (utcnow() + timedelta(hours=6)).isoformat()
            elif vod["status"] == "uploading":
                vod["status"] = "upload_unknown"
            vod["note"] = str(exc) if isinstance(exc, ArchiveError) else type(exc).__name__
            store.save(state)
            raise
    finally:
        signal.alarm(0)
        settle(state, month, key, time.monotonic() - started)
        state["last_run_at"] = timestamp()
        store.save(state)
        summary = {"pilot_verified": state["pilot_verified"],
                   "monthly_recorded_minutes": sum(state["budget"][month].values()),
                   "statuses": {v["id"]: {"status": v["status"], "youtube_id": v.get("youtube_id"),
                                          "note": v.get("note")} for v in state["vods"].values()
                                if v["status"] != "discovered"}}
        Path("report.json").write_text(json.dumps(summary, indent=2) + "\n")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
                out.write("## Infrared archive run\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("discover", "pilot", "verify", "auto"))
    parser.add_argument("--vod-id")
    args = parser.parse_args()
    try:
        run(args.mode, args.vod_id)
    except Exception as exc:
        # Never emit exception bodies containing source URLs, sessions, or OAuth responses.
        message = str(exc) if isinstance(exc, ArchiveError) else type(exc).__name__
        print("Archive stopped:", message)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
