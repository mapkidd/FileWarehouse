"""Pallets: hierarchical holders for crates (streaming, format v2).

A pallet has a tree of nodes addressed with dotted paths, where "root" is the
pallet itself:  root.assets.images  ->  assets/images when unpacked.
Crates live at nodes, e.g. the crate "icons" at root.assets.images.

Pallet body (plaintext, inside the container's encryption stream):
    crates   raw .crate files, concatenated
    index    XZ-compressed UTF-8 JSON: nodes, crate entries (location, name,
             offset, size, sha256, which manifest each crate needs)
    footer   24 bytes: data_len (u64 BE), index_len (u64 BE), b"PALLTIDX"

Crates are copied in as-is (already compressed; encrypted data doesn't
compress). The pallet layer encrypts the whole body, so the hierarchy and
crate names are hidden too: a crate inside an encrypted pallet is encrypted
twice, possibly with two different manifests. Thanks to chunked encryption a
single crate can be read straight out of the pallet without decrypting the rest.
"""

import hashlib
import json
import lzma
import os
import shutil
import struct
import tempfile
import uuid
import weakref

from . import container
from .crate import CrateReader, pack_crate, safe_join, xz_compress_small
from .errors import FormatError, IntegrityError
from .manifest import now_iso
from .streams import RegionReader, copy_stream

MAGIC = b"\x89PALLET\n"
EXTENSION = ".pallet"
ROOT = "root"
FOOTER = struct.Struct(">QQ8s")
FOOTER_MAGIC = b"PALLTIDX"
_BAD_CHARS = set('/\\:*?"<>|')


def _check_segment(seg: str, what: str) -> str:
    seg = seg.strip()
    if not seg or seg in (".", "..") or _BAD_CHARS & set(seg):
        raise ValueError(f"Invalid {what}: {seg!r}")
    return seg


def normalize_location(location: str) -> str:
    """'root.Assets.Images' / 'Assets.Images' / '' -> 'Assets.Images' / ''."""
    location = (location or "").strip().strip(".")
    if not location:
        return ""
    parts = location.split(".")
    if parts[0].lower() == ROOT:
        parts = parts[1:]
    return ".".join(_check_segment(p, "node name") for p in parts)


def normalize_crate_name(name: str) -> str:
    return _check_segment(name, "crate name")


def address(location: str, name: str = None) -> str:
    parts = [ROOT] + ([location] if location else []) + ([name] if name else [])
    return ".".join(parts)


def ancestors(location: str):
    """'a.b.c' -> ['a', 'a.b', 'a.b.c']"""
    if not location:
        return []
    parts = location.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def location_to_dir(dest: str, location: str) -> str:
    if not location:
        return dest
    return safe_join(dest, "/".join(location.split(".")))


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---- building ----------------------------------------------------------------

class PalletBuilder:
    """Collects nodes and crates (by file path - nothing is held in memory)."""

    def __init__(self, name: str = "pallet", comment: str = ""):
        self.name = name
        self.comment = comment
        self.nodes = set()
        self.crates = []  # dicts: name, location, path, size, encrypted, key_id, manifest, temp
        self._tmpdir = None
        self._finalizer = None

    # nodes
    def add_node(self, location: str) -> str:
        location = normalize_location(location)
        self.nodes.update(ancestors(location))
        return location

    def remove_node(self, location: str) -> None:
        location = normalize_location(location)
        inside = lambda loc: not location or loc == location or loc.startswith(location + ".")
        self.nodes = {n for n in self.nodes if not inside(n)}
        for c in [c for c in self.crates if inside(c["location"])]:
            self._drop(c)

    # crates
    def find_crate(self, location: str, name: str):
        for c in self.crates:
            if c["location"] == location and c["name"].lower() == name.lower():
                return c
        return None

    def add_crate_file(self, path: str, location: str, name: str = None, temp: bool = False) -> dict:
        location = normalize_location(location)
        name = normalize_crate_name(name or os.path.splitext(os.path.basename(path))[0])
        if self.find_crate(location, name):
            raise ValueError(f"A crate named '{name}' already exists at {address(location)}")
        with CrateReader.open(path) as reader:  # validates the header
            entry = {"name": name, "location": location, "path": os.path.abspath(path),
                     "size": reader.container.total_size, "encrypted": reader.encrypted,
                     "key_id": reader.key_id, "manifest": reader.container.manifest_name,
                     "temp": temp}
        self.add_node(location)
        self.crates.append(entry)
        return entry

    def remove_crate(self, location: str, name: str) -> None:
        c = self.find_crate(normalize_location(location), name)
        if c:
            self._drop(c)

    def _drop(self, c):
        self.crates.remove(c)
        if c["temp"]:
            _remove_quietly(c["path"])

    def _temp_path(self) -> str:
        if self._tmpdir is None:
            self._tmpdir = tempfile.mkdtemp(prefix="asset-warehouse-")
            self._finalizer = weakref.finalize(self, shutil.rmtree, self._tmpdir, True)
        return os.path.join(self._tmpdir, uuid.uuid4().hex + ".crate")

    def cleanup(self) -> None:
        """Delete crates this builder created in its temp folder."""
        if self._finalizer is not None:
            self._finalizer()
        self._tmpdir = self._finalizer = None
        self.crates = [c for c in self.crates if not c["temp"]]

    def add_folder_tree(self, folder: str, location: str = "", manifest=None,
                        level: str = "max", progress=None) -> int:
        """Mirror a folder into the pallet: every directory becomes a node and
        the files directly inside it become one crate (named after the
        directory) at that node. Crates are packed to temp files.
        Returns the number of crates created."""
        say = progress or (lambda *a: None)
        folder = os.path.abspath(folder)
        base = normalize_location(location)
        top = os.path.basename(folder).replace(".", "_")
        count = 0
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            rel = os.path.relpath(dirpath, folder)
            segs = [top] + ([] if rel == "." else [s.replace(".", "_") for s in rel.split(os.sep)])
            loc = self.add_node(".".join(([base] if base else []) + segs))
            if not filenames:
                continue
            crate_name, tmp = segs[-1], self._temp_path()
            addr = address(loc)
            pack_crate([(os.path.join(dirpath, f), f) for f in sorted(filenames)], tmp,
                       name=crate_name, manifest=manifest, level=level,
                       progress=lambda msg, frac=None: say(f"{addr}: {msg}", frac))
            self.add_crate_file(tmp, loc, crate_name, temp=True)
            count += 1
        return count

    # output
    def pack(self, out_path: str, manifest=None, progress=None) -> dict:
        say = progress or (lambda *a: None)
        crates = sorted(self.crates, key=lambda c: (c["location"], c["name"].lower()))
        total = sum(c["size"] for c in crates) or 1
        created = now_iso()
        tmp = out_path + ".part"
        try:
            with open(tmp, "wb") as f:
                out = container.write_header(
                    f, MAGIC, {"kind": "pallet", "app": "Asset Warehouse", "created": created}, manifest)
                entries, done = [], 0
                for c in crates:
                    offset = out.position
                    addr = address(c["location"], c["name"])

                    def tick(n, addr=addr):
                        nonlocal done
                        done += n
                        say(f"Loading {addr}", done / total)

                    with open(c["path"], "rb") as fin:
                        size = os.fstat(fin.fileno()).st_size
                        digest = copy_stream(fin, out.write, size, tick)
                    entries.append({
                        "name": c["name"], "location": c["location"], "offset": offset,
                        "size": size, "sha256": digest, "encrypted": c["encrypted"],
                        "key_id": c["key_id"], "manifest": c["manifest"],
                    })
                data_len = out.position
                index = {"name": self.name, "comment": self.comment, "created": created,
                         "nodes": sorted(self.nodes), "crates": entries}
                index_bytes = xz_compress_small(json.dumps(index, separators=(",", ":")).encode())
                out.write(index_bytes)
                out.write(FOOTER.pack(data_len, len(index_bytes), FOOTER_MAGIC))
                out.close()
            os.replace(tmp, out_path)
        except BaseException:
            _remove_quietly(tmp)
            raise
        return {"crates": len(crates), "nodes": len(self.nodes),
                "pallet_size": os.path.getsize(out_path)}


# ---- reading -----------------------------------------------------------------

class PalletReader:
    def __init__(self, src, source: str = None, owns=None):
        self.container = container.Container(src, MAGIC, "pallet")
        self.source = source
        self._file = owns
        self._index = None
        self._body = None
        self._data_len = 0

    @classmethod
    def open(cls, path: str) -> "PalletReader":
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
        if self._index is None:
            body = self.container.open_body(keyring)
            if body.size < FOOTER.size:
                raise FormatError("Pallet body is truncated")
            body.seek(body.size - FOOTER.size)
            data_len, index_len, magic = FOOTER.unpack(body.read_exact(FOOTER.size))
            if magic != FOOTER_MAGIC or data_len + index_len + FOOTER.size != body.size:
                raise FormatError("Pallet index footer is corrupt")
            body.seek(data_len)
            try:
                self._index = json.loads(lzma.decompress(body.read_exact(index_len)).decode("utf-8"))
            except (lzma.LZMAError, ValueError) as e:
                raise FormatError(f"Pallet index is corrupt: {e}") from e
            self._body, self._data_len = body, data_len
        return self._index

    @property
    def index(self) -> dict:
        if self._index is None:
            raise RuntimeError("Pallet is locked; call unlock() first")
        return self._index

    def metadata(self, keyring=None) -> dict:
        c = self.container
        meta = {
            "Type": "Pallet",
            "Format version": c.version,
            "Created": c.header.get("created", "?"),
            "Pallet size": c.total_size,
            "Encrypted": f"Yes (AES-256-GCM, {c.chunk_size // 1024} KiB chunks)" if c.encrypted else "No",
        }
        if c.encrypted:
            meta["Manifest"] = f"{c.manifest_name} ({c.key_id})"
        if self.unlocked or self.can_unlock(keyring):
            idx = self.unlock(keyring)
            needed = {}
            for e in idx["crates"]:
                if e.get("key_id"):
                    needed[e["key_id"]] = e.get("manifest") or "?"
            meta.update({
                "Name": idx.get("name", ""),
                "Comment": idx.get("comment", "") or "-",
                "Nodes": len(idx["nodes"]),
                "Crates": len(idx["crates"]),
                "Crate manifests": ", ".join(f"{n} ({k})" for k, n in needed.items()) or "none",
            })
        else:
            meta["Contents"] = "Locked - load the manifest to view"
        return meta

    def crate_region(self, entry: dict) -> RegionReader:
        self.index  # ensure unlocked
        if entry["offset"] + entry["size"] > self._data_len:
            raise FormatError(f"Crate {address(entry['location'], entry['name'])} lies outside the pallet")
        return RegionReader(self._body, entry["offset"], entry["size"])

    def crate_reader(self, entry: dict) -> CrateReader:
        """Open a crate in place - reads straight from the pallet, no copy."""
        return CrateReader(self.crate_region(entry), source=address(entry["location"], entry["name"]))

    def find(self, crate_address: str):
        for e in self.index["crates"]:
            if address(e["location"], e["name"]).lower() == crate_address.lower():
                return e
        return None

    def copy_crate(self, entry: dict, out_path: str, on_block=None) -> None:
        """Stream one crate out to a .crate file, verifying its checksum."""
        tmp = out_path + ".part"
        try:
            with open(tmp, "wb") as out:
                digest = copy_stream(self.crate_region(entry), out.write, entry["size"], on_block)
            if digest != entry["sha256"]:
                raise IntegrityError(f"Checksum mismatch for crate {address(entry['location'], entry['name'])}")
            os.replace(tmp, out_path)
        except BaseException:
            _remove_quietly(tmp)
            raise

    def extract(self, dest: str, keyring=None, unpack_contents: bool = False,
                progress=None) -> dict:
        """Recreate the hierarchy as folders under dest.

        unpack_contents=False: each crate is written as <node>/<name>.crate
        unpack_contents=True:  crate contents are extracted into <node>/;
                               crates whose manifest isn't loaded are written
                               as .crate files instead and reported as skipped.
        """
        say = progress or (lambda *a: None)
        idx = self.unlock(keyring)
        os.makedirs(dest, exist_ok=True)
        for node in idx["nodes"]:
            os.makedirs(location_to_dir(dest, node), exist_ok=True)

        report = {"crates_written": 0, "crates_unpacked": 0, "files": 0, "skipped": []}
        n = len(idx["crates"]) or 1
        for i, e in enumerate(idx["crates"]):
            addr = address(e["location"], e["name"])
            node_dir = location_to_dir(dest, e["location"])
            os.makedirs(node_dir, exist_ok=True)
            step = lambda msg, frac=None, i=i: say(msg, (i + (frac or 0)) / n)
            if unpack_contents:
                reader = self.crate_reader(e)
                if reader.can_unlock(keyring):
                    report["files"] += reader.extract_all(node_dir, keyring, step)
                    report["crates_unpacked"] += 1
                    continue
                report["skipped"].append(f"{addr} (needs manifest '{e.get('manifest')}')")
            done = 0

            def tick(nbytes, addr=addr, size=e["size"] or 1):
                nonlocal done
                done += nbytes
                step(f"Writing {addr}.crate", done / size)

            self.copy_crate(e, safe_join(node_dir, e["name"] + ".crate"), tick)
            report["crates_written"] += 1
        return report
