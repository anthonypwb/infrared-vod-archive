# Operations

## Status and schedule

The scheduled workflow is installed but its job is skipped until `ARCHIVE_CONNECTED`
is `true`. The setup helper sets this only after checking and saving the channel
connection. The public pilot and actual `pilot_verified` state control transfers.

The two-hour UTC schedule runs at minute 17 of every even-numbered UTC hour.
In Massachusetts this is every even-numbered local hour during daylight time,
and every odd-numbered local hour during standard time. GitHub schedules may be delayed and are not exact timers.

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
initially with a 300-minute budget taking priority. The weekly upload cap is now disabled
(`max_new_uploads_per_week=null`). Each two-hour run may transfer one recording,
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
@archiveinfrared as the destination on October 20, 2026, then authorized the
available Kick backlog and budget-first catch-up. No unrelated channel,
thumbnails, paid storage or Talonsight production changes
are included. General rights permission is not represented as a provider OAuth grant.

Each successful record includes the original source UUID, Kick recording URL, title/date,
source byte hash, segment count, duration, codec/dimension checks and YouTube ID.
This is a lookup map for later integration, not proof that YouTube supplies a recoverable
original file. A full restore-to-edit workflow remains to be validated separately.

## Shared allowance allocation (October 21, 2026)

After reporting a GitHub Pro upgrade, the user authorized raising the archive cap
while protecting Talonsight. The archive now allows 1,000 runner minutes per UTC
month. Talonsight's current configured cap is also 1,000 minutes; against Pro's
3,000 included monthly minutes, this leaves 1,000 minutes of shared headroom for
other jobs, retries and development. Talonsight configuration is unchanged.
This allocation is not a GitHub-enforced reservation or an account-wide usage
measurement. Keep account billing controls in place and reassess the allocation
before expanding either project's cap. The existing archive budget admission,
45-minute reservation, five-minute headroom and automatic pause remain active.

On October 21, 2026, the user authorized ramping up archival. The recurring
schedule was shortened from six hours to two hours (up to 12 attempts/day),
with one oldest eligible VOD per run and no overlapping archive workers.
The 1,000-minute monthly cap and Talonsight allocation remain unchanged.
More frequent empty polls also use runner minutes; the budget controller counts
these. Source-failure retries now use a 15-minute backoff so the next two-hour slot can retry the oldest VOD. This cadence does
not increase transfer speed or guarantee YouTube processing completion times.

## Basic upload metadata

The fixed channel tags in `archive/core.py` are included with every new upload.
Descriptions use the clean stream template without permission wording. The creator
cancelled transcript-based enrichment and post-upload editing on October 21, 2026.
The daily metadata task is disabled. See `docs/METADATA.md` for the current policy.

## Creator edits after upload

On October 21, 2026 the creator confirmed that the roughly 51-second difference
on War (BWGvzMeL9CY) was an intentional YouTube edit removing copyrighted intro
material, and requested that small duration changes not trigger archive warnings.
Post-upload duration verification now flags only differences greater than the
larger of five minutes or five percent of source duration. The measured difference
and threshold are saved in duration_check. Source download and pre-upload file
checks remain strict. Archived videos are not periodically rechecked or reuploaded
because of subsequent edits. This policy does not change source download retries.

## Expedited source retry

The creator authorized faster recovery on October 21, 2026. Source failures now
wait 15 minutes instead of six hours before becoming eligible again. Routine
runs still choose the oldest eligible available VOD, with the existing three-attempt
limit and 1,000-minute budget. A one-time immediate retry of It's happening was
requested with FFmpeg stderr, exit code, segment index and disk-space diagnostics
retained in state and the run report. URLs are redacted from captured stderr.
No media-integrity checks were relaxed. The temporary kickoff trigger is removed
once the immediate run is registered.
