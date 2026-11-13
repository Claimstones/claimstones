"""Claim (wallet/keys.py, wallet/claim.py, wallet/cli.py): the wallet
key against the pinned vectors, the wallet file, planning and signing
a claim over a fake indexer RPC, the refusal paths, and the mandated
confirmation wording."""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from claimstones.bitcoin.psbt import Transaction, verify_input
from claimstones.bitcoin.txparse import TxOut
from claimstones.wallet.claim import plan_claim, sign_claim
from claimstones.wallet.cli import do_claim, print_claim_plan
from claimstones.wallet.keys import (
    WalletKey, WalletKeyError, load_wallet, save_wallet,
)
from claimstones.wallet.land import TemplateError
from claimstones.wallet.rpc import NO_NODE, WalletRpcError

# The pinned derivation vectors (tests/test_wallet_vectors.py).
CODE = "orchid1qqqsyqcyq5rqwzqfpg9scrgwpugpzysnzs23v9ccrydpk8qarc0sa7d8kr"
ADDRESS = "bcrt1p7j6z8gtf4mtz727c4g8pq63nfqeu6d93mmyp3vtxv538xex5mmgq59vmxx"

KEY = WalletKey.from_code(CODE)


def utxo(salt: int, sats: int, latent: int | None = None) -> dict:
    latent = sats if latent is None else latent
    return {"txid": f"{salt:064x}", "vout": salt, "sats": sats,
            "latent_sats": latent, "runs": []}


class FakeRpc:
    """Coins keyed by script hex; seeds valued at one per ten latent
    sats; sendrawtransaction parses and remembers."""

    def __init__(self, coins: dict[str, list[dict]], node: bool = True):
        self.coins = coins
        self.node = node
        self.sent: list[str] = []

    def getaddressutxos(self, script_hex: str) -> list[dict]:
        return [dict(u) for u in self.coins.get(script_hex, [])]

    def getlatent(self, script_hex: str) -> dict:
        if not self.node:
            raise WalletRpcError(NO_NODE, "getlatent needs the host node")
        utxos = [dict(u, seeds=u["latent_sats"] // 10)
                 for u in self.coins.get(script_hex, [])]
        return {"valued_at_height": 972_618,
                "seeds": sum(u["seeds"] for u in utxos), "utxos": utxos}

    def sendrawtransaction(self, hexstring: str) -> str:
        tx = Transaction.parse(bytes.fromhex(hexstring))
        self.sent.append(hexstring)
        return tx.txid_hex


def wallet_rpc(*utxos: dict, node: bool = True) -> FakeRpc:
    return FakeRpc({KEY.script.hex(): list(utxos)}, node=node)


class WalletKeyTests(unittest.TestCase):
    def test_pinned_vectors(self):
        self.assertEqual(KEY.address("regtest"), ADDRESS)
        self.assertEqual(KEY.script[:2], bytes([0x51, 0x20]))
        self.assertEqual(KEY.code, CODE)

    def test_new_wallets_differ(self):
        a, b = WalletKey.new(), WalletKey.new()
        self.assertNotEqual(a.secret, b.secret)
        self.assertEqual(WalletKey.from_code(a.code).secret, a.secret)

    def test_refusals(self):
        with self.assertRaises(WalletKeyError):
            WalletKey.from_code("orchid1notacode")
        with self.assertRaises(WalletKeyError):
            WalletKey.from_code(ADDRESS)     # right shape, wrong hrp
        with self.assertRaises(WalletKeyError):
            WalletKey(b"short")

    def test_repr_hides_the_secret(self):
        self.assertNotIn(KEY.secret.hex(), repr(KEY))

    def test_wallet_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub", "wallet.code")
            save_wallet(path, KEY)
            self.assertEqual(load_wallet(path).secret, KEY.secret)
            with self.assertRaises(WalletKeyError):
                save_wallet(path, WalletKey.new())   # never overwrite
            empty = os.path.join(folder, "empty.code")
            with open(empty, "w", encoding="ascii") as f:
                f.write("# nothing here\n")
            with self.assertRaises(WalletKeyError):
                load_wallet(empty)


class PlanClaimTests(unittest.TestCase):
    def test_plan_takes_latent_coins_only(self):
        rpc = wallet_rpc(utxo(1, 50_000),
                         utxo(2, 20_000, latent=0),      # settled: untouched
                         utxo(3, 7_000, latent=3_000))   # partly latent: in
        plan = plan_claim(KEY, rpc, 1.0)
        self.assertEqual(len(plan.summary.inputs), 2)
        self.assertEqual(plan.summary.input_sats, 57_000)
        self.assertEqual(plan.latent_sats, 53_000)
        self.assertEqual(plan.skipped, 1)
        # Seeds for the two claimed coins only, not the settled one.
        self.assertEqual(plan.seeds, (50_000 + 3_000) // 10)
        self.assertEqual(plan.valued_at_height, 972_618)
        self.assertEqual(plan.summary.input_sats,
                         plan.summary.change_sats + plan.summary.fee_sats)

    def test_nothing_to_claim(self):
        with self.assertRaisesRegex(TemplateError, "holds no UTXOs"):
            plan_claim(KEY, wallet_rpc(), 1.0)
        with self.assertRaisesRegex(TemplateError, "already claimed"):
            plan_claim(KEY, wallet_rpc(utxo(1, 20_000, latent=0)), 1.0)

    def test_without_a_node_the_claim_still_builds(self):
        plan = plan_claim(KEY, wallet_rpc(utxo(1, 50_000), node=False), 1.0)
        self.assertIsNone(plan.seeds)
        self.assertIsNone(plan.valued_at_height)
        self.assertEqual(plan.latent_sats, 50_000)


class SignClaimTests(unittest.TestCase):
    def test_sign_produces_a_valid_claim(self):
        rpc = wallet_rpc(utxo(1, 50_000), utxo(2, 30_000))
        plan = plan_claim(KEY, rpc, 2.0)
        tx = sign_claim(plan, KEY)
        prevouts = plan.psbt.prevouts()
        for n in range(len(tx.inputs)):
            self.assertTrue(verify_input(tx, prevouts, n,
                                         tx.inputs[n].witness[0]))
        self.assertEqual(len(tx.outputs), 2)
        self.assertEqual(tx.outputs[1].script_pubkey, KEY.script)

    def test_sign_refuses_a_tampered_plan(self):
        plan = plan_claim(KEY, wallet_rpc(utxo(1, 50_000)), 1.0)
        change = plan.psbt.tx.outputs[1]
        other = bytes([0x51, 0x20]) + bytes(32)
        plan.psbt.tx.outputs[1] = TxOut(change.value, other)
        with self.assertRaisesRegex(TemplateError, "wallet's own address"):
            sign_claim(plan, KEY)


class DoClaimTests(unittest.TestCase):
    def test_confirmed_claim_is_sent(self):
        rpc = wallet_rpc(utxo(1, 50_000))
        out = io.StringIO()
        rc = do_claim(rpc, KEY, 1.0, out, ask=lambda _: "y")
        self.assertEqual(rc, 0)
        self.assertEqual(len(rpc.sent), 1)
        self.assertIn("claim sent: ", out.getvalue())

    def test_declined_claim_sends_nothing(self):
        rpc = wallet_rpc(utxo(1, 50_000))
        out = io.StringIO()
        rc = do_claim(rpc, KEY, 1.0, out, ask=lambda _: "n")
        self.assertEqual(rc, 0)
        self.assertEqual(rpc.sent, [])
        self.assertIn("nothing sent", out.getvalue())

    def test_wording(self):
        plan = plan_claim(KEY, wallet_rpc(utxo(1, 50_000),
                                          utxo(2, 1_000, latent=0)), 1.0)
        out = io.StringIO()
        print_claim_plan(plan, out)
        text = out.getvalue()
        self.assertIn("claiming does not spend your bitcoin;"
                      " you pay a network fee", text)
        self.assertIn("est. orchids to be landed: 50.00", text)   # 5000 seeds
        self.assertIn("(1 UTXO on the address with nothing left to land", text)
        self.assertNotIn("worth", text)
        self.assertNotIn("production cost", text)

    def test_no_node_wording(self):
        plan = plan_claim(KEY, wallet_rpc(utxo(1, 50_000), node=False), 1.0)
        out = io.StringIO()
        print_claim_plan(plan, out)
        self.assertIn("no host node", out.getvalue())
        self.assertNotIn("est. orchids to be landed:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
