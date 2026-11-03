"""Phase 1 — resolve: dense creation ids and death positions.

Every output ever created gets a creation id — its ordinal position in
chain order (block, then tx, then vout) — and every input a spend seq the
same way. The phase is one equi-join of creations against spends, done as
a classic partitioned hash join, all sequential I/O, no store:

  emit     scan blocks once; append fixed-width creation and spend
           records into 256 bucket file pairs by the txid's first byte
  join     one bucket at a time fits RAM: dict outpoint -> creation id,
           stream the spends through it; each match routes straight into
           range files for the two scatters (by spend_seq, by creation_id)
  scatter  each range becomes one numpy array, written once:
             resolved.ids   creation id per input, chain order (u64) —
                            this replaces every lookup of the old build
             death.u32      death height per creation id (u32,
                            unspent = 0xFFFFFFFE)
           plus the death histogram (deaths per 1000-block bin), which
           phase 2 uses to cut epochs that fit the RAM budget.

BIP30 (the duplicated coinbases at heights 91842/91880, spec 9): the later
creation overwrites the earlier, mirroring the old build's INSERT OR
REPLACE. Spends join the surviving creation; the overwritten id is marked
dead at the overwriting height with no spend, so phase 2 frees it with
that epoch and it never reaches the final stream.

Single process for now: the emit scan is the parallelize-if-needed spot
(spec 9 names a process pool; the timed 400k run decides whether it is).

    python -m claimstones.indexer.fastbuild.resolve --end 400000
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import struct
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))

import numpy as np

from claimstones.bitcoin.txparse import parse_block
from claimstones.indexer.fastbuild.blocks import BlockIndex

CREATION = struct.Struct("<32sIQ")       # txid, vout, creation_id      44 B
SPEND = struct.Struct("<32sIQI")         # txid, vout, spend_seq, height 48 B
RES_PART = struct.Struct("<QQ")          # spend_seq, creation_id       16 B
DEATH_PART = struct.Struct("<QI")        # creation_id, death_height    12 B
RES_DT = np.dtype([("s", "<u8"), ("c", "<u8")])
DEATH_DT = np.dtype([("c", "<u8"), ("h", "<u4")])

UNSPENT = 0xFFFFFFFE
N_BUCKETS = 256
FLUSH = 1 << 20
RES_PER_RANGE = 256 << 20                # records: a 2 GB u64 scatter array
DEATH_PER_RANGE = 512 << 20              # records: a 2 GB u32 scatter array
HIST_BIN = 1000

# The two BIP30 coinbases, internal byte order -> the overwriting height.
# 91842 re-mined 91812's txid, 91880 re-mined 91722's.
BIP30_DUP = {
    bytes.fromhex("d5d27987d2a3dfc724e359870c6644b4"
                  "0e497bdc0589a033220fe15429d88599")[::-1]: 91842,
    bytes.fromhex("e3bf3d07d4b0375638d5f1db5255fe07"
                  "ba2c4cb067cd81b84ee974b6585fb468")[::-1]: 91880,
}

_p = functools.partial(print, flush=True)


def fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ")


def bucket_path(bdir: str, kind: str, b: int) -> str:
    return os.path.join(bdir, "buckets", f"{kind}-{b:02x}.bin")


def range_path(bdir: str, kind: str, r: int) -> str:
    return os.path.join(bdir, "buckets", f"{kind}-r{r:04d}.bin")


def _append(path: str, buf: bytearray) -> None:
    """Flush by reopening: 512 held bucket handles would sit exactly on the
    CRT's default fd ceiling, and appends this large amortize the open."""
    with open(path, "ab") as f:
        f.write(buf)
    buf.clear()


def emit(idx: BlockIndex, end: int, bdir: str,
         report_every: int = 20_000, report=_p) -> tuple[int, int]:
    """Scan blocks 0..end; bucket every creation and spend by txid[0].
    Returns (n_creations, n_spends)."""
    bucket_dir = os.path.join(bdir, "buckets")
    os.makedirs(bucket_dir, exist_ok=True)
    for name in os.listdir(bucket_dir):       # stale files poison the join
        os.remove(os.path.join(bucket_dir, name))
    cpaths = [bucket_path(bdir, "cre", b) for b in range(N_BUCKETS)]
    spaths = [bucket_path(bdir, "spd", b) for b in range(N_BUCKETS)]
    cbufs = [bytearray() for _ in range(N_BUCKETS)]
    sbufs = [bytearray() for _ in range(N_BUCKETS)]

    cid = sseq = txs = 0
    t0 = t_win = time.time()
    txs_win = 0
    for height, raw in idx.iter_raw(0, end):
        block = parse_block(raw, height)
        for tx in block.txs:
            if not tx.is_coinbase:
                for inp in tx.inputs:
                    b = inp.prev_txid[0]
                    buf = sbufs[b]
                    buf += SPEND.pack(inp.prev_txid, inp.prev_vout, sseq, height)
                    if len(buf) >= FLUSH:
                        _append(spaths[b], buf)
                    sseq += 1
            txid = tx.txid
            b = txid[0]
            buf = cbufs[b]
            for vout in range(len(tx.outputs)):
                buf += CREATION.pack(txid, vout, cid)
                cid += 1
            if len(buf) >= FLUSH:
                _append(cpaths[b], buf)
        txs += len(block.txs)
        if height % report_every == 0 and height:
            now = time.time()
            dt = now - t_win
            gb = (cid * CREATION.size + sseq * SPEND.size) / 1e9
            eta = (end - height) / (report_every / dt) / 3600 if dt else 0
            report(f"  h={height}  {report_every / dt:,.1f} blk/s  "
                   f"{fmt((txs - txs_win) / dt)} tx/s  cre {fmt(cid)}  "
                   f"spd {fmt(sseq)}  buckets {gb:.2f} GB  eta {eta:.1f} h")
            t_win, txs_win = now, txs
    for b in range(N_BUCKETS):
        if cbufs[b]:
            _append(cpaths[b], cbufs[b])
        if sbufs[b]:
            _append(spaths[b], sbufs[b])
    report(f"emit done: {fmt(cid)} creations, {fmt(sseq)} spends, "
           f"{fmt(txs)} txs, {time.time() - t0:.0f}s")
    return cid, sseq


class _Router:
    """Fixed-width records routed into per-range files by ordinal, so each
    scatter later loads exactly one array's worth."""

    def __init__(self, bdir: str, kind: str, per_range: int):
        self.bdir, self.kind, self.per = bdir, kind, per_range
        self.files: dict[int, object] = {}

    def write(self, ordinal: int, rec: bytes) -> None:
        r = ordinal // self.per
        f = self.files.get(r)
        if f is None:
            f = self.files[r] = open(range_path(self.bdir, self.kind, r),
                                     "wb", buffering=FLUSH)
        f.write(rec)

    def close(self) -> None:
        for f in self.files.values():
            f.close()


def join(bdir: str, end: int, keep: bool = False,
         report=_p) -> tuple[list[int], list[list[int]], int]:
    """Join each bucket's spends against its creations. Returns
    (death histogram, overwritten [creation_id, height] pairs, spends
    joined). The overwritten pairs go into the manifest: phase 2 needs
    them to drop the BIP30-replaced outputs from its live counters."""
    t0 = time.time()
    hist = [0] * (end // HIST_BIN + 1)
    res = _Router(bdir, "res", RES_PER_RANGE)
    dth = _Router(bdir, "dth", DEATH_PER_RANGE)
    spends = 0
    overwritten: list[list[int]] = []
    for b in range(N_BUCKETS):
        cp, sp = bucket_path(bdir, "cre", b), bucket_path(bdir, "spd", b)
        d: dict[bytes, int] = {}
        if os.path.exists(cp):
            with open(cp, "rb") as f:
                data = f.read()
            for txid, vout, cid in CREATION.iter_unpack(data):
                key = txid + vout.to_bytes(4, "little")
                old = d.get(key)
                if old is not None:
                    dup_h = BIP30_DUP.get(txid)
                    if dup_h is None:
                        raise RuntimeError(
                            f"duplicate creation of {txid[::-1].hex()}:{vout} "
                            f"outside the BIP30 pair")
                    dth.write(old, DEATH_PART.pack(old, dup_h))
                    hist[dup_h // HIST_BIN] += 1
                    overwritten.append([old, dup_h])
                d[key] = cid
        if os.path.exists(sp):
            with open(sp, "rb") as f:
                data = f.read()
            for txid, vout, sseq, h in SPEND.iter_unpack(data):
                cid = d.get(txid + vout.to_bytes(4, "little"))
                if cid is None:
                    raise RuntimeError(
                        f"spend {sseq} at height {h} of unknown "
                        f"{txid[::-1].hex()}:{vout}")
                res.write(sseq, RES_PART.pack(sseq, cid))
                dth.write(cid, DEATH_PART.pack(cid, h))
                hist[h // HIST_BIN] += 1
                spends += 1
        if not keep:
            for p in (cp, sp):
                if os.path.exists(p):
                    os.remove(p)
        if (b + 1) % 64 == 0:
            report(f"  join {b + 1}/{N_BUCKETS}  spends {fmt(spends)}  "
                   f"{time.time() - t0:.0f}s")
    res.close()
    dth.close()
    report(f"join done: {fmt(spends)} spends resolved, "
           f"{len(overwritten)} overwritten (BIP30), {time.time() - t0:.0f}s")
    return hist, overwritten, spends


def scatter_resolved(bdir: str, n_spends: int, out_path: str,
                     keep: bool = False, report=_p) -> None:
    """Range files -> resolved.ids: the creation id consumed by each input
    of each tx of each block, in chain order, one u64 each."""
    t0 = time.time()
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as out:
        n_ranges = -(-n_spends // RES_PER_RANGE)
        for r in range(n_ranges):
            base = r * RES_PER_RANGE
            size = min(RES_PER_RANGE, n_spends - base)
            p = range_path(bdir, "res", r)
            recs = np.fromfile(p, dtype=RES_DT)
            if len(recs) != size:
                raise RuntimeError(f"resolved range {r}: {len(recs)} records, "
                                   f"expected {size} — a spend is missing or doubled")
            arr = np.zeros(size, "<u8")
            arr[recs["s"] - base] = recs["c"]
            out.write(arr.tobytes())
            if not keep:
                os.remove(p)
    os.replace(tmp, out_path)
    report(f"resolved.ids: {fmt(n_spends)} inputs, "
           f"{os.path.getsize(out_path) / 1e9:.2f} GB, {time.time() - t0:.0f}s")


def scatter_death(bdir: str, n_creations: int, out_path: str,
                  keep: bool = False, report=_p) -> int:
    """Range files -> death.u32, indexed by creation id. Returns how many
    creations are still alive at the end of the range (the utxo count the
    old build must agree with)."""
    t0 = time.time()
    tmp = out_path + ".tmp"
    alive = 0
    with open(tmp, "wb") as out:
        n_ranges = -(-n_creations // DEATH_PER_RANGE)
        for r in range(n_ranges):
            base = r * DEATH_PER_RANGE
            size = min(DEATH_PER_RANGE, n_creations - base)
            p = range_path(bdir, "dth", r)
            recs = (np.fromfile(p, dtype=DEATH_DT) if os.path.exists(p)
                    else np.empty(0, DEATH_DT))
            arr = np.full(size, UNSPENT, "<u4")
            arr[recs["c"] - base] = recs["h"]
            alive += int((arr == UNSPENT).sum())
            out.write(arr.tobytes())
            if not keep and os.path.exists(p):
                os.remove(p)
    os.replace(tmp, out_path)
    report(f"death.u32: {fmt(n_creations)} creations, alive {fmt(alive)}, "
           f"{os.path.getsize(out_path) / 1e9:.2f} GB, {time.time() - t0:.0f}s")
    return alive


def main() -> int:
    ap = argparse.ArgumentParser(description="fast build phase 1: dense ids, "
                                             "the join, and death positions")
    ap.add_argument("--idx", default=r"D:\claimstones\build\blocks.idx")
    ap.add_argument("--build-dir", default=r"D:\claimstones\build")
    ap.add_argument("--end", type=int, default=None, help="default: chain tip")
    ap.add_argument("--report-every", type=int, default=20_000)
    ap.add_argument("--keep-scratch", action="store_true",
                    help="keep bucket and range files after they are consumed")
    args = ap.parse_args()

    idx = BlockIndex(args.idx)
    end = idx.tip if args.end is None else min(args.end, idx.tip)
    if not idx.manifest.get("verified_rpc"):
        _p("WARNING: blocks.idx is not RPC-verified "
           "(python -m claimstones.indexer.fastbuild.blocks --verify-rpc)")
    _p(f"resolving blocks 0..{end} from {args.idx} "
       f"(chain tip {idx.tip}), phase 1")

    t0 = time.time()
    timings = {}
    t = time.time()
    n_cre, n_spd = emit(idx, end, args.build_dir, args.report_every)
    timings["emit"] = round(time.time() - t, 1)
    t = time.time()
    hist, overwritten_cids, joined = join(args.build_dir, end, args.keep_scratch)
    overwritten = len(overwritten_cids)
    timings["join"] = round(time.time() - t, 1)
    if joined != n_spd:
        raise RuntimeError(f"join resolved {joined} spends, emit saw {n_spd}")
    t = time.time()
    scatter_resolved(args.build_dir, n_spd,
                     os.path.join(args.build_dir, "resolved.ids"),
                     args.keep_scratch)
    alive = scatter_death(args.build_dir, n_cre,
                          os.path.join(args.build_dir, "death.u32"),
                          args.keep_scratch)
    timings["scatter"] = round(time.time() - t, 1)

    manifest = {
        "phase": 1,
        "idx": os.path.abspath(args.idx),
        "end": end,
        "creations": n_cre,
        "spends": n_spd,
        "overwritten_bip30": overwritten,
        "overwritten_cids": overwritten_cids,
        "alive": alive,
        "hist_bin": HIST_BIN,
        "death_histogram": hist,
        "timings_s": timings,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(args.build_dir, "phase-1.manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f)
    _p(f"phase 1 done: {fmt(n_cre)} creations, {fmt(n_spd)} spends, "
       f"{overwritten} overwritten (BIP30), alive {fmt(alive)}, "
       f"{time.time() - t0:.0f}s (emit {timings['emit']:.0f} / "
       f"join {timings['join']:.0f} / scatter {timings['scatter']:.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
