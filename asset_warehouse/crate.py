"""Crates: compressed, encrypted bundles of files (streaming, format v2).

Why this beats .zip on size: zip compresses every file separately with
Deflate (32 KiB window). A crate is *solid*: all files are concatenated
(sorted by extension so similar data sits together) and compressed in large
blocks (up to 128 MiB, LZMA2 with up to a 64 MiB dictionary), so redundancy
*between* files is exploited too. Blocks are independent, so they're
compressed and decompressed in parallel (see codec.py).

Crate body (plaintext, inside the container's encryption stream):
    data     the concatenated file stream, as a sequence of independently
             compressed blocks (zstd frames / XZ streams / raw)
    index    XZ-compressed UTF-8 JSON: name, comment, block table,
             per-file path/offset/size/sha256
    footer   24 bytes: data_len (u64 BE), index_len (u64 BE), b"CRATEIDX"

Crates written before block support have no block table: their data is one
XZ stream (or raw) and they're read as a single block.

Packing streams files through compressor -> encryptor -> disk; memory use is
bounded by the block size and thread count, not the crate size. Everything identifying the content lives in
the index, so an encrypted crate reveals only its size, creation time and
which manifest opens it.
"""

import hashlib
import json
import lzma
import os
import struct
from datetime import datetime, timezone

from . import codec, container
from .codec import LEVELS
from .errors import FormatError, IntegrityError
from .manifest import now_iso
from .streams import BLOCK, RegionReader

MAGIC = b"\x89CRATE\r\n"
EXTENSION = ".crate"
FOOTER = struct.Struct(">QQ8s")
FOOTER_MAGIC = b"CRATEIDX"


# ---- helpers -----------------------------------------------------------------

def normalize_arc_path(path: str) -> str:
    """Archive paths are '/'-separated, relative, and never escape the root."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"Empty archive path: {path!r}")
    for p in parts:
        if p == ".." or ":" in p:
            raise ValueError(f"Unsafe archive path: {path!r}")
    return "/".join(parts)


def safe_join(root: str, arc_path: str) -> str:
    """Join an archive path onto root, refusing anything that escapes it."""
    target = os.path.abspath(os.path.join(root, *normalize_arc_path(arc_path).split("/")))
    root_abs = os.path.abspath(root)
    if os.path.commonpath([root_abs, target]) != root_abs:
        raise FormatError(f"Archive path escapes destination: {arc_path!r}")
    return target


def collect_folder(folder: str, prefix: str = None):
    """Return (source, arc_path) pairs for every file under folder.
    arc paths are prefixed with the folder's own name unless prefix is given."""
    folder = os.path.abspath(folder)
    base = os.path.basename(folder) if prefix is None else prefix
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for fn in sorted(filenames):
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, folder).replace(os.sep, "/")
            out.append((src, f"{base}/{rel}" if base else rel))
    return out


def xz_compress_small(data: bytes) -> bytes:
    """One-shot XZ for indexes: dictionary sized to the data, so a 2 KB index
    doesn't make liblzma allocate ~100 MB like the default preset would."""
    dict_size = 1 << 16
    while dict_size < len(data) and dict_size < (1 << 23):
        dict_size <<= 1
    return lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32,
                         filters=[{"id": lzma.FILTER_LZMA2, "preset": 9, "dict_size": dict_size}])


class _XZStream:
    """Incrementally decompresses one XZ stream read from `raw` (crates without
    a block table); read(n) never holds more than ~n bytes of output in memory."""

    def __init__(self, raw):
        self.raw = raw
        self.dec = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)

    def read(self, n):
        out = bytearray()
        try:
            while len(out) < n and not self.dec.eof:
                if self.dec.needs_input:
                    chunk = self.raw.read(BLOCK)
                    if not chunk:
                        raise IntegrityError("Crate data ended early")
                else:
                    chunk = b""
                out += self.dec.decompress(chunk, max_length=n - len(out))
        except lzma.LZMAError as e:
            raise IntegrityError(f"Crate data is corrupt: {e}") from e
        return bytes(out)


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---- packing -----------------------------------------------------------------

def pack_crate(entries, out_path: str, name: str = None, manifest=None,
               level: str = "max", comment: str = "", progress=None, threads: int = None) -> dict:
    """Stream files into a new crate at out_path.

    entries: iterable of (source_path, arc_path)
    progress: optional callback(message, fraction_or_None)
    threads: compression threads; default picks from CPU count and free RAM
    """
    if level not in LEVELS:
        raise ValueError(f"Unknown level {level!r}; choose from {list(LEVELS)}")
    say = progress or (lambda *a: None)
    if name is None:
        name = os.path.splitext(os.path.basename(out_path))[0]

    seen, items = set(), []
    for src, arc in entries:
        arc = normalize_arc_path(arc)
        if arc.lower() in seen:
            raise ValueError(f"Duplicate path in crate: {arc}")
        seen.add(arc.lower())
        items.append((src, arc))
    if not items:
        raise ValueError("A crate needs at least one file")
    # Group similar files together for better solid compression.
    items.sort(key=lambda it: (os.path.splitext(it[1])[1].lower(), it[1].lower()))
    total = sum(os.path.getsize(src) for src, _ in items) or 1
    block_level, workers = codec.plan(level, total, threads)

    created = now_iso()
    tmp = out_path + ".part"
    try:
        with open(tmp, "wb") as f:
            out = container.write_header(
                f, MAGIC, {"kind": "crate", "app": "Asset Warehouse", "created": created}, manifest)
            state = {"label": "", "done": 0}

            def block_done(usize):
                state["done"] += usize
                say(state["label"], state["done"] / total)

            writer = codec.BlockWriter(block_level, workers, out.write, block_done)
            files, offset = [], 0
            threads_note = f" · {workers} threads" if workers > 1 else ""
            try:
                for i, (src, arc) in enumerate(items, 1):
                    state["label"] = f"Packing {i}/{len(items)}{threads_note}: {arc}"
                    say(state["label"], state["done"] / total)
                    x86 = os.path.splitext(arc)[1].lower() in codec.X86_EXTS
                    h, size = hashlib.sha256(), 0
                    with open(src, "rb") as fin:
                        while block := fin.read(BLOCK):
                            h.update(block)
                            size += len(block)
                            writer.write(block, x86)
                    files.append({"path": arc, "offset": offset, "size": size,
                                  "mtime": int(os.path.getmtime(src)), "sha256": h.hexdigest()})
                    offset += size
                state["label"] = f"Finishing compression{threads_note}…"
                say(state["label"], state["done"] / total)
                writer.close()
            except BaseException:
                writer.abort()
                raise
            data_len = out.position
            index = {
                "name": name, "comment": comment, "created": created,
                "compression": block_level.codec, "level": level,
                "total_size": offset, "compressed_size": data_len,
                "block_size": block_level.block_size, "blocks": writer.blocks, "files": files,
            }
            index_bytes = xz_compress_small(json.dumps(index, separators=(",", ":")).encode())
            out.write(index_bytes)
            out.write(FOOTER.pack(data_len, len(index_bytes), FOOTER_MAGIC))
            out.close()
        os.replace(tmp, out_path)
    except BaseException:
        _remove_quietly(tmp)
        raise
    return {"files": len(items), "original_size": offset, "crate_size": os.path.getsize(out_path)}


# ---- reading -----------------------------------------------------------------

class CrateReader:
    """Reads a crate from a path or any seekable stream (e.g. a region of a pallet).
    Only the parts that are needed are read and decrypted."""

    def __init__(self, src, source: str = None, owns=None):
        self.container = container.Container(src, MAGIC, "crate")
        self.source = source
        self._file = owns
        self._index = None
        self._body = None
        self._data_len = 0

    @classmethod
    def open(cls, path: str) -> "CrateReader":
        f = open(path, "rb")
        try:
            return cls(f, source=path, owns=f)
        except BaseException:
            f.close()
            raise

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def encrypted(self):
        return self.container.encrypted

    @property
    def key_id(self):
        return self.container.key_id

    def can_unlock(self, keyring) -> bool:
        return self.container.can_open(keyring)

    @property
    def unlocked(self) -> bool:
        return self._index is not None

    def unlock(self, keyring=None) -> dict:
        """Read the index (decrypting only the tail of the crate). Returns it."""
        if self._index is None:
            body = self.container.open_body(keyring)
            if body.size < FOOTER.size:
                raise FormatError("Crate body is truncated")
            body.seek(body.size - FOOTER.size)
            data_len, index_len, magic = FOOTER.unpack(body.read_exact(FOOTER.size))
            if magic != FOOTER_MAGIC or data_len + index_len + FOOTER.size != body.size:
                raise FormatError("Crate index footer is corrupt")
            body.seek(data_len)
            try:
                self._index = json.loads(lzma.decompress(body.read_exact(index_len)).decode("utf-8"))
            except (lzma.LZMAError, ValueError) as e:
                raise FormatError(f"Crate index is corrupt: {e}") from e
            self._body, self._data_len = body, data_len
        return self._index

    @property
    def index(self) -> dict:
        if self._index is None:
            raise RuntimeError("Crate is locked; call unlock() first")
        return self._index

    def metadata(self, keyring=None) -> dict:
        """Public metadata always; private metadata too if the key is available."""
        c = self.container
        meta = {
            "Type": "Crate",
            "Format version": c.version,
            "Created": c.header.get("created", "?"),
            "Crate size": c.total_size,
            "Encrypted": f"Yes (AES-256-GCM, {c.chunk_size // 1024} KiB chunks)" if c.encrypted else "No",
        }
        if c.encrypted:
            meta["Manifest"] = f"{c.manifest_name} ({c.key_id})"
        if self.unlocked or self.can_unlock(keyring):
            idx = self.unlock(keyring)
            total = idx["total_size"]
            meta.update({
                "Name": idx.get("name", ""),
                "Comment": idx.get("comment", "") or "-",
                "Files": len(idx["files"]),
                "Original size": total,
                "Compression": f"{idx['compression']} ({idx.get('level', '?')})",
                "Blocks": self._block_summary(),
                "Ratio": f"{(c.total_size / total * 100):.1f}% of original" if total else "-",
            })
        else:
            meta["Contents"] = "Locked - load the manifest to view"
        return meta

    def _block_summary(self) -> str:
        blocks = self.index.get("blocks")
        if blocks is None:
            return "1 (single stream)"
        stored = sum(1 for b in blocks if b["codec"] == "store")
        return f"{len(blocks)}" + (f" ({stored} stored raw - already compressed data)" if stored else "")

    def _data_stream(self, threads: int = None):
        raw = RegionReader(self._body, 0, self._data_len)
        blocks = self.index.get("blocks")
        if blocks is not None:
            return codec.BlockReader(raw, blocks, threads or codec.plan_read_workers(blocks))
        algo = self.index["compression"]  # crate written before block support
        if algo == "store":
            return raw
        if algo == "lzma2":
            return _XZStream(raw)
        raise FormatError(f"Unknown compression '{algo}'")

    def extract_all(self, dest: str, keyring=None, progress=None) -> int:
        """Stream every file out to dest, verifying each SHA-256. Returns file count."""
        say = progress or (lambda *a: None)
        idx = self.unlock(keyring)
        stream = self._data_stream()
        try:
            return self._extract_from(stream, idx, dest, say)
        finally:
            if hasattr(stream, "close"):
                stream.close()

    def _extract_from(self, stream, idx, dest, say) -> int:
        total = idx["total_size"] or 1
        os.makedirs(dest, exist_ok=True)
        pos = done = count = 0
        for entry in sorted(idx["files"], key=lambda e: e["offset"]):
            target = safe_join(dest, entry["path"])
            if entry["offset"] < pos:
                raise FormatError("Crate index has overlapping files")
            skip = entry["offset"] - pos
            while skip:
                skipped = len(stream.read(min(BLOCK, skip)))
                if not skipped:
                    raise IntegrityError("Crate data ended early")
                skip -= skipped
            os.makedirs(os.path.dirname(target), exist_ok=True)
            h, remaining = hashlib.sha256(), entry["size"]
            try:
                with open(target, "wb") as out:
                    while remaining:
                        block = stream.read(min(BLOCK, remaining))
                        if not block:
                            raise IntegrityError("Crate data ended early")
                        h.update(block)
                        out.write(block)
                        remaining -= len(block)
                        done += len(block)
                        say(f"Extracting {entry['path']}", done / total)
                if h.hexdigest() != entry["sha256"]:
                    raise IntegrityError(f"Checksum mismatch for {entry['path']}")
            except BaseException:
                _remove_quietly(target)
                raise
            if entry.get("mtime"):
                os.utime(target, (entry["mtime"], entry["mtime"]))
            pos = entry["offset"] + entry["size"]
            count += 1
        return count


def format_time(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"
