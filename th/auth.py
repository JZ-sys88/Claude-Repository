"""Twitch Helix auth (client-credentials / app access token)."""
import json
import os
import pathlib
import time

import requests

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
CACHE = pathlib.Path(__file__).resolve().parent.parent / ".token_cache.json"


class AuthError(RuntimeError):
    pass


def load_dotenv():
    """Load .env from the project root. Real env vars take precedence.

    Lives here rather than only in the CLI so importing `th.auth` directly
    works without the caller having to bootstrap credentials itself.
    """
    p = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


class Twitch:
    """Thin Helix client. App tokens only -- enough for clips/videos/users."""

    def __init__(self, client_id=None, client_secret=None):
        load_dotenv()
        self.client_id = client_id or os.environ.get("TWITCH_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("TWITCH_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise AuthError(
                "Missing credentials. Register a free app at https://dev.twitch.tv/console/apps "
                "then set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (or put them in .env)."
            )
        self._token = None
        self._expires = 0
        self._load_cache()

    # -- token handling -------------------------------------------------
    def _load_cache(self):
        try:
            data = json.loads(CACHE.read_text())
        except Exception:
            return
        if data.get("client_id") == self.client_id and data.get("expires", 0) > time.time() + 60:
            self._token = data["token"]
            self._expires = data["expires"]

    def _save_cache(self):
        try:
            CACHE.write_text(json.dumps(
                {"client_id": self.client_id, "token": self._token, "expires": self._expires}
            ))
        except Exception:
            pass  # cache is an optimisation, never fatal

    def token(self):
        if self._token and self._expires > time.time() + 60:
            return self._token
        r = requests.post(TOKEN_URL, params={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }, timeout=20)
        if r.status_code != 200:
            raise AuthError(f"Token request failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        self._token = data["access_token"]
        self._expires = time.time() + data.get("expires_in", 3600)
        self._save_cache()
        return self._token

    def headers(self):
        return {"Client-ID": self.client_id, "Authorization": f"Bearer {self.token()}"}

    # -- requests -------------------------------------------------------
    def get(self, path, **params):
        url = f"{HELIX}/{path.lstrip('/')}"
        for attempt in range(3):
            r = requests.get(url, headers=self.headers(), params=params, timeout=30)
            if r.status_code == 401 and attempt == 0:
                self._token = None  # force refresh once
                continue
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code != 200:
                raise AuthError(f"GET {path} failed ({r.status_code}): {r.text[:300]}")
            return r.json()
        raise AuthError(f"GET {path} failed after retries")

    def paginate(self, path, limit=None, **params):
        """Yield items across cursor pages, stopping at `limit`."""
        got = 0
        cursor = None
        while True:
            page = dict(params)
            if cursor:
                page["after"] = cursor
            data = self.get(path, **page)
            items = data.get("data", [])
            if not items:
                return
            for it in items:
                yield it
                got += 1
                if limit and got >= limit:
                    return
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor:
                return

    # -- convenience ----------------------------------------------------
    def user_id(self, login):
        data = self.get("users", login=login.lower().strip())
        items = data.get("data", [])
        if not items:
            raise AuthError(f"No such Twitch channel: {login}")
        return items[0]["id"], items[0]["display_name"]
