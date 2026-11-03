"""Phase 0 — the chain out of Core's blk*.dat files.

The slow build fetched blocks over RPC, which is fine at 0.3 blk/s and the
wall at 30. The files Core already keeps are the same bytes without the
round trip, the hex and the single-threaded feed: 725 GB on the same NVMe,
readable while the node is stopped or running.

Two jobs here:

  - **scan**: walk every blk file once, reading only the 8-byte envelope
    (magic, size) and the 80-byte header of each block and seeking past the
    body. Headers carry prev-hash and nBits, which is enough to link the
    chain and weigh it: the best chain is the connected chain from genesis
    with the most cumulative work, exactly Core's rule. Blocks that lost a
    reorg stay in the files; the weighing is what excludes them. The result
    is `blocks.idx`: one fixed 48-byte record per height
    (hash 32, file u32, offset u64, size u32), height = record position,
    plus a JSON manifest beside it.

  - **read**: `BlockIndex` loads the idx (a few tens of MB) and hands any
    phase raw block bytes by height, in order, through per-file handles.

Core v28+ obfuscates the block files with a rolling 8-byte XOR key
(`blocks/xor.dat`), keyed by absolute file offset; every read here passes
through `unxor`. A missing xor.dat means a zero key, and `unxor` is then a
no-op.

The node is still the authority on the main chain: the manifest records the
tip this scan chose, and `--verify-rpc` (or a later phase) compares our
height->hash mapping against `getblockhash` when the node is up. A scan is
marked unverified until that has happened once.

    python -m claimstones.indexer.fastbuild.blocks ^
        --blocks-dir D:\\bitcoin-data\\blocks --out D:\\claimstones\\build\\blocks.idx
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import struct
import sys
import time
from collections import deque
from typing import Iterator, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))

MAGIC_MAINNET = bytes.fromhex("f9beb4d9")
ZERO32 = b"\x00" * 32
IDX_RECORD = struct.Struct("<32sIQI")       # hash, file, offset, size
GENESIS_MAINNET = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


def read_xor_key(blocks_dir: str) -> bytes:
    """The 8-byte obfuscation key, or eight zero bytes for an older datadir."""
    path = os.path.join(blocks_dir, "xor.dat")
    try:
        with open(path, "rb") as f:
            key = f.read(8)
        if len(key) != 8:
            raise RuntimeError(f"{path}: expected 8 bytes, found {len(key)}")
        return key
    except FileNotFoundError:
        return b"\x00" * 8


def unxor(data: bytes, key: bytes, offset: int) -> bytes:
    """Undo Core's rolling XOR for bytes read at an absolute file offset.

    The key repeats every 8 bytes from the start of the file, so the only
    thing the offset decides is the rotation. One big int XOR instead of a
    per-byte loop: a 4 MB block is a single operation, not four million."""
    if key == b"\x00" * 8:
        return data
    n = len(data)
    r = offset & 7
    pattern = (key[r:] + key[:r]) * ((n >> 3) + 2)
    return (int.from_bytes(data, "little")
            ^ int.from_bytes(pattern[:n], "little")).to_bytes(n, "little")


def header_hash(header: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(header).digest()).digest()


def work_from_bits(bits: int) -> int:
    """Chainwork of one header: 2^256 / (target + 1), Core's arithmetic."""
    exp = bits >> 24
    mant = bits & 0x007FFFFF
    target = mant << (8 * (exp - 3)) if exp > 3 else mant >> (8 * (3 - exp))
    if target <= 0:
        return 0
    return (1 << 256) // (target + 1)


def blk_files(blocks_dir: str) -> list[tuple[int, str]]:
    out = []
    for path in glob.glob(os.path.join(blocks_dir, "blk*.dat")):
        m = re.fullmatch(r"blk(\d{5})\.dat", os.path.basename(path))
        if m:
            out.append((int(m.group(1)), path))
    return sorted(out)


def scan(blocks_dir: str, magic: bytes = MAGIC_MAINNET,
         report=print) -> dict[bytes, tuple]:
    """Every block in every blk file: hash -> (prev, work, file, offset, size).

    Duplicates (the same block stored twice after a redownload) keep the
    first copy. A file ends at zero padding (Core preallocates in 16 MB
    steps) or at a record the file is too short to hold (a write the
    process did not survive); both just end that file's scan."""
    key = read_xor_key(blocks_dir)
    blocks: dict[bytes, tuple] = {}
    files = blk_files(blocks_dir)
    t0 = time.time()
    for done, (file_no, path) in enumerate(files):
        file_len = os.path.getsize(path)
        with open(path, "rb", buffering=1 << 20) as f:
            pos = 0
            while pos + 88 <= file_len:
                raw8 = f.read(8)
                # Preallocation padding is raw zeros: Core grows files in
                # 16 MB steps and never XORs bytes it never wrote, so the
                # padding test must run BEFORE unxor -- decoded, zero padding
                # reads as the key pattern and looks like a bad magic.
                if raw8[:4] == b"\x00\x00\x00\x00":
                    break
                envelope = unxor(raw8, key, pos)
                if envelope[:4] != magic:
                    raise RuntimeError(f"{path} at {pos}: bad magic "
                                       f"{envelope[:4].hex()} (wrong network, "
                                       f"wrong xor key, or a corrupt file)")
                size = struct.unpack_from("<I", envelope, 4)[0]
                if pos + 8 + size > file_len or size < 80:
                    break                      # a torn tail write
                header = unxor(f.read(80), key, pos + 8)
                h = header_hash(header)
                if h not in blocks:
                    bits = struct.unpack_from("<I", header, 72)[0]
                    blocks[h] = (header[4:36], work_from_bits(bits),
                                 file_no, pos + 8, size)
                pos += 8 + size
                f.seek(pos)
        if done % 500 == 0 or done == len(files) - 1:
            report(f"  scanned {done + 1}/{len(files)} files, "
                   f"{len(blocks):,} blocks, {time.time() - t0:.0f}s", flush=True)
    return blocks


def best_chain(blocks: dict[bytes, tuple]) -> list[bytes]:
    """The connected chain from genesis with the most cumulative work,
    as a list of hashes ordered by height."""
    children: dict[bytes, list[bytes]] = {}
    genesis = None
    for h, rec in blocks.items():
        prev = rec[0]
        if prev == ZERO32:
            genesis = h
        else:
            children.setdefault(prev, []).append(h)
    if genesis is None:
        raise RuntimeError("no genesis block in the files")

    cumwork: dict[bytes, int] = {genesis: blocks[genesis][1]}
    best_tip, best_work = genesis, cumwork[genesis]
    queue = deque([genesis])
    while queue:
        h = queue.popleft()
        base = cumwork[h]
        for child in children.get(h, ()):
            w = base + blocks[child][1]
            cumwork[child] = w
            if w > best_work:
                best_tip, best_work = child, w
            queue.append(child)

    chain = []
    h = best_tip
    while True:
        chain.append(h)
        prev = blocks[h][0]
        if prev == ZERO32:
            break
        h = prev
    chain.reverse()
    return chain


def write_index(out_path: str, blocks: dict[bytes, tuple],
                chain: list[bytes], blocks_dir: str, seconds: float) -> dict:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb", buffering=1 << 20) as f:
        for h in chain:
            _, _, file_no, offset, size = blocks[h]
            f.write(IDX_RECORD.pack(h, file_no, offset, size))
    os.replace(tmp, out_path)
    manifest = {
        "phase": 0,
        "blocks_dir": os.path.abspath(blocks_dir),
        "xor": read_xor_key(blocks_dir) != b"\x00" * 8,
        "genesis": chain[0][::-1].hex(),
        "tip": chain[-1][::-1].hex(),
        "height": len(chain) - 1,
        "blocks_in_files": len(blocks),
        "orphans": len(blocks) - len(chain),
        "scan_seconds": round(seconds, 1),
        "verified_rpc": False,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(out_path + ".manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest


class BlockIndex:
    """blocks.idx reader: raw block bytes by height, for every later phase."""

    def __init__(self, idx_path: str):
        with open(idx_path, "rb") as f:
            self.data = f.read()
        if len(self.data) % IDX_RECORD.size:
            raise RuntimeError(f"{idx_path}: truncated index")
        with open(idx_path + ".manifest.json", encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.blocks_dir = self.manifest["blocks_dir"]
        self.key = read_xor_key(self.blocks_dir)
        self.tip = len(self.data) // IDX_RECORD.size - 1
        self._handles: dict[int, object] = {}

    def entry(self, height: int) -> tuple[bytes, int, int, int]:
        if not 0 <= height <= self.tip:
            raise IndexError(f"height {height} beyond tip {self.tip}")
        return IDX_RECORD.unpack_from(self.data, height * IDX_RECORD.size)

    def block_hash(self, height: int) -> bytes:
        return self.entry(height)[0]

    def raw(self, height: int) -> bytes:
        h, file_no, offset, size = self.entry(height)
        fh = self._handles.get(file_no)
        if fh is None:
            path = os.path.join(self.blocks_dir, f"blk{file_no:05d}.dat")
            fh = self._handles[file_no] = open(path, "rb")
            if len(self._handles) > 8:       # phases read mostly in order
                old = next(iter(self._handles))
                if old != file_no:
                    self._handles.pop(old).close()
        fh.seek(offset)
        return unxor(fh.read(size), self.key, offset)

    def iter_raw(self, start: int = 0,
                 end: Optional[int] = None) -> Iterator[tuple[int, bytes]]:
        end = self.tip if end is None else min(end, self.tip)
        for height in range(start, end + 1):
            yield height, self.raw(height)

    def close(self) -> None:
        for fh in self._handles.values():
            fh.close()
        self._handles.clear()


def verify_rpc(idx: BlockIndex, rpc, sample_every: int = 10_000) -> None:
    """Compare our height->hash against the node's, every N heights plus the
    tip, and flip verified_rpc in the manifest. Raises on any mismatch."""
    heights = list(range(0, idx.tip + 1, sample_every)) + [idx.tip]
    for h in heights:
        node = rpc("getblockhash", h)
        ours = idx.block_hash(h)[::-1].hex()
        if node != ours:
            raise RuntimeError(f"height {h}: files say {ours}, node says {node}")
    idx.manifest["verified_rpc"] = True
    idx.manifest["verified_heights"] = len(heights)


def main() -> int:
    ap = argparse.ArgumentParser(description="fast build phase 0: scan blk files, "
                                             "pick the best chain, write blocks.idx")
    ap.add_argument("--blocks-dir", default=r"D:\bitcoin-data\blocks")
    ap.add_argument("--out", default=r"D:\claimstones\build\blocks.idx")
    ap.add_argument("--magic", default=MAGIC_MAINNET.hex(),
                    help="network magic, hex (default mainnet)")
    ap.add_argument("--verify-rpc", action="store_true",
                    help="no scan: check the existing index against the "
                         "node's getblockhash and mark the manifest verified")
    ap.add_argument("--rpc-url", default="http://127.0.0.1:8332")
    ap.add_argument("--rpc-cookie", default=r"D:\bitcoin-data\.cookie")
    ap.add_argument("--sample-every", type=int, default=10_000)
    args = ap.parse_args()

    if args.verify_rpc:
        from claimstones.lineage.walker import Rpc, basic_auth_header
        with open(args.rpc_cookie, encoding="utf-8") as f:
            rpc = Rpc(args.rpc_url, basic_auth_header(f.read().strip()))
        idx = BlockIndex(args.out)
        verify_rpc(idx, rpc, args.sample_every)
        with open(args.out + ".manifest.json", "w", encoding="utf-8") as f:
            json.dump(idx.manifest, f, indent=2)
        print(f"verified_rpc: {idx.manifest['verified_heights']} heights "
              f"sampled across 0..{idx.tip:,} all match the node; "
              f"manifest updated", flush=True)
        return 0

    t0 = time.time()
    print(f"scanning {args.blocks_dir} ...", flush=True)
    blocks = scan(args.blocks_dir, bytes.fromhex(args.magic))
    chain = best_chain(blocks)
    manifest = write_index(args.out, blocks, chain, args.blocks_dir,
                           time.time() - t0)
    print(f"genesis {manifest['genesis']}")
    print(f"tip     {manifest['tip']} at height {manifest['height']:,}")
    print(f"{manifest['blocks_in_files']:,} blocks in files, "
          f"{manifest['orphans']:,} off the best chain; "
          f"index {os.path.getsize(args.out):,} bytes, "
          f"{manifest['scan_seconds']}s", flush=True)
    if manifest["genesis"] != GENESIS_MAINNET and args.magic == MAGIC_MAINNET.hex():
        print("WARNING: genesis is not mainnet's; wrong files or a parse fault")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
