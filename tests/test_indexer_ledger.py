"""The fused indexer: the ledger half inside BirthIndexer (indexer/birth.py,
step 4 of the ledger build; docs/spec/indexer.md §Determinism, §Work,
§Reorg).

Hand-built blocks carry regtest's nBits, so w(h) = 2 for every block and
W(c) = 2 (c + 1). Landing the whole 50 BTC coinbase of block 0 in block c
therefore mints A_0 * 2 / (2 (c + 1)) = A_0 / (c + 1) seeds exactly:
A_0 / 2 in block 1, A_0 / 3 in block 2. Vector 14 (reorg: the land
moves one block, the amount is recomputed, state equals a fresh sync) is
here at the indexer level; the regtest end-to-end repeats it against a
node.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import pytest

pytest.importorskip("lmdb")

from claimstones.bitcoin.txparse import Block, Tx, TxOut
from claimstones.indexer.birth import PLAIN, BirthIndexer
from claimstones.indexer.store_lmdb import LmdbBirthStore
from claimstones.indexer.work import fill_work
from claimstones.lineage.walker import A_0
from claimstones.protocol.codec import (MAGIC, Tag, land_script, send_script,
                                        varint_encode)
from tests.test_birth import P2WPKH, S, coinbase, tid, tx
from tests.test_store_lmdb import fake_node_call, lmdb_utxo_rows

OTHER = bytes([0x00, 0x14]) + b"\x55" * 20


def raw_script(integers) -> bytes:
    payload = b"".join(varint_encode(i) for i in integers)
    return MAGIC + bytes([len(payload)]) + payload


class FusedCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._stores = []
        self.store = self.open("db")
        self.ix = BirthIndexer(self.store)
        self.ix.activation_height = 0        # the constant ships undecided

    def tearDown(self):
        for s in self._stores:
            s.env.close()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def open(self, name: str, **kw) -> LmdbBirthStore:
        s = LmdbBirthStore(os.path.join(self._tmp.name, name),
                           commit_every=1, map_mb=64, **kw)
        self._stores.append(s)
        return s

    def block(self, height: int, txs, ix=None, salt: int = 200) -> None:
        (ix or self.ix).connect_block(Block(height, tid(salt + height), 0, txs))

    def land_of_coinbase_1(self, n: int, fee: int = 1000) -> Tx:
        """A land of block 0's coinbase (tid(1):0): change + envelope."""
        return tx(n, inputs=[(tid(1), 0)],
                  outputs=[(S - fee, None), (0, land_script())])

    def state(self, store):
        """Everything consensus: rows, balances, lands, counters but txs."""
        store.commit()
        stats = {k: v for k, v in store.stats.items() if k != "txs"}
        return (lmdb_utxo_rows(store), list(store.iter_balances()), stats,
                [store.get_land(tid(n)) for n in range(1, 20)])


class WorkTests(FusedCase):
    def test_w_per_block_from_the_header(self):
        for h in range(3):
            self.block(h, [coinbase(h + 1, h)])
        self.assertEqual([self.store.get_work(h) for h in range(3)], [2, 4, 6])
        self.assertEqual(self.store.get_meta("ledger_height"), "0")

    def test_resume_above_genesis_needs_the_work_table(self):
        store = self.open("db2")
        store.put(tid(1), 0, P2WPKH, [(0, S)], 0)
        store.set_meta("height", "0")
        store.commit()
        ix = BirthIndexer(store)
        with self.assertRaisesRegex(RuntimeError, "fill_work"):
            self.block(1, [coinbase(2, 1)], ix=ix)
        store.rollback()
        fill_work(store, fake_node_call(0), 0, report=lambda *a, **k: None)
        self.block(1, [coinbase(2, 1)], ix=ix)
        self.assertEqual(store.get_work(1), 4)

    def test_fill_work_refuses_a_chainwork_mismatch(self):
        store = self.open("db2")

        def lying(method, *a):
            if method == "getblockhash":
                return "x"
            return {"bits": "207fffff", "chainwork": f"{999:064x}"}
        with self.assertRaisesRegex(RuntimeError, "not constant per period"):
            fill_work(store, lying, 5, report=lambda *a, **k: None)
        self.assertIsNone(store.get_work(0))

    def test_ledger_height_is_set_once(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1)])
        self.assertEqual(self.store.get_meta("ledger_height"), "0")
        self.store.env.close()
        self._stores.remove(self.store)
        store = self.open("db")
        BirthIndexer(store).connect_block(Block(2, tid(202), 0, [coinbase(3, 2)]))
        self.assertEqual(store.get_meta("ledger_height"), "0")


class LandTests(FusedCase):
    def test_land_credits_the_default_output(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1, claim=S + 1000), self.land_of_coinbase_1(3)])
        self.assertEqual(self.store.get_balance(tid(3), 0), A_0 // 2)
        self.assertEqual(self.store.get(tid(3), 0), [(PLAIN, S - 1000)])
        self.assertEqual(self.store.get_land(tid(3)), (1, A_0 // 2, False))
        self.assertEqual(self.store.balances_by_script(P2WPKH),
                         [(tid(3), 0, A_0 // 2)])
        s = self.store.stats
        self.assertEqual((s["lands"], s["minted"], s["burned"]), (1, A_0 // 2, 0))
        # the fee sats were valued (the stream is the inputs) but their
        # runs reach the coinbase unlanded, as before
        self.assertEqual(self.store.get(tid(2), 0), [(1, S), (0, 1000)])

    def test_balance_follows_a_plain_spend_and_an_edict(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1, claim=S + 1000), self.land_of_coinbase_1(3)])
        # no envelope: sweep to the first non-OP_RETURN output
        sweep = tx(4, inputs=[(tid(3), 0)], outputs=[(S - 2000, OTHER)])
        self.block(2, [coinbase(5, 2, claim=S + 1000), sweep])
        self.assertEqual(self.store.get_balance(tid(3), 0), 0)
        self.assertEqual(self.store.get_balance(tid(4), 0), A_0 // 2)
        self.assertEqual(self.store.balances_by_script(OTHER), [(tid(4), 0, A_0 // 2)])
        # an edict pays 1000 seeds to output 1, the pointer keeps the rest at 0
        pay = tx(6, inputs=[(tid(4), 0)],
                 outputs=[(S - 4000, OTHER), (1000, None),
                          (0, send_script([(1000, 1)], change=0))])
        self.block(3, [coinbase(7, 3, claim=S + 1000), pay])
        self.assertEqual(self.store.get_balance(tid(6), 0), A_0 // 2 - 1000)
        self.assertEqual(self.store.get_balance(tid(6), 1), 1000)
        self.assertEqual(self.store.stats["burned"], 0)
        # the pay transaction carried no LAND: its outputs keep PLAIN from the input
        self.assertEqual(self.store.get(tid(6), 1), [(PLAIN, 1000)])
        self.assertIsNone(self.store.get_land(tid(6)))

    def test_land_of_landed_sats_is_void(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1, claim=S + 1000), self.land_of_coinbase_1(3)])
        again = tx(4, inputs=[(tid(3), 0)], outputs=[(S - 2000, None), (0, land_script())])
        self.block(2, [coinbase(5, 2, claim=S + 1000), again])
        self.assertEqual(self.store.get_balance(tid(4), 0), A_0 // 2)   # moved, not minted
        self.assertEqual(self.store.get_land(tid(4)), (2, 0, False))
        self.assertEqual(self.store.stats["lands"], 2)
        self.assertEqual(self.store.stats["minted"], A_0 // 2)

    def test_cenotaph_with_land_burns_the_minted_amount(self):
        self.block(0, [coinbase(1, 0)])
        bad = tx(3, inputs=[(tid(1), 0)],
                 outputs=[(S - 1000, None), (0, raw_script([Tag.LAND, 0, Tag.LAND, 0]))])
        self.block(1, [coinbase(2, 1, claim=S + 1000), bad])
        self.assertEqual(self.store.get_balance(tid(3), 0), 0)
        self.assertEqual(self.store.get(tid(3), 0), [(PLAIN, S - 1000)])
        self.assertEqual(self.store.get_land(tid(3)), (1, A_0 // 2, True))
        s = self.store.stats
        self.assertEqual((s["lands"], s["minted"], s["burned"]), (1, A_0 // 2, A_0 // 2))
        self.assertEqual(list(self.store.iter_balances()), [])

    def test_below_activation_nothing_is_minted(self):
        self.ix.activation_height = 5
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1, claim=S + 1000), self.land_of_coinbase_1(3)])
        self.assertEqual(self.store.get_balance(tid(3), 0), 0)
        self.assertEqual(self.store.get(tid(3), 0), [(0, S - 1000)])
        self.assertIsNone(self.store.get_land(tid(3)))
        self.assertEqual(self.store.stats["minted"], 0)


class ReorgTests(FusedCase):
    def test_14_land_moves_one_block_amount_recomputed_state_equals_fresh_sync(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1, claim=S + 1000), self.land_of_coinbase_1(3)])
        self.assertEqual(self.store.get_balance(tid(3), 0), A_0 // 2)

        # the network replaces block 1 with an empty one; the land is
        # mined in block 2 instead, where W(c) = 6
        self.ix.disconnect_block(1)
        self.assertEqual(self.store.get(tid(1), 0), [(0, S)])
        self.assertEqual(self.store.get_balance(tid(3), 0), 0)
        self.assertIsNone(self.store.get_land(tid(3)))
        self.assertIsNone(self.store.get_work(1))
        s = self.store.stats
        self.assertEqual((s["lands"], s["minted"]), (0, 0))      # counters undone too

        self.block(1, [coinbase(4, 1)], salt=300)
        self.block(2, [coinbase(5, 2, claim=S + 1000), self.land_of_coinbase_1(3)], salt=300)
        self.assertEqual(self.store.get_balance(tid(3), 0), A_0 // 3)
        self.assertEqual(self.store.get_land(tid(3)), (2, A_0 // 3, False))
        self.assertEqual(self.store.get_work(2), 6)

        fresh_store = self.open("fresh")
        fresh = BirthIndexer(fresh_store)
        fresh.activation_height = 0
        self.block(0, [coinbase(1, 0)], ix=fresh)
        self.block(1, [coinbase(4, 1)], ix=fresh, salt=300)
        self.block(2, [coinbase(5, 2, claim=S + 1000), self.land_of_coinbase_1(3)],
                   ix=fresh, salt=300)
        ours, theirs = self.state(self.store), self.state(fresh_store)
        self.assertEqual(ours[0], theirs[0])          # rows
        self.assertEqual(ours[1], theirs[1])          # balances
        self.assertEqual(ours[3], theirs[3])          # land log
        self.assertEqual(ours[2], theirs[2])          # every counter but txs

    def test_disconnect_restores_a_spent_balance(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1, claim=S + 1000), self.land_of_coinbase_1(3)])
        sweep = tx(4, inputs=[(tid(3), 0)], outputs=[(S - 2000, OTHER)])
        self.block(2, [coinbase(5, 2, claim=S + 1000), sweep])
        self.ix.disconnect_block(2)
        self.assertEqual(self.store.get_balance(tid(3), 0), A_0 // 2)
        self.assertEqual(self.store.get_balance(tid(4), 0), 0)
        self.assertEqual(list(self.store.iter_balances()), [(tid(3), 0, A_0 // 2)])

    def test_cenotaph_burn_is_undone_with_its_block(self):
        self.block(0, [coinbase(1, 0)])
        bad = tx(3, inputs=[(tid(1), 0)],
                 outputs=[(S - 1000, None), (0, raw_script([Tag.LAND, 0, Tag.LAND, 0]))])
        self.block(1, [coinbase(2, 1, claim=S + 1000), bad])
        self.assertEqual(self.store.stats["burned"], A_0 // 2)
        self.ix.disconnect_block(1)
        s = self.store.stats
        self.assertEqual((s["lands"], s["minted"], s["burned"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
