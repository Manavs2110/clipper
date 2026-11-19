"""Minimal Twitch Helix client (app access token)."""
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

API = "https://api.twitch.tv/helix"


class Twitch:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._expires = 0.0

    def _auth(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._expires = time.time() + body.get("expires_in", 3600)
        return self._token

    def get(self, path: str, **params) -> dict:
        r = requests.get(
            f"{API}/{path}",
            params=params,
            headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self._auth()}"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def game_id(self, name: str) -> str:
        data = self.get("games", name=name)["data"]
        if not data:
            raise SystemExit(f"Twitch has no category named {name!r}; set GAME_NAME in .env")
        return data[0]["id"]

    def top_clips(self, game_id: str, hours: int = 24, first: int = 50) -> list:
        """Most-viewed clips in the category created in the last `hours`."""
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return self.get(
            "clips",
            game_id=game_id,
            started_at=(now - timedelta(hours=hours)).strftime(fmt),
            ended_at=now.strftime(fmt),
            first=min(first, 100),
        )["data"]
