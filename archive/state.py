"""Checkpoint metadata on a dedicated branch with compare-and-swap writes."""
import base64
import json
import os
from urllib.parse import quote
from .core import ArchiveError, json_request, request


class StateStore:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        if self.repo != "anthonypwb/infrared-vod-archive":
            raise ArchiveError("Refusing state writes to a different repository")
        self.api = f"https://api.github.com/repos/{self.repo}"
        self.headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                        "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                        "X-GitHub-Api-Version": "2022-11-28"}
        self.sha = None

    def load(self):
        url = self.api + "/contents/state.json?ref=archive-state"
        status, _, body = request(url, headers=self.headers)
        if status == 404:
            # Create the state branch only if it is genuinely absent.
            branch_status, _, _ = request(self.api + "/git/ref/heads/archive-state", headers=self.headers)
            if branch_status == 404:
                head, _ = json_request(self.api + "/git/ref/heads/main", headers=self.headers)
                json_request(self.api + "/git/refs", method="POST", headers=self.headers,
                             data=json.dumps({"ref": "refs/heads/archive-state", "sha": head["object"]["sha"]}).encode())
            elif branch_status != 200:
                raise ArchiveError("Cannot verify state branch")
            return {"schema_version": 1, "vods": {}, "budget": {}, "pilot_verified": False}
        if status != 200:
            raise ArchiveError(f"Cannot load state: HTTP {status}")
        payload = json.loads(body)
        self.sha = payload["sha"]
        state = json.loads(base64.b64decode(payload["content"]))
        if state.get("schema_version") != 1:
            raise ArchiveError("Unknown state schema")
        return state

    def save(self, state):
        payload = {"message": "Checkpoint archive queue and usage", "branch": "archive-state",
                   "content": base64.b64encode((json.dumps(state, indent=2, sort_keys=True) + "\n").encode()).decode()}
        if self.sha:
            payload["sha"] = self.sha
        result, _ = json_request(self.api + "/contents/state.json", method="PUT", headers=self.headers,
                                 data=json.dumps(payload).encode())
        self.sha = result["content"]["sha"]
