# Infrared VOD Archive

An independent archive worker for full Infrared Kick livestreams, publishing to
[@archiveinfrared](https://youtube.com/@archiveinfrared) with the creator's permission.
The existing `talonsight-pilot` repository is not modified or required.

## Current status

Implementation and automated tests are installed. YouTube was connected on
October 20, 2026, and the first pilot was requested. Check the
[live workflow](https://github.com/anthonypwb/infrared-vod-archive/actions/workflows/archive.yml)
and [runtime state](https://github.com/anthonypwb/infrared-vod-archive/blob/archive-state/state.json)
for the current result. The [connection guide](docs/CONNECT-YOUTUBE.md) also covers reconnection.
After the pilot passes source media checks, YouTube processing, full-duration and
public-visibility checks, subsequent scheduled runs archive available recordings oldest first.
Human playback review of the first upload remains recommended before increasing volume.

## Operating defaults

| Setting | Default |
| --- | --- |
| Discovery | Infrared's recent public VOD index directly on Kick; no transcript dependency |
| Destination | Exactly `@archiveinfrared`, checked against the authenticated channel ID every run |
| Schedule | Every 12 hours, at 00:17 and 12:17 UTC |
| Upload volume | No weekly count cap; at most one transfer per run, admitted only within the monthly budget |
| Initial pilot | One newest eligible complete recording, or an exact supplied VOD UUID |
| Routine coverage | All unarchived recordings in Kick's current public completed VOD index, oldest first, including the existing backlog |
| Source availability | No artificial age cutoff; require a valid non-future date, fresh Kick listing and complete accessible media |
| Quality | Highest supported muxed audio/video rendition; original codecs, no re-encoding |
| Runtime allowance | 1,000 repository runner minutes per UTC calendar month; reconcile rounded GitHub job durations, including CI, with conservative worker reservations |
| Job ceiling | 45 minutes; media work stops after 40 minutes to leave checkpoint time |
| Temporary storage | Up to 10 GB, with at least 2 GB left free |
| Retention | Temporary footage removed after the attempt; diagnostic artifacts expire after seven days |
| AI/API-model usage | None in routine discovery or transfer |

Before a transfer, the worker reads repository Actions job history, rounds each job
up to a minute, includes CI and reruns, and preserves larger crash reservations.
It requires room for a full 45-minute job plus five minutes of safety headroom.
If there is not enough room, it disables the archive workflow so idle polling does
not keep consuming minutes. A separate five-minute-maximum job checks once at the
start of each UTC month and re-enables only a prior-month budget pause.

This is a conservative controller for this repository's standard Ubuntu runners,
**not an account-wide billing cap**. It cannot cap other repositories or unrelated
manual jobs/changes. GitHub account billing controls remain separate. If usage
cannot be verified, automatic transfers pause. Budget takes priority over coverage;
some allowance can remain unused because the next full attempt would not fit.

## Reliability behavior

- Check Kick's channel metadata for slug `infrared` and channel ID `129849`, then
  read `https://kick.com/api/v2/channels/infrared/videos`. Accept only explicitly
  public, non-live, non-private, non-pruned recordings from that channel. Titles,
  UTC source dates, UUIDs and playback URLs come from Kick's response.
- Require accessible HLS, a completed `ENDLIST`, and an unchanged playlist across
  a settling interval. The footage is fetched directly from Kick's media hosts.
  Transcript availability is not consulted. The website metadata endpoint is not
  a guaranteed stable developer API; changed layouts or access refusals stop the
  worker with a diagnostic instead of silently using stale transcript metadata.
- Download every segment in order and fail on a missing segment. Feed segments
  through a local pipe to FFmpeg; never let FFmpeg fetch remote URLs. Only one
  full output file is stored, preserving disk space.
- Check audio/video presence, source/output duration, and decoded samples at the
  beginning, middle and end before requesting an upload.
- Use YouTube resumable uploads and query the same session after network errors.
- Checkpoint the VOD UUID before starting an upload. If completion is uncertain,
  reconcile against the channel's recent uploads using the source UUID embedded
  in the description. Never start a duplicate upload merely because a response
  was lost. Ambiguous cases remain on hold for inspection.
- Keep queue, source-to-YouTube mapping, checks and usage in `state.json` on the
  `archive-state` branch. No media, OAuth tokens, or upload-session URLs are committed.
- Prevent simultaneous workers with a repository concurrency group and reject
  stale state writes using GitHub's content SHA.

## Limits of this pilot

The 1,000-minute allowance takes priority over rescuing every stream. Removing the
weekly cap lets the backlog clear at scheduled intervals, but expiry, source failures
and the finite budget prevent an unconditional preservation guarantee.
Discovery uses Kick's returned recent-recording window; it does not promise
exhaustive historical pagination. Longer-than-12-hour streams, recordings that exceed available disk, unusual
HLS layouts (encryption, gaps, discontinuities, separate audio, byte ranges), expired
sources and unresolved upload outcomes stop with a report. The worker never silently
reduces quality, splits a full stream, skips footage, or starts paid infrastructure.

YouTube performs its own processing and may restrict an upload. A successful API
request alone is not counted as a public archive. YouTube is also not an original-file
backup: footage recovery for later clipping has not been integrated or tested.
The source/YouTube mapping is available to Talonsight, but no cross-repository writes
are performed by this worker.

## Development and operations

Runtime: Python 3.12+ and FFmpeg/FFprobe. The worker uses only the Python standard
library. Third-party packages are required only for the one-time local OAuth helper.

```sh
python -m unittest discover -s tests -v
python -m archive.worker discover
```

See [Operations](docs/OPERATIONS.md) for status, recovery and stopping the schedule.

Primary technical references checked October 20, 2026:

- [YouTube video uploads](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert)
- [Resumable upload protocol](https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol)
- [Google OAuth and refresh-token expiration](https://developers.google.com/identity/protocols/oauth2)
- [GitHub-hosted runner resources](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
