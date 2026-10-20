# Connect @archiveinfrared once

The repository cannot upload using a channel URL alone. This setup obtains a
Google OAuth grant for the archive channel, saves the three credentials as encrypted
GitHub Actions secrets, records the verified channel ID, and requests one full pilot
upload. It does not require commenting on GitHub for each VOD.

## 1. Create a Google API client

Use a desktop computer for this one-time setup.

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select
   a project named **Infrared VOD Archive**.
2. Enable **YouTube Data API v3** from the API Library.
3. Configure **Google Auth Platform** with your own account as the app's user.
   Use the upload and read-only YouTube scopes requested by the helper.
4. Put the consent audience in **Production** for durable background access.
   Google documents that external apps in **Testing** normally receive refresh
   tokens that expire after seven days when requesting these scopes. Complete
   any verification Google requires for your account/project; do not bypass it.
5. Create an OAuth client with application type **Desktop app**, and download its
   JSON file. Keep it out of Git and do not paste it or tokens into chat.
6. Confirm this channel can upload videos longer than 15 minutes in
   [YouTube Studio](https://studio.youtube.com/), under channel feature eligibility.

Google's documentation and project restrictions can change. The worker verifies
the actual uploaded video's public status. If YouTube keeps it private or rejects
it, inspect the project's eligibility/restriction details before proceeding. Changing
the consent audience alone does not certify public-upload eligibility.

## 2. Run the sign-in helper

Install [Python](https://www.python.org/downloads/), [Git](https://git-scm.com/downloads)
and [GitHub CLI](https://cli.github.com/) if needed, then open a terminal:

```sh
gh auth login
gh repo clone anthonypwb/infrared-vod-archive
cd infrared-vod-archive
python -m pip install -r requirements-setup.txt
python setup_youtube.py --client-secrets /path/to/client_secret.json
```

On Windows, replace `/path/to/client_secret.json` with the downloaded file's actual
path, quoted if it contains spaces. `py` can be used instead of `python`.

Choose the Google account and **@archiveinfrared** channel in the sign-in flow.
The helper checks the exact channel identity and long-upload eligibility before
changing GitHub settings. A token for Talonsight or another channel is rejected.

The helper stores:

| GitHub setting | Type |
| --- | --- |
| `YOUTUBE_CLIENT_ID` | Actions secret |
| `YOUTUBE_CLIENT_SECRET` | Actions secret |
| `YOUTUBE_REFRESH_TOKEN` | Actions secret |
| `YOUTUBE_CHANNEL_ID` | Actions variable, verified immutable channel ID |
| `ARCHIVE_CONNECTED` | Actions variable set to `true` after the connection succeeds |

It then requests **one complete public pilot upload**. All recurring transfers
wait for the pilot to pass media checks and become a processed, full-length,
public YouTube video. Scheduled runs check processing every six hours, or you can
use the workflow's `verify` mode to check sooner. No additional per-VOD approval
or GitHub comment is needed.

Optional: choose an exact recent Infrared recording for the pilot:

```sh
python setup_youtube.py --client-secrets /path/to/client_secret.json --pilot-vod-id UUID
```

## 3. Verify the pilot

Open the [archive workflow](https://github.com/anthonypwb/infrared-vod-archive/actions/workflows/archive.yml).
The run summary and `archive-state/state.json` record the YouTube video ID, source
duration, output checks and final status. Play the first upload and check its beginning,
middle and ending. Record actual transfer minutes before increasing volume or budget.

If Google rejects sign-in or requires account interaction, complete that prompt in
your browser. Do not send passwords, verification codes or refresh tokens in chat.
