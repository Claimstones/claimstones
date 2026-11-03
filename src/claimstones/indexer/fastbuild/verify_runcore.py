"""Replay the real chain through BOTH run-arithmetic implementations and
compare every byte — the acceptance run for runcore.py, and the benchmark
that decides numpy vs Rust (fastbuild.md: the kernel moves to Rust if it
sustains under ~20M fragments/s here).

Replays from genesis with the whole UTXO->runs state in RAM (no store, no
lookups: this is also a first taste of phase 2's shape). Every transaction
is computed twice: birth.py's tuple machinery and runcore's packed bytes;
any divergence aborts with full context. --mode new skips the old path for
a pure speed run.

Memory grows with the UTXO set: roughly 10-12 GB at the default
--end 400000. Ctrl+C is always safe — this verifies, it does not build.

    python -m claimstones.indexer.fastbuild.verify_runcore
    python -m claimstones.indexer.fastbuild.verify_runcore --end 200000 --mode new
"""

from __future__ import annotations

import argparse
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))

from claimstones.bitcoin.txparse import parse_block
from claimstones.indexer import runcore
from claimstones.indexer.birth import (cut_runs, decode_runs, encode_runs,
                                       extend_merged)
from claimstones.indexer.fastbuild.blocks import BlockIndex
from claimstones.lineage.walker import subsidy_sats


def fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ")


class Mismatch(RuntimeError):
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="replay the chain through runcore "
                                             "and birth.py in lockstep")
    ap.add_argument("--idx", default=r"D:\claimstones\build\blocks.idx")
    ap.add_argument("--end", type=int, default=400_000)
    ap.add_argument("--report-every", type=int, default=20_000)
    ap.add_argument("--mode", choices=("both", "new"), default="both",
                    help="both: verify and time both paths (default); "
                         "new: runcore only, pure speed")
    args = ap.parse_args()

    idx = BlockIndex(args.idx)
    end = min(args.end, idx.tip)
    both = args.mode == "both"
    print(f"replaying blocks 0..{end} from {args.idx} "
          f"(chain tip {idx.tip}), mode {args.mode}", flush=True)

    state: dict[bytes, bytes] = {}
    state_bytes = 0                       # keys + values, the honest RAM core
    txs = 0
    frags = 0                             # runs flowing through concat+cut
    t_old = 0.0
    t_new = 0.0
    t0 = time.time()
    t_win = t0
    txs_win = 0
    pc = time.perf_counter

    def mismatch(height, tx, what, old, new):
        raise Mismatch(
            f"MISMATCH at height {height}, tx {tx.txid[::-1].hex()}, {what}:\n"
            f"  old {old.hex()}\n  new {new.hex()}")

    try:
        for height, raw in idx.iter_raw(0, end):
            block = parse_block(raw, height)
            tails_new: list[bytes] = []
            tails_old: list[list] = []
            for tx in block.txs[1:]:
                parts = []
                for inp in tx.inputs:
                    part = state.pop(inp.prev_txid + inp.prev_vout.to_bytes(4, "little"), None)
                    if part is None:
                        raise Mismatch(f"height {height}: {tx.txid[::-1].hex()} spends "
                                       f"unknown {inp.prev_txid[::-1].hex()}:{inp.prev_vout}")
                    state_bytes -= 36 + len(part)
                    parts.append(part)
                sizes = [o.value for o in tx.outputs]

                t = pc()
                stream = runcore.concat(parts)
                pieces, leftover = runcore.cut(stream, sizes)
                t_new += pc() - t
                frags += len(stream) // 12

                if both:
                    t = pc()
                    stream_o: list = []
                    for p in parts:
                        extend_merged(stream_o, decode_runs(p))
                    pieces_o, leftover_o = cut_runs(stream_o, sizes)
                    t_old += pc() - t
                    for vout, (pn, po) in enumerate(zip(pieces, pieces_o)):
                        if pn != encode_runs(po):
                            mismatch(height, tx, f"output {vout}", encode_runs(po), pn)
                    if leftover != encode_runs(leftover_o):
                        mismatch(height, tx, "fee tail", encode_runs(leftover_o), leftover)
                    tails_old.append(leftover_o)

                tails_new.append(leftover)
                for vout, piece in enumerate(pieces):
                    key = tx.txid + vout.to_bytes(4, "little")
                    state[key] = piece
                    state_bytes += 36 + len(piece)

            coinbase = block.txs[0]
            subsidy = subsidy_sats(height)
            sizes = [o.value for o in coinbase.outputs]

            t = pc()
            stream = runcore.concat(
                ([runcore.make_run(height, subsidy)] if subsidy else []) + tails_new)
            pieces, _ = runcore.cut(stream, sizes)    # the rest is never born
            t_new += pc() - t
            frags += len(stream) // 12

            if both:
                t = pc()
                stream_o = [(height, subsidy)] if subsidy else []
                for tail in tails_old:
                    extend_merged(stream_o, tail)
                pieces_o, _ = cut_runs(stream_o, sizes)
                t_old += pc() - t
                for vout, (pn, po) in enumerate(zip(pieces, pieces_o)):
                    if pn != encode_runs(po):
                        mismatch(height, coinbase, f"coinbase output {vout}",
                                 encode_runs(po), pn)

            for vout, piece in enumerate(pieces):
                key = coinbase.txid + vout.to_bytes(4, "little")
                old = state.get(key)
                if old is not None:                   # BIP30: 91842, 91880
                    state_bytes -= 36 + len(old)
                state[key] = piece
                state_bytes += 36 + len(piece)

            txs += len(block.txs)
            if height % args.report_every == 0 and height:
                now = time.time()
                dt = now - t_win
                blk_rate = args.report_every / dt if dt else 0
                rate = (txs - txs_win) / dt if dt else 0
                speed = (f"old {t_old / txs * 1e6:5.1f} new {t_new / txs * 1e6:5.1f} "
                         f"us/tx  x{t_old / max(t_new, 1e-9):.1f}") if both else \
                        f"new {t_new / max(txs, 1) * 1e6:5.1f} us/tx"
                mfrag = frags / max(t_new, 1e-9) / 1e6
                blocks_left = end - height
                eta = blocks_left / (args.report_every / dt) / 3600 if dt else 0
                print(f"  h={height}  {blk_rate:,.1f} blk/s  {fmt(rate)} tx/s  utxos {fmt(len(state))}  "
                      f"state {state_bytes / 1e9:.2f} GB  {speed}  "
                      f"{mfrag:.1f} Mfrag/s  eta {eta:.1f} h", flush=True)
                t_win = now
                txs_win = txs
    except Mismatch as e:
        print(f"\n{e}", flush=True)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — partial figures below", flush=True)

    wall = time.time() - t0
    done = min(end, height) + 1
    print(f"\n{'VERIFIED' if both else 'ran'} blocks 0..{done - 1}: "
          f"{fmt(txs)} txs in {wall:.0f}s "
          f"({done / max(wall, 1e-9):,.1f} blk/s, {fmt(txs / max(wall, 1e-9))} tx/s), "
          f"utxos {fmt(len(state))}, state {state_bytes / 1e9:.2f} GB", flush=True)
    if both:
        print(f"arithmetic: old {t_old:.0f}s, new {t_new:.0f}s "
              f"(x{t_old / max(t_new, 1e-9):.1f}); "
              f"new path {frags / max(t_new, 1e-9) / 1e6:.1f} Mfrag/s "
              f"(Rust trigger: < 20)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
