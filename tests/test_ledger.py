"""The ledger half's pure functions (docs/spec/indexer.md, test vectors
1-12 and 15, on indexer/ledger.py).

Envelopes are generated with the codec, never written by hand. Work is
a table: w(h) per height, W(c) chosen per test so the expected amounts
are small integers that can be checked by hand against constants.md
§Valuation.

Run:  python -m unittest tests.test_ledger   (from the repo root)
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from claimstones.bitcoin.txparse import Tx, TxIn, TxOut
from claimstones.indexer.birth import PLAIN
from claimstones.indexer.ledger import allocate, apply, land_amount, stream_work
from claimstones.lineage.walker import A_0, SUBSIDY_SCALE, subsidy_sats
from claimstones.protocol.codec import (MAGIC, OP_RETURN, U128_MAX, AssetId,
                                        Cenotaph, Edict, Envelope, Tag,
                                        decipher, encipher, land_script,
                                        send_script, varint_encode)

P2WPKH = bytes([0x00, 0x14]) + b"\x11" * 20
RUNESTONE = bytes([OP_RETURN, 0x5D, 0x01, 0x00])    # OP_RETURN OP_13: not ours

H0 = 100                 # a birth height in halving 0: s = 5e9
H1 = 210_000 + 5         # halving 1: s = 2.5e9
H_NO_CARRIER = 6_930_000  # halving 33: s = 0


def tid(n: int) -> bytes:
    return bytes([n]) + b"\x00" * 31


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


def w_table(**per_height: int):
    """w(h) lookup from a {height: hashes} table; unknown heights raise,
    so a test that reaches a height it did not expect fails loudly."""
    table = {int(k[1:]): v for k, v in per_height.items()}   # h100=... -> 100

    def work_of(h: int) -> int:
        return table[h]
    return work_of


def expected(stream, chainwork, work_of) -> int:
    """constants.md §Valuation evaluated as an exact rational, floored
    once -- the formula the implementation must agree with."""
    total = Fraction(0)
    for h, n in stream:
        if h == PLAIN or subsidy_sats(h) == 0:
            continue
        total += Fraction(n * work_of(h), subsidy_sats(h))
    return int(A_0 * total / chainwork)


class LandAmountTests(unittest.TestCase):
    """Vectors 4, 5, 7, 8 and the void cases, on land_amount alone."""

    def test_4_one_birth_block_matches_the_formula(self):
        # Every sat of block 100 carries w/s = 1000/5e9 hashes. Landing
        # the whole block at W = 1000 mints A_0: the identity of
        # constants.md §After the last halving, one block in isolation.
        work_of = w_table(h100=1000)
        self.assertEqual(land_amount([(H0, 5_000_000_000)], 1000, work_of), A_0)
        # Half the block at four times the work: A_0 / 8.
        self.assertEqual(land_amount([(H0, 2_500_000_000)], 4000, work_of), A_0 // 8)
        # An awkward one, against the exact rational.
        stream = [(H0, 123_456_789)]
        W = 987_654_321_000
        self.assertEqual(land_amount(stream, W, work_of), expected(stream, W, work_of))

    def test_5_several_birth_blocks_one_floor(self):
        # One sat from halving 0 (w = s, so 1 hash per sat) and one from
        # halving 1 (likewise): each alone is worth A_0 / W = 2/3 of a
        # seed at W = 1.5 A_0 and floors to 0; together 4/3 floors to 1.
        work_of = w_table(h100=5_000_000_000, h210005=2_500_000_000)
        W = 3 * A_0 // 2
        self.assertEqual(land_amount([(H0, 1)], W, work_of), 0)
        self.assertEqual(land_amount([(H1, 1)], W, work_of), 0)
        self.assertEqual(land_amount([(H0, 1), (H1, 1)], W, work_of), 1)

    def test_order_and_merging_do_not_matter(self):
        work_of = w_table(h100=7_919, h210005=104_729)
        a = [(H0, 10), (H1, 5), (H0, 3)]
        b = [(H1, 5), (H0, 13)]
        self.assertEqual(stream_work(a, work_of), stream_work(b, work_of))

    def test_7_coinbase_land_is_the_block_itself(self):
        # The coinbase stream is s(h) sats born at h (plus fee tails,
        # none here): a coinbase LAND at W(h) = w(h) + W(h-1) mints
        # A_0 * w(h) / W(h).
        work_of = w_table(h100=1_000)
        W = 1_000 + 9_000
        self.assertEqual(land_amount([(H0, subsidy_sats(H0))], W, work_of), A_0 // 10)

    def test_8_fee_sats_count_the_stream_is_the_inputs(self):
        # Output values play no part: the stream is the inputs' runs.
        work_of = w_table(h100=1000)
        stream = [(H0, 5_000_000_000)]
        full = apply(tx(1, outputs=[out(1), op_return(land_script())]), decipher(
            tx(1, outputs=[out(1), op_return(land_script())])), stream, 0, 1000, work_of)
        self.assertEqual(full.landed, A_0)
        self.assertEqual(full.seeds, [A_0, 0])

    def test_plain_runs_carry_nothing(self):
        work_of = w_table(h100=1000)
        self.assertEqual(land_amount([(PLAIN, 5_000_000_000)], 1000, work_of), 0)
        self.assertEqual(land_amount([(PLAIN, 10), (H0, 5_000_000_000)], 1000, work_of), A_0)

    def test_no_carrier_after_halving_33(self):
        work_of = w_table(h6930000=10 ** 30)
        self.assertEqual(land_amount([(H_NO_CARRIER, 1)], 1, work_of), 0)

    def test_amount_over_u128_is_void_not_capped(self):
        work_of = w_table(h100=1)
        # W = 1 and a stream of the whole block: A_0 * 1 * (L/s) / (1 * L) = A_0 -- fine.
        self.assertEqual(land_amount([(H0, 5_000_000_000)], 1, work_of), A_0)
        # Enough work to pass u128: void.
        huge = w_table(h100=(U128_MAX // A_0 + 1) * 5_000_000_000)
        self.assertEqual(land_amount([(H0, 1)], 1, huge), 0)
        just_under = w_table(h100=(U128_MAX // A_0) * 5_000_000_000)
        self.assertGreater(land_amount([(H0, 1)], 1, just_under), 0)


class AllocationTests(unittest.TestCase):
    """Vectors 1, 2, 3, 12, 15 and the edict rules, on allocate."""

    def test_1_plain_transfer_one_edict(self):
        t = tx(1, outputs=[out(), out(), op_return(send_script([(300, 1)]))])
        seeds, burned = allocate(t.outputs, decipher(t), 1000)
        self.assertEqual((seeds, burned), ([700, 300, 0], 0))

    def test_2_no_envelope_sweeps_to_first_non_op_return(self):
        t = tx(1, outputs=[op_return(RUNESTONE), out(), out()])
        self.assertIsNone(decipher(t))
        seeds, burned = allocate(t.outputs, None, 1000)
        self.assertEqual((seeds, burned), ([0, 1000, 0], 0))

    def test_3_pointer_takes_the_unallocated(self):
        t = tx(1, outputs=[out(), out(), op_return(send_script([(300, 0)], change=1))])
        seeds, burned = allocate(t.outputs, decipher(t), 1000)
        self.assertEqual((seeds, burned), ([300, 700, 0], 0))

    def test_12_unknown_asset_id_edict_is_void(self):
        script = encipher(Envelope(edicts=[Edict(AssetId(5, 1), 300, 1)]))
        t = tx(1, outputs=[out(), out(), op_return(script)])
        artifact = decipher(t)
        self.assertIsInstance(artifact, Envelope)
        seeds, burned = allocate(t.outputs, artifact, 1000)
        self.assertEqual((seeds, burned), ([1000, 0, 0], 0))

    def test_15_runestone_in_the_same_tx_is_independent(self):
        t = tx(1, outputs=[op_return(RUNESTONE), out(), out(),
                           op_return(send_script([(300, 2)]))])
        seeds, burned = allocate(t.outputs, decipher(t), 1000)
        self.assertEqual((seeds, burned), ([0, 700, 300, 0], 0))

    def test_edict_amount_zero_is_all_remaining(self):
        t = tx(1, outputs=[out(), out(), op_return(send_script([(0, 1)]))])
        self.assertEqual(allocate(t.outputs, decipher(t), 1000), ([0, 1000, 0], 0))

    def test_edict_over_the_pot_is_capped(self):
        t = tx(1, outputs=[out(), out(), op_return(send_script([(5000, 1)]))])
        self.assertEqual(allocate(t.outputs, decipher(t), 1000), ([0, 1000, 0], 0))

    def test_split_edict_across_non_op_return_outputs(self):
        # output index == len(outputs): amount 0 splits evenly, the
        # remainder one seed each to the first outputs
        t = tx(1, outputs=[out(), out(), out(), op_return(b"")])
        t.outputs[3] = op_return(send_script([(0, 4)]))
        self.assertEqual(allocate(t.outputs, decipher(t), 1000), ([334, 333, 333, 0], 0))

    def test_split_edict_with_amount(self):
        t = tx(1, outputs=[out(), out(), out(), op_return(send_script([(100, 4)]))])
        # 100 to each destination, the 700 left to the default output
        self.assertEqual(allocate(t.outputs, decipher(t), 1000), ([800, 100, 100, 0], 0))
        # ...and the pot runs out part way
        self.assertEqual(allocate(t.outputs, decipher(t), 150), ([100, 50, 0, 0], 0))

    def test_seeds_to_an_op_return_output_burn(self):
        # a pointer to OP_RETURN is a cenotaph at decode time; an edict to
        # one is accepted by the codec and burns here
        t = tx(1, outputs=[out(), op_return(send_script([(300, 1)]))])
        self.assertEqual(allocate(t.outputs, decipher(t), 1000), ([700, 0], 300))

    def test_all_op_return_outputs_burn_the_remainder(self):
        t = tx(1, outputs=[op_return(RUNESTONE), op_return(land_script())])
        self.assertEqual(allocate(t.outputs, decipher(t), 1000), ([0, 0], 1000))


class StepTests(unittest.TestCase):
    """Vectors 6, 9, 10, 11 and LAND + POINTER, on the composed step."""

    def setUp(self):
        self.work_of = w_table(h100=1000)
        self.W = 1000                 # the whole block 100 lands A_0
        self.block = [(H0, 5_000_000_000)]

    def step(self, t, stream, pot=0):
        return apply(t, decipher(t), stream, pot, self.W, self.work_of)

    def test_land_credits_the_default_output(self):
        s = self.step(tx(1, outputs=[op_return(land_script()), out()]), self.block)
        self.assertTrue(s.land)
        self.assertEqual(s.landed, A_0)
        self.assertEqual((s.seeds, s.burned), ([0, A_0], 0))

    def test_6_land_of_landed_inputs_is_void_transfer_executes(self):
        plain = [(PLAIN, 5_000_000_000)]
        t = tx(2, outputs=[out(), out(), op_return(send_script([(300, 1)], land=True))])
        s = self.step(t, plain, pot=1000)
        self.assertTrue(s.land)                   # outputs go PLAIN regardless
        self.assertEqual(s.landed, 0)
        self.assertEqual((s.seeds, s.burned), ([700, 300, 0], 0))

    def test_land_and_pointer_do_not_interact(self):
        # land.md §Closed 2026-11-13: the landed amount joins the pot
        # before the edicts; the pointer takes what they leave
        script = encipher(Envelope(edicts=[Edict(AssetId(0, 0), 120, 1)], pointer=2, land=True))
        t = tx(1, outputs=[out(), out(), out(), op_return(script)])
        # one sat of block 100: A_0 * (1000 / 5e9) / 1000 = 6000 seeds
        s = self.step(t, [(H0, 1)], pot=0)
        self.assertEqual(s.landed, 6000)
        self.assertEqual(s.seeds, [0, 120, 5880, 0])
        s = self.step(t, [(H0, 1)], pot=500)       # inputs' own balance joins too
        self.assertEqual(s.seeds, [0, 120, 6380, 0])

    def test_9_cenotaph_with_land_burns_everything(self):
        # a LAND tag with a nonzero value is a cenotaph, land=True
        t = tx(1, outputs=[out(), op_return(raw_script([Tag.LAND, 0, Tag.LAND, 0]))])
        artifact = decipher(t)
        self.assertIsInstance(artifact, Cenotaph)
        self.assertTrue(artifact.land)
        s = self.step(t, self.block, pot=1000)
        self.assertTrue(s.land)
        self.assertEqual(s.landed, A_0)           # landed, and then burned
        self.assertEqual((s.seeds, s.burned), ([0, 0], A_0 + 1000))
        # a later land of the (now PLAIN) outputs is void
        self.assertEqual(land_amount([(PLAIN, 5_000_000_000)], self.W, self.work_of), 0)

    def test_10_cenotaph_without_land_burns_balances_only(self):
        t = tx(1, outputs=[out(), op_return(raw_script([Tag.POINTER, 0, Tag.POINTER, 0]))])
        artifact = decipher(t)
        self.assertIsInstance(artifact, Cenotaph)
        self.assertFalse(artifact.land)
        s = self.step(t, self.block, pot=1000)
        self.assertFalse(s.land)                  # runs keep their heights
        self.assertEqual((s.landed, s.seeds, s.burned), (0, [0, 0], 1000))
        # so a later land of the outputs is valid
        self.assertEqual(land_amount(self.block, self.W, self.work_of), A_0)

    def test_11_lift_present_is_cenotaph(self):
        t = tx(1, outputs=[out(), op_return(raw_script([Tag.LIFT, 0]))])
        artifact = decipher(t)
        self.assertIsInstance(artifact, Cenotaph)
        s = self.step(t, [(H0, 10)], pot=50)
        self.assertEqual((s.seeds, s.burned), ([0, 0], 50))

    def test_cenotaph_with_nonzero_land_value_does_not_land(self):
        t = tx(1, outputs=[out(), op_return(raw_script([Tag.LAND, 1]))])
        artifact = decipher(t)
        self.assertIsInstance(artifact, Cenotaph)
        self.assertFalse(artifact.land)
        s = self.step(t, self.block, pot=7)
        self.assertEqual((s.land, s.landed, s.burned), (False, 0, 7))

    def test_below_activation_there_is_no_artifact(self):
        # the caller passes None; the step is the default rule alone
        t = tx(1, outputs=[op_return(land_script()), out()])
        s = apply(t, None, self.block, 0, self.W, self.work_of)
        self.assertEqual((s.land, s.landed, s.seeds, s.burned), (False, 0, [0, 0], 0))

    def test_void_land_never_asks_for_work(self):
        def never(h):
            raise AssertionError("w(h) asked for a PLAIN stream")
        t = tx(1, outputs=[out(), op_return(land_script())])
        s = apply(t, decipher(t), [(PLAIN, 5)], 0, self.W, never)
        self.assertEqual(s.landed, 0)


if __name__ == "__main__":
    unittest.main()
