# Historical Rumble archive

User-approved scope: a separate workflow imports at most **one historical VOD
per UTC calendar week (Monday–Sunday)**, earliest eligible original broadcast first, from:

- https://rumble.com/c/InfraredArchive
- https://rumble.com/user/swaggghili

The initial twice-monthly proposal was superseded by the user's explicit
one-VOD-weekly correction. There is no June 7 cutoff. This does not change the
existing twice-daily new-Kick workflow or Talonsight.

## Current activation status

**Historical publication is disabled in `config/history.json`.** The initial
read-only GitHub runner probe (run 39718248660, October 22, 2026 UTC) received
HTTP 403 and a `Just a moment...` browser-verification page from both catalogs.
The cloud-browser approval gate also denied Rumble access. Do not work around
that denial or a source security challenge. There has been no historical media
download or YouTube upload.

`history.yml` has a separate Saturday 14:37 UTC schedule and a configuration
gate. While disabled it writes a local diagnostic report, makes no Rumble or
YouTube requests, and does not create state entries. Its normal upload path is
prepared but cannot be considered operational until live source extraction,
channel identity, pagination, original-date matching, and media integrity are
verified through an authorized access method. `validate` is local-only.

## Exact rejection policy

`config/history-filter.json` is the explicit list from the latest chat response:
the slur table, ordinary profanity list, and the listed dehumanizing/abusive
expressions. This is the user's stricter lexical rule, not an assertion that
every occurrence violates YouTube rules. No contextual exceptions are made for
quotations, reclaimed language, benign alternate meanings, or criticism.

The filter rejects exact words, inflections, common spellings, spaced or
punctuated letters, Unicode normalization, zero-width characters, common leet
substitutions, censored markers, and close spellings. Fuzzy matching allows one
character edit for words of at least five letters and two for at least nine.
Short words require exact/explicit variants to avoid treating ordinary short
words as slurs. Plural forms are included. Close spelling can reject unrelated
words; this is intentional conservatism under the user's instruction.

There is no claim that an ASR transcript detects every spoken word, visual
element, sound-alike, or prohibited statement. Passing the list is not a
guarantee of YouTube compliance. This module does not alter the forward Kick
pipeline and does not restore the canceled transcript-based tag enrichment.

## Required gates

1. Read the complete catalogs from both allowed channels. Partial pagination,
   challenge pages, repeated pages, and fetch failures cannot establish order.
2. Match each candidate to one unique transcript in the provided site's index
   using normalized title and original broadcast date. A bracketed original
   date overrides a transcript page's repost date. Unknown/ambiguous matches
   are skipped. Only this transcript site is accepted.
3. Extract timestamped transcript items, excluding navigation/comments. Reject
   malformed, missing, mismatched, or visibly incomplete transcripts.
4. Reject the entire VOD on any filter match, including its title. Store the
   rule, timestamp, transcript hash, and filter hash in the historical catalog.
5. For a passing transcript, verify the Rumble uploader, completed player,
   full source duration, beginning/end coverage and large transcript gaps.
   Sparse transcripts and unsupported media are skipped.
6. Deduplicate both Rumble channels against one another and against uploads in
   this archive's state using original title/date; source markers also prevent
   re-uploading historical records after an uncertain response.
7. Preserve new Kick priority, the 1,000-minute repository cap, a 180-minute
   Kick reserve, and a 250-minute historical sublimit within the same cap.
   The 45-minute reservation is settled to rounded actual use. Talonsight's
   repository and allocation are not modified.
8. Download one source MP4, verify full byte length, duration, audio/video and
   decode samples. Re-fetch the exact transcript and require the same hash and
   a fresh filter pass immediately before upload.
9. Checkpoint `uploading` and the weekly slot before the non-idempotent request.
   An unknown outcome blocks further historical uploads until reconciled.

## State and operation

The existing `archive-state` branch holds a separate `history` section with
`catalog`, `vods`, `budget`, and `last_report`. Both workers share the concurrency
group `infrared-archive-worker` to prevent overlapping state writes.

Historical reports are retained as small GitHub artifacts for 14 days. No media
or full transcript is stored as an artifact. Rejected language results are
cached for the current filter version; passing transcripts are always read
again before publication. Unverifiable candidates are also cached as skips so
the first 40 failures cannot permanently prevent progress through the catalog.
Clear an individual catalog record or change the filter version to rescreen it.
Missing/ambiguous index matches are reconsidered on each run.
The scan is bounded to 40 new candidate transcripts per run. A week with no
eligible source produces no upload; rules are never relaxed to fill a slot.

To activate after the access restriction has been resolved and the source
parser validated, set `enabled` to true and remove the resolved blocker. Run
the tests first. Do not enable on assumptions or solely because tests with
synthetic HTML pass.
