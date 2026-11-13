"""Birth index — forward replay of every sat's birth height (prototype).

For every unspent output the index holds an ordered list of runs
(birth_height, sat_count): the output's sats in ordinal FIFO order,
grouped by the coinbase block that created them. A run with the PLAIN
height marks sats whose latent value was already landed.

Connecting a block (docs/spec/land.md, Lineage walk):

  - a non-coinbase transaction concatenates its inputs' runs in input
    order, cuts them across the outputs in output order by value, and
    the leftover is its fee tail;
  - the coinbase's stream is s(h) sats born at h followed by the fee
    tails of the block's transactions in block order, cut across the
    coinbase outputs the same way; whatever the miner did not claim is
    never born;
  - a transaction whose envelope carries LAND (valid or cenotaph) makes
    every output run PLAIN.

Adjacent runs with the same height merge, so a run list is usually far
shorter than the ordinals sat-range list for the same output.

Each row also carries the output's script (what an address encodes),
with a secondary index on it: the address table. Check finds a user's
outpoints by script and their runs in the same row, so the node's UTXO
scan is never needed.

This is the measurement prototype for the design discussed 2026-09-17:
replay from genesis, then one small update per block. It records the
numbers that decide the design (runs per UTXO, bytes per UTXO, blocks
per second) and keeps an undo log so a block can be disconnected.

Run next to a Bitcoin Core node (no txindex needed; blocks come from
getblock verbosity 0):

    python -m claimstones.indexer.birth --rpc-url http://127.0.0.1:8332 \
        --rpc-user U --rpc-pass P --db D:/claimstones/birth.sqlite --end 400000
"""

from __future__ import annotations

import argparse
import os
import json
import sqlite3
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
from claimstones.bitcoin.txparse import Block, Tx, parse_block
from claimstones.lineage.walker import (Rpc, RpcCookieWait, RpcError,
                                       RpcMethodMissing, resolve_rpc_auth,
                                       resolve_rpc_cookie_path, subsidy_sats)
from claimstones.protocol.codec import (ACTIVATION_HEIGHT, MAGIC,
                                        activation_height, decipher)

PLAIN = 0xFFFFFFFF  # run height meaning "landed already; no latent value"

BIP30_HEIGHTS = {91842, 91880}   # duplicate coinbase txids (BIP30)

# The counters both stores keep in meta (reporting, never consensus).
# minted/burned are the ledger half's (docs/spec/indexer.md §Derived).
STAT_KEYS = ("utxos", "runs", "bytes", "script_bytes", "txs", "lands",
             "minted", "burned")

Run = tuple[int, int]   # (birth height or PLAIN, sat count)


# ---------------------------------------------------------------------------
# Run lists
# ---------------------------------------------------------------------------

def merge_runs(runs: Iterable[Run]) -> list[Run]:
    """Concatenate runs, merging neighbours with the same height and
    dropping empty ones."""
    out: list[Run] = []
    for h, n in runs:
        if n <= 0:
            continue
        if out and out[-1][0] == h:
            out[-1] = (h, out[-1][1] + n)
        else:
            out.append((h, n))
    return out


def extend_merged(stream: list[Run], runs: list[Run]) -> None:
    """Append an already-merged run list to an already-merged stream,
    merging only at the boundary."""
    if not runs:
        return
    if stream and stream[-1][0] == runs[0][0]:
        stream[-1] = (runs[0][0], stream[-1][1] + runs[0][1])
        stream.extend(runs[1:])
    else:
        stream.extend(runs)


def cut_runs(stream: list[Run], sizes: list[int]) -> tuple[list[list[Run]], list[Run]]:
    """Cut a merged run stream into consecutive pieces of the given sizes
    (FIFO). Returns (pieces, leftover). A piece is empty if its size is
    0; if the stream runs short, the remaining pieces are cut short and
    leftover is empty. Pieces of a merged stream are merged already."""
    pieces: list[list[Run]] = []
    i = 0                       # current run index
    off = 0                     # sats already taken from stream[i]
    total = len(stream)
    for size in sizes:
        piece: list[Run] = []
        need = size
        while need > 0 and i < total:
            h, n = stream[i]
            avail = n - off
            if need >= avail:
                piece.append((h, avail) if off else stream[i])
                need -= avail
                i += 1
                off = 0
            else:
                piece.append((h, need))
                off += need
                need = 0
        pieces.append(piece)
    leftover: list[Run] = []
    if i < total:
        h, n = stream[i]
        leftover.append((h, n - off))
        leftover.extend(stream[i + 1:])
    return pieces, leftover


RUN_SIZE = 12           # uint32 height, uint64 count, little-endian
_HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35)


def encode_runs(runs: list[Run]) -> bytes:
    """Fixed-width pairs (uint32 height, uint64 count); PLAIN is 0xFFFFFFFF."""
    flat = [x for run in runs for x in run]
    return struct.pack(f"<{'IQ' * len(runs)}", *flat)


def decode_runs(data: bytes) -> list[Run]:
    return list(struct.iter_unpack("<IQ", data))


def run_count(data: bytes) -> int:
    return len(data) // RUN_SIZE


def _old_varint(data: bytes, i: int) -> tuple[int, int]:
    """Schema 2 varint reader, kept for the migration only."""
    v = 0
    shift = 0
    while True:
        b = data[i]
        i += 1
        v |= (b & 0x7F) << shift
        if b < 0x80:
            return v, i
        shift += 7


def runs_total(runs: list[Run]) -> int:
    return sum(n for _, n in runs)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class BirthStore:
    """utxo(txid, vout) -> encoded runs; undo log per block; counters.

    Bulk-build tuning: WAL with synchronous=NORMAL and one commit every
    `commit_every` blocks. A crash loses at most that many blocks; the
    replay resumes from the last committed height. NORMAL, not OFF: with
    OFF a power loss can leave a file that passes quick_check but has
    lost single rows (2026-09-18 on the node machine, found at block
    629 743 by a spend of an output the index did not have). In WAL
    mode NORMAL syncs once per checkpoint, nothing per statement.

    Write-back cache: outputs created since the last commit live in the
    `hot` dict, not in SQLite. An output spent inside the commit window
    (most of them) never reaches the database; the survivors are
    written at commit time in one key-sorted batch. This is what keeps
    the random B-tree traffic from dominating once the table outgrows
    the page cache, the same trick as Bitcoin Core's dbcache.
    """

    SCHEMA = "3"     # 2: script column + address index; 3: fixed-width runs (2026-09-18)

    def __init__(self, path: str, commit_every: int = 1000, cache_mb: int = 256,
                 undo_from: int = 0):
        """undo_from: heights below it write no undo rows. During the
        initial sync nothing can reorg old blocks, and the undo rows are
        about half the writes; the CLI sets it a retarget period below
        the tip."""
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.path = path
        self.undo_from = undo_from
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(f"PRAGMA cache_size=-{cache_mb * 1024}")   # page cache, KiB
        # Not MEMORY: the replay itself needs no temp space, but the one
        # operation that does -- CREATE INDEX utxo_by_script near the tip --
        # sorts (script, txid, vout) over every row. On a WITHOUT ROWID table
        # the index entry carries the whole primary key, so at mainnet scale
        # that sort is ~13 GB; in memory, next to the page cache, it is an
        # out-of-memory failure at the end of a week-long build. On disk it
        # also lands on the OS temp volume, off the spindle holding the db.
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA wal_autocheckpoint=50000")          # ~200 MB between checkpoints
        self.hot: dict[tuple[bytes, int], tuple[bytes, bytes]] = {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS utxo (
                txid BLOB NOT NULL, vout INTEGER NOT NULL,
                script BLOB NOT NULL, runs BLOB NOT NULL,
                PRIMARY KEY (txid, vout)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS undo (
                height INTEGER NOT NULL, txid BLOB NOT NULL, vout INTEGER NOT NULL,
                script BLOB, runs BLOB, PRIMARY KEY (height, txid, vout)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        schema = self.get_meta("schema")
        if schema is None:
            self.set_meta("schema", self.SCHEMA)
        elif schema == "2":
            self._migrate_2_to_3()
        elif schema != self.SCHEMA:
            self.db.close()
            raise RuntimeError(
                f"{path}: birth index schema {schema}, this code writes schema {self.SCHEMA}; "
                f"start a fresh database (delete the file and its -wal/-shm)")
        self.commit_every = commit_every
        self.in_tx = False
        self.stats = {k: int(self.get_meta(k) or 0) for k in STAT_KEYS}

    def has_script_index(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='utxo_by_script'").fetchone() is not None

    def ensure_script_index(self) -> bool:
        """Build the address index (once). During the initial sync it is
        absent so every insert and delete touches one B-tree, not two;
        it is built when the sync nears the tip. Returns True if built."""
        if self.has_script_index():
            return False
        self.commit()
        t = time.time()
        print("building the address index over "
              f"{fmt(self.stats['utxos'])} rows ...", flush=True)
        self.db.execute("CREATE INDEX utxo_by_script ON utxo (script)")
        print(f"address index built in {time.time() - t:.0f}s", flush=True)
        return True

    def drop_script_index(self) -> None:
        self.commit()
        self.db.execute("DROP INDEX IF EXISTS utxo_by_script")

    def _migrate_2_to_3(self) -> None:
        """Re-encode every run list from varint pairs to fixed width."""
        print("migrating birth index from schema 2 to 3 (re-encoding runs) ...", flush=True)

        def old_decode(data: bytes) -> list[Run]:
            runs, i, n = [], 0, len(data)
            while i < n:
                h, i = _old_varint(data, i)
                c, i = _old_varint(data, i)
                runs.append((PLAIN if h == 0 else h - 1, c))
            return runs

        self.db.execute("BEGIN")
        done = 0
        for table in ("utxo", "undo"):
            if table == "utxo":
                cur = self.db.execute("SELECT txid, vout, runs FROM utxo")
                batch = []
                for txid, vout, data in cur.fetchall():
                    batch.append((encode_runs(old_decode(data)), txid, vout))
                    if len(batch) >= 100_000:
                        self.db.executemany("UPDATE utxo SET runs=? WHERE txid=? AND vout=?", batch)
                        done += len(batch)
                        batch = []
                        print(f"  {done:,} rows", flush=True)
                self.db.executemany("UPDATE utxo SET runs=? WHERE txid=? AND vout=?", batch)
                done += len(batch)
            else:
                batch = [(encode_runs(old_decode(data)), h, txid, vout) for h, txid, vout, data in
                         self.db.execute("SELECT height, txid, vout, runs FROM undo WHERE runs IS NOT NULL").fetchall()]
                self.db.executemany("UPDATE undo SET runs=? WHERE height=? AND txid=? AND vout=?", batch)
        self.set_meta("schema", "3")
        self.db.execute("COMMIT")
        print(f"migrated {done:,} rows", flush=True)

    # --- meta ---

    def get_meta(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def del_meta(self, key: str) -> None:
        self.db.execute("DELETE FROM meta WHERE key=?", (key,))

    def last_height(self) -> Optional[int]:
        v = self.get_meta("height")
        return int(v) if v is not None else None

    # --- transactions ---

    def begin(self) -> None:
        if not self.in_tx:
            self.db.execute("BEGIN")
            self.in_tx = True

    def flush_hot(self) -> None:
        """Write the surviving hot outputs to SQLite, sorted by key."""
        if not self.hot:
            return
        self.begin()
        rows = [(k[0], k[1], v[0], v[1]) for k, v in sorted(self.hot.items())]
        self.db.executemany("INSERT INTO utxo VALUES (?, ?, ?, ?)", rows)
        self.hot.clear()

    def commit(self) -> None:
        self.flush_hot()
        for k, v in self.stats.items():
            self.set_meta(k, str(v))
        if self.in_tx:
            self.db.execute("COMMIT")
            self.in_tx = False

    def rollback(self) -> None:
        """Drop everything since the last commit and reload the counters."""
        self.hot.clear()
        if self.in_tx:
            self.db.execute("ROLLBACK")
            self.in_tx = False
        self.stats = {k: int(self.get_meta(k) or 0) for k in self.stats}

    def repair(self) -> int:
        """Undo any block applied beyond the committed height (a crash
        between a block's writes and its commit). Returns the number
        of blocks undone."""
        last = self.last_height()
        if last is None:
            return 0
        rows = self.db.execute("SELECT DISTINCT height FROM undo WHERE height>? ORDER BY height DESC",
                               (last,)).fetchall()
        self.begin()
        for (h,) in rows:
            self.undo_block(h)
        self.commit()
        return len(rows)

    def close(self) -> None:
        self.commit()
        self.db.close()

    # --- utxo ---

    def _count(self, script: bytes, data: bytes, sign: int) -> None:
        self.stats["utxos"] += sign
        self.stats["runs"] += sign * run_count(data)
        self.stats["bytes"] += sign * len(data)
        self.stats["script_bytes"] += sign * len(script)

    def take(self, txid: bytes, vout: int, height: int) -> Optional[list[Run]]:
        """Read and delete an outpoint's runs, logging them for undo."""
        hot = self.hot.pop((txid, vout), None)
        if hot is not None:
            script, data = hot
        elif _HAS_RETURNING:
            row = self.db.execute("DELETE FROM utxo WHERE txid=? AND vout=? RETURNING script, runs",
                                  (txid, vout)).fetchone()
            if row is None:
                return None
            script, data = row
        else:
            row = self.db.execute("SELECT script, runs FROM utxo WHERE txid=? AND vout=?", (txid, vout)).fetchone()
            if row is None:
                return None
            script, data = row
            self.db.execute("DELETE FROM utxo WHERE txid=? AND vout=?", (txid, vout))
        if height >= self.undo_from:
            # created and spent in the same block: the two cancel in the undo log
            cur = self.db.execute("DELETE FROM undo WHERE height=? AND txid=? AND vout=? AND runs IS NULL",
                                  (height, txid, vout))
            if cur.rowcount == 0:
                self.db.execute("INSERT INTO undo VALUES (?, ?, ?, ?, ?)", (height, txid, vout, script, data))
        self._count(script, data, -1)
        return decode_runs(data)

    def put(self, txid: bytes, vout: int, script: bytes, runs: list[Run], height: int) -> None:
        data = encode_runs(runs)
        if height in BIP30_HEIGHTS:
            # two early coinbases reused a txid and overwrote the earlier
            # unspent output; mirror that (the only heights it happened)
            self.flush_hot()
            old = self.db.execute("SELECT script, runs FROM utxo WHERE txid=? AND vout=?", (txid, vout)).fetchone()
            if old is not None:
                self._count(old[0], old[1], -1)
            self.db.execute("INSERT OR REPLACE INTO utxo VALUES (?, ?, ?, ?)", (txid, vout, script, data))
        else:
            self.hot[(txid, vout)] = (script, data)
        if height >= self.undo_from:
            self.db.execute("INSERT OR REPLACE INTO undo VALUES (?, ?, ?, NULL, NULL)", (height, txid, vout))
        self._count(script, data, +1)

    def get(self, txid: bytes, vout: int) -> Optional[list[Run]]:
        hot = self.hot.get((txid, vout))
        if hot is not None:
            return decode_runs(hot[1])
        row = self.db.execute("SELECT runs FROM utxo WHERE txid=? AND vout=?", (txid, vout)).fetchone()
        return decode_runs(row[0]) if row else None

    def by_script(self, script: bytes) -> list[tuple[bytes, int, list[Run]]]:
        """The address table lookup: every unspent output locked by `script`.
        A full scan until ensure_script_index() has run."""
        rows = self.db.execute("SELECT txid, vout, runs FROM utxo WHERE script=?", (script,)).fetchall()
        rows += [(k[0], k[1], v[1]) for k, v in self.hot.items() if v[0] == script]
        return [(txid, vout, decode_runs(runs)) for txid, vout, runs in sorted(rows)]

    # --- undo ---

    def undo_block(self, height: int) -> None:
        """Revert a connected block: re-create spent outpoints, drop created ones."""
        self.flush_hot()
        rows = self.db.execute("SELECT txid, vout, script, runs FROM undo WHERE height=?", (height,)).fetchall()
        for txid, vout, script, runs in rows:
            if runs is None:
                row = self.db.execute("SELECT script, runs FROM utxo WHERE txid=? AND vout=?", (txid, vout)).fetchone()
                if row is not None:
                    self.db.execute("DELETE FROM utxo WHERE txid=? AND vout=?", (txid, vout))
                    self._count(row[0], row[1], -1)
            else:
                self.db.execute("INSERT INTO utxo VALUES (?, ?, ?, ?)", (txid, vout, script, runs))
                self._count(script, runs, +1)
        self.db.execute("DELETE FROM undo WHERE height=?", (height,))

    def prune_undo(self, below_height: int) -> None:
        self.db.execute("DELETE FROM undo WHERE height<?", (below_height,))

    def file_size(self) -> int:
        if self.path == ":memory:":
            return 0
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

class MissingInput(RuntimeError):
    pass


def _has_envelope(tx: Tx) -> bool:
    return any(o.script_pubkey[:2] == MAGIC for o in tx.outputs)


class BirthIndexer:
    def __init__(self, store: BirthStore, undo_depth: int = 1000,
                 chain: str = "main"):
        self.store = store
        # Consensus, and deliberately not an option: protocol/codec.py
        # holds the per-chain rule, and `chain` comes from the node's
        # getblockchaininfo, never from a flag.
        self.activation_height = activation_height(chain)
        self.undo_depth = undo_depth

    def connect_block(self, block: Block) -> None:
        store = self.store
        height = block.height
        store.begin()
        fee_tails: list[list[Run]] = []
        for tx in block.txs[1:]:
            fee_tails.append(self._connect_tx(tx, height))
        coinbase = block.txs[0]
        stream: list[Run] = [(height, subsidy_sats(height))] if subsidy_sats(height) else []
        for tail in fee_tails:
            extend_merged(stream, tail)
        self._place(coinbase, stream, height)
        store.stats["txs"] += len(block.txs)
        store.set_meta("height", str(height))
        if height % self.store.commit_every == 0:
            if self.undo_depth is not None:
                store.prune_undo(height - self.undo_depth)
            store.commit()

    def disconnect_block(self, height: int) -> None:
        self.store.begin()
        self.store.undo_block(height)
        self.store.set_meta("height", str(height - 1))
        self.store.commit()

    def _connect_tx(self, tx: Tx, height: int) -> list[Run]:
        stream: list[Run] = []
        for inp in tx.inputs:
            runs = self.store.take(inp.prev_txid, inp.prev_vout, height)
            if runs is None:
                raise MissingInput(f"{tx.txid_hex} spends unknown {inp.prev_txid[::-1].hex()}:{inp.prev_vout}")
            extend_merged(stream, runs)
        return self._place(tx, stream, height)

    def _place(self, tx: Tx, stream: list[Run], height: int) -> list[Run]:
        pieces, leftover = cut_runs(stream, [o.value for o in tx.outputs])
        land = False
        # A None activation height means the constant is still undecided, so
        # nothing lands and the stream carries only real birth heights. Note
        # what the collapse below does: it replaces this output's runs in the
        # stored row, and that row is the stream a later spend reads, so PLAIN
        # propagates forward. Landedness flows with the sats exactly as a
        # birth height does -- see docs/birth-index-check.md.
        if (self.activation_height is not None
                and height >= self.activation_height and _has_envelope(tx)):
            artifact = decipher(tx)
            land = artifact is not None and artifact.land
        for vout, runs in enumerate(pieces):
            # a zero-value output is still an outpoint a later tx may
            # spend; it is stored with an empty run list
            if land and runs:
                runs = [(PLAIN, runs_total(runs))]
            self.store.put(tx.txid, vout, tx.outputs[vout].script_pubkey, runs, height)
        if land:
            self.store.stats["lands"] += 1
        return leftover


# ---------------------------------------------------------------------------
# Block source and CLI
# ---------------------------------------------------------------------------

RPC_IN_WARMUP = -28      # Core while it loads the block index after a restart


def transient_rpc(e: BaseException) -> bool:
    """True for the states a node clears by itself: gone, restarting, or
    still behind our height. Everything else — bad auth, a missing method,
    a parse failure, a MissingInput — is a real fault and must not be
    waited out, or a broken index would look like a slow one."""
    if isinstance(e, RpcMethodMissing):
        return False
    if isinstance(e, RpcError):
        # -28 while the block index loads; "out of range" while the node is
        # behind the height we asked for (it restarted into a catch-up).
        return e.code == RPC_IN_WARMUP or "out of range" in str(e).lower()
    if isinstance(e, RpcCookieWait):
        # 401 before the restarting node has written its new .cookie —
        # the next attempt re-reads the file. Only cookie-backed clients
        # raise this; fixed credentials keep the fatal 401 below.
        return True
    if isinstance(e, RuntimeError):
        m = str(e)
        if "authentication failed" in m:
            return False
        # "cannot reach RPC": refused, reset or timed out, three times over.
        # "non-JSON response": Core's HTTP layer answering mid start or stop.
        return "cannot reach RPC" in m or "non-JSON response" in m
    return False


def wait_for_rpc(fn, what: str, wait_max: float = 0.0,
                 first_delay: float = 5.0, max_delay: float = 60.0):
    """Call fn(), waiting out an absent node instead of ending the run.

    The index is built over days beside a node that will be restarted for
    its own reasons, and the blocks applied since the last commit live only
    in this process: dying costs that whole window (up to --commit-every
    blocks), so waiting is always cheaper. wait_max 0 waits as long as it
    takes; any other value is a budget in seconds.
    """
    waited = 0.0
    delay = first_delay
    next_report = 0.0
    while True:
        try:
            out = fn()
        except Exception as e:
            if not transient_rpc(e):
                raise
            if wait_max and waited >= wait_max:
                raise RuntimeError(f"node still unavailable after {waited:.0f}s "
                                   f"while {what}: {e}") from None
            if waited >= next_report:      # first failure, then every 5 min
                print(f"  node unavailable while {what}: {e}", flush=True)
                next_report = waited + 300.0
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, max_delay)
        else:
            if waited:
                print(f"  node back after {waited:.0f}s, resuming {what}",
                      flush=True)
            return out


class RpcBlocks:
    """Blocks by height via batched getblockhash + getblock(hash, 0):
    `chunk` blocks per round trip, `workers` chunks in flight, so a
    remote node's latency is paid once per chunk, not per block.

    A chunk whose node has gone away waits for it to come back rather than
    ending the run; parsing stays outside that wait, so a bad block still
    fails loudly."""

    def __init__(self, rpc: Rpc, chunk: int = 64, workers: int = 3,
                 timeout: int = 300, wait_max: float = 0.0):
        self.rpc = rpc
        self.chunk = chunk
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.timeout = timeout
        self.wait_max = wait_max

    def _fetch_raw(self, heights: list[int]) -> list[str]:
        hashes = self.rpc.batch([("getblockhash", h) for h in heights],
                                timeout=self.timeout)
        return self.rpc.batch([("getblock", h, 0) for h in hashes],
                              timeout=self.timeout)

    def _fetch_chunk(self, first: int, last: int) -> list[Block]:
        heights = list(range(first, last + 1))
        raws = wait_for_rpc(lambda: self._fetch_raw(heights),
                            f"fetching blocks {first}..{last}",
                            wait_max=self.wait_max)
        return [parse_block(bytes.fromhex(raw), h) for h, raw in zip(heights, raws)]

    def iter(self, start: int, end: int):
        chunks = [(a, min(a + self.chunk - 1, end)) for a in range(start, end + 1, self.chunk)]
        pending = {}
        for i in range(min(len(chunks), self.pool._max_workers)):
            pending[i] = self.pool.submit(self._fetch_chunk, *chunks[i])
        for i in range(len(chunks)):
            blocks = pending.pop(i).result()
            ahead = i + self.pool._max_workers
            if ahead < len(chunks):
                pending[ahead] = self.pool.submit(self._fetch_chunk, *chunks[ahead])
            yield from blocks


def write_status(path: str, height: int, store, t0: float) -> None:
    """One small JSON file, rewritten as the build advances.

    The log speaks once per --report-every blocks, which at mainnet scale is
    the better part of an hour -- too coarse for a monitor to show live
    progress, and too coarse to extrapolate across, because throughput falls
    from one window to the next. This is the cheap exact answer: a couple of
    hundred bytes, written via a temp file and os.replace so a reader never
    sees half a record.

    Nothing here may raise.  Windows refuses to replace a file that another
    process holds open -- a monitor reading the status, or a virus scanner
    touching the new temp file -- with WinError 5, and that is exactly what
    a monitor polling twice a second does to a file rewritten every block.
    A week-long build must not die for a progress report, so the replace
    gets a few short retries and then the record is simply skipped: the
    next block writes another one.
    """
    s = store.stats
    doc = {"height": height, "txs": s["txs"], "utxos": s["utxos"],
           "runs": s["runs"], "run_bytes": s["bytes"], "lands": s["lands"],
           "hot": len(store.hot), "db_bytes": store.file_size(),
           "started": t0, "t": time.time()}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                if attempt == 5:
                    return
                time.sleep(0.02)
    except OSError:
        return


def fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ")


def main() -> int:
    ap = argparse.ArgumentParser(description="birth index prototype: forward replay of sat birth heights")
    ap.add_argument("--rpc-url", default="http://127.0.0.1:8332")
    ap.add_argument("--rpc-user")
    ap.add_argument("--rpc-pass")
    ap.add_argument("--rpc-cookie")
    ap.add_argument("--no-auth", action="store_true")
    ap.add_argument("--rpc-timeout", type=int, default=300,
                    help="seconds for one batched RPC round trip (default 300); "
                         "a node that listens but never answers is noticed after this")
    ap.add_argument("--rpc-wait-max", type=int, default=0,
                    help="seconds to wait for an absent node before giving up "
                         "(default 0: wait for as long as it takes, so a node "
                         "restart does not end the run)")
    ap.add_argument("--db", default=os.path.join("index", "birth.sqlite"))
    ap.add_argument("--start", type=int, default=None, help="default: resume after the last committed height")
    ap.add_argument("--end", type=int, default=None, help="default: node tip")
    ap.add_argument("--report-every", type=int, default=1000)
    ap.add_argument("--status", default=None,
                    help="JSON status file for a monitor, rewritten every "
                         "--status-every blocks (default: birth.status beside "
                         "the db); pass '' to switch it off")
    ap.add_argument("--stop-file", default=None,
                    help="a path whose existence asks the build to finish the "
                         "block it is on, commit it and exit cleanly (default: "
                         "birth.stop beside the db); pass '' to switch it off")
    ap.add_argument("--status-every", type=int, default=1,
                    help="blocks between status writes (default 1: a few "
                         "hundred buffered bytes per block)")
    ap.add_argument("--commit-every", type=int, default=1000,
                    help="blocks per commit; also the write-back window (default 1000)")
    ap.add_argument("--cache-mb", type=int, default=8192, help="SQLite page cache (default 8 GB)")
    ap.add_argument("--undo-from", type=int, default=None,
                    help="write undo rows from this height (default: tip minus 2016)")
    ap.add_argument("--profile", type=int, default=None, metavar="N",
                    help="profile the next N blocks, print the hot spots, stop")
    args = ap.parse_args()

    auth = resolve_rpc_auth(args.no_auth, args.rpc_user, args.rpc_pass, args.rpc_cookie)
    rpc = Rpc(args.rpc_url, auth,
              cookie_path=resolve_rpc_cookie_path(args.no_auth, args.rpc_user,
                                                  args.rpc_cookie))
    tip = wait_for_rpc(lambda: rpc("getblockchaininfo"), "reading the node tip",
                       wait_max=args.rpc_wait_max)["blocks"]
    total_txs = int(wait_for_rpc(lambda: rpc("getchaintxstats"),
                                 "reading the chain tx count",
                                 wait_max=args.rpc_wait_max)["txcount"])
    undo_from = args.undo_from if args.undo_from is not None else max(0, tip - 2016)

    store = BirthStore(args.db, commit_every=args.commit_every, cache_mb=args.cache_mb,
                       undo_from=undo_from)
    repaired = store.repair()
    if repaired:
        print(f"repaired: undid {repaired} half-applied block(s)", flush=True)
    indexer = BirthIndexer(store)
    last = store.last_height()
    start = args.start if args.start is not None else (last + 1 if last is not None else 0)
    end = args.end if args.end is not None else tip
    if start < undo_from and store.has_script_index():
        print("initial sync: dropping the address index until near the tip", flush=True)
        store.drop_script_index()
    if args.profile:
        end = min(end, start + args.profile - 1)
    if start > end:
        print(f"nothing to do: index at {last}, end {end}")
        return 0
    print(f"birth index: blocks {start}..{end} -> {args.db}  (node tip {tip}, "
          f"{fmt(total_txs)} txs on chain, undo from {undo_from}, cache {args.cache_mb} MB, "
          f"activation {ACTIVATION_HEIGHT if ACTIVATION_HEIGHT is not None else 'undecided: no lands'})",
          flush=True)

    status_path = args.status
    if status_path is None:
        status_path = os.path.join(os.path.dirname(os.path.abspath(args.db)),
                                   "birth.status")

    # Asking for a stop by killing the process works -- an uncommitted block
    # has no commit record and repair() undoes a half-applied one -- but it
    # throws away every block since the last commit, it leaves the WAL for
    # the next process to recover, and on Windows it needs elevation to
    # reach a build the scheduled task started in session 0. A file costs
    # one stat per block and loses nothing at all.
    stop_path = args.stop_file
    if stop_path is None:
        stop_path = os.path.join(os.path.dirname(os.path.abspath(args.db)),
                                 "birth.stop")

    blocks = RpcBlocks(rpc, timeout=args.rpc_timeout,
                       wait_max=args.rpc_wait_max)
    t0 = time.time()
    t_last = t0
    txs_start = txs_last = store.stats["txs"]
    done = 0
    done_last = 0
    ok = False
    reached_end = False
    profiler = None
    if args.profile:
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()
    try:
        for block in blocks.iter(start, end):
            if block.height == undo_from:
                store.ensure_script_index()
            indexer.connect_block(block)
            done += 1
            if status_path and done % args.status_every == 0:
                write_status(status_path, block.height, store, t0)
            if block.height % args.report_every == 0:
                now = time.time()
                s = store.stats
                dt = now - t_last
                rate_run = (s["txs"] - txs_start) / (now - t0)     # tx/s since this run started
                remaining = max(total_txs - s["txs"], 0) if end >= tip else None
                eta = f"eta {remaining / rate_run / 3600:.1f} h (at this run's average tx/s)" \
                    if remaining is not None and rate_run else ""
                print(f"  h={block.height}  {(done - done_last) / dt:.1f} blk/s  "
                      f"{(s['txs'] - txs_last) / dt:.0f} tx/s  "
                      f"utxos {fmt(s['utxos'])}  runs {fmt(s['runs'])}  "
                      f"runs/utxo {s['runs'] / max(s['utxos'], 1):.2f}  "
                      f"bytes/utxo {s['bytes'] / max(s['utxos'], 1):.1f}  "
                      f"script b/utxo {s['script_bytes'] / max(s['utxos'], 1):.1f}  "
                      f"unflushed {fmt(len(store.hot))}  db {store.file_size() / 1e9:.2f} GB  {eta}", flush=True)
                t_last = now
                txs_last = s["txs"]
                done_last = done
            if stop_path and os.path.exists(stop_path):
                why = ""
                try:
                    # utf-8-sig, not utf-8: PowerShell's Out-File writes a
                    # BOM, and the reason is echoed into the log.
                    with open(stop_path, encoding="utf-8-sig", errors="replace") as f:
                        why = f.read().strip()
                except OSError:
                    pass
                print(f"  stop requested by {stop_path}"
                      + (f": {why}" if why else "")
                      + f" -- committing {block.height} and exiting", flush=True)
                ok = True
                break
        else:
            # Only a loop that ran to completion has reached the tip.
            ok = True
            reached_end = True
    finally:
        if ok:
            store.commit()
            # The script index sorts (script, txid, vout) over every row of a
            # WITHOUT ROWID table, which at mainnet scale is tens of GB of
            # sorting. It belongs at the end of the build and nowhere else --
            # a stop at height 644 000 satisfies `end >= tip` just as the
            # real finish does, and must not start it.
            if reached_end and end >= tip:
                store.ensure_script_index()
        else:
            store.rollback()     # never commit a half-applied block
        if profiler is not None:
            import pstats
            profiler.disable()
            print(f"\nprofile of {done} blocks, {store.stats['txs'] - txs_start} txs, "
                  f"{time.time() - t0:.1f}s wall:")
            pstats.Stats(profiler).sort_stats("tottime").print_stats(22)
        s = store.stats
        print(f"stopped at {store.last_height()}: {done} blocks in {time.time() - t0:.0f}s; "
              f"utxos {fmt(s['utxos'])}, runs {fmt(s['runs'])}, run bytes {fmt(s['bytes'])}, "
              f"db {store.file_size() / 1e9:.2f} GB, lands {s['lands']}", flush=True)
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
