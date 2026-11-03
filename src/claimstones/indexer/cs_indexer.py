"""cs_indexer — the resident tip daemon over the LMDB birth index.

The fastbuild (fastbuild/) built the index to a height; this process
keeps it at the chain tip. Named per the 2026-10-28 decision: the fused
daemon (birth runs + orchid ledger) is cs_indexer; the ledger half joins
once ACTIVATION_HEIGHT exists, so today this maintains birth runs only.

One cycle (`ChainFollower.sync_once`):

  1. Reorg check. Every block the daemon connects records its hash under
     the meta key `hash:<height>`. While our recorded hash at our tip
     disagrees with the node's chain, disconnect — the undo log restores
     the spent rows and drops the created ones. The walk stops where no
     hash is recorded (below the watch window the chain is trusted: at
     the handover height nothing is recorded, and that deep a reorg has
     no undo rows either).
  2. Catch-up. Apply every block from our height to the node tip, in
     order, through the same BirthIndexer the builds use. A backlog is
     not a mode: a night offline is the same loop with more blocks.

There is no separate initial-sync path and no speed requirement — one
block per ten minutes needs correctness. Correctness comes from the
stores' commit discipline (default --commit-every 1: each block is one
durable LMDB transaction; the hash record and the block's rows commit
together) and from wait_for_rpc: a node that is away, restarting or
behind us is waited out, while a real fault (bad auth, MissingInput —
a broken index) still fails loudly.

A node *behind* us is left alone: it restarted into its own catch-up,
and disconnecting healthy blocks because the node is slow would be
self-harm. The daemon waits until the node passes our height again.

Conventions carried over from the build: a stop file beside the db asks
the daemon to finish the block it is on, commit and exit; a status JSON
beside the db feeds the monitor.

    python -m claimstones.indexer.cs_indexer \
        --db D:/claimstones/build-prod/birth.lmdb --rpc-cookie ...
"""

from __future__ import annotations

import argparse
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
from claimstones.indexer.birth import (BirthIndexer, RpcBlocks, fmt,
                                       wait_for_rpc, write_status)
from claimstones.indexer.store_lmdb import LmdbBirthStore
from claimstones.lineage.walker import Rpc, resolve_rpc_auth


def hash_key(height: int) -> str:
    return f"hash:{height}"


class ChainFollower:
    """One reorg-check-then-extend cycle over an injected block source.

    `call(method, *args)` is the node RPC (already wrapped in whatever
    waiting policy the caller wants); `blocks.iter(start, end)` yields
    parsed Blocks in height order. Both are injected so tests can run
    the follower against a scripted chain."""

    def __init__(self, store, indexer, blocks, call, undo_depth: int = 1000,
                 report=print, report_every: int = 100,
                 status_path: str | None = None,
                 stop_path: str | None = None):
        self.store = store
        self.ix = indexer
        self.blocks = blocks
        self.call = call
        self.undo_depth = undo_depth
        self.report = report
        self.report_every = report_every
        self.status_path = status_path
        self.stop_path = stop_path
        self.t0 = time.time()

    def stop_requested(self) -> str | None:
        """The stop reason, or None. An existing stop file means: finish
        the block you are on, commit it and exit cleanly."""
        if not self.stop_path or not os.path.exists(self.stop_path):
            return None
        try:
            with open(self.stop_path, encoding="utf-8-sig",
                      errors="replace") as f:
                return f.read().strip() or "stop file present"
        except OSError:
            return "stop file present"

    def _disconnect_tip(self) -> int:
        """Walk our tip back to the node's chain. Returns blocks undone."""
        undone = 0
        while True:
            last = self.store.last_height()
            ours = self.store.get_meta(hash_key(last))
            if ours is None:
                break                    # below the watch window: trusted
            node = self.call("getblockhash", last)
            if node == ours:
                break
            if undone >= self.undo_depth:
                raise RuntimeError(
                    f"reorg deeper than {self.undo_depth} blocks at "
                    f"h={last}; refusing to unwind further")
            self.report(f"  reorg: disconnecting h={last} "
                        f"(ours {ours[:16]}…, node {node[:16]}…)", flush=True)
            self.store.begin()
            self.store.del_meta(hash_key(last))
            self.ix.disconnect_block(last)
            undone += 1
        return undone

    def _connect(self, block) -> None:
        self.store.begin()
        self.store.set_meta(hash_key(block.height), block.hash_hex)
        self.store.del_meta(hash_key(block.height - self.undo_depth))
        self.ix.connect_block(block)

    def sync_once(self) -> tuple[int, int]:
        """One cycle: reorg check, then extend to the node tip.
        Returns (blocks disconnected, blocks connected)."""
        tip = self.call("getblockcount")
        last = self.store.last_height()
        if last is not None and tip < last:
            # the node restarted into its own catch-up; wait, never unwind
            return 0, 0
        undone = self._disconnect_tip() if last is not None else 0
        start = (self.store.last_height() + 1
                 if self.store.last_height() is not None else 0)
        connected = 0
        stopped = None
        if tip >= start:
            for block in self.blocks.iter(start, tip):
                self._connect(block)
                connected += 1
                if self.status_path:
                    write_status(self.status_path, block.height,
                                 self.store, self.t0)
                if (block.height % self.report_every == 0
                        or block.height == tip):
                    s = self.store.stats
                    self.report(f"  h={block.height} {block.hash_hex[:16]}… "
                                f"utxos {fmt(s['utxos'])} "
                                f"runs {fmt(s['runs'])}", flush=True)
                stopped = self.stop_requested()
                if stopped:
                    break
        self.store.commit()
        if stopped:
            self.report(f"  stop requested by {self.stop_path}: {stopped} "
                        f"-- committed h={self.store.last_height()}",
                        flush=True)
            raise SystemExit(0)
        return undone, connected

    def run(self, poll_s: float = 10.0) -> None:
        """Follow forever: cheap best-hash poll, full cycle on change."""
        best = None
        while True:
            if self.stop_requested():
                self.store.commit()
                self.report(f"  stop requested by {self.stop_path} "
                            f"-- exiting at h={self.store.last_height()}",
                            flush=True)
                return
            now = self.call("getbestblockhash")
            if now != best:
                # remember the hash from BEFORE the sync: a block arriving
                # mid-cycle changes it again and must trigger another cycle
                best = now
                self.sync_once()
            time.sleep(poll_s)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="cs_indexer: keep the LMDB birth index at the chain tip")
    ap.add_argument("--rpc-url", default="http://127.0.0.1:8332")
    ap.add_argument("--rpc-user")
    ap.add_argument("--rpc-pass")
    ap.add_argument("--rpc-cookie")
    ap.add_argument("--no-auth", action="store_true")
    ap.add_argument("--rpc-timeout", type=int, default=300)
    ap.add_argument("--rpc-wait-max", type=int, default=0,
                    help="seconds to wait for an absent node (0: forever)")
    ap.add_argument("--db", default=r"D:\claimstones\build-prod\birth.lmdb")
    ap.add_argument("--poll", type=float, default=10.0,
                    help="seconds between best-hash polls at the tip")
    ap.add_argument("--undo-depth", type=int, default=1000,
                    help="blocks kept disconnectable (undo rows + hashes)")
    ap.add_argument("--map-gb", type=int, default=200,
                    help="LMDB map size (the file is allocated to this size "
                         "on Windows). The bulk load packs pages 100% full, "
                         "so the daemon's random inserts grow the tree toward "
                         "~1.4x the loaded size as leaves split; on "
                         "MDB_MAP_FULL restart with a larger value — the db "
                         "stays at the last committed block")
    ap.add_argument("--commit-every", type=int, default=1,
                    help="blocks per LMDB transaction (default 1: every "
                         "block durable the moment it is applied)")
    ap.add_argument("--report-every", type=int, default=100,
                    help="blocks between report lines during catch-up; "
                         "at the tip every block reports")
    ap.add_argument("--status", default=None,
                    help="status JSON for the monitor (default: "
                         "cs_indexer.status beside the db; '' disables)")
    ap.add_argument("--stop-file", default=None,
                    help="existence asks for a clean exit (default: "
                         "cs_indexer.stop beside the db; '' disables)")
    args = ap.parse_args()

    auth = resolve_rpc_auth(args.no_auth, args.rpc_user, args.rpc_pass,
                            args.rpc_cookie)
    rpc = Rpc(args.rpc_url, auth)

    def call(method, *a):
        return wait_for_rpc(lambda: rpc(method, *a), f"calling {method}",
                            wait_max=args.rpc_wait_max)

    beside = os.path.dirname(os.path.abspath(args.db))
    status_path = args.status if args.status is not None else \
        os.path.join(beside, "cs_indexer.status")
    stop_path = args.stop_file if args.stop_file is not None else \
        os.path.join(beside, "cs_indexer.stop")

    store = LmdbBirthStore(args.db, commit_every=args.commit_every,
                           map_mb=args.map_gb * 1024)
    repaired = store.repair()
    if repaired:
        print(f"repaired: undid {repaired} half-applied block(s)", flush=True)
    indexer = BirthIndexer(store, undo_depth=args.undo_depth)
    blocks = RpcBlocks(rpc, timeout=args.rpc_timeout,
                       wait_max=args.rpc_wait_max)
    follower = ChainFollower(store, indexer, blocks, call,
                             undo_depth=args.undo_depth,
                             report_every=args.report_every,
                             status_path=status_path or None,
                             stop_path=stop_path or None)
    print(f"cs_indexer: following the tip from h={store.last_height()} "
          f"-> {args.db}  (utxos {fmt(store.stats['utxos'])}, "
          f"undo depth {args.undo_depth}, poll {args.poll}s)", flush=True)
    ok = False
    try:
        follower.run(poll_s=args.poll)
        ok = True
    except SystemExit:
        ok = True
    finally:
        if ok:
            store.commit()
        else:
            store.rollback()     # never commit a half-applied block
        print(f"stopped at h={store.last_height()}: "
              f"utxos {fmt(store.stats['utxos'])}, "
              f"runs {fmt(store.stats['runs'])}", flush=True)
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
