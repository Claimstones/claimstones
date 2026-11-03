"""Phase 3 — the external sort, the LMDB bulk load, and the tip handover.

The final stream arrives in creation-id order. One external sort by
(txid, vout) makes the bulk load append-ordered: records are read once,
cut into sorted chunk files, and merged straight into LMDB with
MDB_APPEND, so the B-tree is written left to right at sequential speed.
The address table is built the same way in the same pass: (script,
outpoint) pairs spill to their own chunks and merge-append under dupsort.

Store layout (spec, decided 2026-10-26) — one environment, sub-databases:

    utxo   txid(32) + vout(4 BE)  ->  script_len u32 + script + runs
           (schema-3 12-byte pairs; run count implicit in record length;
           u32 because post-2025 scripts can pass 64 KiB)
    addr   sha256(script) -> outpoint, dupsort. The spec said raw script,
           but scripts are unbounded and may be empty while LMDB keys are
           neither (511-byte cap); lookups are exact-script, so hashing
           the query loses nothing.
    undo   the tip daemon's undo log, empty at load
    meta   counters as in birth.py: height, utxos, runs, schema

`iter_sorted` is also the acceptance gate's walk (compare.py): the gate
must not depend on LMDB, so lmdb imports stay inside load().
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import heapq
import json
import os
import struct
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))

from claimstones.indexer.fastbuild import spill

_p = functools.partial(print, flush=True)

KEY = struct.Struct(">32sI")             # txid + vout, big-endian: byte order
                                         # IS (txid, vout) numeric order
CHUNK_HEAD = struct.Struct("<II")        # script_len, run_count
CHUNK_BYTES = 256 << 20                  # payload per in-RAM sort chunk


def fmt(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


def _write_chunk(records: list, dirpath: str, n: int) -> str:
    """Sort one chunk by key and write it: key(36) + lens + script + runs."""
    records.sort(key=lambda r: r[0])
    path = os.path.join(dirpath, f"chunk-{n:04d}.sorted")
    with open(path, "wb", buffering=1 << 20) as f:
        for key, script, runs in records:
            f.write(key + CHUNK_HEAD.pack(len(script), len(runs) // 12)
                    + script + runs)
    return path


def _read_chunk(path: str):
    """Yield (key, script, runs) from one sorted chunk, then delete it."""
    with open(path, "rb", buffering=1 << 18) as f:
        while True:
            head = f.read(KEY.size + CHUNK_HEAD.size)
            if not head:
                break
            slen, n = CHUNK_HEAD.unpack_from(head, KEY.size)
            yield head[:KEY.size], f.read(slen), f.read(n * 12)
    os.remove(path)


def iter_sorted(stream_path: str, scratch_dir: str,
                chunk_bytes: int = CHUNK_BYTES, report=_p):
    """Yield (txid, vout, script, runs) from a final stream in (txid, vout)
    order: the external sort. Scratch holds one copy of the stream."""
    os.makedirs(scratch_dir, exist_ok=True)
    for stale in os.listdir(scratch_dir):
        os.remove(os.path.join(scratch_dir, stale))
    chunks: list[str] = []
    records: list = []
    acc = rows = 0
    t0 = time.time()
    for _cid, vout, txid, script, runs in spill.iter_final(stream_path):
        records.append((KEY.pack(txid, vout), script, runs))
        acc += KEY.size + len(script) + len(runs)
        rows += 1
        if acc >= chunk_bytes:
            chunks.append(_write_chunk(records, scratch_dir, len(chunks)))
            records, acc = [], 0
    if records:
        chunks.append(_write_chunk(records, scratch_dir, len(chunks)))
    del records
    report(f"  sort: {fmt(rows)} rows into {len(chunks)} chunks, "
           f"{time.time() - t0:.0f}s; merging")
    for key, script, runs in heapq.merge(*(map(_read_chunk, chunks)),
                                         key=lambda r: r[0]):
        txid, vout = KEY.unpack(key)
        yield txid, vout, script, runs


def load(stream_path: str, db_path: str, scratch_dir: str, height: int,
         map_gb: int = 120, report=_p) -> tuple[int, int]:
    """Bulk-load the sorted stream into LMDB; returns (utxos, runs)."""
    import lmdb                           # only phase 3 proper needs it

    t0 = time.time()
    env = lmdb.open(db_path, map_size=map_gb << 30, subdir=True,
                    max_dbs=4, writemap=True, metasync=False, sync=False)
    d_utxo = env.open_db(b"utxo")
    d_addr = env.open_db(b"addr", dupsort=True)
    env.open_db(b"undo")
    d_meta = env.open_db(b"meta")

    addr_dir = os.path.join(scratch_dir, "addr")
    os.makedirs(addr_dir, exist_ok=True)
    for stale in os.listdir(addr_dir):
        os.remove(os.path.join(addr_dir, stale))
    addr_chunks: list[str] = []
    pairs: list[tuple[bytes, bytes]] = []
    acc = utxos = runs = 0

    def flush_pairs() -> None:
        nonlocal pairs, acc
        pairs.sort()
        path = os.path.join(addr_dir, f"addr-{len(addr_chunks):04d}.sorted")
        with open(path, "wb", buffering=1 << 20) as f:
            for shash, out in pairs:
                f.write(shash + out)                 # fixed 32 + 36 bytes
        addr_chunks.append(path)
        pairs, acc = [], 0

    with env.begin(write=True, db=d_utxo) as txn:
        cur = txn.cursor()
        for txid, vout, script, runlist in iter_sorted(
                stream_path, os.path.join(scratch_dir, "utxo"), report=report):
            key = txid + vout.to_bytes(4, "big")
            cur.put(key, struct.pack("<I", len(script)) + script + runlist,
                    append=True)
            pairs.append((hashlib.sha256(script).digest(), key))
            acc += 68
            utxos += 1
            runs += len(runlist) // 12
            if acc >= CHUNK_BYTES:
                flush_pairs()
    if pairs:
        flush_pairs()
    report(f"  utxo table: {fmt(utxos)} rows, {fmt(runs)} runs, "
           f"{time.time() - t0:.0f}s")

    def read_addr(path: str):
        with open(path, "rb", buffering=1 << 18) as f:
            while rec := f.read(68):
                yield rec[:32], rec[32:]
        os.remove(path)

    t1 = time.time()
    with env.begin(write=True, db=d_addr) as txn:
        cur = txn.cursor()
        for script, out in heapq.merge(*(map(read_addr, addr_chunks))):
            cur.put(script, out, append=True)   # MDB_APPENDDUP under dupsort
    report(f"  addr table: {time.time() - t1:.0f}s")

    with env.begin(write=True, db=d_meta) as txn:
        for k, v in (("schema", "3"), ("height", str(height)),
                     ("utxos", str(utxos)), ("runs", str(runs))):
            txn.put(k.encode(), str(v).encode())
    env.sync(True)
    env.close()
    return utxos, runs


def main() -> int:
    ap = argparse.ArgumentParser(description="fast build phase 3: sort and "
                                             "bulk-load LMDB")
    ap.add_argument("--stream", default=r"D:\claimstones\build\final.stream")
    ap.add_argument("--build-dir", default=r"D:\claimstones\build")
    ap.add_argument("--db", default=r"D:\claimstones\build\birth.lmdb")
    ap.add_argument("--scratch", default=None,
                    help="default: <build-dir>\\sort-scratch")
    ap.add_argument("--map-gb", type=int, default=120)
    args = ap.parse_args()
    with open(os.path.join(args.build_dir, "phase-2.manifest.json"),
              encoding="utf-8") as f:
        man2 = json.load(f)
    scratch = args.scratch or os.path.join(args.build_dir, "sort-scratch")
    t0 = time.time()
    _p(f"loading {args.stream} (height {man2['end']}) into {args.db}, "
       f"phase 3")
    utxos, runs = load(args.stream, args.db, scratch, man2["end"],
                       args.map_gb)
    if (utxos, runs) != (man2["utxos"], man2["runs"]):
        raise RuntimeError(f"loaded {utxos} utxos / {runs} runs, phase 2 "
                           f"wrote {man2['utxos']} / {man2['runs']}")
    with open(os.path.join(args.build_dir, "phase-3.manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump({"phase": 3, "height": man2["end"], "utxos": utxos,
                   "runs": runs, "db": args.db,
                   "seconds": round(time.time() - t0, 1),
                   "created": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    _p(f"phase 3 done: {fmt(utxos)} utxos, {fmt(runs)} runs, "
       f"{time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
