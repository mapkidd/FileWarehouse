"""Shared binary envelope used by both crates and pallets (format v2, streaming).

Layout:
    magic          8 bytes   b"\\x89CRATE\\r\\n" or b"\\x89PALLET\\n"
    version        1 byte    (2)
    header_len     4 bytes   big-endian uint32
    header         header_len bytes of UTF-8 JSON (public metadata)
    body           rest of the container

If encrypted, body = chunked AES-256-GCM stream (see streams.py) with
    key  = HKDF-SHA256(manifest key, salt, info="asset-warehouse/container/" + magic)
    aad  = SHA-256(magic + version + header_len + header)
so the public header can't be altered without every chunk failing.

The header is written before the body, so it only holds things known up
front. Anything computed while streaming (sizes, checksums, the file index)
goes in an index at the *end* of the body, located through a fixed footer.
"""

import hashlib
import json
import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import FormatError, KeyMissingError
from .manifest import b64d, b64e
from .streams import NONCE_PREFIX_LEN, DecryptReader, EncryptWriter, PlainWriter, RegionReader

FORMAT_VERSION = 2
MAGIC_LEN = 8
PREFIX = struct.Struct(">BI")  # version, header length
MAX_HEADER = 1 << 20
DEFAULT_CHUNK = 1 << 18  # 256 KiB
MIN_CHUNK, MAX_CHUNK = 1 << 12, 1 << 26


def _derive_key(master: bytes, salt: bytes, magic: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=b"asset-warehouse/container/" + magic).derive(master)


def write_header(out, magic: bytes, header: dict, manifest=None, chunk_size=DEFAULT_CHUNK):
    """Write magic + header to `out`; return a writer (write/position/close) for the body.
    Call writer.close() when done - it writes the final encrypted chunk."""
    header = dict(header)
    if manifest is not None:
        salt, prefix = os.urandom(16), os.urandom(NONCE_PREFIX_LEN)
        header["encryption"] = {
            "cipher": "AES-256-GCM", "mode": "chunked", "chunk_size": chunk_size,
            "kdf": "HKDF-SHA256", "key_id": manifest.key_id, "manifest": manifest.name,
            "salt": b64e(salt), "nonce_prefix": b64e(prefix),
        }
    else:
        header["encryption"] = None

    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    head = magic + PREFIX.pack(FORMAT_VERSION, len(header_bytes)) + header_bytes
    out.write(head)
    if manifest is None:
        return PlainWriter(out)
    return EncryptWriter(out, _derive_key(manifest.key, salt, magic), prefix,
                         hashlib.sha256(head).digest(), chunk_size)


class Container:
    """Parses the envelope from a seekable source (a file or a RegionReader)."""

    def __init__(self, src, magic: bytes, kind: str):
        if not isinstance(src, RegionReader):
            src = RegionReader(src)
        src.seek(0)
        fixed = src.read(MAGIC_LEN + PREFIX.size)
        if len(fixed) < MAGIC_LEN + PREFIX.size or fixed[:MAGIC_LEN] != magic:
            raise FormatError(f"Not a {kind} file")
        version, hlen = PREFIX.unpack_from(fixed, MAGIC_LEN)
        if version != FORMAT_VERSION:
            raise FormatError(f"{kind} format version {version} is not supported "
                              f"(this build reads version {FORMAT_VERSION})")
        start = MAGIC_LEN + PREFIX.size + hlen
        if hlen > MAX_HEADER or start > src.size:
            raise FormatError(f"Corrupt {kind} header")
        header_bytes = src.read(hlen)
        try:
            self.header = json.loads(header_bytes.decode("utf-8"))
        except ValueError as e:
            raise FormatError(f"Corrupt {kind} header: {e}") from e
        self.magic, self.kind, self.version = magic, kind, version
        self._aad = hashlib.sha256(fixed + header_bytes).digest()
        self._src, self._body_start = src, start
        self.total_size = src.size

    @property
    def encrypted(self) -> bool:
        return bool(self.header.get("encryption"))

    @property
    def key_id(self):
        enc = self.header.get("encryption")
        return enc["key_id"] if enc else None

    @property
    def manifest_name(self):
        enc = self.header.get("encryption")
        return enc.get("manifest") if enc else None

    @property
    def chunk_size(self):
        enc = self.header.get("encryption")
        return enc.get("chunk_size") if enc else None

    def can_open(self, keyring) -> bool:
        return not self.encrypted or (keyring is not None and self.key_id in keyring)

    def open_body(self, keyring=None):
        """Return a seekable plaintext stream (with .size) of the body."""
        body = RegionReader(self._src, self._body_start, self._src.size - self._body_start)
        if not self.encrypted:
            return body
        manifest = keyring.get(self.key_id) if keyring is not None else None
        if manifest is None:
            raise KeyMissingError(self.key_id, self.manifest_name)
        enc = self.header["encryption"]
        cs = enc.get("chunk_size")
        if not isinstance(cs, int) or not MIN_CHUNK <= cs <= MAX_CHUNK:
            raise FormatError(f"Corrupt {self.kind} header (chunk size)")
        key = _derive_key(manifest.key, b64d(enc["salt"]), self.magic)
        return DecryptReader(body, key, b64d(enc["nonce_prefix"]), self._aad, cs, label=self.kind)
