"""Streaming building blocks: seekable regions and chunked authenticated encryption.

Chunked AES-256-GCM ("STREAM" construction):
    The plaintext is cut into chunks of `chunk_size` bytes (the last one may be
    shorter, but is only empty when the whole stream is empty). Chunk i is
    encrypted on its own with
        nonce = nonce_prefix (7 bytes) || i (uint32 BE) || last_flag (1 byte)
    and the same associated data for every chunk. Each ciphertext chunk is
    chunk_size + 16 bytes (tag), except the last.

    - the counter in the nonce stops chunks from being reordered or dropped
    - the last-chunk flag stops the stream from being truncated at a chunk boundary
    - every chunk is authenticated before any of its bytes are returned

    Because chunk boundaries are fixed, any plaintext offset maps to one chunk,
    so readers get random access: reading a crate's index, or one crate out of a
    pallet, decrypts only the chunks it touches. Memory use is one chunk.
"""

import hashlib
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import FormatError, IntegrityError

TAG = 16
NONCE_PREFIX_LEN = 7
BLOCK = 1 << 20  # I/O block size for copying / compressing
_NONCE_TAIL = struct.Struct(">IB")
MAX_CHUNKS = 1 << 32


class _Seekable:
    """Shared seek/tell for read-only streams that define .size."""
    size = 0
    _pos = 0

    def seek(self, pos, whence=os.SEEK_SET):
        if whence == os.SEEK_CUR:
            pos += self._pos
        elif whence == os.SEEK_END:
            pos += self.size
        if pos < 0:
            raise ValueError("negative seek position")
        self._pos = pos
        return pos

    def tell(self):
        return self._pos

    def read_exact(self, n):
        data = self.read(n)
        if len(data) != n:
            raise FormatError("Unexpected end of data")
        return data


class RegionReader(_Seekable):
    """Read-only, seekable window [start, start+length) onto another seekable stream.
    Seeks the base on every read, so many regions can share one base stream."""

    def __init__(self, base, start=0, length=None):
        if length is None:
            base.seek(0, os.SEEK_END)
            length = base.tell() - start
        self.base, self.start, self.size = base, start, length

    def read(self, n=-1):
        remaining = self.size - self._pos
        if remaining <= 0:
            return b""
        n = remaining if n is None or n < 0 else min(n, remaining)
        self.base.seek(self.start + self._pos)
        data = self.base.read(n)
        self._pos += len(data)
        return data


class PlainWriter:
    """Pass-through writer with the same interface as EncryptWriter."""

    def __init__(self, out):
        self.out, self.position = out, 0

    def write(self, data):
        if data:
            self.out.write(data)
            self.position += len(data)

    def close(self):
        pass


class EncryptWriter:
    """Encrypts everything written to it into fixed-size authenticated chunks."""

    def __init__(self, out, key, nonce_prefix, aad, chunk_size):
        if len(nonce_prefix) != NONCE_PREFIX_LEN:
            raise ValueError("nonce prefix must be 7 bytes")
        self.out, self.aead, self.prefix = out, AESGCM(key), nonce_prefix
        self.aad, self.chunk_size = aad, chunk_size
        self.position = 0  # plaintext bytes written so far
        self._buf = bytearray()
        self._counter = 0
        self._closed = False

    def write(self, data):
        if not data:
            return
        self._buf += data
        self.position += len(data)
        cs = self.chunk_size
        # Keep at least one byte back so the final chunk is never empty
        # (unless the whole stream is) - readers rely on this.
        if len(self._buf) > cs:
            n_full = (len(self._buf) - 1) // cs
            view = memoryview(self._buf)
            for i in range(n_full):
                self._emit(view[i * cs:(i + 1) * cs], last=False)
            view.release()
            del self._buf[:n_full * cs]

    def _emit(self, chunk, last):
        if self._counter >= MAX_CHUNKS:
            raise ValueError("stream too long for chunk counter")
        nonce = self.prefix + _NONCE_TAIL.pack(self._counter, 1 if last else 0)
        self.out.write(self.aead.encrypt(nonce, bytes(chunk), self.aad))
        self._counter += 1

    def close(self):
        if not self._closed:
            self._emit(self._buf, last=True)
            self._buf.clear()
            self._closed = True


class DecryptReader(_Seekable):
    """Seekable plaintext view of a chunk-encrypted region."""

    def __init__(self, src, key, nonce_prefix, aad, chunk_size, label="data"):
        self.src, self.aead, self.prefix = src, AESGCM(key), nonce_prefix
        self.aad, self.chunk_size, self.label = aad, chunk_size, label
        self.ct_chunk = chunk_size + TAG
        ct_len = src.size
        if ct_len < TAG:
            raise FormatError(f"{label} is truncated")
        self.n_chunks = -(-ct_len // self.ct_chunk)
        last_ct = ct_len - (self.n_chunks - 1) * self.ct_chunk
        if last_ct < TAG:
            raise FormatError(f"{label} is truncated")
        self.size = (self.n_chunks - 1) * chunk_size + last_ct - TAG
        self._cache_idx, self._cache = -1, b""

    def _chunk(self, i):
        if i != self._cache_idx:
            self.src.seek(i * self.ct_chunk)
            ct = self.src.read(self.ct_chunk)
            nonce = self.prefix + _NONCE_TAIL.pack(i, 1 if i == self.n_chunks - 1 else 0)
            try:
                self._cache = self.aead.decrypt(nonce, ct, self.aad)
            except InvalidTag:
                raise IntegrityError(
                    f"{self.label} failed authentication - wrong key or tampered/corrupt data"
                ) from None
            self._cache_idx = i
        return self._cache

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self._pos
        out = bytearray()
        while n > 0 and self._pos < self.size:
            i, off = divmod(self._pos, self.chunk_size)
            piece = self._chunk(i)[off:off + n]
            if not piece:
                break
            out += piece
            self._pos += len(piece)
            n -= len(piece)
        return bytes(out)


def copy_stream(src, write, length, on_block=None) -> str:
    """Copy exactly `length` bytes from src.read to write(); returns their SHA-256."""
    h = hashlib.sha256()
    remaining = length
    while remaining:
        block = src.read(min(BLOCK, remaining))
        if not block:
            raise IntegrityError("Data ended early")
        h.update(block)
        write(block)
        remaining -= len(block)
        if on_block:
            on_block(len(block))
    return h.hexdigest()
