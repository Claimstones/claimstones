"""Tests for the orchid balance replay (docs/spec/indexer.md, test vectors).

Valuation is faked: the FakeValuer hands out scaled work per outpoint and
honours the double-claim rule through the store's land table. Vector 13
(walk cutoff) is a walker concern and vector 14 (reorg) is not built yet.

Run:  python -m unittest tests.test_replay   (from the repo root)
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from claimstones.bitcoin.txparse import Block, Tx, TxIn, TxOut
from claimstones.indexer.replay import OrchidIndexer, OrchidStore
from claimstones.lineage.walker import A_0, SUBSIDY_SCALE, seeds_from_work
from claimstones.protocol.codec import (
    MAGIC,
    OP_RETURN,
    U128_MAX,
    AssetId,
    Edict,
    Envelope,
    Tag,
    encipher,
    land_script,
    send_script,
    varint_encode,
)

W = A_0  # chainwork chosen so that N = k * D lands exactly k seeds
P2WPKH = bytes([0x00, 0x14]) + b"\x11" * 20


def tid(n: int) -> bytes:
    return bytes([n]) + b"\x00" * 31


def work(seeds: int | float | str) -> int:
    """Scaled work that is worth `seeds` at chainwork W. Exact: the
    scale is 140 bits, so a float multiply would round the product and
    a fixture meant to be worth 1.5 seeds would come out just under."""
    return int(Fraction(str(seeds)) * SUBSIDY_SCALE)


def out(value: int = 10_000, script: bytes = P2WPKH) -> TxOut:
    return TxOut(value, script)


def op_return(script: bytes) -> TxOut:
    return TxOut(0, script)


def tx(n: int, inputs=(), outputs=(), coinbase: bool = False) -> Tx:
    ins = [TxIn(prev, vout, b"", 0xFFFFFFFF) for prev, vout in inputs]
    if coinbase:
        ins = [TxIn(b"\x00" * 32, 0xFFFFFFFF, b"", 0xFFFFFFFF)]
    return Tx(tid(n), ins, list(outputs), coinbase, 400)


def raw_script(integers) -> bytes:
    payload = b"".join(varint_encode(i) for i in integers)
    return MAGIC + bytes([len(payload)]) + payload


class FakeValuer:
    def __init__(self, store: OrchidStore, chainwork: int = W):
        self.store = store
        self.cw = chainwork
        self.work_of: dict = {}   # (txid, vout) or ("cb", height) -> scaled work
        self.calls: list = []

    def chainwork(self, height: int) -> int:
        return self.cw

    def latent_work(self, tx: Tx, height: int) -> int:
        self.calls.append(tx.txid)
        if tx.is_coinbase:
            return self.work_of.get(("cb", height), 0)
        total = 0
        for inp in tx.inputs:
            if self.store.is_landed(inp.prev_txid):
                continue
            total += self.work_of.get((inp.prev_txid, inp.prev_vout), 0)
        return total


class ReplayCase(unittest.TestCase):
    def setUp(self):
        self.store = OrchidStore(":memory:")
        self.valuer = FakeValuer(self.store)
        self.indexer = OrchidIndexer(self.store, self.valuer)
        self.indexer.activation_height = 0   # the constant ships undecided
        self.height = 100

    def run_block(self, *txs: Tx, coinbase: Tx | None = None) -> None:
        cb = coinbase or tx(200 + self.height % 50, outputs=[out(5_000_000_000)], coinbase=True)
        self.indexer.index_block(Block(self.height, tid(255), 0, [cb, *txs]))
        self.height += 1

    def balance(self, n: int, vout: int) -> int:
        return self.store.get_balance(tid(n), vout)

    def burned(self) -> int:
        return self.store.get_meta_int("burned")

    def land_1000_into(self, n: int) -> None:
        """A land of tx n's single input worth 1000 seeds, credited to output 0."""
        self.valuer.work_of[(tid(90), 0)] = work(1000)
        self.run_block(tx(n, inputs=[(tid(90), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(n, 0), 1000)


class LandTests(ReplayCase):
    def test_4_land_amount_matches_formula(self):
        n = 123_456_789 * SUBSIDY_SCALE + 987
        self.valuer.work_of[(tid(90), 0)] = n
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[op_return(land_script()), out()]))
        self.assertEqual(self.balance(1, 1), seeds_from_work(n, W))
        self.assertEqual(self.balance(1, 1), A_0 * n // (W * SUBSIDY_SCALE))
        self.assertEqual(self.store.get_land(tid(1)), (100, seeds_from_work(n, W), False))

    def test_5_several_birth_blocks_one_floor(self):
        self.valuer.work_of[(tid(90), 0)] = work(1.5)
        self.valuer.work_of[(tid(91), 0)] = work(1.5)
        self.run_block(tx(1, inputs=[(tid(90), 0), (tid(91), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(1, 0), 3)  # per-input flooring would give 2

    def test_6_land_of_landed_inputs_is_void_transfer_executes(self):
        self.land_1000_into(1)
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), out(), op_return(send_script([(300, 1)], land=True))]))
        self.assertEqual(self.balance(2, 0), 700)
        self.assertEqual(self.balance(2, 1), 300)
        self.assertEqual(self.balance(1, 0), 0)
        self.assertEqual(self.store.get_land(tid(2)), (101, 0, False))  # outputs are plain anyway
        self.assertEqual(self.burned(), 0)

    def test_7_coinbase_land(self):
        self.valuer.work_of[("cb", 100)] = work(42)
        cb = tx(9, outputs=[out(5_000_000_000), op_return(land_script())], coinbase=True)
        self.run_block(coinbase=cb)
        self.assertEqual(self.balance(9, 0), 42)
        self.assertTrue(self.store.is_landed(tid(9)))

    def test_8_land_with_fee_sats_pays_the_claimer(self):
        # the valuer sees every input sat; output sat values do not matter
        self.valuer.work_of[(tid(90), 0)] = work(10)
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[out(1), op_return(land_script())]))
        self.assertEqual(self.balance(1, 0), 10)

    def test_land_with_pointer_and_edict(self):
        self.valuer.work_of[(tid(90), 0)] = work(500)
        script = encipher(Envelope(edicts=[Edict(AssetId(0, 0), 120, 1)], pointer=2, land=True))
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[out(), out(), out(), op_return(script)]))
        self.assertEqual((self.balance(1, 0), self.balance(1, 1), self.balance(1, 2)), (0, 120, 380))

    def test_zero_latent_value_is_void_but_marks_land(self):
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(1, 0), 0)
        self.assertEqual(self.store.get_land(tid(1)), (100, 0, False))

    def test_amount_over_u128_is_void(self):
        self.valuer.work_of[(tid(90), 0)] = work(U128_MAX + 1)
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(1, 0), 0)

    def test_no_land_tag_never_calls_the_valuer(self):
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[out(), op_return(send_script([(1, 0)]))]))
        self.assertEqual(self.valuer.calls, [])

    def test_undecided_activation_lands_nothing(self):
        # ACTIVATION_HEIGHT is None until it is decided (protocol/codec.py),
        # and while it is, no envelope counts at any height. The gate is a
        # test for None, never a comparison: 0 means active since genesis.
        self.indexer.activation_height = None
        self.valuer.work_of[(tid(90), 0)] = work(10)
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(1, 0), 0)
        self.assertFalse(self.store.is_landed(tid(1)))
        self.assertEqual(self.valuer.calls, [])

    def test_apply_tx_honours_the_activation_gate(self):
        # apply_tx is the wallet server's replay of its own broadcasts. It
        # used to decipher unconditionally, so it would credit a land that
        # index_block ignores and the server would disagree with the index it
        # stands in for.
        self.valuer.work_of[(tid(90), 0)] = work(10)
        landing = tx(1, inputs=[(tid(90), 0)], outputs=[out(), op_return(land_script())])
        self.indexer.activation_height = 101
        self.indexer.apply_tx(landing, 100)
        self.assertEqual(self.balance(1, 0), 0)
        self.indexer.activation_height = None
        self.indexer.apply_tx(landing, 100)
        self.assertEqual(self.balance(1, 0), 0)
        self.indexer.activation_height = 100
        self.indexer.apply_tx(landing, 100)
        self.assertEqual(self.balance(1, 0), 10)

    def test_activation_height_ignores_earlier_envelopes(self):
        self.indexer.activation_height = 101
        self.valuer.work_of[(tid(90), 0)] = work(10)
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(1, 0), 0)
        self.assertFalse(self.store.is_landed(tid(1)))
        self.valuer.work_of[(tid(91), 0)] = work(10)
        self.run_block(tx(2, inputs=[(tid(91), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(2, 0), 10)


class TransferTests(ReplayCase):
    def test_1_plain_transfer_one_edict(self):
        self.land_1000_into(1)
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), out(), op_return(send_script([(300, 1)]))]))
        self.assertEqual(self.balance(1, 0), 0)
        self.assertEqual(self.balance(2, 0), 700)
        self.assertEqual(self.balance(2, 1), 300)

    def test_2_no_envelope_sweeps_to_first_non_op_return(self):
        self.land_1000_into(1)
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[op_return(bytes([OP_RETURN]) + b"\x03abc"), out(), out()]))
        self.assertEqual(self.balance(2, 1), 1000)
        self.assertEqual(self.balance(2, 2), 0)

    def test_3_pointer_to_change_output(self):
        self.land_1000_into(1)
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), out(), out(), op_return(send_script([(300, 1)], change=2))]))
        self.assertEqual((self.balance(2, 0), self.balance(2, 1), self.balance(2, 2)), (0, 300, 700))

    def test_12_unknown_asset_id_edict_is_void(self):
        self.land_1000_into(1)
        script = encipher(Envelope(edicts=[Edict(AssetId(0, 0), 100, 1), Edict(AssetId(840_000, 3), 900, 2)]))
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), out(), out(), op_return(script)]))
        self.assertEqual((self.balance(2, 0), self.balance(2, 1), self.balance(2, 2)), (900, 100, 0))

    def test_15_runestone_in_same_tx_is_independent(self):
        self.land_1000_into(1)
        runestone = bytes([OP_RETURN, 0x5D, 0x04, 20, 1, 20, 0])  # a UNCOMMON GOODS mint
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[op_return(runestone), out(), out(), op_return(send_script([(250, 2)]))]))
        self.assertEqual((self.balance(2, 1), self.balance(2, 2)), (750, 250))

    def test_edict_amount_zero_is_all_remaining(self):
        self.land_1000_into(1)
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), out(), op_return(send_script([(0, 1)]))]))
        self.assertEqual((self.balance(2, 0), self.balance(2, 1)), (0, 1000))

    def test_edict_over_balance_is_capped(self):
        self.land_1000_into(1)
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), out(), op_return(send_script([(5000, 1)]))]))
        self.assertEqual((self.balance(2, 0), self.balance(2, 1)), (0, 1000))

    def test_split_edict_across_non_op_return_outputs(self):
        self.land_1000_into(1)
        # output index == number of outputs, amount 0: even split with remainder to the first
        script = raw_script([Tag.BODY, 0, 0, 0, 4])
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), out(), out(), op_return(script)]))
        self.assertEqual((self.balance(2, 0), self.balance(2, 1), self.balance(2, 2)), (334, 333, 333))

    def test_split_edict_with_amount(self):
        self.land_1000_into(1)
        script = raw_script([Tag.BODY, 0, 0, 400, 4])
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), out(), out(), op_return(script)]))
        self.assertEqual((self.balance(2, 0), self.balance(2, 1), self.balance(2, 2)), (400, 400, 200))

    def test_edict_to_op_return_output_burns(self):
        self.land_1000_into(1)
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), op_return(send_script([(100, 1)]))]))
        self.assertEqual(self.balance(2, 0), 900)
        self.assertEqual(self.burned(), 100)

    def test_all_op_return_outputs_burn_remainder(self):
        self.land_1000_into(1)
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[op_return(send_script([]))]))
        self.assertEqual(self.burned(), 1000)

    def test_inputs_merge(self):
        self.land_1000_into(1)
        self.valuer.work_of[(tid(91), 0)] = work(5)
        self.run_block(tx(2, inputs=[(tid(91), 0)], outputs=[out(), op_return(land_script())]))
        self.run_block(tx(3, inputs=[(tid(1), 0), (tid(2), 0)], outputs=[out()]))
        self.assertEqual(self.balance(3, 0), 1005)


class CenotaphTests(ReplayCase):
    def test_9_cenotaph_with_land_burns_all_and_later_land_is_void(self):
        self.land_1000_into(1)
        self.valuer.work_of[(tid(91), 0)] = work(5)  # an unlanded second input
        script = raw_script([Tag.LAND, 0, Tag.LIFT, 0])
        self.run_block(tx(2, inputs=[(tid(1), 0), (tid(91), 0)], outputs=[out(), op_return(script)]))
        self.assertEqual(self.balance(2, 0), 0)
        self.assertEqual(self.burned(), 1005)
        self.assertEqual(self.store.get_land(tid(2)), (101, 5, True))
        # later land of the cenotaph's outputs: plain sats, void
        self.valuer.work_of[(tid(2), 0)] = work(7)
        self.run_block(tx(3, inputs=[(tid(2), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(3, 0), 0)

    def test_10_cenotaph_without_land_burns_balances_only(self):
        self.land_1000_into(1)
        script = raw_script([Tag.LIFT, 0])
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), op_return(script)]))
        self.assertEqual(self.burned(), 1000)
        self.assertFalse(self.store.is_landed(tid(2)))
        # latent value untouched: a later land of the outputs is valid
        self.valuer.work_of[(tid(2), 0)] = work(7)
        self.run_block(tx(3, inputs=[(tid(2), 0)], outputs=[out(), op_return(land_script())]))
        self.assertEqual(self.balance(3, 0), 7)

    def test_11_lift_present_is_cenotaph(self):
        self.land_1000_into(1)
        script = raw_script([Tag.LIFT, 1, Tag.BODY, 0, 0, 300, 0])
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), op_return(script)]))
        self.assertEqual(self.balance(2, 0), 0)
        self.assertEqual(self.burned(), 1000)

    def test_unrecognized_even_tag_is_cenotaph(self):
        self.land_1000_into(1)
        script = raw_script([24, 0])  # old CLAIM tag
        self.run_block(tx(2, inputs=[(tid(1), 0)], outputs=[out(), op_return(script)]))
        self.assertEqual(self.burned(), 1000)

    def test_cenotaph_with_nonzero_land_value_does_not_land(self):
        self.valuer.work_of[(tid(90), 0)] = work(5)
        self.run_block(tx(1, inputs=[(tid(90), 0)], outputs=[out(), op_return(raw_script([Tag.LAND, 1]))]))
        self.assertFalse(self.store.is_landed(tid(1)))
        self.assertEqual(self.valuer.calls, [])
        self.assertEqual(self.burned(), 0)


class StoreTests(unittest.TestCase):
    def test_resume_height(self):
        store = OrchidStore(":memory:")
        self.assertIsNone(store.last_indexed_height())
        OrchidIndexer(store, FakeValuer(store)).index_block(Block(7, tid(1), 0, []))
        self.assertEqual(store.last_indexed_height(), 7)

    def test_u128_balances_round_trip(self):
        store = OrchidStore(":memory:")
        store.add_balance(tid(1), 0, U128_MAX)
        self.assertEqual(store.get_balance(tid(1), 0), U128_MAX)
        self.assertEqual(store.take_balance(tid(1), 0), U128_MAX)
        self.assertEqual(store.get_balance(tid(1), 0), 0)


if __name__ == "__main__":
    unittest.main()
