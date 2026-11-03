"""Tests for the LMDB birth store (indexer/store_lmdb.py).

Three angles: the whole indexer-level suite from test_birth.py rerun on
LMDB (same interface, same semantics); a differential drive of both
stores through one block sequence, comparing complete state; and the
handover — a db written by fastbuild phase 3, picked up by the store.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import pytest

lmdb = pytest.importorskip("lmdb")

from claimstones.bitcoin.txparse import Block
from claimstones.indexer.birth import BirthIndexer, BirthStore, encode_runs
from claimstones.indexer.fastbuild import spill
from claimstones.indexer.fastbuild.load import load
from claimstones.indexer.store_lmdb import LmdbBirthStore, _unrow
from tests.test_birth import P2WPKH, S, IndexerTests, coinbase, tid, tx

OTHER = bytes([0x00, 0x14]) + b"\x33" * 20


def lmdb_utxo_rows(store: LmdbBirthStore) -> dict:
    # read through the store's own open txn, as SQLite's connection sees
    # its uncommitted writes; a fresh read txn sees only committed state
    txn = store._read()
    try:
        return {(bytes(k[:32]), int.from_bytes(k[32:], "big")): _unrow(bytes(v))
                for k, v in txn.cursor(db=store.d_utxo)}
    finally:
        if txn is not store.txn:
            txn.abort()


def sqlite_utxo_rows(store: BirthStore) -> dict:
    return {(t_, v): (s, r) for t_, v, s, r in store.db.execute(
        "SELECT txid, vout, script, runs FROM utxo")}


def entries(store: LmdbBirthStore, db) -> int:
    txn = store._read()
    try:
        return txn.stat(db)["entries"]
    finally:
        if txn is not store.txn:
            txn.abort()


class LmdbIndexerTests(IndexerTests):
    """Every indexer-level test from test_birth.py, on the LMDB store.
    The handful that reach into SQLite internals are re-expressed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._extra = []
        self.store = self._open("db", commit_every=1)
        self.ix = BirthIndexer(self.store)

    def tearDown(self):
        for s in self._extra:
            s.env.close()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def _open(self, name: str, **kw) -> LmdbBirthStore:
        s = LmdbBirthStore(os.path.join(self._tmp.name, name),
                           map_mb=64, **kw)
        self._extra.append(s)
        return s

    def test_undo_rows_skipped_below_undo_from(self):
        store = self._open("db2", commit_every=1, undo_from=2)
        ix = BirthIndexer(store)
        for h, cb in ((0, coinbase(1, 0)), (1, coinbase(2, 1))):
            ix.connect_block(Block(h, tid(200 + h), 0, [cb]))
        self.assertEqual(entries(store, store.d_undo), 0)
        spend = tx(3, inputs=[(tid(1), 0)], outputs=[(S, None)])
        ix.connect_block(Block(2, tid(202), 0, [coinbase(4, 2), spend]))
        self.assertEqual(entries(store, store.d_undo), 3)
        ix.disconnect_block(2)
        self.assertEqual(store.get(tid(1), 0), [(0, S)])

    def test_hot_outputs_spent_inside_window_never_reach_sqlite(self):
        store = self._open("db2", commit_every=10)
        ix = BirthIndexer(store)
        ix.connect_block(Block(0, tid(200), 0, [coinbase(1, 0)]))
        a = tx(2, inputs=[(tid(1), 0)], outputs=[(S // 2, None), (S // 2, None)])
        ix.connect_block(Block(1, tid(201), 0, [coinbase(3, 1), a]))
        b = tx(4, inputs=[(tid(2), 0)], outputs=[(S // 2, None)])
        ix.connect_block(Block(2, tid(202), 0, [coinbase(5, 2), b]))
        self.assertEqual(len(lmdb_utxo_rows(store)), 0)
        self.assertEqual(store.get(tid(2), 1), [(0, S // 2)])          # hot
        self.assertEqual(store.get(tid(2), 0), None)                    # spent while hot
        self.assertEqual([(t_, v) for t_, v, _ in store.by_script(P2WPKH)],
                         [(tid(2), 1), (tid(3), 0), (tid(4), 0), (tid(5), 0)])
        self.assertEqual(store.stats["utxos"], 4)
        store.commit()
        self.assertEqual(store.hot, {})
        self.assertEqual(len(lmdb_utxo_rows(store)), 4)
        self.assertEqual(store.get(tid(4), 0), [(0, S // 2)])
        # rollback drops uncommitted hot rows and reloads the counters
        ix.connect_block(Block(3, tid(203), 0, [coinbase(6, 3)]))
        self.assertIn((tid(6), 0), store.hot)
        store.rollback()
        self.assertEqual(store.hot, {})
        self.assertEqual(store.stats["utxos"], 4)

    def test_script_index_built_once_and_lookup_works_either_way(self):
        # the addr table is maintained on every write; the SQLite build-it-
        # near-the-tip dance degenerates to stubs that must stay harmless
        self.block(0, [coinbase(1, 0)])
        self.assertTrue(self.store.has_script_index())
        self.assertFalse(self.store.ensure_script_index())
        self.block(1, [coinbase(2, 1)])
        self.assertEqual([(t_, v) for t_, v, _ in self.store.by_script(P2WPKH)],
                         [(tid(1), 0), (tid(2), 0)])
        self.store.drop_script_index()
        self.assertTrue(self.store.has_script_index())

    def test_schema_mismatch_refuses(self):
        s = self._open("db3")
        s.set_meta("schema", "1")
        s.close()
        with self.assertRaises(RuntimeError):
            LmdbBirthStore(os.path.join(self._tmp.name, "db3"), map_mb=64)

    def test_sat_conservation(self):
        from claimstones.indexer.birth import decode_runs
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1)])
        spend = tx(3, inputs=[(tid(1), 0)], outputs=[(S // 3, None), (S // 3, None)])
        self.block(2, [coinbase(4, 2, claim=S + (S - 2 * (S // 3))), spend])
        self.store.commit()
        total = sum(sum(n for _, n in decode_runs(data))
                    for _, data in lmdb_utxo_rows(self.store).values())
        self.assertEqual(total, 3 * S)   # three blocks' subsidy, nothing lost

    def test_duplicate_outpoint_outside_bip30_refuses(self):
        self.block(0, [coinbase(1, 0)])
        with self.assertRaises(RuntimeError):
            self.block(1, [coinbase(1, 1)])          # same txid again
        self.store.rollback()


class DifferentialTests(unittest.TestCase):
    """One block sequence, two stores, identical state at every step."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sq = BirthStore(":memory:", commit_every=3)
        self.lm = LmdbBirthStore(os.path.join(self._tmp.name, "db"),
                                 commit_every=3, map_mb=64)
        self.ixs = [BirthIndexer(self.sq), BirthIndexer(self.lm)]

    def tearDown(self):
        self.sq.db.close()
        self.lm.env.close()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def both(self, fn):
        for ix in self.ixs:
            fn(ix)

    def assert_same_state(self):
        self.sq.commit()
        self.lm.commit()
        self.assertEqual(sqlite_utxo_rows(self.sq), lmdb_utxo_rows(self.lm))
        self.assertEqual(self.sq.stats, self.lm.stats)
        self.assertEqual(self.sq.last_height(), self.lm.last_height())
        for script in (P2WPKH, OTHER):
            self.assertEqual(self.sq.by_script(script),
                             self.lm.by_script(script))

    def test_connect_disconnect_parity(self):
        blocks = [
            Block(0, tid(200), 0, [coinbase(1, 0)]),
            Block(1, tid(201), 0, [
                coinbase(2, 1, claim=S + S // 4),
                tx(3, inputs=[(tid(1), 0)],
                   outputs=[(S // 2, None), (S // 4, OTHER), (0, OTHER)])]),
            Block(2, tid(202), 0, [
                coinbase(4, 2),
                tx(5, inputs=[(tid(3), 0)], outputs=[(S // 2, None)]),
                tx(6, inputs=[(tid(5), 0), (tid(3), 1)],
                   outputs=[(S // 2 + S // 4, OTHER)])]),
            Block(3, tid(203), 0, [coinbase(7, 3)]),
        ]
        for b in blocks:
            self.both(lambda ix, b=b: ix.connect_block(b))
            self.assert_same_state()
        for h in (3, 2):
            self.both(lambda ix, h=h: ix.disconnect_block(h))
            self.assert_same_state()
        self.both(lambda ix: ix.connect_block(
            Block(2, tid(212), 0, [coinbase(8, 2)])))
        self.assert_same_state()


def seed_stream(path) -> list:
    """A two-row final stream with arithmetic-friendly runs."""
    rows = [(tid(1), 0, P2WPKH, encode_runs([(0, S)])),
            (tid(2), 0, OTHER, encode_runs([(1, S)]))]
    w = spill.FinalWriter(str(path))
    for cid, (txid, vout, script, runs) in enumerate(rows):
        w.write(cid, vout, txid, script, runs)
    w.close()
    return rows


def test_handover_from_phase3_db(tmp_path):
    seed_stream(tmp_path / "f.stream")
    load(str(tmp_path / "f.stream"), str(tmp_path / "db"),
         str(tmp_path / "scratch"), height=1, map_gb=1,
         report=lambda *a: None)

    store = LmdbBirthStore(str(tmp_path / "db"), commit_every=1, map_mb=64)
    try:
        assert store.last_height() == 1
        assert store.stats["utxos"] == 2
        assert store.stats["runs"] == 2
        assert store.stats["bytes"] == 24          # derived: 12 per run
        assert store.get(tid(1), 0) == [(0, S)]
        assert store.by_script(OTHER) == [(tid(2), 0, [(1, S)])]

        # the daemon takes over: spend a loaded output, then reorg it away
        ix = BirthIndexer(store)
        spend = tx(3, inputs=[(tid(1), 0)], outputs=[(S, None)])
        ix.connect_block(Block(2, tid(202), 0, [coinbase(4, 2), spend]))
        assert store.last_height() == 2
        assert store.get(tid(1), 0) is None
        assert store.get(tid(3), 0) == [(0, S)]
        assert (tid(3), 0) in [(t_, v) for t_, v, _ in store.by_script(P2WPKH)]

        ix.disconnect_block(2)
        assert store.last_height() == 1
        assert store.get(tid(1), 0) == [(0, S)]
        assert store.get(tid(3), 0) is None
        assert store.get(tid(4), 0) is None
        assert store.by_script(P2WPKH) == [(tid(1), 0, [(0, S)])]
        assert store.stats["utxos"] == 2
    finally:
        store.close()


def test_readonly_reader_cannot_write(tmp_path):
    seed_stream(tmp_path / "f.stream")
    load(str(tmp_path / "f.stream"), str(tmp_path / "db"),
         str(tmp_path / "scratch"), height=1, map_gb=1,
         report=lambda *a: None)
    reader = LmdbBirthStore(str(tmp_path / "db"), map_mb=64, readonly=True)
    try:
        assert reader.get(tid(1), 0) == [(0, S)]
        assert reader.by_script(P2WPKH) == [(tid(1), 0, [(0, S)])]
        with pytest.raises(lmdb.Error):
            reader.set_meta("height", "2")
    finally:
        reader.env.close()
