"""M6 adapters: AES-GCM secret box, YouTube publisher (mocked HTTP), consent flow, text rules."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest

from clipforge.adapters.publishers import youtube as yt
from clipforge.adapters.publishers.stubs import InstagramPublisher
from clipforge.adapters.secrets.aesgcm import AesGcmSecretBox, SecretUnavailable, generate_key
from clipforge.domain.values import WorkspaceId
from clipforge.ports.publish import PublishError, PublishNotSupported, PublishRequest
from clipforge.services.publish import DESCRIPTION_MAX, clean_description, clean_title

WS1, WS2 = WorkspaceId(UUID(int=1)), WorkspaceId(UUID(int=2))
CREDS = yt.YouTubeCredentials("cid", "csecret", "refresh-1").dumps()


# ---------------------------------------------------------------- secret box
def test_secret_box_round_trip_and_workspace_binding() -> None:
    box = AesGcmSecretBox(generate_key())
    ref = box.seal(WS1, b"refresh-token")
    assert ref.startswith("v1:") and "refresh-token" not in ref
    assert box.open(WS1, ref) == b"refresh-token"
    with pytest.raises(SecretUnavailable):
        box.open(WS2, ref)  # copied into another workspace
    with pytest.raises(SecretUnavailable):
        AesGcmSecretBox(generate_key()).open(WS1, ref)  # wrong key
    with pytest.raises(SecretUnavailable):
        box.open(WS1, ref[:-4] + "AAAA")  # tampered
    assert box.seal(WS1, b"x") != box.seal(WS1, b"x")  # fresh nonce each time


def test_secret_key_must_be_32_bytes() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        AesGcmSecretBox("c2hvcnQ=")


# ---------------------------------------------------------------- YouTube publisher
class FakeYouTube:
    """Just enough of Google's token + resumable upload + statistics endpoints."""

    def __init__(self, *, flaky_at: int | None = None, token_error: str | None = None) -> None:
        self.received = b""
        self.flaky_at = flaky_at  # return 503 once when this many bytes have arrived
        self.token_error = token_error
        self.meta: dict[str, object] = {}
        self.auth_headers: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.startswith(yt.TOKEN_URL):
            if self.token_error:
                return httpx.Response(400, json={"error": self.token_error})
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        self.auth_headers.append(req.headers.get("authorization", ""))
        if url.startswith(yt.UPLOAD_URL) and req.method == "POST":
            self.meta = json.loads(req.content)
            return httpx.Response(200, headers={"Location": "https://upload.example/session/1"})
        if url.startswith("https://upload.example/session/1"):
            rng = req.headers["content-range"]
            total = int(rng.rsplit("/", 1)[1])
            if rng.startswith("bytes */"):  # status query
                return self._progress(total)
            if self.flaky_at is not None and len(self.received) >= self.flaky_at:
                self.flaky_at = None
                return httpx.Response(503)
            self.received += req.content
            return self._progress(total)
        if url.startswith(f"{yt.API_URL}/videos"):
            ids = parse_qs(urlparse(url).query)["id"][0].split(",")
            items = [{"id": i, "statistics": {"viewCount": "42", "likeCount": "3"}} for i in ids]
            return httpx.Response(200, json={"items": items})
        return httpx.Response(404)

    def _progress(self, total: int) -> httpx.Response:
        if len(self.received) >= total:
            return httpx.Response(200, json={"id": "VIDEO123"})
        return httpx.Response(308, headers={"Range": f"bytes=0-{len(self.received) - 1}"})


def _publisher(fake: FakeYouTube) -> yt.YouTubePublisher:
    return yt.YouTubePublisher(
        httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None
    )


def test_resumable_upload_in_chunks_resumes_after_a_5xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(yt, "CHUNK", 256 * 1024)
    data = bytes(range(256)) * 4096  # 1 MiB -> 4 chunks
    f = tmp_path / "final.mp4"
    f.write_bytes(data)
    fake = FakeYouTube(flaky_at=512 * 1024)
    res = _publisher(fake).publish(PublishRequest(f, "My title", "desc", "unlisted"), CREDS)
    assert res.external_id == "VIDEO123" and res.url == "https://youtu.be/VIDEO123"
    assert fake.received == data  # nothing lost or duplicated across the resume
    assert fake.meta["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
    assert fake.meta["snippet"]["title"] == "My title"  # type: ignore[index]
    assert set(fake.auth_headers) == {"Bearer at-1"}


def test_revoked_access_is_a_clear_error(tmp_path: Path) -> None:
    f = tmp_path / "f.mp4"
    f.write_bytes(b"x")
    with pytest.raises(PublishError, match="reconnect the channel"):
        _publisher(FakeYouTube(token_error="invalid_grant")).publish(
            PublishRequest(f, "t", "d"), CREDS
        )


def test_quota_error_names_the_reason_without_leaking_the_token(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).startswith(yt.TOKEN_URL):
            return httpx.Response(200, json={"access_token": "SECRET-AT"})
        body = {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}
        return httpx.Response(403, json=body)

    f = tmp_path / "f.mp4"
    f.write_bytes(b"x")
    p = yt.YouTubePublisher(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PublishError) as err:
        p.publish(PublishRequest(f, "t", "d"), CREDS)
    assert "quotaExceeded" in str(err.value) and "SECRET-AT" not in str(err.value)


def test_stats_are_fetched_in_batches_of_50() -> None:
    fake = FakeYouTube()
    stats = _publisher(fake).stats([f"v{i}" for i in range(120)], CREDS)
    assert len(stats) == 120 and stats["v7"].views == 42 and stats["v7"].likes == 3
    assert sum(1 for h in fake.auth_headers if h) == 3  # 50 + 50 + 20


def test_bad_credentials_blob_is_refused() -> None:
    with pytest.raises(PublishError, match="reconnect"):
        yt.YouTubeCredentials.parse(b'{"client_id": "x"}')


def test_stub_platforms_refuse_clearly(tmp_path: Path) -> None:
    with pytest.raises(PublishNotSupported, match="instagram"):
        InstagramPublisher().publish(PublishRequest(tmp_path / "x.mp4", "t", "d"), b"")


# ---------------------------------------------------------------- consent flow
def test_loopback_consent_checks_state_and_exchanges_the_code() -> None:
    exchanged: dict[str, list[str]] = {}

    def token_endpoint(req: httpx.Request) -> httpx.Response:
        exchanged.update(parse_qs(req.content.decode()))
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r-1"})

    def browser(url: str) -> None:  # the user approving in their browser
        q = parse_qs(urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
        redirect = f"{q['redirect_uri'][0]}/?state={q['state'][0]}&code=CODE-9"
        threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=5).read()).start()

    creds = yt.loopback_consent(
        "cid",
        "cs",
        browser,
        timeout_s=10,
        http=httpx.Client(transport=httpx.MockTransport(token_endpoint)),
    )
    assert creds.refresh_token == "r-1"
    assert exchanged["code"] == ["CODE-9"] and exchanged["code_verifier"][0]


def test_loopback_consent_rejects_a_forged_state() -> None:
    def attacker(url: str) -> None:
        redirect = f"{parse_qs(urlparse(url).query)['redirect_uri'][0]}/?state=forged&code=EVIL"
        threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=5).read()).start()

    with pytest.raises(PublishError, match="state mismatch"):
        yt.loopback_consent("cid", "cs", attacker, timeout_s=10)


def test_client_secrets_file(tmp_path: Path) -> None:
    f = tmp_path / "client.json"
    f.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}))
    assert yt.read_client_secrets(f) == ("a", "b")
    f.write_text("{}")
    with pytest.raises(ValueError):
        yt.read_client_secrets(f)


# ---------------------------------------------------------------- post text rules
def test_title_and_description_rules() -> None:
    assert clean_title("  <b>Huge</b>   play  ") == "bHuge/b play"
    assert len(clean_title("x" * 300)) == 100
    assert clean_description("hook", "Clip: twitch.tv/a") == "hook\n\nClip: twitch.tv/a"
    assert clean_description("hook\n\nClip: twitch.tv/a", "Clip: twitch.tv/a").count("Clip") == 1
    long = clean_description("y" * 9000, "Clip: twitch.tv/a")
    assert len(long) == DESCRIPTION_MAX and long.endswith("Clip: twitch.tv/a")  # credit survives
    assert clean_description("", "Credit") == "Credit"
