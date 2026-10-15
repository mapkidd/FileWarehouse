"""Block compression engine: parallel and memory-aware.

A crate's data stream is cut into independent blocks, and each block is
compressed on its own by a thread pool (liblzma and zstd release the GIL while
they work, so threads give real parallelism). Blocks are written in order, so
the output is identical whatever the thread count.

Per block the engine also:
  - stores data raw when a quick trial shows it won't compress (already
    compressed media: audio banks, video, PNG/JPEG...) - LZMA would grind
    through it for nothing
  - adds the x86 BCJ filter in front of LZMA2 when the block is mostly
    EXE/DLL code, which makes machine code noticeably more compressible

The thread count is capped by *available* RAM: an LZMA encoder at "max" needs
roughly 11x its 64 MiB dictionary, so blindly using every core could exhaust
memory.
"""

import ctypes
import dataclasses
import lzma
import math
import os
import sys
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

try:
    from compression import zstd  # Python 3.14+
except ImportError:  # pragma: no cover - older Pythons fall back to LZMA
    zstd = None

from .errors import FormatError, IntegrityError

MiB = 1 << 20

X86_EXTS = {".exe", ".dll", ".pyd", ".sys", ".ocx", ".drv", ".cpl", ".scr", ".so", ".node"}


@dataclass(frozen=True)
class Level:
    codec: str             # "store" | "zstd" | "lzma2"
    setting: int = 0       # zstd level or LZMA preset
    dict_size: int = 0     # LZMA dictionary size
    block_size: int = 16 * MiB

    def worker_memory(self) -> int:
        """Rough peak RAM for one compressing worker (encoder + input + output)."""
        if self.codec == "lzma2":
            factor = 11.5 if self.setting >= 4 else 7.5  # bt4 vs hc4 match finder
            # compress_block never uses a dictionary larger than the block
            encoder = int(min(self.dict_size, self.block_size) * factor) + 8 * MiB
        elif self.codec == "zstd":
            encoder = 32 * MiB
        else:
            encoder = 0
        return encoder + 2 * self.block_size


# Measured on a mixed 64 MiB sample (DLLs, text, Python files), single thread:
#   zstd 3      33% of original   ~110 MiB/s
#   zstd 9      31%               ~35 MiB/s    <- "fast": with 8 threads, file reading /
#                                                 hashing is the bottleneck, so zstd 9
#                                                 costs little more wall time than zstd 3
#   lzma 6      27%               ~2.5 MiB/s
#   lzma 9e     27% (-0.1%)       ~1.6 MiB/s   <- "extreme" isn't worth 50% more time, and
#                                                 is pathologically slow on hard-to-compress data
LEVELS = {
    "store": Level("store", block_size=16 * MiB),
    "fast": (Level("zstd", 9, block_size=16 * MiB) if zstd is not None
             else Level("lzma2", 1, 1 * MiB, 16 * MiB)),
    "balanced": Level("lzma2", 6, 8 * MiB, 32 * MiB),
    "max": Level("lzma2", 9, 64 * MiB, 128 * MiB),
}
# "max" with half the dictionary: ~half the RAM per thread, used when memory
# would otherwise limit "max" to very few threads.
MAX_LEAN = Level("lzma2", 9, 32 * MiB, 64 * MiB)


def available_memory():
    """Bytes of RAM currently available, or None if unknown."""
    try:
        if sys.platform == "win32":
            class MemStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virt", ctypes.c_ulonglong), ("avail_virt", ctypes.c_ulonglong),
                            ("avail_ext", ctypes.c_ulonglong)]
            st = MemStatus()
            st.length = ctypes.sizeof(MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.avail_phys
        elif os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        else:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _memory_budget() -> int:
    avail = available_memory()
    return int(avail * 0.6) if avail else 2 << 30


def _fit_blocks(lvl: Level, total: int, want: int) -> Level:
    """Shrink blocks for inputs too small to give each thread its own block,
    but never below half the dictionary (or 4 MiB) to keep the ratio."""
    if lvl.codec == "store" or math.ceil(total / lvl.block_size) >= want:
        return lvl
    floor = max(4 * MiB, lvl.dict_size // 2)
    target = math.ceil(total / want / MiB) * MiB
    return dataclasses.replace(lvl, block_size=max(floor, min(lvl.block_size, target)))


def plan(level_name: str, total: int, threads: int = None):
    """Pick the block settings and worker count for packing `total` bytes."""
    cpus = os.cpu_count() or 1
    # Block layout depends on the core count only (not `threads`), so choosing
    # a thread count changes speed, never the output.
    level = _fit_blocks(LEVELS[level_name], total, cpus)

    def workers_for(lvl):
        blocks = max(1, math.ceil(total / lvl.block_size))
        if threads:
            return max(1, min(threads, blocks))
        if lvl.codec == "store":
            return 1
        return max(1, min(cpus, blocks, _memory_budget() // lvl.worker_memory()))

    workers = workers_for(level)
    if level_name == "max" and not threads:
        lean_level = _fit_blocks(MAX_LEAN, total, cpus)
        lean = workers_for(lean_level)
        want = min(cpus, max(1, math.ceil(total / level.block_size)), 4)
        if workers < want and lean > workers:
            level, workers = lean_level, lean
    return level, workers


def plan_read_workers(blocks) -> int:
    if len(blocks) <= 1:
        return 1
    largest = max(b["usize"] for b in blocks)
    per_worker = 2 * largest + 72 * MiB  # output + input + decoder dictionary
    return max(1, min(os.cpu_count() or 1, len(blocks), _memory_budget() // per_worker))


# ---- per-block codecs --------------------------------------------------------

_SAMPLE, _N_SAMPLES = 64 * 1024, 8


def looks_incompressible(data) -> bool:
    """~1 ms trial: compress 8 slices spread over the block with a very fast codec."""
    n = len(data)
    if n < 4 * _SAMPLE * _N_SAMPLES:
        return False  # small block: just compress it
    step = n // _N_SAMPLES
    sample = b"".join(bytes(data[i * step:i * step + _SAMPLE]) for i in range(_N_SAMPLES))
    test = zstd.compress(sample, level=1) if zstd is not None else lzma.compress(sample, preset=0)
    return len(test) >= 0.985 * len(sample)


def compress_block(level: Level, data, x86: bool = False):
    """Returns (codec, payload). Falls back to 'store' when compression doesn't help."""
    if level.codec == "store" or not data or looks_incompressible(data):
        return "store", data
    if level.codec == "zstd":
        out = zstd.compress(data, options={zstd.CompressionParameter.compression_level: level.setting,
                                           zstd.CompressionParameter.checksum_flag: 1})
    else:
        dict_size = 1 << 16
        while dict_size < len(data) and dict_size < level.dict_size:
            dict_size <<= 1
        filters = ([{"id": lzma.FILTER_X86}] if x86 else []) + [
            {"id": lzma.FILTER_LZMA2, "preset": level.setting, "dict_size": dict_size}]
        out = lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, filters=filters)
    if len(out) >= len(data):
        return "store", data
    return level.codec, out


_ZSTD_ERRORS = (getattr(zstd, "ZstdError", lzma.LZMAError),) if zstd is not None else ()


def decompress_block(codec: str, raw: bytes, usize: int) -> bytes:
    try:
        if codec == "store":
            out = raw
        elif codec == "lzma2":
            dec = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
            out = dec.decompress(raw, max_length=usize + 1)
            if not dec.eof or dec.unused_data:
                raise IntegrityError("Crate block is corrupt (bad XZ stream)")
        elif codec == "zstd":
            if zstd is None:
                raise FormatError("This crate uses zstd compression, which needs Python 3.14 or newer")
            dec = zstd.ZstdDecompressor()
            out = dec.decompress(raw, max_length=usize + 1)
            if not dec.eof:
                raise IntegrityError("Crate block is corrupt (bad zstd frame)")
        else:
            raise FormatError(f"Unknown compression '{codec}'")
    except (lzma.LZMAError, *_ZSTD_ERRORS) as e:
        raise IntegrityError(f"Crate data is corrupt: {e}") from e
    if len(out) != usize:
        raise IntegrityError("Crate block has the wrong size")
    return out


# ---- parallel pipeline -------------------------------------------------------

def _done(value) -> Future:
    f = Future()
    f.set_result(value)
    return f


class BlockWriter:
    """write() bytes in; full blocks are compressed on a thread pool and passed to
    `sink` in order. At most workers+1 blocks are in flight, bounding memory."""

    def __init__(self, level: Level, workers: int, sink, on_block=None):
        self.level, self.sink, self.on_block = level, sink, on_block
        self.ex = ThreadPoolExecutor(workers, thread_name_prefix="crate-pack") if workers > 1 else None
        self.max_pending = workers + 1
        self.pending = deque()
        self.blocks = []
        self._buf = bytearray()
        self._x86 = 0

    def write(self, data, x86: bool = False):
        mv = memoryview(data)
        bs = self.level.block_size
        while len(mv):
            take = mv[:bs - len(self._buf)]
            self._buf += take
            if x86:
                self._x86 += len(take)
            mv = mv[len(take):]
            if len(self._buf) >= bs:
                self._submit()

    def _submit(self):
        data, x86 = self._buf, self._x86 * 2 >= len(self._buf)
        self._buf, self._x86 = bytearray(), 0
        if self.ex is not None:
            fut = self.ex.submit(compress_block, self.level, data, x86)
        else:
            fut = _done(compress_block(self.level, data, x86))
        self.pending.append((fut, len(data)))
        self._drain(self.max_pending)

    def _drain(self, keep):
        while len(self.pending) > keep:
            fut, usize = self.pending.popleft()
            codec, payload = fut.result()
            self.sink(payload)
            self.blocks.append({"codec": codec, "usize": usize, "csize": len(payload)})
            if self.on_block:
                self.on_block(usize)

    def close(self):
        try:
            if self._buf:
                self._submit()
            self._drain(0)
        finally:
            self.abort()

    def abort(self):
        if self.ex is not None:
            self.ex.shutdown(wait=True, cancel_futures=True)
            self.ex = None


class BlockReader:
    """Sequential read() over the decompressed blocks, decompressing upcoming
    blocks on a thread pool. Compressed bytes are read on the calling thread
    (the decrypting source stream isn't thread-safe)."""

    def __init__(self, src, blocks, workers: int):
        self.src, self.blocks = src, blocks
        self.offsets, off = [], 0
        for b in blocks:
            self.offsets.append(off)
            off += b["csize"]
        if off != src.size:
            raise FormatError("Crate block table doesn't match the data size")
        self.ex = ThreadPoolExecutor(workers, thread_name_prefix="crate-unpack") if workers > 1 else None
        self.ahead = workers
        self.pending = deque()
        self.next = 0
        self.cur, self.pos = memoryview(b""), 0

    def _fill(self):
        while self.next < len(self.blocks) and len(self.pending) < self.ahead:
            b = self.blocks[self.next]
            self.src.seek(self.offsets[self.next])
            raw = self.src.read_exact(b["csize"])
            if self.ex is not None:
                self.pending.append(self.ex.submit(decompress_block, b["codec"], raw, b["usize"]))
            else:
                self.pending.append(_done(decompress_block(b["codec"], raw, b["usize"])))
            self.next += 1

    def read(self, n):
        out = bytearray()
        while len(out) < n:
            if self.pos >= len(self.cur):
                self._fill()
                if not self.pending:
                    break
                self.cur, self.pos = memoryview(self.pending.popleft().result()), 0
                self._fill()  # keep the pool busy while this block is consumed
            piece = self.cur[self.pos:self.pos + n - len(out)]
            out += piece
            self.pos += len(piece)
        return bytes(out)

    def close(self):
        if self.ex is not None:
            self.ex.shutdown(wait=True, cancel_futures=True)
            self.ex = None
