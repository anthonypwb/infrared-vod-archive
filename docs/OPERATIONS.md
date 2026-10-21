# Operations

## Status and schedule

The scheduled workflow is installed but its job is skipped until `ARCHIVE_CONNECTED`
is `true`. The setup helper sets this only after checking and saving the channel
connection. The public pilot and actual `pilot_verified` state control transfers.

The six-hour UTC schedule runs at 8:17 PM, 2:17 AM, 8:17 AM and 2:17 PM in
Massachusetts during daylight time; during standard time those become 7:17 PM,
1:17 AM, 7:17 AM and 1:17 PM. GitHub schedules may be delayed and are not exact timers.

`Archive Infrared VODs` has four modes:

| Mode | Behavior |
| --- | --- |
| `discover` | Read Kick channel/VOD metadata and the newest eligible HLS playlist; no credentials or video segments needed |
| `pilot` | Request one complete public test upload from a verified account |
| `verify` | Reconcile pending/uncertain uploads and check processing/public status |
| `auto` | Verify earlier work, then transfer at most one eligible available recording, oldest first, within limits |

Worker runs and retries share one concurrency group. Runtime state lives on
`archive-state` rather than changing implementation code on `main`.

## Direct Kick discovery

Both discovery and media retrieval use Kick directly. The worker validates the
`infrared` channel's ID (`129849`) and reads its recent public recordings via
`https://kick.com/api/v2/channels/infrared/videos`. No transcript-site request is
required. Live, private, deleted and pruned entries are excluded. A VOD must also
have a completed, settled HLS playlist before any transfer starts.

The user authorized oldest-first archival of the currently available backlog on
October 20, 2026. `backfill_available_vods=true` includes older unarchived recordings
from Kick's current public completed index, sorted by exact UTC start time. A null
`max_source_age_days` removes the former 21-day cutoff; it does not imply that media
is still available. Valid non-future dates, fresh playback URLs and complete,
accessible playlists remain required. UUIDs, attempts, YouTube IDs, pilot results and
budget history are preserved. Previously recorded transcript links are
retained as optional provenance, not used to discover or retrieve the source.
Playback URLs are kept only in memory and refreshed from each run's current index.
An in-flight pilot continues using the commit it started with.

The user subsequently authorized enough exceptions to build an expiry buffer,
with the 300-minute budget taking priority. The weekly upload cap is now disabled
(`max_new_uploads_per_week=null`). Each six-hour run may transfer one recording,
oldest first, until the available queue is caught up or the monthly budget pauses
work. An immediate catch-up run applies this policy; ongoing runs need no prompting.

Budget admission reconciles rounded GitHub job durations (including CI, retries
and polls) with conservative state reservations, then requires 45 minutes plus
five minutes of safety headroom. The worker has `actions: write` solely to read
usage and disable its own `archive.yml` workflow when budget is insufficient or
unverifiable. The separate `budget-reset.yml` runs at 00:07 UTC on the first of
each month, shares the same concurrency group, and re-enables only a budget pause
from an earlier month when `ARCHIVE_CONNECTED=true`. This reset job's minutes are
included in the next usage scan. An operator stop using `ARCHIVE_CONNECTED=false`
remains respected. Account-wide billing and unrelated manually initiated work are
outside this repository controller's scope. It does not guarantee all VODs survive.

## Stop and resume

Set repository Actions variable `ARCHIVE_CONNECTED=false` to skip future scheduled
jobs. Disable or cancel a running workflow in GitHub Actions when an immediate stop
is needed. Reset the variable to `true` to resume the already-configured schedule.
Do not delete `state.json` to resume; that loses duplicate prevention and the ledger.

## Failure handling

- **HTTP 403/404 at source:** preserve state, retry at most three source attempts;
  no alternate accounts, proxy bypasses, or invented source URLs. If expired, an
  accessible authorized copy is needed.
- **Live/changing playlist:** wait for completion; no partial archive is published.
- **Unknown date or changed Kick index layout:** discovery stops before acquisition.
  Correct the parser using a verified current Kick response. Do not fabricate a date,
  use stale saved playback URLs or silently switch back to delayed transcript discovery.
- **Output too large:** stop. Moving to a larger worker or storage service is a
  separate infrastructure decision; the code does not silently lower quality.
- **`upload_unknown`:** inspect YouTube Studio and recent source-ID mappings before
  any retry. If a matching video exists, record its ID and use `uploaded_pending`.
  If the operator verifies that no upload exists (including processing/private
  uploads), reset to `retry` and remove `upload_started_at` to permit another attempt.
  A lost response is not proof of failure.
- **YouTube `blocked`:** inspect rejection, privacy, processing or duration problems.
  Do not publish a replacement automatically. Once corrected, set `uploaded_pending`
  to recheck that same video.
- **Monthly allowance reached:** the archive workflow is disabled to stop idle polls,
  and resumes automatically after the UTC month changes. No transfer starts without
  a complete reservation and safety headroom. Reservations are conservative
  and separate from actual account-wide billing. Reconcile cancelled runs against
  GitHub job timing before lowering a reservation. Do not erase history to get past a cap.
- **Expired OAuth token:** run the connection helper again after resolving the
  consent/token issue. A new token is stored as an Actions secret.

Failure notification comes from GitHub Actions according to the account's notification
settings. There is no configured email, Slack, or separate ChatGPT notification service.

## Scope and provenance

The user authorized full public republication of Infrared livestreams and selected
@archiveinfrared as the destination on October 20, 2026. No unrelated channel,
bulk historical backfill, thumbnails, paid storage or Talonsight production changes
are included. General rights permission is not represented as a provider OAuth grant.

Each successful record includes the original source UUID, Kick recording URL, title/date,
source byte hash, segment count, duration, codec/dimension checks and YouTube ID.
This is a lookup map for later integration, not proof that YouTube supplies a recoverable
original file. A full restore-to-edit workflow remains to be validated separately.
