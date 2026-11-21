"""SecretBox with AES-256-GCM. The workspace id is bound in as associated data, so a ref
copied into another workspace fails to open. Refs are ``v1:<base64url(nonce|ciphertext)>``
and live in ``publish_targets.secret_ref``; the key lives only in CLIPFORGE_SECRET_KEY."""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from clipforge.domain.values import WorkspaceId
from clipforge.ports.pipeline import PipelineError

PREFIX = "v1:"


class SecretUnavailable(PipelineError):
    pass


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _decode_key(key: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(key.strip() + "=" * (-len(key.strip()) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("CLIPFORGE_SECRET_KEY must be base64 (32 bytes)") from exc
    if len(raw) != 32:
        raise ValueError("CLIPFORGE_SECRET_KEY must decode to exactly 32 bytes")
    return raw


class AesGcmSecretBox:
    def __init__(self, key: str) -> None:
        self._aead = AESGCM(_decode_key(key))

    @staticmethod
    def _aad(ws: WorkspaceId) -> bytes:
        return b"clipforge-secret:" + str(ws).encode()

    def seal(self, ws: WorkspaceId, plaintext: bytes) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext, self._aad(ws))
        return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode()

    def open(self, ws: WorkspaceId, ref: str) -> bytes:
        if not ref.startswith(PREFIX):
            raise SecretUnavailable("unknown secret format")
        try:
            blob = base64.urlsafe_b64decode(ref[len(PREFIX) :])
            return self._aead.decrypt(blob[:12], blob[12:], self._aad(ws))
        except (InvalidTag, binascii.Error, ValueError) as exc:
            # wrong key, wrong workspace, or tampered: never say which
            raise SecretUnavailable("stored credentials can't be opened with this key") from exc
