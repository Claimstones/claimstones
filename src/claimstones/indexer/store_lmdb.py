"""LMDB birth store — the tip daemon's store, same interface as BirthStore.

Opens the environment phase 3 wrote (fastbuild/load.py) and carries the
same semantics as the SQLite BirthStore in birth.py, so BirthIndexer
runs unchanged on either. Sub-databases (spec: fastbuild.md §Phase 3):

    utxo   txid(32) + vout(4 BE) -> script_len u32 + script + runs
    addr   sha256(script) -> outpoint(36), dupsort — maintained here on
           every put/take, where SQLite deferred it to an end-of-build
           CREATE INDEX
    undo   height u32 BE + txid + vout -> b"" for a created outpoint
           (undo = delete it), or the spent row's value (undo = restore
           it); the height prefix keys range scans for undo and prune
    meta   schema, height, and the counters, as text

The ledger half (docs/spec/indexer.md §State, 2026-11-13) adds three
sub-databases beside them, so `utxo` keeps schema 3 and the land-free
snapshot needs no migration:

    bal    outpoint(36) -> seeds u128 BE, only for nonzero balances
    work   height u32 BE -> W(height) u256 BE, cumulative chainwork
    land   txid -> height u32 BE + cenotaph u8 + amount u128 BE

and two more kinds of undo entry, told apart by key length (40 bytes is
the row kind above):

    undo   height + txid + vout + b"B" (41) -> the spent outpoint's old
           balance (undo = restore it; a created outpoint's balance goes
           with its row)
    undo   height + txid + b"L" (37) -> b"" for a land recorded in that
           block (undo = delete the land row)

`work` rows are deleted on undo and rewritten on reconnect.

The write-back cache carries over: outputs created since the last commit
live in the `hot` dict (and their balances in `hot_bal`) and never reach
LMDB if spent inside the window; hot rows are also not yet in `addr`, so
every reader checks both. One write transaction spans a commit window
(begin .. commit), and LMDB's copy-on-write makes a crash lose the
window, never corrupt the file — there are no half-applied blocks, so
repair() is a formality here.

A read-only reader opened on an environment the fused daemon has not
written yet finds no `bal`/`work`/`land`; it then serves the birth half
and answers the ledger questions with "nothing" (0, [], None). Start the
daemon first, the reader after, and the reader sees all three.

Counters: a phase-3 db carries utxos and runs; run bytes are exactly
12 x runs. script_bytes is not derivable from the meta, so on first
open after a handover it starts at 0 and becomes a delta, not a total
(it is reporting, never consensus).

Readers (the wallet server) open with readonly=True on the same
environment; LMDB's single-writer/many-reader model means they never
block the daemon and cannot write.
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
from typing import Optional

import lmdb

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
from claimstones.indexer.birth import (BIP30_HEIGHTS, STAT_KEYS, Run,
                                       decode_runs, encode_runs, run_count)

SLEN = struct.Struct("<I")       # script_len header of a utxo value

UNDO_ROW, UNDO_BAL, UNDO_LAND = 40, 41, 37     # undo key lengths, by kind


def _ukey(txid: bytes, vout: int) -> bytes:
    return txid + vout.to_bytes(4, "big")


def _undo_key(height: int, txid: bytes, vout: int) -> bytes:
    return height.to_bytes(4, "big") + txid + vout.to_bytes(4, "big")


def _undo_bal_key(height: int, txid: bytes, vout: int) -> bytes:
    return _undo_key(height, txid, vout) + b"B"


def _undo_land_key(height: int, txid: bytes) -> bytes:
    return height.to_bytes(4, "big") + txid + b"L"


def _seeds(value: Optional[bytes]) -> int:
    return int.from_bytes(value, "big") if value else 0


def _seeds_bytes(seeds: int) -> bytes:
    return seeds.to_bytes(16, "big")       # OverflowError above u128: a fault


def _land_value(height: int, amount: int, cenotaph: bool) -> bytes:
    return (height.to_bytes(4, "big") + bytes([int(cenotaph)])
            + amount.to_bytes(16, "big"))


def _unland(value: bytes) -> tuple[int, int, bool]:
    return (int.from_bytes(value[:4], "big"), int.from_bytes(value[5:], "big"),
            bool(value[4]))


def _row(script: bytes, data: bytes) -> bytes:
    return SLEN.pack(len(script)) + script + data


def _unrow(value: bytes) -> tuple[bytes, bytes]:
    n = SLEN.unpack_from(value)[0]
    return value[4:4 + n], value[4 + n:]


class LmdbBirthStore:
    """utxo(txid, vout) -> encoded runs; undo log per block; counters."""

    SCHEMA = "3"

    def __init__(self, path: str, commit_every: int = 1000,
                 undo_from: int = 0, map_mb: int = 120 * 1024,
                 readonly: bool = False):
        self.path = path
        self.undo_from = undo_from
        self.commit_every = commit_every
        self.readonly = readonly
        self.env = lmdb.open(path, map_size=map_mb << 20, subdir=True,
                             max_dbs=8, readonly=readonly)
        # no explicit txn: a handle opened in a read-only txn dies with it
        # when the txn is aborted on close (MDB_CREATE is refused there too)
        create = not readonly
        self.d_utxo = self.env.open_db(b"utxo", create=create)
        self.d_addr = self.env.open_db(b"addr", create=create, dupsort=True)
        self.d_undo = self.env.open_db(b"undo", create=create)
        self.d_meta = self.env.open_db(b"meta", create=create)
        # the ledger half's sub-databases; absent in an environment the
        # fused daemon has not written yet, which a reader must tolerate
        self.d_bal = self._open_optional(b"bal", create)
        self.d_work = self._open_optional(b"work", create)
        self.d_land = self._open_optional(b"land", create)
        self.hot: dict[tuple[bytes, int], tuple[bytes, bytes]] = {}
        self.hot_bal: dict[tuple[bytes, int], int] = {}
        self.txn: Optional[lmdb.Transaction] = None
        self.in_tx = False
        schema = self.get_meta("schema")
        if schema is None:
            self.set_meta("schema", self.SCHEMA)
        elif schema != self.SCHEMA:
            self.env.close()
            raise RuntimeError(
                f"{path}: birth index schema {schema}, this code writes "
                f"schema {self.SCHEMA}; start a fresh database")
        self.stats = self._load_stats()

    def _open_optional(self, name: bytes, create: bool):
        if create:
            return self.env.open_db(name, create=True)
        try:
            return self.env.open_db(name, create=False)
        except lmdb.NotFoundError:
            return None

    @property
    def has_ledger(self) -> bool:
        """False for a reader on an environment without the ledger half."""
        return self.d_bal is not None

    def _load_stats(self) -> dict[str, int]:
        stats = {k: int(self.get_meta(k) or 0) for k in STAT_KEYS}
        if stats["bytes"] == 0 and stats["runs"]:
            stats["bytes"] = stats["runs"] * 12       # schema-3 pairs
        return stats

    def reload_stats(self) -> dict[str, int]:
        """Fresh counters from the database. A reader's `stats` are
        otherwise frozen at open while the writer moves on — a serving
        process must reload, not trust its copy."""
        self.stats = self._load_stats()
        return self.stats

    def _read(self):
        """The open write transaction, or a fresh read view."""
        return self.txn if self.in_tx else self.env.begin()

    # --- the SQLite store built its address index once, near the tip;
    # --- here `addr` is maintained on every write and always exists

    def has_script_index(self) -> bool:
        return True

    def ensure_script_index(self) -> bool:
        return False

    def drop_script_index(self) -> None:
        pass

    # --- meta ---

    def get_meta(self, key: str) -> Optional[str]:
        txn = self._read()
        try:
            v = txn.get(key.encode(), db=self.d_meta)
        finally:
            if txn is not self.txn:
                txn.abort()
        return v.decode() if v is not None else None

    def set_meta(self, key: str, value: str) -> None:
        if self.in_tx:
            self.txn.put(key.encode(), value.encode(), db=self.d_meta)
        else:
            with self.env.begin(write=True) as txn:
                txn.put(key.encode(), value.encode(), db=self.d_meta)

    def del_meta(self, key: str) -> None:
        if self.in_tx:
            self.txn.delete(key.encode(), db=self.d_meta)
        else:
            with self.env.begin(write=True) as txn:
                txn.delete(key.encode(), db=self.d_meta)

    def last_height(self) -> Optional[int]:
        v = self.get_meta("height")
        return int(v) if v is not None else None

    # --- transactions ---

    def begin(self) -> None:
        if not self.in_tx:
            self.txn = self.env.begin(write=True)
            self.in_tx = True

    def flush_hot(self) -> None:
        """Write the surviving hot outputs and their address rows."""
        if not self.hot:
            return
        self.begin()
        for (txid, vout), (script, data) in sorted(self.hot.items()):
            key = _ukey(txid, vout)
            # overwrite=False keeps SQLite's integrity error: a duplicate
            # outpoint outside the BIP30 heights is a broken index
            if not self.txn.put(key, _row(script, data), db=self.d_utxo,
                                overwrite=False):
                raise RuntimeError(f"duplicate outpoint "
                                   f"{txid[::-1].hex()}:{vout}")
            self.txn.put(hashlib.sha256(script).digest(), key, db=self.d_addr)
        self.hot.clear()
        for (txid, vout), seeds in sorted(self.hot_bal.items()):
            self.txn.put(_ukey(txid, vout), _seeds_bytes(seeds), db=self.d_bal)
        self.hot_bal.clear()

    def commit(self) -> None:
        self.flush_hot()
        for k, v in self.stats.items():
            self.set_meta(k, str(v))
        if self.in_tx:
            self.txn.commit()
            self.txn = None
            self.in_tx = False

    def rollback(self) -> None:
        """Drop everything since the last commit and reload the counters."""
        self.hot.clear()
        self.hot_bal.clear()
        if self.in_tx:
            self.txn.abort()
            self.txn = None
            self.in_tx = False
        self.stats = self._load_stats()

    def repair(self) -> int:
        """Undo any block applied beyond the committed height. A crashed
        LMDB transaction never commits, so unlike SQLite this finds work
        only after an explicit commit of a half-applied block."""
        last = self.last_height()
        if last is None:
            return 0
        heights = set()
        with self.env.begin(db=self.d_undo) as txn:
            cur = txn.cursor()
            if cur.set_range((last + 1).to_bytes(4, "big")):
                for key in cur.iternext(values=False):
                    heights.add(int.from_bytes(key[:4], "big"))
        self.begin()
        for h in sorted(heights, reverse=True):
            self.undo_block(h)
        self.commit()
        return len(heights)

    def close(self) -> None:
        if not self.readonly:
            self.commit()
        self.env.close()

    # --- utxo ---

    def _count(self, script: bytes, data: bytes, sign: int) -> None:
        self.stats["utxos"] += sign
        self.stats["runs"] += sign * run_count(data)
        self.stats["bytes"] += sign * len(data)
        self.stats["script_bytes"] += sign * len(script)

    def take_row(self, txid: bytes, vout: int,
                 height: int) -> Optional[tuple[list[Run], int]]:
        """Read and delete an outpoint -- its runs and its balance --
        logging both for undo. None if the index does not have it."""
        self.begin()
        key = _ukey(txid, vout)
        hot = self.hot.pop((txid, vout), None)
        if hot is not None:
            script, data = hot
            seeds = self.hot_bal.pop((txid, vout), 0)
        else:
            value = self.txn.get(key, db=self.d_utxo)
            if value is None:
                return None
            script, data = _unrow(value)
            self.txn.delete(key, db=self.d_utxo)
            self.txn.delete(hashlib.sha256(script).digest(), key,
                            db=self.d_addr)
            seeds = _seeds(self.txn.get(key, db=self.d_bal))
            if seeds:
                self.txn.delete(key, db=self.d_bal)
        if height >= self.undo_from:
            # created and spent in the same block: the two cancel
            uk = _undo_key(height, txid, vout)
            if self.txn.get(uk, db=self.d_undo) == b"":
                self.txn.delete(uk, db=self.d_undo)
            else:
                self.txn.put(uk, _row(script, data), db=self.d_undo)
                if seeds:
                    self.txn.put(_undo_bal_key(height, txid, vout),
                                 _seeds_bytes(seeds), db=self.d_undo)
        self._count(script, data, -1)
        return decode_runs(data), seeds

    def take(self, txid: bytes, vout: int, height: int) -> Optional[list[Run]]:
        """take_row for a caller that wants the runs only (the balance is
        removed all the same)."""
        row = self.take_row(txid, vout, height)
        return row[0] if row is not None else None

    def put(self, txid: bytes, vout: int, script: bytes, runs: list[Run],
            height: int, seeds: int = 0) -> None:
        data = encode_runs(runs)
        key = _ukey(txid, vout)
        if height in BIP30_HEIGHTS:
            # two early coinbases reused a txid and overwrote the earlier
            # unspent output; mirror that (the only heights it happened)
            self.flush_hot()
            old = self.txn.get(key, db=self.d_utxo)
            if old is not None:
                old_script, old_data = _unrow(old)
                self._count(old_script, old_data, -1)
                self.txn.delete(hashlib.sha256(old_script).digest(), key,
                                db=self.d_addr)
                self.txn.delete(key, db=self.d_bal)
            self.txn.put(key, _row(script, data), db=self.d_utxo)
            self.txn.put(hashlib.sha256(script).digest(), key, db=self.d_addr)
            if seeds:
                self.txn.put(key, _seeds_bytes(seeds), db=self.d_bal)
        else:
            self.hot[(txid, vout)] = (script, data)
            if seeds:
                self.hot_bal[(txid, vout)] = seeds
        if height >= self.undo_from:
            self.begin()
            self.txn.put(_undo_key(height, txid, vout), b"", db=self.d_undo)
        self._count(script, data, +1)

    def get(self, txid: bytes, vout: int) -> Optional[list[Run]]:
        hot = self.hot.get((txid, vout))
        if hot is not None:
            return decode_runs(hot[1])
        txn = self._read()
        try:
            value = txn.get(_ukey(txid, vout), db=self.d_utxo)
        finally:
            if txn is not self.txn:
                txn.abort()
        return decode_runs(_unrow(value)[1]) if value is not None else None

    def by_script(self, script: bytes) -> list[tuple[bytes, int, list[Run]]]:
        """The address table lookup: every unspent output locked by `script`."""
        rows = []
        txn = self._read()
        try:
            cur = txn.cursor(db=self.d_addr)
            if cur.set_key(hashlib.sha256(script).digest()):
                for key in cur.iternext_dup():
                    value = txn.get(key, db=self.d_utxo)
                    rows.append((key[:32], int.from_bytes(key[32:], "big"),
                                 _unrow(value)[1]))
        finally:
            if txn is not self.txn:
                txn.abort()
        rows += [(k[0], k[1], v[1]) for k, v in self.hot.items()
                 if v[0] == script]
        return [(txid, vout, decode_runs(data))
                for txid, vout, data in sorted(rows)]

    # --- the ledger half: balances ---

    def get_balance(self, txid: bytes, vout: int) -> int:
        """Seeds on an outpoint; 0 when none, unknown, or no ledger."""
        hot = self.hot_bal.get((txid, vout))
        if hot is not None:
            return hot
        if self.d_bal is None:
            return 0
        txn = self._read()
        try:
            return _seeds(txn.get(_ukey(txid, vout), db=self.d_bal))
        finally:
            if txn is not self.txn:
                txn.abort()

    def balances_by_script(self, script: bytes) -> list[tuple[bytes, int, int]]:
        """(txid, vout, seeds) for every unspent output locked by `script`
        that holds a nonzero balance -- what getorchidbalance serves."""
        if self.d_bal is None:
            return []
        rows = []
        txn = self._read()
        try:
            cur = txn.cursor(db=self.d_addr)
            if cur.set_key(hashlib.sha256(script).digest()):
                for key in cur.iternext_dup():
                    seeds = _seeds(txn.get(key, db=self.d_bal))
                    if seeds:
                        rows.append((key[:32], int.from_bytes(key[32:], "big"),
                                     seeds))
        finally:
            if txn is not self.txn:
                txn.abort()
        rows += [(k[0], k[1], self.hot_bal[k]) for k, v in self.hot.items()
                 if v[0] == script and k in self.hot_bal]
        return sorted(rows)

    def iter_balances(self):
        """Every committed (txid, vout, seeds) in key order -- the ledger
        digest's input (indexer.md §State). Committed state only: a
        writer's hot window is not included."""
        if self.d_bal is None:
            return
        with self.env.begin() as txn:
            for key, value in txn.cursor(db=self.d_bal):
                yield (bytes(key[:32]), int.from_bytes(key[32:], "big"),
                       _seeds(value))

    # --- the ledger half: work and the land log ---

    def put_work(self, height: int, chainwork: int) -> None:
        """W(height), cumulative hashes including that block."""
        self.begin()
        self.txn.put(height.to_bytes(4, "big"), chainwork.to_bytes(32, "big"),
                     db=self.d_work)

    def get_work(self, height: int) -> Optional[int]:
        if self.d_work is None:
            return None
        txn = self._read()
        try:
            v = txn.get(height.to_bytes(4, "big"), db=self.d_work)
        finally:
            if txn is not self.txn:
                txn.abort()
        return int.from_bytes(v, "big") if v is not None else None

    def add_land(self, txid: bytes, height: int, amount: int,
                 cenotaph: bool) -> None:
        """Record a transaction that carried LAND (derived, not read by
        the step); undone with its block."""
        self.begin()
        self.txn.put(txid, _land_value(height, amount, cenotaph), db=self.d_land)
        if height >= self.undo_from:
            self.txn.put(_undo_land_key(height, txid), b"", db=self.d_undo)

    def get_land(self, txid: bytes) -> Optional[tuple[int, int, bool]]:
        """(height, amount, cenotaph) of a recorded land, or None."""
        if self.d_land is None:
            return None
        txn = self._read()
        try:
            v = txn.get(txid, db=self.d_land)
        finally:
            if txn is not self.txn:
                txn.abort()
        return _unland(v) if v is not None else None

    # --- undo ---

    def undo_block(self, height: int) -> None:
        """Revert a connected block: re-create spent outpoints, drop
        created ones."""
        self.begin()
        self.flush_hot()
        prefix = height.to_bytes(4, "big")
        rows = []
        cur = self.txn.cursor(db=self.d_undo)
        if cur.set_range(prefix):
            for key, value in cur.iternext():
                if key[:4] != prefix:
                    break
                rows.append((key, value))
        for key, value in rows:
            if len(key) == UNDO_LAND:
                self.txn.delete(key[4:36], db=self.d_land)
            elif len(key) == UNDO_BAL:
                self.txn.put(key[4:40], value, db=self.d_bal)
            else:
                txid, vout = key[4:36], int.from_bytes(key[36:], "big")
                ukey = _ukey(txid, vout)
                if value == b"":
                    old = self.txn.get(ukey, db=self.d_utxo)
                    if old is not None:
                        script, data = _unrow(old)
                        self.txn.delete(ukey, db=self.d_utxo)
                        self.txn.delete(hashlib.sha256(script).digest(), ukey,
                                        db=self.d_addr)
                        self.txn.delete(ukey, db=self.d_bal)
                        self._count(script, data, -1)
                else:
                    script, data = _unrow(value)
                    self.txn.put(ukey, value, db=self.d_utxo)
                    self.txn.put(hashlib.sha256(script).digest(), ukey,
                                 db=self.d_addr)
                    self._count(script, data, +1)
            self.txn.delete(key, db=self.d_undo)
        # W(height) is rewritten on reconnect; never serve a stale one
        self.txn.delete(prefix, db=self.d_work)

    def prune_undo(self, below_height: int) -> None:
        if below_height <= 0:
            return
        self.begin()
        limit = below_height.to_bytes(4, "big")
        cur = self.txn.cursor(db=self.d_undo)
        while cur.first() and bytes(cur.key())[:4] < limit:
            cur.delete()

    def file_size(self) -> int:
        """Bytes in use, not the (preallocated) map size."""
        return self.env.info()["last_pgno"] * self.env.stat()["psize"]
