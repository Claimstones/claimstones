"""The external claim and collect (wallet/collect.py, wallet/cli.py,
psbt extract): plan against a fake indexer, the record round trip, a
simulated external signer returning raw hex and a finalized PSBT, the
tamper refusals, and the mandated wording."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest

from claimstones.bitcoin.descriptors import parse_line
from claimstones.bitcoin.psbt import Psbt, Transaction, compact_size
from claimstones.bitcoin.txparse import TxOut
from claimstones.wallet.cli import do_collect, do_relay, print_collect_plan
from claimstones.wallet.collect import (
    load_record, plan_collect, plan_record, verify_returned,
)
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


def funding_rpc(*utxos: dict, node: bool = True) -> FakeRpc:
    return FakeRpc({FUNDING_SCRIPT.hex(): list(utxos)}, node=node)


def fake_sign(psbt: Psbt) -> Transaction:
    """What an external wpkh wallet does: finalize with signature
    material. The signatures are fakes — the template checks do not
    verify ECDSA; the network does."""
    tx = Transaction.parse(psbt.tx.serialize(with_witness=False))
    for txin in tx.inputs:
        txin.witness = [bytes(71), bytes(33)]
    return tx


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
        with self.assertRaisesRegex(TemplateError, "no coins"):
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


class ReturnedTests(unittest.TestCase):
    def setUp(self):
        self.rpc = funding_rpc(utxo(1, 50_000), utxo(2, 30_000))
        self.plan = plan_collect(KEY.script, FUNDING, self.rpc, 2.0)
        self.record = json.loads(json.dumps(plan_record(self.plan)))

    def test_record_round_trip(self):
        record = load_record(json.dumps(self.record))
        self.assertEqual(record["funding_address"], FUNDING)
        with self.assertRaisesRegex(TemplateError, "not a claim-and-collect"):
            load_record("{}")
        with self.assertRaisesRegex(TemplateError, "not JSON"):
            load_record("junk")

    def test_signed_hex_is_accepted(self):
        signed = fake_sign(self.plan.psbt)
        tx = verify_returned(signed.serialize().hex(), self.record)
        self.assertEqual(tx.txid(), self.plan.psbt.tx.txid())

    def test_finalized_psbt_is_accepted(self):
        # The signer returns the PSBT with final fields instead of hex.
        psbt = Psbt.from_base64(self.record["psbt"])
        for inp in psbt.inputs:
            witness = compact_size(2) + bytes([71]) + bytes(71) \
                + bytes([33]) + bytes(33)
            inp.final_script_witness = witness
        again = Psbt.from_base64(psbt.to_base64())   # survives the wire
        self.assertEqual(again.inputs[0].final_script_witness,
                         psbt.inputs[0].final_script_witness)
        tx = verify_returned(psbt.to_base64(), self.record)
        self.assertEqual(len(tx.inputs[0].witness), 2)

    def test_unsigned_psbt_is_refused(self):
        with self.assertRaisesRegex(TemplateError, "not signed"):
            verify_returned(self.record["psbt"], self.record)

    def test_foreign_input_is_refused(self):
        signed = fake_sign(self.plan.psbt)
        signed.inputs[0].prev_vout = 7
        with self.assertRaisesRegex(TemplateError, "not in the record"):
            verify_returned(signed.serialize().hex(), self.record)

    def test_edited_outputs_are_refused(self):
        signed = fake_sign(self.plan.psbt)
        # The classic attack: swap the carrier away from the orchid wallet.
        signed.outputs[1] = TxOut(signed.outputs[1].value, FUNDING_SCRIPT)
        with self.assertRaisesRegex(TemplateError, "orchid wallet"):
            verify_returned(signed.serialize().hex(), self.record)

    def test_tampered_record_is_refused(self):
        signed = fake_sign(self.plan.psbt)
        self.record["summary"]["fee_sats"] += 1
        with self.assertRaisesRegex(TemplateError, "do not match"):
            verify_returned(signed.serialize().hex(), self.record)


class CliCollectTests(unittest.TestCase):
    def test_collect_then_relay(self):
        rpc = funding_rpc(utxo(1, 50_000))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            rc = do_collect(rpc, KEY, "main", FUNDING, 2.0,
                            out_dir=folder, out=out)
            self.assertEqual(rc, 0)
            files = sorted(os.listdir(folder))
            self.assertEqual([f.rsplit(".", 1)[1] for f in files],
                             ["json", "psbt"])
            record_path = os.path.join(folder, files[0])
            with open(os.path.join(folder, files[1]), encoding="ascii") as f:
                psbt = Psbt.from_base64(f.read())
            signed_hex = fake_sign(psbt).serialize().hex()
            signed_path = os.path.join(folder, "signed.hex")
            with open(signed_path, "w", encoding="ascii") as f:
                f.write(signed_hex + "\n")
            out2 = io.StringIO()
            rc = do_relay(rpc, record_path, signed_path, out=out2)
            self.assertEqual(rc, 0)
            self.assertEqual(len(rpc.sent), 1)
            self.assertIn("collect sent: ", out2.getvalue())
        text = out.getvalue()
        self.assertIn("The signer pays the miner fee (2 sat/vB) plus 330 sats",
                      text)
        self.assertIn("use CPFP in your other wallet", text)
        self.assertIn("do not use Increase fee (RBF)", text)
        self.assertIn("est. orchids to be landed: 50.00", text)   # 5000 seeds
        self.assertNotIn("worth", text)
        self.assertNotIn("production cost", text)

    def test_relay_refuses_tamper(self):
        rpc = funding_rpc(utxo(1, 50_000))
        with tempfile.TemporaryDirectory() as folder:
            do_collect(rpc, KEY, "main", FUNDING, 2.0,
                       out_dir=folder, out=io.StringIO())
            files = sorted(os.listdir(folder))
            with open(os.path.join(folder, files[1]), encoding="ascii") as f:
                psbt = Psbt.from_base64(f.read())
            signed = fake_sign(psbt)
            signed.outputs[1] = TxOut(signed.outputs[1].value, FUNDING_SCRIPT)
            rc = do_relay(rpc, os.path.join(folder, files[0]),
                          signed.serialize().hex(), out=io.StringIO())
            self.assertEqual(rc, 1)
            self.assertEqual(rpc.sent, [])


if __name__ == "__main__":
    unittest.main()
