"""Orchid balance index — deterministic replay (docs/spec/indexer.md).

Consensus state is one number per outpoint: its orchid balance in seeds.
Per transaction, in block order:

  1. locate the envelope (protocol/codec.py);
  2. classify cenotaph or valid;
  3. sum the input balances into the unallocated pot;
  4. if LAND, value every input sat at W(c) and add the amount (a land
     of inputs with zero latent value is void: skipped);
  5. apply edicts, the pointer, the default rule;
  6. on a cenotaph, burn everything (and if the cenotaph carried LAND,
     the inputs' latent value is landed and burned too).

The allocation algorithm (edict distribution, split across outputs,
default output, cenotaph burn) is ported from ord's rune_updater.rs,
specialised to a single asset. Etching and minting do not exist here:
orchids come into existence only by landing.

Valuation is injected as a `Valuer` so the engine stays a pure function
of chain data and can be tested with a fake. `WalkerValuer` is the
production one: it drives lineage/walker.py against a full node with
txindex and refuses to let a non-deterministic (budget-truncated) walk
into the state.

Persistence is SQLite (stdlib). Balances, land txids and per-block meta
live on disk; one commit per block is the resume point.

Not yet here: reorg undo (docs/spec/indexer.md, Reorg). The store is
append-only per block; a reorg currently needs a resync from the fork
point.

Run from the repo root (resumable, one SQLite file):

    python -m claimstones.indexer.replay --rpc-url http://127.0.0.1:8332 ...
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Callable, Optional, Protocol

if __package__ in (None, ""):       # `python .../indexer/replay.py` script run
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
from claimstones.bitcoin.blockfile import PYNODE_DIR, BlockFile
from claimstones.bitcoin.txparse import Block, Tx, parse_block
from claimstones.lineage.walker import seeds_from_work
from claimstones.protocol.codec import (
    ACTIVATION_HEIGHT,
    ORCHID,
    U128_MAX,
    Cenotaph,
    Envelope,
    decipher,
    is_op_return,
)

# ---------------------------------------------------------------------------
# Valuation interface (docs/spec/constants.md, docs/spec/land.md)
# ---------------------------------------------------------------------------

class Valuer(Protocol):
    """What the land step needs from the outside world."""

    def chainwork(self, height: int) -> int:
        """W(c): cumulative chainwork in hashes including block `height`."""

    def latent_work(self, tx: Tx, height: int) -> int:
        """N of constants.md for every input sat of `tx` (for a coinbase,
        for its whole sat stream: subsidy then fee tails), in scaled work
        (hashes times SUBSIDY_SCALE). Ranges dropped by the walk cutoff
        and sats descending from an earlier land contribute 0.

        Must be a deterministic function of chain data; raise rather
        than return a truncated value."""


class NonDeterministicWalk(RuntimeError):
    """A lineage walk was cut short by something other than the
    consensus cutoff; its result must not enter indexer state."""


# ---------------------------------------------------------------------------
# File-backed store (SQLite only; no in-memory index)
# ---------------------------------------------------------------------------

class OrchidStore:
    """Persistent per-outpoint balances plus the set of land txids.

    Balances are u128 seeds and are stored as decimal TEXT (SQLite's
    INTEGER is signed 64-bit); arithmetic happens in Python.
    """

    SCHEMA = "1"

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS balances (
                txid BLOB NOT NULL,
                vout INTEGER NOT NULL,
                amount TEXT NOT NULL,
                PRIMARY KEY (txid, vout)
            );
            CREATE TABLE IF NOT EXISTS lands (
                txid BLOB PRIMARY KEY,
                height INTEGER NOT NULL,
                amount TEXT NOT NULL,
                cenotaph INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS lands_by_height ON lands (height);
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        if self.get_meta("schema") is None:
            self.set_meta("schema", self.SCHEMA)
        elif self.get_meta("schema") != self.SCHEMA:
            raise RuntimeError("orchid index schema mismatch; resync with a fresh file")
        self.db.commit()

    # --- meta -------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta_int(self, key: str) -> int:
        return int(self.get_meta(key) or "0")

    def add_meta_int(self, key: str, delta: int) -> None:
        self.set_meta(key, str(self.get_meta_int(key) + delta))

    def last_indexed_height(self) -> Optional[int]:
        v = self.get_meta("last_indexed_height")
        return int(v) if v is not None else None

    # --- balances -----------------------------------------------------

    def take_balance(self, txid: bytes, vout: int) -> int:
        """Read and delete the balance of an outpoint being spent."""
        row = self.db.execute(
            "SELECT amount FROM balances WHERE txid=? AND vout=?", (txid, vout)
        ).fetchone()
        if row is None:
            return 0
        self.db.execute("DELETE FROM balances WHERE txid=? AND vout=?", (txid, vout))
        return int(row[0])

    def get_balance(self, txid: bytes, vout: int) -> int:
        """Non-destructive read (wallet coin-control queries)."""
        row = self.db.execute(
            "SELECT amount FROM balances WHERE txid=? AND vout=?", (txid, vout)
        ).fetchone()
        return int(row[0]) if row else 0

    def add_balance(self, txid: bytes, vout: int, amount: int) -> None:
        self.db.execute(
            "INSERT INTO balances (txid, vout, amount) VALUES (?, ?, ?)",
            (txid, vout, str(amount)),
        )

    # --- lands --------------------------------------------------------

    def is_landed(self, txid: bytes) -> bool:
        return self.db.execute("SELECT 1 FROM lands WHERE txid=?", (txid,)).fetchone() is not None

    def add_land(self, txid: bytes, height: int, amount: int, cenotaph: bool) -> None:
        self.db.execute(
            "INSERT INTO lands (txid, height, amount, cenotaph) VALUES (?, ?, ?, ?)",
            (txid, height, str(amount), int(cenotaph)),
        )

    def get_land(self, txid: bytes) -> Optional[tuple[int, int, bool]]:
        row = self.db.execute(
            "SELECT height, amount, cenotaph FROM lands WHERE txid=?", (txid,)
        ).fetchone()
        return (row[0], int(row[1]), bool(row[2])) if row else None

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()


# ---------------------------------------------------------------------------
# OrchidIndexer (spec 4, Determinism)
# ---------------------------------------------------------------------------

class OrchidIndexer:
    def __init__(self, store: OrchidStore, valuer: Valuer):
        self.store = store
        self.valuer = valuer
        # Consensus, and deliberately not an option: protocol/codec.py holds
        # the constant and the reminder that goes with it.
        self.activation_height = ACTIVATION_HEIGHT

    def _counts(self, height: int) -> bool:
        """Whether an envelope at this height counts. None means the constant
        is still undecided, so nothing counts at any height -- and note that
        this is a test for None, not a comparison: 0 would mean the opposite,
        active since genesis."""
        return (self.activation_height is not None
                and height >= self.activation_height)

    def index_block(self, block: Block) -> None:
        chainwork: Optional[int] = None   # W(c), fetched on first land
        burned = 0
        counts = self._counts(block.height)      # constant across the block
        for tx in block.txs:
            artifact = decipher(tx) if counts else None
            chainwork, burned = self._index_tx(tx, block.height, artifact, chainwork, burned)

        if burned:
            self.store.add_meta_int("burned", burned)
        self.store.set_meta("last_indexed_height", str(block.height))
        self.store.commit()

    def apply_tx(self, tx: Tx, height: int, chainwork: Optional[int] = None) -> None:
        """Index one transaction on its own (the wallet server's replay of
        the transactions it broadcast, server/orchids.py): decipher, land,
        edicts, remainder. `chainwork` is W(c) of the inclusion block."""
        # The same activation gate as index_block. Without it this path would
        # credit a land that index_block ignores, and the server's own replay
        # would disagree with the index it is standing in for.
        artifact = decipher(tx) if self._counts(height) else None
        self._index_tx(tx, height, artifact, chainwork, 0)

    def _index_tx(self, tx: Tx, height: int, artifact, chainwork, burned):
        txid = tx.txid

        # (3) seeds freed up by spending this tx's inputs
        unallocated = 0
        for inp in tx.inputs:
            unallocated += self.store.take_balance(inp.prev_txid, inp.prev_vout)

        allocated = [0] * len(tx.outputs)

        # (4) the land step: value every input sat at W(c)
        landed = 0
        if artifact is not None and artifact.land:
            if chainwork is None:
                chainwork = self.valuer.chainwork(height)
            work = self.valuer.latent_work(tx, height)
            landed = seeds_from_work(work, chainwork)
            if landed > U128_MAX:        # constants.md, Arithmetic width
                landed = 0
            if landed > 0:
                unallocated += landed
            # Every output sat is plain from here on, even for a void
            # land (zero latent value) and for a cenotaph with LAND.
            self.store.add_land(txid, height, landed, isinstance(artifact, Cenotaph))

        # (5) edicts
        if isinstance(artifact, Envelope):
            for edict in artifact.edicts:
                if edict.id != ORCHID:
                    continue                # unknown asset: void edict
                balance = unallocated
                output = edict.output
                assert output <= len(tx.outputs)

                if output == len(tx.outputs):
                    destinations = [
                        i for i, o in enumerate(tx.outputs)
                        if not is_op_return(o.script_pubkey)
                    ]
                    if destinations:
                        if edict.amount == 0:
                            share = balance // len(destinations)
                            remainder = balance % len(destinations)
                            for i, out_idx in enumerate(destinations):
                                give = share + 1 if i < remainder else share
                                if give > 0:
                                    balance -= give
                                    allocated[out_idx] += give
                        else:
                            for out_idx in destinations:
                                give = min(edict.amount, balance)
                                if give > 0:
                                    balance -= give
                                    allocated[out_idx] += give
                else:
                    give = balance if edict.amount == 0 else min(edict.amount, balance)
                    if give > 0:
                        balance -= give
                        allocated[output] += give

                unallocated = balance

        # (6) remainder: burn on a cenotaph, else pointer, else the
        # first non-OP_RETURN output, else burn
        if isinstance(artifact, Cenotaph):
            burned += unallocated + sum(allocated)
            return chainwork, burned

        vout = artifact.pointer if isinstance(artifact, Envelope) else None
        if vout is None:
            for i, o in enumerate(tx.outputs):
                if not is_op_return(o.script_pubkey):
                    vout = i
                    break
        if vout is not None:
            allocated[vout] += unallocated
        else:
            burned += unallocated

        # flush: seeds an edict aimed at an OP_RETURN output are burned
        for i, amount in enumerate(allocated):
            if amount <= 0:
                continue
            if is_op_return(tx.outputs[i].script_pubkey):
                burned += amount
                continue
            self.store.add_balance(txid, i, amount)

        return chainwork, burned


# ---------------------------------------------------------------------------
# Production valuer: lineage/walker.py over a full node
# ---------------------------------------------------------------------------

class WalkerValuer:
    """Values lands by walking input lineages through Bitcoin Core RPC.

    The walker's node budget is set unbounded and any walk that is not
    deterministic (budget, RPC failure, anomaly) raises
    NonDeterministicWalk so the indexer stops instead of diverging.
    """

    def __init__(self, rpc, cache, table, is_landed: Callable[[bytes], bool],
                 dust_seeds: int = 1):
        from claimstones.lineage import walker as w
        self._w = w
        self.rpc = rpc
        self.cache = cache
        self.table = table
        self.dust = dust_seeds
        self._is_landed = is_landed
        self._chainwork: dict[int, int] = {}

    def chainwork(self, height: int) -> int:
        cw = self._chainwork.get(height)
        if cw is None:
            block_hash = self.rpc("getblockhash", height)
            cw = int(self.rpc("getblockheader", block_hash)["chainwork"], 16)
            if len(self._chainwork) > 1000:
                self._chainwork.clear()
            self._chainwork[height] = cw
        return cw

    def latent_work(self, tx: Tx, height: int) -> int:
        w = self._w
        chainwork = self.chainwork(height)
        walker = w.LineageWalker(
            self.rpc, self.cache, self.table, max_nodes=1 << 62,
            dust=self.dust, tip=height, chainwork=chainwork,
            landed=lambda txid_hex: self._is_landed(bytes.fromhex(txid_hex)[::-1]),
        )
        total = 0
        if tx.is_coinbase:
            # The coinbase's sat stream is the subsidy then the block's
            # fee tails; walking its own outputs covers exactly that.
            ranges = [(tx.txid_hex, i, o.value) for i, o in enumerate(tx.outputs) if o.value > 0]
        else:
            ranges = []
            for inp in tx.inputs:
                prev_hex = inp.prev_txid[::-1].hex()
                prev = walker._get_tx(prev_hex)
                if prev is None:
                    raise NonDeterministicWalk(f"cannot fetch {prev_hex}")
                value = w.dec_sats(prev["vout"][inp.prev_vout]["value"])
                ranges.append((prev_hex, inp.prev_vout, value))
        for txid_hex, vout, value in ranges:
            result = walker.walk_utxo(txid_hex, vout, value)
            if not result.deterministic:
                raise NonDeterministicWalk(f"walk of {txid_hex}:{vout} truncated")
            total += w.work_scaled(result.birth, self.table)
        return total


# ---------------------------------------------------------------------------
# Resumable CLI driver
# ---------------------------------------------------------------------------

def main() -> int:
    from claimstones.lineage import walker as w

    parser = argparse.ArgumentParser(description="orchid balance index (replay)")
    parser.add_argument("--pynode", default=PYNODE_DIR, help="Pynode block storage dir")
    parser.add_argument("--rpc-url", default="http://127.0.0.1:8332")
    parser.add_argument("--rpc-user", default=None)
    parser.add_argument("--rpc-pass", default=None)
    parser.add_argument("--rpc-cookie", default=None)
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--db", default=os.path.join("index", "orchids.db"))
    parser.add_argument("--walk-cache", default=os.path.join("index", "walk_cache.sqlite"))
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--progress-every", type=int, default=5000)
    args = parser.parse_args()

    auth = w.resolve_rpc_auth(args.no_auth, args.rpc_user, args.rpc_pass, args.rpc_cookie)
    rpc = w.Rpc(args.rpc_url, auth)
    w.require_txindex(rpc)
    tip = rpc("getblockchaininfo")["blocks"]

    bf = BlockFile(args.pynode)
    store = OrchidStore(args.db)
    cache = w.Cache(args.walk_cache, fresh=False)
    table = w.WorkTable(rpc, cache, tip)
    valuer = WalkerValuer(rpc, cache, table, store.is_landed)
    indexer = OrchidIndexer(store, valuer)

    resumed = store.last_indexed_height()
    if args.start is not None:
        start = args.start
    else:
        floor = (bf.min_height if ACTIVATION_HEIGHT is None
                 else max(bf.min_height, ACTIVATION_HEIGHT))
        start = resumed + 1 if resumed is not None else floor
    end = args.end if args.end is not None else bf.max_height

    print(f"orchid index pass {start}..{end} -> {args.db}")
    t0 = time.time()
    scanned = 0
    for height in range(start, end + 1):
        if height not in bf.by_height:
            print(f"WARNING: block {height} missing, stopping")
            break
        block = parse_block(bf.read_block(height), height)
        indexer.index_block(block)
        scanned += 1
        if scanned % args.progress_every == 0:
            rate = scanned / (time.time() - t0)
            left = (end - height) / rate / 60 if rate else 0
            print(f"  {height} ({rate:.1f} blk/s, ~{left:.0f} min left)")

    store.close()
    print(f"done: {scanned} blocks in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
