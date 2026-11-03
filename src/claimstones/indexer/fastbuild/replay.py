"""Phase 2 — replay with lookahead: the index is written, once, in order.

The run arithmetic is exactly the settled model (runcore: positional FIFO
runs, adjacent-only merge, fee tails into the coinbase stream, unclaimed
subsidy never born). What phase 1 bought is that nothing is ever looked
up: every input's creation id arrives on the resolved stream in chain
order, every output's death height streams by in the death array, and
both route straight to flat arrays.

Slot addressing, the no-hash-map trick: an input always dies *now*, so
its runs are in the current epoch's table. The table is a flat list over
the epoch's dying population — the sorted array ids_e of every creation
id whose death height falls in the epoch — and a creation id becomes a
slot through one vectorized np.searchsorted per block. Outputs route the
same way: die this epoch -> table slot; die in a later epoch -> that
epoch's spill file; never die -> the final stream.

BIP30: the two overwritten creations (ids in phase 1's manifest) are
dropped from the table and the live counters at their overwriting
heights, mirroring the old build's INSERT OR REPLACE.

--reference points at an old build's log: utxo and run counts are then
compared at every matching report height. A difference is reported and
counted (manifest: ref_mismatches), never fatal — this build is the
defining code and the reference a temporary side check, to be removed;
it must not be able to abort anything.

A checkpoint is written at every epoch boundary (phase-2.checkpoint.json:
counters plus byte offsets into resolved.ids, final.stream.tmp and every
spill file); a rerun resumes there after truncating the files back. Added
2026-11-01 after two full phase-2 losses to crashes past h=600k. Delete
the checkpoint to force a fresh start.

    python -m claimstones.indexer.fastbuild.replay ^
        --reference D:\\claimstones\\slowbuild\\logs\\birth-2026-10-21.log
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))

import numpy as np

from claimstones.indexer import runcore
from claimstones.indexer.fastbuild import parallel, spill
from claimstones.indexer.fastbuild.blocks import BlockIndex
from claimstones.lineage.walker import subsidy_sats

UNSPENT = 0xFFFFFFFE
RUN_SIZE = 12

_p = functools.partial(print, flush=True)

# the old build's report line, as far as the comparable counters
# counts are space-grouped ("1 172 103 540"); fields are two spaces apart,
# so a greedy single-space group captures a whole number and nothing more
REF_RE = re.compile(r"h=(\d+)\s+[\d.]+ blk/s\s+[\d ]+tx/s\s+"
                    r"utxos (\d+(?: \d+)*)\s+runs (\d+(?: \d+)*)")


def fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ")


class U64Stream:
    """Sequential reader of a u64 file (resolved.ids), in numpy chunks."""

    def __init__(self, path: str, chunk: int = 1 << 22):
        self.f = open(path, "rb")
        self.chunk = chunk
        self.buf = np.empty(0, "<u8")
        self.pos = 0
        self.taken = 0                        # u64s handed out, for resume

    def seek_to(self, taken: int) -> None:
        self.f.seek(taken * 8)
        self.buf = np.empty(0, "<u8")
        self.pos = 0
        self.taken = taken

    def take(self, n: int) -> np.ndarray:
        self.taken += n
        parts = []
        while n:
            if self.pos == len(self.buf):
                self.buf = np.fromfile(self.f, "<u8", self.chunk)
                self.pos = 0
                if not len(self.buf):
                    raise RuntimeError("resolved.ids exhausted early — "
                                       "it does not match these blocks")
            m = min(n, len(self.buf) - self.pos)
            parts.append(self.buf[self.pos:self.pos + m])
            self.pos += m
            n -= m
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def assert_exhausted(self) -> None:
        if self.pos != len(self.buf) or len(np.fromfile(self.f, "<u8", 1)):
            raise RuntimeError("resolved.ids has records left over — "
                               "it does not match these blocks")

    def close(self) -> None:
        self.f.close()


def load_reference(path: str) -> dict[int, tuple[int, int]]:
    """height -> (utxos, runs) out of an old build's log."""
    ref = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = REF_RE.search(line)
            if m:
                ref[int(m.group(1))] = (int(m.group(2).replace(" ", "")),
                                        int(m.group(3).replace(" ", "")))
    if not ref:
        raise RuntimeError(f"{path}: no report lines recognized")
    return ref


def _dying_ids(death: np.ndarray, h0: int, h1: int,
               chunk: int = 1 << 24) -> np.ndarray:
    """Sorted creation ids with h0 <= death < h1 (ascending ids come free:
    the death array is indexed by id)."""
    parts = []
    for off in range(0, len(death), chunk):
        d = np.asarray(death[off:off + chunk])
        sel = np.nonzero((d >= h0) & (d < h1))[0]
        if len(sel):
            parts.append((sel + off).astype(np.uint64))
    return np.concatenate(parts) if parts else np.empty(0, np.uint64)


def replay(idx: BlockIndex, build_dir: str, out_path: str,
           budget_bytes: int = 20 << 30, report_every: int = 20_000,
           reference: str | None = None, report=_p, workers: int = 0,
           _stop_after_epoch: int | None = None) -> tuple[int, int] | None:
    """Run the phase; returns (final utxo count, final run count).

    A checkpoint (phase-2.checkpoint.json) is written at every epoch
    boundary — all phase state is on disk there — and a rerun resumes
    from it: files are truncated back to the recorded offsets and the
    completed epochs are skipped. Delete the checkpoint to start over;
    a checkpoint from different parameters refuses to resume.
    _stop_after_epoch is the tests' crash lever: return (None) right
    after that epoch's checkpoint, leaving the files mid-phase."""
    with open(os.path.join(build_dir, "phase-1.manifest.json"),
              encoding="utf-8") as f:
        man = json.load(f)
    end = man["end"]
    n_cre, n_spd = man["creations"], man["spends"]
    if man.get("overwritten_bip30") and "overwritten_cids" not in man:
        raise RuntimeError("phase-1 manifest has no overwritten_cids (made "
                           "by an older resolve.py) — rerun phase 1")
    overwritten_at: dict[int, list[int]] = {}
    for cid, dup_h in man.get("overwritten_cids", []):
        overwritten_at.setdefault(dup_h, []).append(cid)

    death = np.memmap(os.path.join(build_dir, "death.u32"),
                      dtype="<u4", mode="r")
    if len(death) != n_cre:
        raise RuntimeError(f"death.u32 holds {len(death)} entries, "
                           f"the manifest says {n_cre}")
    epochs = spill.cut_epochs(man["death_histogram"], man["hist_bin"],
                              end, budget_bytes)
    starts = np.array([h0 for h0, _ in epochs], np.uint32)
    report(f"replaying blocks 0..{end} from {idx.manifest['blocks_dir']} "
           f"(chain tip {idx.tip}), phase 2 ({len(epochs)} epochs)")

    ref = load_reference(reference) if reference else {}
    ckpt_path = os.path.join(build_dir, "phase-2.checkpoint.json")
    ck = None
    if os.path.exists(ckpt_path):
        with open(ckpt_path, encoding="utf-8") as f:
            ck = json.load(f)
        if (ck["end"], ck["budget_bytes"],
                ck["epochs"]) != (end, budget_bytes,
                                  [list(e) for e in epochs]):
            raise RuntimeError(f"{ckpt_path} was written under different "
                               f"parameters — delete it to start over")

    resolved = U64Stream(os.path.join(build_dir, "resolved.ids"))
    if ck:
        resolved.seek_to(ck["resolved_taken"])
        final = spill.FinalWriter(out_path, resume=tuple(ck["final"]))
        spills = spill.SpillSet(
            os.path.join(build_dir, "epochs"),
            resume={int(e): s for e, s in ck["spill_sizes"].items()},
            resume_total=ck["spill_bytes"])
    else:
        if os.path.exists(out_path + ".tmp"):
            os.remove(out_path + ".tmp")
        final = spill.FinalWriter(out_path)
        spills = spill.SpillSet(os.path.join(build_dir, "epochs"))

    first_epoch = ck["epoch"] if ck else 0
    start_h = epochs[first_epoch][0]
    source = (parallel.pooled_blocks(idx, start_h, end, workers) if workers
              else parallel.serial_blocks(idx, start_h, end))

    utxos = runs_live = txs = cid_base = ref_bad = 0
    frags = 0                 # 12-byte run fragments through the kernel
    t_kern = 0.0              # seconds inside runcore.concat/cut
    t0 = t_win = time.time()
    txs_win = 0
    if ck:
        utxos, runs_live, txs = ck["utxos"], ck["runs_live"], ck["txs"]
        cid_base, ref_bad = ck["cid_base"], ck["ref_bad"]
        frags, t_kern = ck["frags"], ck["kernel_s"]
        t0 = time.time() - ck["seconds"]
        txs_win = txs
        report(f"resuming at epoch {first_epoch + 1}/{len(epochs)} "
               f"(h={start_h}) from the checkpoint")

    for ei, (h0, h1) in enumerate(epochs):
        if ei < first_epoch:
            continue
        ids_e = _dying_ids(death, h0, h1)
        table: list = [None] * len(ids_e)
        # One vectorized lookup: a Python-int key makes numpy cast the whole
        # uint64 array per call (~3000x slower on a 5M-id epoch).
        spilled = list(spills.load(ei))
        if spilled:
            slots = np.searchsorted(
                ids_e, np.fromiter((c for c, _ in spilled), np.uint64,
                                   len(spilled))).tolist()
            for s, (_, piece) in zip(slots, spilled):
                table[s] = piece
        del spilled

        for height in range(h0, min(h1 - 1, end) + 1):
            bh, (txids, nin_a, nout_a, values, soff_a, scripts) = next(source)
            if bh != height:
                raise RuntimeError(f"producer gave block {bh}, "
                                   f"expected {height}")
            nin, vals, soff = nin_a.tolist(), values.tolist(), soff_a.tolist()
            n_tx = len(nin)
            n_in = int(nin_a[1:].sum())
            if n_in:
                ids_in = resolved.take(n_in)
                s_in = np.searchsorted(ids_e, ids_in)
                if not np.array_equal(ids_e[s_in], ids_in):
                    raise RuntimeError(f"height {height}: an input's id does "
                                       f"not die in this epoch — resolve and "
                                       f"the epochs disagree")
                slots_in = s_in.tolist()
            else:
                slots_in = []
            n_out = int(nout_a.sum())
            d_arr = np.asarray(death[cid_base:cid_base + n_out])
            this_e = ((d_arr >= h0) & (d_arr < h1)).tolist()
            slots_out = np.searchsorted(
                ids_e, np.arange(cid_base, cid_base + n_out,
                                 dtype=np.uint64)).tolist()
            route = (np.searchsorted(starts, d_arr, side="right") - 1).tolist()
            d_out = d_arr.tolist()
            k_in = 0
            tails: list[bytes] = []
            # Creation ids run in block order, coinbase first; the coinbase
            # is replayed last, so each tx carries its own output offset.
            off_out = [0]
            for c in nout_a.tolist():
                off_out.append(off_out[-1] + c)

            def place(ti, pieces, base):
                nonlocal utxos, runs_live
                txid = txids[ti * 32:ti * 32 + 32]
                for vout, piece in enumerate(pieces):
                    k_out = base + vout
                    dh = d_out[k_out]
                    if dh == UNSPENT:
                        final.write(cid_base + k_out, vout, txid,
                                    scripts[soff[k_out]:soff[k_out + 1]],
                                    piece)
                    elif this_e[k_out]:
                        table[slots_out[k_out]] = piece
                    else:
                        spills.write(route[k_out], cid_base + k_out, piece)
                    utxos += 1
                    runs_live += len(piece) // RUN_SIZE

            for ti in range(1, n_tx):
                parts = []
                for _ in range(nin[ti]):
                    s = slots_in[k_in]
                    k_in += 1
                    p = table[s]
                    if p is None:
                        raise RuntimeError(f"height {height}: spend of an id "
                                           f"that is not in the table")
                    table[s] = None
                    parts.append(p)
                    runs_live -= len(p) // RUN_SIZE
                utxos -= nin[ti]
                t = time.perf_counter()
                stream = runcore.concat(parts)
                pieces, leftover = runcore.cut(
                    stream, vals[off_out[ti]:off_out[ti + 1]])
                t_kern += time.perf_counter() - t
                frags += len(stream) // RUN_SIZE
                tails.append(leftover)
                place(ti, pieces, off_out[ti])

            sub = subsidy_sats(height)
            t = time.perf_counter()
            stream = runcore.concat(
                ([runcore.make_run(height, sub)] if sub else []) + tails)
            pieces, _ = runcore.cut(stream, vals[:off_out[1]])
            t_kern += time.perf_counter() - t
            frags += len(stream) // RUN_SIZE
            place(0, pieces, 0)                   # the rest is never born

            for cid in overwritten_at.get(height, ()):
                s = int(np.searchsorted(ids_e, np.uint64(cid)))
                piece = table[s]                 # BIP30: replaced, not spent
                table[s] = None
                utxos -= 1
                runs_live -= len(piece) // RUN_SIZE

            txs += n_tx
            cid_base += n_out
            if height % report_every == 0 and height:
                now = time.time()
                dt = now - t_win
                mark = ""
                if height in ref:
                    ru, rr = ref[height]
                    if (utxos, runs_live) == (ru, rr):
                        mark = "  ref OK"
                    else:   # report and continue: the reference is the
                        # temporary side, never allowed to stop this build
                        mark = "  ref MISMATCH"
                        ref_bad += 1
                        report(f"  REF MISMATCH h={height}: utxos "
                               f"{fmt(utxos)} vs {fmt(ru)}, runs "
                               f"{fmt(runs_live)} vs {fmt(rr)} (reference)")
                eta = (end - height) / (report_every / dt) / 3600 if dt else 0
                report(f"  h={height}  {report_every / dt:,.1f} blk/s  "
                       f"{fmt((txs - txs_win) / dt)} tx/s  utxos {fmt(utxos)}  "
                       f"runs {fmt(runs_live)}  final {fmt(final.count)}  "
                       f"spill {spills.bytes / 1e9:.2f} GB  "
                       f"{frags / max(t_kern, 1e-9) / 1e6:.1f} Mfrag/s  "
                       f"eta {eta:.1f} h{mark}")
                t_win, txs_win = now, txs

        leak = sum(1 for x in table if x is not None)
        if leak:
            raise RuntimeError(f"epoch {ei}: {leak} table entries never "
                               f"consumed — resolve and replay disagree")
        del table, ids_e

        if ei + 1 < len(epochs):
            # the epoch boundary: everything is on disk — flush it, write
            # the checkpoint, and only then drop the spill just consumed
            # (a resume of THIS epoch would have needed it)
            final.flush()
            spills.flush()
            tmp = ckpt_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "epoch": ei + 1, "end": end,
                    "budget_bytes": budget_bytes,
                    "epochs": [list(e) for e in epochs],
                    "utxos": utxos, "runs_live": runs_live, "txs": txs,
                    "cid_base": cid_base, "ref_bad": ref_bad,
                    "frags": frags, "kernel_s": round(t_kern, 3),
                    "seconds": round(time.time() - t0, 1),
                    "resolved_taken": resolved.taken,
                    "final": [final.bytes, final.count, final.run_count],
                    "spill_sizes": {str(e): s for e, s in
                                    spills.sizes.items() if s and e != ei},
                    "spill_bytes": spills.bytes,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }, f)
            os.replace(tmp, ckpt_path)
            spills.drop(ei)
            if _stop_after_epoch is not None and ei >= _stop_after_epoch:
                source.close()          # the tests' simulated crash: leave
                spills.close()          # .tmp and spills exactly mid-phase
                final.f.close()
                resolved.close()
                return None

    source.close()
    final.close()
    spills.close()
    for e in list(spills.sizes):              # the last epoch's spill
        spills.drop(e)
    resolved.assert_exhausted()
    resolved.close()
    if cid_base != n_cre:
        raise RuntimeError(f"replay created {cid_base} outputs, "
                           f"phase 1 counted {n_cre}")
    if final.count != man["alive"] or (utxos, runs_live) != (final.count,
                                                            final.run_count):
        raise RuntimeError(
            f"final stream holds {fmt(final.count)} utxos / "
            f"{fmt(final.run_count)} runs; live counters say {fmt(utxos)} / "
            f"{fmt(runs_live)}; phase 1 says alive {fmt(man['alive'])}")

    manifest = {
        "phase": 2,
        "end": end,
        "utxos": final.count,
        "runs": final.run_count,
        "txs": txs,
        "epochs": epochs,
        "spill_bytes": spills.bytes,
        "seconds": round(time.time() - t0, 1),
        "kernel_s": round(t_kern, 1),
        "mfrag_per_s": round(frags / max(t_kern, 1e-9) / 1e6, 1),
        "reference": reference,
        "ref_heights_checked": sum(1 for h in ref if h % report_every == 0
                                   and 0 < h <= end),
        "ref_mismatches": ref_bad,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if ck:
        manifest["resumed_from_epoch"] = first_epoch
    with open(os.path.join(build_dir, "phase-2.manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f)
    if os.path.exists(ckpt_path):
        os.remove(ckpt_path)
    refnote = f", REF MISMATCHES {ref_bad}" if ref_bad else ""
    report(f"phase 2 done: {fmt(final.count)} utxos, {fmt(final.run_count)} "
           f"runs, {fmt(txs)} txs, {len(epochs)} epochs, "
           f"{os.path.getsize(out_path) / 1e9:.2f} GB final stream, "
           f"{time.time() - t0:.0f}s, kernel {t_kern:.0f}s at "
           f"{frags / max(t_kern, 1e-9) / 1e6:.1f} Mfrag/s{refnote}")
    return final.count, final.run_count


def main() -> int:
    ap = argparse.ArgumentParser(description="fast build phase 2: the "
                                             "lookahead replay")
    ap.add_argument("--idx", default=r"D:\claimstones\build\blocks.idx")
    ap.add_argument("--build-dir", default=r"D:\claimstones\build")
    ap.add_argument("--out", default=r"D:\claimstones\build\final.stream")
    ap.add_argument("--budget-gb", type=float, default=20.0)
    ap.add_argument("--report-every", type=int, default=20_000)
    ap.add_argument("--reference", default=None,
                    help="an old build's log; utxo/run counts are compared "
                         "at every matching report height")
    ap.add_argument("--workers", type=int, default=0,
                    help="parse-pool processes; 0 = parse in-process "
                         "(the fold itself is always one process)")
    args = ap.parse_args()
    replay(BlockIndex(args.idx), args.build_dir, args.out,
           int(args.budget_gb * (1 << 30)), args.report_every, args.reference,
           workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
