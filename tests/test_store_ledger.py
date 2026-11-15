"""The ledger half of the LMDB store (indexer/store_lmdb.py, step 3 of the
ledger build): balances beside the rows, W per height, the land log,
and the undo log carrying all three.

Driven at the store level -- put/take_row/undo_block -- so the semantics
are pinned before the indexer calls them.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import pytest

lmdb = pytest.importorskip("lmdb")

from claimstones.indexer.birth import STAT_KEYS, encode_runs
from claimstones.indexer.fastbuild import spill
from claimstones.indexer.fastbuild.load import load
from claimstones.indexer.store_lmdb import (LmdbBirthStore, _land_value,
                                            _unland)
from tests.test_birth import P2WPKH, S, tid

OTHER = bytes([0x00, 0x14]) + b"\x33" * 20
W1, W2 = 10 ** 29, 10 ** 29 + 5 * 10 ** 23


def undo_entries(store: LmdbBirthStore) -> list[bytes]:
    txn = store._read()
    try:
        return [bytes(k) for k, _ in txn.cursor(db=store.d_undo)]
    finally:
        if txn is not store.txn:
            txn.abort()


class LedgerStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._stores = []
        self.store = self.open("db", commit_every=1)

    def tearDown(self):
        for s in self._stores:
            s.env.close()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def open(self, name: str, **kw) -> LmdbBirthStore:
        s = LmdbBirthStore(os.path.join(self._tmp.name, name), map_mb=64, **kw)
        self._stores.append(s)
        return s

    def close(self, s: LmdbBirthStore) -> None:
        """py-lmdb allows one open per environment per process."""
        s.env.close()
        self._stores.remove(s)

    # --- balances ---

    def test_balance_travels_with_the_row_hot_and_committed(self):
        s = self.store
        s.put(tid(1), 0, P2WPKH, [(0, S)], 0, seeds=1234)
        s.put(tid(1), 1, P2WPKH, [(0, S)], 0)              # no balance
        s.put(tid(2), 0, OTHER, [(0, S)], 0, seeds=5)
        # hot: visible before any commit
        self.assertEqual(s.get_balance(tid(1), 0), 1234)
        self.assertEqual(s.get_balance(tid(1), 1), 0)
        self.assertEqual(s.balances_by_script(P2WPKH), [(tid(1), 0, 1234)])
        self.assertEqual(list(s.iter_balances()), [])     # committed only
        s.set_meta("height", "0")
        s.commit()
        self.assertEqual(s.hot_bal, {})
        self.assertEqual(s.get_balance(tid(1), 0), 1234)
        self.assertEqual(s.balances_by_script(P2WPKH), [(tid(1), 0, 1234)])
        self.assertEqual(list(s.iter_balances()),
                         [(tid(1), 0, 1234), (tid(2), 0, 5)])
        self.close(s)
        reader = self.open("db", readonly=True)
        self.assertTrue(reader.has_ledger)
        self.assertEqual(reader.get_balance(tid(2), 0), 5)
        self.assertEqual(reader.balances_by_script(OTHER), [(tid(2), 0, 5)])

    def test_take_row_returns_and_removes_the_balance(self):
        s = self.store
        s.put(tid(1), 0, P2WPKH, [(0, S)], 0, seeds=1234)
        s.commit()
        self.assertEqual(s.take_row(tid(1), 0, 1), ([(0, S)], 1234))
        self.assertEqual(s.get_balance(tid(1), 0), 0)
        self.assertEqual(s.balances_by_script(P2WPKH), [])
        self.assertIsNone(s.take_row(tid(1), 0, 1))
        # take() still removes the balance, it just does not return it
        s.put(tid(2), 0, P2WPKH, [(0, S)], 1, seeds=7)
        self.assertEqual(s.take(tid(2), 0, 1), [(0, S)])
        self.assertEqual(s.get_balance(tid(2), 0), 0)

    def test_undo_restores_a_spent_balance_and_drops_a_created_one(self):
        s = self.store
        s.put(tid(1), 0, P2WPKH, [(0, S)], 0, seeds=1000)
        s.set_meta("height", "0")
        s.commit()
        # block 1 spends it into a new output carrying 600, 400 burned
        self.assertEqual(s.take_row(tid(1), 0, 1), ([(0, S)], 1000))
        s.put(tid(2), 0, OTHER, [(0, S)], 1, seeds=600)
        s.set_meta("height", "1")
        s.commit()
        kinds = sorted(len(k) for k in undo_entries(s)
                       if k[:4] == (1).to_bytes(4, "big"))
        self.assertEqual(kinds, [40, 40, 41])      # spent row, created row, old balance
        self.assertEqual(s.get_balance(tid(2), 0), 600)
        self.assertEqual(list(s.iter_balances()), [(tid(2), 0, 600)])

        s.undo_block(1)
        s.set_meta("height", "0")
        s.commit()
        self.assertEqual(s.get(tid(1), 0), [(0, S)])
        self.assertEqual(s.get_balance(tid(1), 0), 1000)
        self.assertIsNone(s.get(tid(2), 0))
        self.assertEqual(s.get_balance(tid(2), 0), 0)
        self.assertEqual(list(s.iter_balances()), [(tid(1), 0, 1000)])
        self.assertEqual([k for k in undo_entries(s)
                          if k[:4] == (1).to_bytes(4, "big")], [])

    def test_created_and_spent_in_one_block_leave_nothing(self):
        s = self.store
        s.put(tid(1), 0, P2WPKH, [(0, S)], 3, seeds=50)
        self.assertEqual(s.take_row(tid(1), 0, 3), ([(0, S)], 50))
        s.commit()
        self.assertEqual(undo_entries(s), [])
        self.assertEqual(list(s.iter_balances()), [])
        self.assertEqual(s.stats["utxos"], 0)

    def test_rollback_drops_hot_balances(self):
        s = self.open("db2", commit_every=10)
        s.put(tid(1), 0, P2WPKH, [(0, S)], 0, seeds=9)
        self.assertEqual(s.get_balance(tid(1), 0), 9)
        s.rollback()
        self.assertEqual(s.hot_bal, {})
        self.assertEqual(s.get_balance(tid(1), 0), 0)

    def test_below_undo_from_no_balance_undo_rows(self):
        s = self.open("db2", commit_every=1, undo_from=5)
        s.put(tid(1), 0, P2WPKH, [(0, S)], 0, seeds=9)
        s.commit()
        s.take_row(tid(1), 0, 1)
        s.commit()
        self.assertEqual(undo_entries(s), [])

    # --- work ---

    def test_work_per_height_and_undo_deletes_it(self):
        s = self.store
        self.assertIsNone(s.get_work(0))
        s.put_work(0, W1)
        s.put_work(1, W2)
        s.commit()
        self.assertEqual((s.get_work(0), s.get_work(1)), (W1, W2))
        s.undo_block(1)
        s.commit()
        self.assertEqual((s.get_work(0), s.get_work(1)), (W1, None))
        s.put_work(1, W2 + 1)                       # reconnect rewrites
        s.commit()
        self.assertEqual(s.get_work(1), W2 + 1)

    # --- the land log ---

    def test_land_log_and_its_undo(self):
        s = self.store
        self.assertIsNone(s.get_land(tid(9)))
        s.add_land(tid(9), 7, 12294, False)
        s.add_land(tid(8), 7, 0, True)
        s.commit()
        self.assertEqual(s.get_land(tid(9)), (7, 12294, False))
        self.assertEqual(s.get_land(tid(8)), (7, 0, True))
        self.assertEqual(sorted(len(k) for k in undo_entries(s)), [37, 37])
        s.undo_block(7)
        s.commit()
        self.assertIsNone(s.get_land(tid(9)))
        self.assertIsNone(s.get_land(tid(8)))
        self.assertEqual(undo_entries(s), [])

    def test_land_value_round_trip(self):
        amount = (1 << 128) - 1
        self.assertEqual(_unland(_land_value(980_000, amount, True)),
                         (980_000, amount, True))

    # --- counters ---

    def test_ledger_counters_exist_and_persist(self):
        s = self.store
        self.assertIn("minted", STAT_KEYS)
        self.assertEqual((s.stats["minted"], s.stats["burned"]), (0, 0))
        s.stats["minted"] += 500
        s.stats["burned"] += 20
        s.commit()
        self.close(s)
        again = self.open("db", readonly=True)
        self.assertEqual((again.stats["minted"], again.stats["burned"]), (500, 20))


def test_reader_without_the_ledger_answers_nothing(tmp_path):
    """A phase-3 environment (fastbuild load) has no bal/work/land; a
    read-only reader must still serve the birth half."""
    rows = [(tid(1), 0, P2WPKH, encode_runs([(0, S)]))]
    w = spill.FinalWriter(str(tmp_path / "f.stream"))
    for cid, (txid, vout, script, runs) in enumerate(rows):
        w.write(cid, vout, txid, script, runs)
    w.close()
    load(str(tmp_path / "f.stream"), str(tmp_path / "db"),
         str(tmp_path / "scratch"), height=1, map_gb=1, report=lambda *a: None)

    reader = LmdbBirthStore(str(tmp_path / "db"), map_mb=64, readonly=True)
    try:
        assert not reader.has_ledger
        assert reader.get(tid(1), 0) == [(0, S)]
        assert reader.get_balance(tid(1), 0) == 0
        assert reader.balances_by_script(P2WPKH) == []
        assert list(reader.iter_balances()) == []
        assert reader.get_work(1) is None
        assert reader.get_land(tid(1)) is None
    finally:
        reader.env.close()

    # the writer creates them; a reader opened afterwards sees them
    writer = LmdbBirthStore(str(tmp_path / "db"), map_mb=64)
    writer.put_work(1, W1)
    writer.commit()
    writer.env.close()
    reader = LmdbBirthStore(str(tmp_path / "db"), map_mb=64, readonly=True)
    try:
        assert reader.has_ledger
        assert reader.get_work(1) == W1
    finally:
        reader.env.close()
