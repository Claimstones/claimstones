"""The acceptance gate: the fast build against the old build, row for row.

Walks the final stream in (txid, vout) order (load.iter_sorted — the same
external sort phase 3 uses) and the old SQLite index in primary-key order,
in lockstep. Every row must match on all four fields: txid, vout, script,
runs. Any difference is reported with its key and classified (only in one
build, script differs, runs differ); the first --max-mismatches are shown
and the walk continues, so one bad row near the start still yields the
full damage count.

Both walks are also digested (sha256 over txid + vout + script + runs per
row) so a passed gate leaves one checkable line for the record:

    python -m claimstones.indexer.fastbuild.compare ^
        --stream D:\\claimstones\\build-710k\\final.stream ^
        --db D:\\claimstones\\slowbuild\\index\\birth.sqlite

Exit 0 only on a bit-for-bit pass with equal row counts.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import os
import sqlite3
import struct
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))

from claimstones.indexer.fastbuild.load import fmt, iter_sorted

_p = functools.partial(print, flush=True)
VOUT = struct.Struct("<I")


def iter_sqlite(db_path: str):
    """Yield (txid, vout, script, runs) in primary-key order; WITHOUT ROWID
    makes ORDER BY the natural walk, no sort."""
    db = sqlite3.connect(f"file:{db_path.replace(os.sep, '/')}?mode=ro",
                         uri=True)
    db.execute("PRAGMA cache_size=-262144")           # 256 MiB of page cache
    cur = db.execute(
        "SELECT txid, vout, script, runs FROM utxo ORDER BY txid, vout")
    cur.arraysize = 10_000
    while rows := cur.fetchmany():
        yield from rows
    db.close()


def _digest_row(h, row) -> None:
    txid, vout, script, runs = row
    h.update(txid)
    h.update(VOUT.pack(vout))
    h.update(script)
    h.update(runs)


def compare(stream_path: str, db_path: str, scratch_dir: str,
            max_mismatches: int = 10, report=_p) -> int:
    """Returns the number of differing rows (0 is the gate pass)."""
    t0 = time.time()
    new = iter_sorted(stream_path, scratch_dir, report=report)
    old = iter_sqlite(db_path)
    h_new, h_old = hashlib.sha256(), hashlib.sha256()
    n_new = n_old = bad = shown = 0

    def show(kind: str, row) -> None:
        nonlocal shown
        if shown < max_mismatches:
            report(f"  MISMATCH {kind}: txid {row[0][::-1].hex()} "
                   f"vout {row[1]}")
            shown += 1

    a = next(new, None)
    b = next(old, None)
    while a is not None or b is not None:
        ka = (a[0], a[1]) if a is not None else None
        kb = (b[0], b[1]) if b is not None else None
        if kb is None or (ka is not None and ka < kb):
            _digest_row(h_new, a); n_new += 1
            bad += 1; show("only in the fast build", a)
            a = next(new, None)
        elif ka is None or kb < ka:
            _digest_row(h_old, b); n_old += 1
            bad += 1; show("only in the old build", b)
            b = next(old, None)
        else:
            _digest_row(h_new, a); n_new += 1
            _digest_row(h_old, b); n_old += 1
            if a[2] != b[2]:
                bad += 1; show("script differs", a)
            elif a[3] != b[3]:
                bad += 1; show("runs differ", a)
            a = next(new, None)
            b = next(old, None)
        if (n_old % 10_000_000) == 0 and n_old:
            report(f"  {fmt(n_old)} rows compared, {bad} mismatches, "
                   f"{time.time() - t0:.0f}s")

    report(f"fast build: {fmt(n_new)} rows, sha256 {h_new.hexdigest()}")
    report(f"old build:  {fmt(n_old)} rows, sha256 {h_old.hexdigest()}")
    if bad == 0 and n_new == n_old:
        report(f"GATE PASSED: {fmt(n_new)} rows bit-for-bit identical, "
               f"{time.time() - t0:.0f}s")
    else:
        report(f"GATE FAILED: {bad} differing rows "
               f"({fmt(n_new)} vs {fmt(n_old)}), {time.time() - t0:.0f}s")
    return bad or int(n_new != n_old)


def main() -> int:
    ap = argparse.ArgumentParser(description="acceptance gate: fast build "
                                             "vs the old index, row for row")
    ap.add_argument("--stream",
                    default=r"D:\claimstones\build-710k\final.stream")
    ap.add_argument("--db",
                    default=r"D:\claimstones\slowbuild\index\birth.sqlite")
    ap.add_argument("--scratch",
                    default=r"D:\claimstones\build-710k\sort-scratch")
    ap.add_argument("--max-mismatches", type=int, default=10)
    args = ap.parse_args()
    return 1 if compare(args.stream, args.db, args.scratch,
                        args.max_mismatches) else 0


if __name__ == "__main__":
    sys.exit(main())
