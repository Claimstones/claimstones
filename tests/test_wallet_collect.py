"""The external claim and collect (wallet/collect.py, wallet/cli.py):
plan against a fake indexer, the refusals, the PSBT file, and the
mandated wording. The signing wallet broadcasts; nothing comes back."""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from claimstones.bitcoin.descriptors import parse_line
from claimstones.bitcoin.psbt import Psbt
from claimstones.wallet.cli import do_collect
from claimstones.wallet.collect import plan_collect
from claimstones.wallet.keys import WalletKey
from claimstones.wallet.land import CARRIER_SATS, TemplateError
from claimstones.wallet.rpc import NO_NODE, WalletRpcError

CODE = "orchid1qqqsyqcyq5rqwzqfpg9scrgwpugpzysnzs23v9ccrydpk8qarc0sa7d8kr"
KEY = WalletKey.from_code(CODE)

# An external wpkh address (BIP-84 test vector, first receive address).
FUNDING = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
FUNDING_SCRIPT = parse_line(FUNDING, "main")[0].derive(0).script


def utxo(salt: int, sats: int, latent: int | None = None) -> dict:
    latent = sats if latent is None else latent
    return {"txid": f"{salt:064x}", "vout": salt, "sats": sats,
            "latent_sats": latent, "runs": []}


class FakeRpc:
    def __init__(self, coins: dict[str, list[dict]], node: bool = True):
        self.coins = coins
        self.node = node

    def getaddressutxos(self, script_hex: str) -> list[dict]:
        return [dict(u) for u in self.coins.get(script_hex, [])]

    def getlatent(self, script_hex: str) -> dict:
        if not self.node:
            raise WalletRpcError(NO_NODE, "getlatent needs the host node")
        utxos = [dict(u, seeds=u["latent_sats"] // 10)
                 for u in self.coins.get(script_hex, [])]
        return {"valued_at_height": 972_618,
                "seeds": sum(u["seeds"] for u in utxos), "utxos": utxos}


def funding_rpc(*utxos: dict, node: bool = True) -> FakeRpc:
    return FakeRpc({FUNDING_SCRIPT.hex(): list(utxos)}, node=node)


class PlanCollectTests(unittest.TestCase):
    def test_plan(self):
        rpc = funding_rpc(utxo(1, 50_000), utxo(2, 30_000, latent=10_000))
        plan = plan_collect(KEY.script, FUNDING, rpc, 2.0)
        s = plan.summary
        self.assertEqual(len(s.inputs), 2)
        self.assertEqual(s.input_sats, 80_000)
        self.assertEqual(s.carrier_sats, CARRIER_SATS)
        self.assertEqual(s.input_sats,
                         s.carrier_sats + s.return_sats + s.fee_sats)
        self.assertEqual(plan.latent_sats, 60_000)
        self.assertEqual(plan.seeds, 6_000)      # one per ten latent sats
        # Output 1 pays the wallet, output 2 returns to the funding script.
        self.assertEqual(plan.psbt.tx.outputs[1].script_pubkey, KEY.script)
        self.assertEqual(plan.psbt.tx.outputs[2].script_pubkey, FUNDING_SCRIPT)

    def test_refusals(self):
        with self.assertRaisesRegex(TemplateError, "no UTXOs"):
            plan_collect(KEY.script, FUNDING, funding_rpc(), 1.0)
        zpub = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH"
                "1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs")
        with self.assertRaisesRegex(TemplateError, "one funding address"):
            plan_collect(KEY.script, zpub, funding_rpc(utxo(1, 50_000)), 1.0)
        with self.assertRaisesRegex(TemplateError, "plain claim"):
            plan_collect(KEY.script, KEY.address("main"),
                         FakeRpc({KEY.script.hex(): [utxo(1, 50_000)]}), 1.0)

    def test_without_a_node(self):
        plan = plan_collect(KEY.script, FUNDING,
                            funding_rpc(utxo(1, 50_000), node=False), 1.0)
        self.assertIsNone(plan.seeds)
        self.assertEqual(plan.latent_sats, 50_000)


class CliCollectTests(unittest.TestCase):
    def test_collect_writes_the_psbt_and_says_what_is_paid(self):
        rpc = funding_rpc(utxo(1, 50_000))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            rc = do_collect(rpc, KEY, "main", FUNDING, 2.0,
                            out_dir=folder, out=out)
            self.assertEqual(rc, 0)
            files = os.listdir(folder)
            self.assertEqual([f.rsplit(".", 1)[1] for f in files], ["psbt"])
            with open(os.path.join(folder, files[0]), encoding="ascii") as f:
                psbt = Psbt.from_base64(f.read())
            self.assertEqual(len(psbt.tx.outputs), 3)
            self.assertEqual(psbt.tx.outputs[1].script_pubkey, KEY.script)
        text = out.getvalue()
        self.assertIn("PSBT written to", text)
        self.assertIn("The signer pays the miner fee (2 sat/vB) plus 330 sats",
                      text)
        self.assertIn("use CPFP in your other wallet", text)
        self.assertIn("do not use Increase fee (RBF)", text)
        self.assertIn("est. orchids to be landed: 50.00", text)   # 5000 seeds
        self.assertNotIn("worth", text)
        self.assertNotIn("production cost", text)

    def test_no_node_wording(self):
        rpc = funding_rpc(utxo(1, 50_000), node=False)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            rc = do_collect(rpc, KEY, "main", FUNDING, 2.0,
                            out_dir=folder, out=out)
        self.assertEqual(rc, 0)
        self.assertIn("no host node", out.getvalue())
        self.assertNotIn("est. orchids to be landed:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
