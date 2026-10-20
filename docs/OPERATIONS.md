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
| `discover` | Read inventory metadata only; no credentials needed, no video download or upload |
| `pilot` | Request one complete public test upload from a verified account |
| `verify` | Reconcile pending/uncertain uploads and check processing/public status |
| `auto` | Verify earlier work, then transfer at most one eligible new recording within limits |

Worker runs and retries share one concurrency group. Runtime state lives on
`archive-state` rather than changing implementation code on `main`.

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
- **Unknown date or changed inventory layout:** `metadata_review` prevents acquisition.
  Correct the parser with a verified HTML example and return the affected entry to
  `discovered` after its actual source date is recorded.
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
- **Monthly allowance reached:** no transfer starts. Reservations are conservative
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

Each successful record includes the original source UUID, transcript page, title/date,
source byte hash, segment count, duration, codec/dimension checks and YouTube ID.
This is a lookup map for later integration, not proof that YouTube supplies a recoverable
original file. A full restore-to-edit workflow remains to be validated separately.
