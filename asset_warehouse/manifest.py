"""Manifests: the keys of the warehouse.

A manifest holds a random 256-bit master key. Crates and pallets never use the
master key directly; each container derives its own key from it with
HKDF-SHA256 and a fresh random salt.

A manifest can optionally be protected with a passphrase, in which case the
master key is wrapped with AES-256-GCM under a key derived by scrypt.
"""

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import FormatError, PassphraseRequiredError

MANIFEST_FORMAT = "asset-warehouse-manifest"
MANIFEST_VERSION = 1

# scrypt cost: 2**17 * 8 * 128 bytes = 128 MiB of memory per attempt
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1
SCRYPT_MAXMEM = 256 * 1024 * 1024


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compute_key_id(key: bytes) -> str:
    """Public fingerprint of a key. Lets containers say which manifest they need."""
    return hashlib.sha256(b"asset-warehouse/key-id" + key).hexdigest()[:16]


def _derive_wrap_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=SCRYPT_N,
                          r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)


class Manifest:
    def __init__(self, name: str, key: bytes, created: str = None, path: str = None,
                 protected: bool = False):
        if len(key) != 32:
            raise ValueError("Manifest key must be 32 bytes")
        self.name = name
        self.key = key
        self.key_id = compute_key_id(key)
        self.created = created or now_iso()
        self.path = path
        self.protected = protected

    @classmethod
    def create(cls, name: str) -> "Manifest":
        return cls(name=name, key=os.urandom(32))

    # ---- persistence -------------------------------------------------------

    def to_dict(self, passphrase: str = None) -> dict:
        doc = {
            "format": MANIFEST_FORMAT,
            "version": MANIFEST_VERSION,
            "name": self.name,
            "key_id": self.key_id,
            "created": self.created,
        }
        if passphrase:
            salt, nonce = os.urandom(16), os.urandom(12)
            wrapped = AESGCM(_derive_wrap_key(passphrase, salt)).encrypt(
                nonce, self.key, self._wrap_aad())
            doc["protection"] = {
                "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                "salt": b64e(salt), "cipher": "AES-256-GCM", "nonce": b64e(nonce),
                "wrapped_key": b64e(wrapped),
            }
        else:
            doc["key"] = b64e(self.key)
        return doc

    def save(self, path: str, passphrase: str = None) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(passphrase), f, indent=2)
        self.path = path
        self.protected = bool(passphrase)

    @staticmethod
    def _read_doc(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            raise FormatError(f"Cannot read manifest: {e}") from e
        if not isinstance(doc, dict) or doc.get("format") != MANIFEST_FORMAT:
            raise FormatError("Not an Asset Warehouse manifest")
        if doc.get("version", 0) > MANIFEST_VERSION:
            raise FormatError(f"Manifest version {doc.get('version')} is newer than supported")
        return doc

    @classmethod
    def is_protected(cls, path: str) -> bool:
        return "protection" in cls._read_doc(path)

    @classmethod
    def load(cls, path: str, passphrase: str = None) -> "Manifest":
        doc = cls._read_doc(path)
        name, key_id = doc.get("name", "unnamed"), doc.get("key_id", "")
        prot = doc.get("protection")
        if prot:
            if not passphrase:
                raise PassphraseRequiredError(f"Manifest '{name}' is passphrase protected")
            if prot.get("kdf") != "scrypt":
                raise FormatError(f"Unsupported KDF {prot.get('kdf')}")
            wrap_key = hashlib.scrypt(
                passphrase.encode("utf-8"), salt=b64d(prot["salt"]), n=prot["n"],
                r=prot["r"], p=prot["p"], maxmem=SCRYPT_MAXMEM, dklen=32)
            try:
                key = AESGCM(wrap_key).decrypt(
                    b64d(prot["nonce"]), b64d(prot["wrapped_key"]),
                    b"asset-warehouse/manifest-wrap/" + key_id.encode())
            except InvalidTag:
                raise PassphraseRequiredError("Wrong passphrase") from None
        else:
            try:
                key = b64d(doc["key"])
            except (KeyError, ValueError) as e:
                raise FormatError("Manifest has no valid key") from e
        m = cls(name=name, key=key, created=doc.get("created"), path=path,
                protected=bool(prot))
        if key_id and m.key_id != key_id:
            raise FormatError("Manifest key does not match its key id (corrupt file?)")
        return m

    def _wrap_aad(self) -> bytes:
        return b"asset-warehouse/manifest-wrap/" + self.key_id.encode()

    def __repr__(self):
        return f"Manifest({self.name!r}, key_id={self.key_id})"


class Keyring:
    """The set of manifests currently loaded; containers look keys up by id."""

    def __init__(self):
        self._by_id = {}

    def add(self, manifest: Manifest) -> None:
        self._by_id[manifest.key_id] = manifest

    def remove(self, key_id: str) -> None:
        self._by_id.pop(key_id, None)

    def get(self, key_id: str):
        return self._by_id.get(key_id)

    def __contains__(self, key_id):
        return key_id in self._by_id

    def __iter__(self):
        return iter(list(self._by_id.values()))

    def __len__(self):
        return len(self._by_id)
