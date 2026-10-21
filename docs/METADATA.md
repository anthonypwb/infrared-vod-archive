# Archive metadata policy

The creator superseded transcript-based enrichment on October 21, 2026.
Use only the fixed basic tags in `archive/core.py` on every new upload:

Haz, Haz Al-Din, Haz Al Din, InfraHaz, Infrared, Infrared Show, American Communist Party, ACP, Communism, Marxism, Marxism-Leninism, Communist Party, Political Commentary, Political Philosophy, American Politics, MAGA Communism, Infrared livestream, full livestream.

Keep the clean stream description template without permission wording. Do not
scrape transcripts, generate specialized tags or descriptions, or apply queued
metadata plans. The daily Archive video metadata task is disabled and the archive
workflow no longer invokes post-upload metadata editing. The old metadata module
entry point is a no-op even if ARCHIVE_METADATA_ENABLED remains set. Existing
videos are left for manual editing. No additional Google editing grant is needed.

Two-hour archival scheduling, the 1,000-minute cap and Talonsight are unchanged.
