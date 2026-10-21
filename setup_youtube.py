"""One-time local sign-in; sends secrets directly to GitHub using gh stdin."""
import argparse
import json
import shutil
import subprocess
import sys

REPO = "anthonypwb/infrared-vod-archive"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def gh(*args, value=None):
    result = subprocess.run(["gh", *args], input=value, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("GitHub setup failed; verify gh authentication and repository permissions")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secrets", required=True, help="Downloaded Desktop OAuth client JSON")
    parser.add_argument("--pilot-vod-id", help="Optional VOD UUID; otherwise use the newest eligible recording")
    parser.add_argument("--enable-metadata", action="store_true", help="Authorize transcript-based description and tag updates; do not request a pilot")
    args = parser.parse_args()
    if not shutil.which("gh"):
        raise RuntimeError("Install GitHub CLI from https://cli.github.com and run gh auth login first")
    gh("auth", "status")
    client = json.load(open(args.client_secrets, encoding="utf-8"))
    if "installed" not in client:
        raise RuntimeError("Create a Desktop app OAuth client and download its JSON")
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import AuthorizedSession
    scopes = SCOPES + (["https://www.googleapis.com/auth/youtube.force-ssl"] if args.enable_metadata else [])
    flow = InstalledAppFlow.from_client_config(client, scopes)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent select_account",
        authorization_prompt_message="Open this Google sign-in link in your browser: {url}",
        success_message="Authorization received. You can close this tab.")
    if not credentials.refresh_token:
        raise RuntimeError("Google did not return an offline token; reconnect with offline access")
    session = AuthorizedSession(credentials)
    mine_response = session.get("https://www.googleapis.com/youtube/v3/channels",
                                params={"part": "id,status,snippet", "mine": "true"}, timeout=30)
    handle_response = session.get("https://www.googleapis.com/youtube/v3/channels",
                                  params={"part": "id", "forHandle": "@archiveinfrared"}, timeout=30)
    if mine_response.status_code != 200 or handle_response.status_code != 200:
        raise RuntimeError("YouTube API lookup failed; confirm the API is enabled in your Google project")
    mine, target = mine_response.json().get("items", []), handle_response.json().get("items", [])
    if len(mine) != 1 or len(target) != 1 or mine[0]["id"] != target[0]["id"]:
        raise RuntimeError("Wrong YouTube channel selected. Re-run and select @archiveinfrared")
    if mine[0].get("status", {}).get("longUploadsStatus") != "allowed":
        raise RuntimeError("Enable uploads longer than 15 minutes for @archiveinfrared in YouTube Studio, then re-run")
    print("Verified destination: @archiveinfrared")
    # No token file, command-line secret values, or secret output is created.
    for name, value in (("YOUTUBE_CLIENT_ID", credentials.client_id),
                        ("YOUTUBE_CLIENT_SECRET", credentials.client_secret),
                        ("YOUTUBE_REFRESH_TOKEN", credentials.refresh_token)):
        gh("secret", "set", name, "--repo", REPO, value=value)
    gh("variable", "set", "YOUTUBE_CHANNEL_ID", "--repo", REPO, "--body", mine[0]["id"])
    # All account operations completed before enabling the prepared schedule.
    gh("variable", "set", "ARCHIVE_CONNECTED", "--repo", REPO, "--body", "true")
    if args.enable_metadata:
        gh("variable", "set", "ARCHIVE_METADATA_ENABLED", "--repo", REPO, "--body", "true")
        print("Metadata permission saved. Prepared updates will apply after scheduled archive runs.")
        return
    # A normal reconnect uses the original scopes, so disable metadata until reauthorized.
    gh("variable", "set", "ARCHIVE_METADATA_ENABLED", "--repo", REPO, "--body", "false")
    command = ["workflow", "run", "archive.yml", "--repo", REPO, "--ref", "main", "-f", "mode=pilot"]
    if args.pilot_vod_id:
        command += ["-f", "vod_id=" + args.pilot_vod_id]
    gh(*command)
    print("Connection saved and one complete pilot upload requested.")
    print("Recurring uploads wait until the pilot is verified public with the expected full duration.")
    print("Follow progress: https://github.com/" + REPO + "/actions/workflows/archive.yml")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Setup stopped:", str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        sys.exit(1)
