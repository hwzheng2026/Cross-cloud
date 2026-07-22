"""Encryption transformer — symmetric AES-256-GCM in-flight.

Encrypts the post-transform bytes with a per-share symmetric key.
The key is supplied via the policy / share config; for production,
inject it from a secrets manager rather than hard-coding.

Format (bytes):
    12-byte nonce || ciphertext || 16-byte GCM tag

The destination sees the raw encrypted blob — to decrypt, the
consumer needs the same key. For a real-world cross-cloud flow, a
common pattern is:

  1. Producer generates a random data-encryption key (DEK)
  2. Producer encrypts the payload with the DEK (this transformer)
  3. Producer wraps the DEK with the consumer's public RSA /
     KMS key (separate transformer or out-of-band)
  4. Both DEK and ciphertext land on the destination

For the demo, this transformer just takes a single key. Operators
who need the full KMS flow should layer it on top.

Usage:
    "encrypt:<hex-32-bytes-key>"           # 64 hex chars = 256 bits
    "encrypt:env:CDEH_ENCRYPTION_KEY"      # read key from env var
    "encrypt:keyring:my-secret-name"       # read key from secret manager
                                           # (extensible; default impl
                                           # reads from env)
"""
from __future__ import annotations

import binascii
import os
from typing import Any, Dict

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .base import BaseTransformer


class EncryptTransformer(BaseTransformer):
    kind = "encrypt"

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError(
                f"AES key must be 16/24/32 bytes (got {len(key)}); "
                "use a 256-bit (32-byte) key for AES-GCM-256."
            )
        self.key = key

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "EncryptTransformer":
        raw = cfg.get("key", "")
        if not raw:
            raise ValueError("EncryptTransformer requires 'key' in config")
        if raw.startswith("env:"):
            env_name = raw[len("env:"):]
            raw = os.environ.get(env_name, "")
            if not raw:
                raise ValueError(f"encryption key env var {env_name!r} "
                                  "is empty or unset")
        try:
            key = binascii.unhexlify(raw)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"encryption key must be hex: {e}")
        return cls(key=key)

    def transform(self, data: bytes, params: Dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        aes = AESGCM(self.key)
        ct = aes.encrypt(nonce, data, associated_data=None)
        return nonce + ct  # tag is appended by AESGCM

    def inverse(self, blob: bytes) -> bytes:
        """Decrypt (used by consumers + tests)."""
        if len(blob) < 12 + 16:
            raise ValueError("ciphertext too short")
        nonce, ct = blob[:12], blob[12:]
        aes = AESGCM(self.key)
        return aes.decrypt(nonce, ct, associated_data=None)

    def __repr__(self) -> str:
        return f"EncryptTransformer(key_len={len(self.key)})"


def make_key() -> bytes:
    """Module-level helper. Convenience for demos / unit tests —
    production keys come from a KMS."""
    return os.urandom(32)


# Backward-compatible alias (older test/example code may use this name).
make_key = make_key