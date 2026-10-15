# Asset Warehouse

Custom archive formats with strong compression and authenticated encryption.

| File | What it is |
|---|---|
| `.crate` | A bundle of files, solid-compressed (LZMA2 or zstd, multi-threaded) and encrypted with AES-256-GCM |
| `.pallet` | A holder for crates, organised as a hierarchy of nodes (`root.assets.images`) |
| `.manifest` | A key. Crates and pallets are encrypted to a manifest and need it to be opened |

## Running

```bash
pip install -r requirements.txt
python main.py
```

Requires Python 3.14+ for the `fast` level (zstd), 3.10+ otherwise. tkinter ships with the standard Windows/macOS installers.
The UI uses a dark theme and the pixel-art icons in `icons/` (mapped to UI roles in
`asset_warehouse/iconset.py`; a missing icon just shows no image).

Explorer icons and a right-click **File Warehouse ▸ Extract crate here** menu are planned:
see [docs/explorer-integration.md](docs/explorer-integration.md).

## Using the app

1. **Manifests tab** — *New Manifest…* creates a key (optionally passphrase-protected) and loads it.
   *Load Manifest…* adds existing ones. Every loaded manifest is available everywhere in the app.
2. **Crates tab** — add files/folders, pick a compression level and a manifest, *Pack Crate…*.
   *Open Crate…* shows metadata; contents appear once the right manifest is loaded. *Unpack…* extracts.
3. **Pallets tab** — build a hierarchy: *Add Node…* (e.g. `root.assets.images`), select a node and
   *Add Crates…*, or *Add Folder Tree…* to mirror a folder (every directory becomes a node holding one
   crate of its files). *Open Pallet…* shows metadata and the hierarchy, down to the files inside each
   crate. *Unpack…* recreates the hierarchy as folders (`root.assets.images` → `assets/images/`),
   optionally unpacking each crate's contents into its node folder.

## Why it compresses better than .zip

Zip compresses each file on its own with Deflate, which can only look back 32 KiB.
A crate is **solid**: all files are concatenated (sorted by extension so similar data is adjacent)
and compressed in large blocks (up to 128 MiB, LZMA2 with up to a 64 MiB dictionary), so repetition
*across* files is exploited. Per block, data that won't compress (audio banks, video, JPEG/PNG…)
is detected with a ~1 ms trial and stored raw instead of wasting time on it, and blocks that are
mostly EXE/DLL code get the x86 BCJ filter, which makes machine code more compressible.

| Level | Codec | Use it for |
|---|---|---|
| `store` | none | already-compressed media, or just encryption |
| `fast` | zstd 9 | quick packs; still clearly smaller than zip |
| `balanced` | LZMA2 preset 6 | nearly `max` ratio, uses every core |
| `max` | LZMA2 preset 9, 64 MiB dictionary | smallest files |

Measured on a 256 MiB mix of DLLs, Python files, docs and already-compressed data
(8-thread CPU, ~3.5 GB RAM free):

| | pack | unpack | size |
|---|---|---|---|
| zip -9 (reference) | 15.6 s | | 38.8% |
| `fast` | 12.0 s | ~5 s | 32.0% |
| `balanced` | 47.8 s | ~7 s | 26.3% |
| `max` | 55.1 s | ~8 s | 26.3% |
| `max` before the speed pass (single thread, preset 9 *extreme*) | 2239 s | 10 s | 25.7% |

"Extreme" was dropped: it gains ~0.1% and is pathologically slow on hard-to-compress data.

## Speed: parallel blocks

Blocks are independent, so they're compressed **and decompressed on a thread pool** (liblzma and
zstd release Python's GIL while working). The thread count adapts to *free RAM*, not just core count:
an LZMA encoder at `max` needs ~11× its dictionary, so on a machine with little free memory `max`
automatically switches to a 32 MiB dictionary to run more threads. Small inputs get smaller blocks
so every core has work. The block layout depends only on the core count, so the `threads` option of
`pack_crate` changes speed, never the output.

## Streaming

Packing and unpacking stream data: file → compressor → encryption → disk. Memory use is bounded by
block size × thread count, never by archive size: roughly 50 MB packing at `fast`, up to ~0.5–1 GB
per thread at `max` (that's what the free-RAM check accounts for), and around 8 decompressed
blocks in flight while unpacking (~170 MB at `fast`).

Encryption is chunked (256 KiB chunks, each separately authenticated), which also gives
**random access**: viewing a crate's contents only decrypts its index, and a crate inside a
pallet can be opened or extracted without decrypting the rest of the pallet.

## Security model

- **Manifest**: random 256-bit master key + a public key id (`SHA-256(key)` prefix).
  With a passphrase, the key is wrapped with AES-256-GCM under a scrypt-derived key
  (N=2^17, r=8, p=1 → 128 MiB per guess, which slows brute-forcing).
- **Per-container keys**: each crate/pallet derives its own key with HKDF-SHA256 from the master key
  and a fresh random 16-byte salt.
- **Chunked AES-256-GCM** (the "STREAM" construction): every 256 KiB chunk is encrypted and
  authenticated separately, with nonce = random 7-byte prefix ‖ chunk counter ‖ last-chunk flag.
  The counter stops chunks being reordered or dropped, the flag stops truncation, and every chunk
  is bound to the public header (its SHA-256 is the associated data), so editing the header fails
  too. Nothing is returned from a chunk until its tag verifies.
- **What an encrypted container reveals**: its size, creation time, and which manifest opens it.
  Names, comments, file names, file sizes and the pallet hierarchy are all inside the encrypted body.
- Every file also has a SHA-256 checked on extraction (and the XZ streams carry CRCs), which is what
  protects unencrypted containers from silent corruption.
- Extraction rejects paths that would escape the destination folder (`..`, absolute paths).

**If you lose a manifest or its passphrase, its data can't be recovered.** Back manifests up, and
don't keep an unprotected manifest next to the crates it opens.

## File formats (version 2)

Both containers share one envelope:

```
magic        8 bytes   \x89CRATE\r\n  or  \x89PALLET\n
version      1 byte    (2)
header_len   uint32 BE
header       JSON: kind, created,
             encryption {cipher, mode:"chunked", chunk_size, kdf, key_id, manifest, salt, nonce_prefix} | null
body         chunked AES-256-GCM stream if encrypted, else plaintext
```

Container key = HKDF-SHA256(manifest key, salt, info = "asset-warehouse/container/" + magic).
Associated data for every chunk = SHA-256(magic + version + header_len + header).
Encrypted chunk i = AES-GCM(key, nonce_prefix ‖ uint32 i ‖ last_flag, plaintext[i·cs : (i+1)·cs]).

The header is written before the data, so anything computed while streaming lives in an
**index at the end of the body**, found through a fixed 24-byte footer
`uint64 data_len | uint64 index_len | 8-byte magic`.

**Crate body**: `blocks | XZ(index JSON) | footer "CRATEIDX"`, where each block is an independent
zstd frame, XZ stream or raw bytes. The index lists name, comment, the block table
(`codec, usize, csize` per block, in order) and per-file `path, offset, size, mtime, sha256`, with
offsets into the concatenated uncompressed stream. Crates made before block support have no block
table (their data is one XZ stream or raw) and are still read.

**Pallet body**: `crate files concatenated | XZ(index JSON) | footer "PALLTIDX"`.
The index lists `nodes` (dotted paths, without the `root.` prefix) and crates with
`name, location, offset, size, sha256, key_id, manifest`. Crates keep their own encryption, so a crate
in an encrypted pallet is encrypted twice, possibly with two different manifests.

Version 1 files (from the first, non-streaming build) aren't readable by this version; repack them.

**Manifest**: JSON with `format, version, name, key_id, created`, plus either `key` (base64) or
`protection {kdf, n, r, p, salt, cipher, nonce, wrapped_key}`.

## Limitations of this basic version

- Crates can't be modified in place; repack to change contents.
- `fast` uses zstd from Python 3.14's standard library; on older Pythons `fast` falls back to LZMA
  and crates made with zstd can't be opened.
- Independent blocks cost a little ratio versus one giant stream (~0.5% in the table above) in
  exchange for parallelism; the free-RAM check means `max` speed varies with what else is running.
- Pallet hierarchy is edited by rebuilding; there's no rename/move yet.

## Library use

```python
from asset_warehouse import Manifest, Keyring, pack_crate, CrateReader, PalletBuilder, PalletReader
from asset_warehouse.crate import collect_folder

key = Manifest.create("TeamKey"); key.save("team.manifest", passphrase="...")
pack_crate(collect_folder("Assets"), "assets.crate", manifest=key)

kr = Keyring(); kr.add(Manifest.load("team.manifest", "..."))
CrateReader.open("assets.crate").extract_all("out", kr)

p = PalletBuilder("game")
p.add_crate_file("assets.crate", "root.assets.images")
p.pack("game.pallet", manifest=key)
PalletReader.open("game.pallet").extract("out", kr, unpack_contents=True)
```
