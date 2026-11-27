"""Strict construction, fuzzed (wallet-policy.md §Mainnet safety checklist).

Every wallet template — claim, claim-and-collect, send, withdraw — is
built many times from random coins, amounts and fee rates. Each build
either refuses with a TemplateError (a plain-words refusal is fine) or
yields a transaction whose envelope decodes as a valid Envelope of
exactly the template's shape: never a cenotaph, never an extra tag,
edict or pointer, one OP_RETURN only.

Seeded, so a failure reproduces: the seed and the case are in the
message. Test code only — nothing here computes a balance.
"""

from __future__ import annotations

import random
import unittest

from claimstones.bitcoin import bech32, secp
from claimstones.bitcoin.txparse import TxOut
from claimstones.protocol.codec import Envelope, decipher, is_op_return
from claimstones.wallet.land import (
    CARRIER_SATS, FundingInput, TemplateError, build_claim, build_collect,
)
from claimstones.wallet.spend import plan_send, plan_withdraw, sign_send, sign_withdraw
from tests.test_land_templates import WALLET_INTERNAL, WALLET_SCRIPT
from tests.test_wallet_spend import DEST_WPKH, KEY, RECIPIENT, FakeRpc, utxo

SEEDS = range(8)
CASES = 40           # per seed and template: 320 builds each


def external_taproot(rng: random.Random) -> bytes:
    sk = rng.randrange(1, secp.N)
    return bech32.p2tr_script(secp.taproot_output_key(secp.pubkey(sk)))


def coins(rng: random.Random, script: bytes, n: int) -> list[FundingInput]:
    return [FundingInput(rng.randbytes(32), rng.randrange(0, 8),
                         TxOut(rng.choice([rng.randrange(330, 2_000),
                                           rng.randrange(2_000, 200_000),
                                           rng.randrange(200_000, 50_000_000)]),
                               script))
            for _ in range(n)]


def fee_rate(rng: random.Random) -> float:
    return rng.choice([1, 1.5, 2, 5, 12.3, 40, 150])


class TemplateFuzz(unittest.TestCase):
    def assert_shape(self, tx, where: str, *, land: bool, edicts: int,
                     pointer, n_outputs: int) -> Envelope:
        self.assertEqual(len(tx.outputs), n_outputs, where)
        self.assertEqual(sum(is_op_return(o.script_pubkey) for o in tx.outputs), 1, where)
        art = decipher(tx)
        self.assertIsInstance(art, Envelope, f"{where}: {art!r}")
        self.assertEqual(art.land, land, where)
        self.assertEqual(len(art.edicts), edicts, where)
        self.assertEqual(art.pointer, pointer, where)
        return art

    def run_cases(self, name: str, case) -> None:
        built = refused = 0
        for seed in SEEDS:
            rng = random.Random(f"{name}-{seed}")
            for i in range(CASES):
                where = f"{name} seed={seed} case={i}"
                try:
                    case(rng, where)
                    built += 1
                except TemplateError:
                    refused += 1
        # the fuzz must mostly build, or it tests nothing
        self.assertGreater(built, refused, f"{name}: {built} built, {refused} refused")

    def test_claim(self):
        def case(rng, where):
            inputs = coins(rng, WALLET_SCRIPT, rng.randrange(1, 12))
            psbt, s = build_claim(inputs, WALLET_SCRIPT, WALLET_INTERNAL, fee_rate(rng))
            self.assert_shape(psbt.tx, where, land=True, edicts=0, pointer=None,
                              n_outputs=2)
            self.assertEqual(psbt.tx.outputs[1].script_pubkey, WALLET_SCRIPT, where)
            self.assertEqual(s.input_sats, s.change_sats + s.fee_sats, where)
        self.run_cases("claim", case)

    def test_collect(self):
        def case(rng, where):
            funding = external_taproot(rng)
            inputs = coins(rng, funding, rng.randrange(1, 12))
            psbt, s = build_collect(inputs, WALLET_SCRIPT, funding, fee_rate(rng))
            tx = psbt.tx
            self.assert_shape(tx, where, land=True, edicts=0, pointer=None, n_outputs=3)
            self.assertEqual(tx.outputs[1], TxOut(CARRIER_SATS, WALLET_SCRIPT), where)
            self.assertEqual(tx.outputs[2].script_pubkey, funding, where)
            self.assertEqual(s.input_sats, s.carrier_sats + s.return_sats + s.fee_sats,
                             where)
        self.run_cases("collect", case)

    def _wallet(self, rng):
        cs = []
        for k in range(rng.randrange(1, 10)):
            seeds = rng.choice([0, 0, rng.randrange(1, 10_000), rng.randrange(1, 10**12)])
            cs.append(utxo(k + 1, rng.randrange(330, 5_000_000), seeds=seeds))
        return FakeRpc(cs)

    def test_send(self):
        def case(rng, where):
            rpc = self._wallet(rng)
            total = sum(u["seeds"] for u in rpc.coins)
            amount = rng.randrange(1, total + 1) if total else 1
            plan = plan_send(KEY, rpc, RECIPIENT, amount, fee_rate(rng))
            tx = sign_send(plan, KEY)
            art = self.assert_shape(tx, where, land=False, edicts=1,
                                    pointer=art_pointer(tx), n_outputs=3)
            self.assertEqual((art.edicts[0].amount, art.edicts[0].output), (amount, 2),
                             where)
            self.assertEqual(tx.outputs[2].value, CARRIER_SATS, where)
        self.run_cases("send", case)

    def test_withdraw(self):
        def case(rng, where):
            rpc = self._wallet(rng)
            total = sum(u["sats"] for u in rpc.coins)
            amount = rng.randrange(546, max(547, total))
            plan = plan_withdraw(KEY, rpc, DEST_WPKH, amount, fee_rate(rng))
            tx = sign_withdraw(plan, KEY)
            self.assert_shape(tx, where, land=False, edicts=0, pointer=1, n_outputs=3)
            self.assertEqual(tx.outputs[1].script_pubkey, KEY.script, where)
            self.assertEqual(tx.outputs[2].value, amount, where)
        self.run_cases("withdraw", case)


def art_pointer(tx):
    """The send template permits pointer absent or 1 (wallet-policy.md)."""
    p = decipher(tx).pointer
    assert p in (None, 1), p
    return p


if __name__ == "__main__":
    unittest.main()
