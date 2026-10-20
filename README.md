# Infrared VOD Archive

An independent archive worker for full Infrared Kick livestreams, publishing to
[@archiveinfrared](https://youtube.com/@archiveinfrared) with the creator's permission.
The existing `talonsight-pilot` repository is not modified or required.

## Current status

Implementation and automated tests are installed. **YouTube is not connected yet;
no VOD has been uploaded and the scheduled worker is inactive.** Complete the
[one-time connection](docs/CONNECT-YOUTUBE.md) to request a full pilot upload.
After the pilot passes source media checks, YouTube processing, full-duration and
public-visibility checks, subsequent scheduled runs may upload new recordings.
Human playback review of the first upload remains recommended before increasing volume.

## Operating defaults

| Setting | Default |
| --- | --- |
| Discovery | Infrared Kick entries on the Infrared Transcripts inventory |
| Destination | Exactly `@archiveinfrared`, checked against the authenticated channel ID every run |
| Schedule | Every six hours, at 00:17, 06:17, 12:17 and 18:17 UTC |
| Pilot volume | At most three new uploads in a rolling seven-day period |
| Initial pilot | One newest eligible complete recording, or an exact supplied VOD UUID |
| Routine coverage | New recordings from the pilot's monitoring start date; no historical bulk backfill |
| Source freshness | At most 21 days old; unknown dates do not transfer |
| Quality | Highest supported muxed audio/video rendition; original codecs, no re-encoding |
| Runtime allowance | 300 recorded/estimated runner minutes per calendar month, including worker polling |
| Job ceiling | 45 minutes; media work stops after 40 minutes to leave checkpoint time |
| Temporary storage | Up to 10 GB, with at least 2 GB left free |
| Retention | Temporary footage removed after the attempt; diagnostic artifacts expire after seven days |
| AI/API-model usage | None in routine discovery or transfer |

The runtime allowance is this repository's estimate, **not an account-wide billing
cap**. GitHub rounds job usage, and CI checks consume additional minutes. Configure
GitHub account budgets separately. A failed/cancelled worker leaves its full
45-minute reservation charged until safely reconciled. This intentionally favors
underspending over exhausting the account allowance. Admission requires room for
the next complete reservation, so some allowance may remain unused.

## Reliability behavior

- Require an exact Infrared source page, accessible HLS, a completed `ENDLIST`,
  and an unchanged playlist across a settling interval.
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

The 300-minute allowance and three-per-week limit do not guarantee archiving every
stream. Longer-than-12-hour streams, recordings that exceed available disk, unusual
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
