"""YouTube Data API v3 publisher: OAuth refresh, resumable upload, statistics.

Uses the official REST endpoints over httpx (no Google client library needed). Uploads from
an API project that hasn't passed Google's audit are forced to private by YouTube, so
``private`` is the default and the UI says so (ADR-0039).

Credentials (sealed in publish_targets.secret_ref) are JSON:
``{"client_id", "client_secret", "refresh_token"}``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from clipforge.ports.publish import PublishError, PublishRequest, PublishResult, VideoStats

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (an endpoint, not a secret)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # channel name + statistics
)
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB
GAMING_CATEGORY = "20"
RETRYABLE = {500, 502, 503, 504}


def _google_error(resp: httpx.Response) -> str:
    """Google's error reason + message, never request headers (they carry the token)."""
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(err, str):  # OAuth endpoint style: {"error": "invalid_grant", ...}
        return err
    reasons = [e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict)]
    msg = str(err.get("message", ""))[:300]
    return f"HTTP {resp.status_code} {','.join(r for r in reasons if r)} {msg}".strip()


@dataclass(frozen=True)
class YouTubeCredentials:
    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def parse(cls, raw: bytes) -> YouTubeCredentials:
        try:
            d = json.loads(raw)
            return cls(d["client_id"], d["client_secret"], d["refresh_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise PublishError("stored YouTube credentials are incomplete; reconnect") from exc

    def dumps(self) -> bytes:
        return json.dumps(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            }
        ).encode()


class YouTubePublisher:
    platform: ClassVar[str] = "youtube"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self.max_retries = max_retries
        self.sleep = sleep

    # ------------------------------------------------------------ auth
    def access_token(self, creds: YouTubeCredentials) -> str:
        resp = self.http.post(
            TOKEN_URL,
            data={
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "refresh_token": creds.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            reason = _google_error(resp)
            if "invalid_grant" in reason:
                raise PublishError("YouTube access was revoked or expired: reconnect the channel")
            raise PublishError(f"YouTube sign-in failed: {reason}")
        return str(resp.json()["access_token"])

    # ------------------------------------------------------------ upload
    def publish(self, req: PublishRequest, credentials: bytes) -> PublishResult:
        creds = YouTubeCredentials.parse(credentials)
        token = self.access_token(creds)
        auth = {"Authorization": f"Bearer {token}"}
        size = req.path.stat().st_size
        meta = {
            "snippet": {
                "title": req.title,
                "description": req.description,
                "tags": list(req.tags),
                "categoryId": GAMING_CATEGORY,
            },
            "status": {"privacyStatus": req.privacy, "selfDeclaredMadeForKids": False},
        }
        start = self.http.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **auth,
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
            json=meta,
        )
        if start.status_code != 200 or "location" not in start.headers:
            raise PublishError(f"YouTube refused the upload: {_google_error(start)}")
        session = start.headers["location"]
        video = self._send(session, req.path, size, auth)
        vid = str(video["id"])
        return PublishResult(vid, f"https://youtu.be/{vid}")

    def _send(self, session: str, path: Path, size: int, auth: dict[str, str]) -> dict[str, Any]:
        """Chunked PUTs; on a network error or 5xx ask YouTube how much it has and resume."""
        offset, failures = 0, 0
        with path.open("rb") as f:
            while True:
                try:
                    if offset >= size:  # everything sent but no final answer yet: ask
                        resp = self._status(session, size, auth)
                    else:
                        f.seek(offset)
                        chunk = f.read(CHUNK)
                        end = offset + len(chunk) - 1
                        resp = self.http.put(
                            session,
                            content=chunk,
                            headers={**auth, "Content-Range": f"bytes {offset}-{end}/{size}"},
                        )
                except httpx.TransportError:
                    resp = None
                if resp is not None and resp.status_code in (200, 201):
                    return dict(resp.json())
                if resp is not None and resp.status_code == 308:
                    offset = self._next_offset(resp)
                    failures = 0
                    continue
                if resp is not None and resp.status_code not in RETRYABLE:
                    raise PublishError(f"YouTube upload failed: {_google_error(resp)}")
                failures += 1
                if failures > self.max_retries:
                    raise PublishError("YouTube upload kept failing; try again later")
                self.sleep(min(60.0, 2.0**failures))
                try:
                    st = self._status(session, size, auth)
                except httpx.TransportError:
                    continue
                if st.status_code in (200, 201):
                    return dict(st.json())
                if st.status_code == 308:
                    offset = self._next_offset(st)

    def _status(self, session: str, size: int, auth: dict[str, str]) -> httpx.Response:
        return self.http.put(session, headers={**auth, "Content-Range": f"bytes */{size}"})

    @staticmethod
    def _next_offset(resp: httpx.Response) -> int:
        rng = resp.headers.get("range")  # "bytes=0-1234" = received through byte 1234
        return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0

    # ------------------------------------------------------------ stats
    def stats(self, external_ids: Sequence[str], credentials: bytes) -> dict[str, VideoStats]:
        creds = YouTubeCredentials.parse(credentials)
        token = self.access_token(creds)
        out: dict[str, VideoStats] = {}
        ids = list(dict.fromkeys(external_ids))
        for i in range(0, len(ids), 50):  # API max per call
            resp = self.http.get(
                f"{API_URL}/videos",
                params={"part": "statistics", "id": ",".join(ids[i : i + 50])},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                raise PublishError(f"YouTube statistics failed: {_google_error(resp)}")
            for item in resp.json().get("items", []):
                s = item.get("statistics", {})

                def num(k: str, s: dict[str, Any] = s) -> int | None:
                    return int(s[k]) if k in s else None

                out[str(item["id"])] = VideoStats(
                    views=num("viewCount") or 0,
                    likes=num("likeCount"),
                    comments=num("commentCount"),
                )
        return out

    def channel_title(self, creds: YouTubeCredentials) -> str:
        token = self.access_token(creds)
        resp = self.http.get(
            f"{API_URL}/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            raise PublishError(f"couldn't read the channel: {_google_error(resp)}")
        items = resp.json().get("items", [])
        return str(items[0]["snippet"]["title"]) if items else "YouTube channel"


# ---------------------------------------------------------------- one-time consent (CLI)
def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def consent_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return f"{AUTH_URL}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",  # always return a refresh token
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )


def loopback_consent(
    client_id: str,
    client_secret: str,
    show_url: Callable[[str], None],
    *,
    timeout_s: float = 300,
    http: httpx.Client | None = None,
) -> YouTubeCredentials:
    """Google's installed-app flow: a one-shot server on 127.0.0.1 catches the redirect.
    PKCE + state protect the code; nothing listens beyond loopback or after one request."""
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            q = parse_qs(urlparse(self.path).query)
            ok = q.get("state", [""])[0] == state and "code" in q
            if ok:
                result["code"] = q["code"][0]
            else:
                result["error"] = q.get("error", ["state mismatch"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"ClipForge: YouTube connected. You can close this tab."
                if ok
                else b"ClipForge: connection failed. Check the terminal."
            )

        def log_message(self, *args: Any) -> None:  # keep the code out of logs
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = timeout_s
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    show_url(consent_url(client_id, redirect, state, challenge))
    try:
        server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        raise PublishError(f"YouTube consent failed: {result.get('error', 'timed out')}")
    client = http or httpx.Client(timeout=30)
    resp = client.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
    )
    if resp.status_code != 200:
        raise PublishError(f"YouTube token exchange failed: {_google_error(resp)}")
    refresh = resp.json().get("refresh_token")
    if not refresh:
        raise PublishError("Google returned no refresh token; remove the app's access and retry")
    return YouTubeCredentials(client_id, client_secret, str(refresh))


def read_client_secrets(path: Path) -> tuple[str, str]:
    """The JSON Google Cloud Console downloads for a Desktop OAuth client."""
    d = json.loads(path.read_text(encoding="utf-8"))
    block = d.get("installed") or d.get("web") or {}
    if not block.get("client_id") or not block.get("client_secret"):
        raise ValueError("not a Google OAuth client file (expected an 'installed' client)")
    return str(block["client_id"]), str(block["client_secret"])
