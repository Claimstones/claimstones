"""The land templates (wallet/land.py): build and verify the claim and
the claim-and-collect against wallet-policy.md §Transaction templates
and §Envelope scan, across all four funding script types, with PSBT
round trips and a signed claim end to end."""

from __future__ import annotations

import unittest

from claimstones.bitcoin import bech32, secp
from claimstones.bitcoin.descriptors import parse_line
from claimstones.bitcoin.psbt import Psbt, sign_input
from claimstones.bitcoin.txparse import TxOut
from claimstones.protocol.codec import send_script
from claimstones.wallet.land import (
    CARRIER_SATS, SEQUENCE, ClaimSummary, CollectSummary, FundingInput,
    TemplateError, build_claim, build_collect, script_type, verify_collect,
    verify_land,
)

BIP86_XPUB = ("xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVk"
              "y7D3UKDP9QyrLprQ3VCECoY49yfdDEHGCtMMj92pReUsQ")

WALLET_SECKEY = 0xC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9
WALLET_INTERNAL = secp.pubkey(WALLET_SECKEY)
WALLET_SCRIPT = bech32.p2tr_script(secp.taproot_output_key(WALLET_INTERNAL))


def coin(script: bytes, value: int, salt: int, **kw) -> FundingInput:
    return FundingInput(bytes([salt]) * 32, salt, TxOut(value, script), **kw)


def derived(kind_desc: str, index: int = 0):
    """DerivedScript via the descriptors module, origin included."""
    line = kind_desc.replace("KEY", f"[aabbccdd/86h/0h/0h]{BIP86_XPUB}/0/*")
    return parse_line(line)[0].derive(index)


class ClaimTests(unittest.TestCase):
    def build(self, values=(50_000, 30_000), fee_rate=2):
        inputs = [coin(WALLET_SCRIPT, v, n + 1) for n, v in enumerate(values)]
        return build_claim(inputs, WALLET_SCRIPT, WALLET_INTERNAL, fee_rate)

    def test_template_and_summary(self):
        psbt, summary = self.build()
        self.assertEqual(len(psbt.tx.outputs), 2)
        self.assertEqual(psbt.tx.outputs[0].value, 0)
        self.assertEqual(summary.input_sats, 80_000)
        self.assertEqual(summary.input_sats,
                         summary.change_sats + summary.fee_sats)
        self.assertTrue(all(i.sequence == SEQUENCE for i in psbt.tx.inputs))
        self.assertTrue(all(inp.tap_internal_key == WALLET_INTERNAL
                            for inp in psbt.inputs))

    def test_signed_end_to_end(self):
        psbt, summary = self.build()
        prevouts = psbt.prevouts()
        for n in range(len(psbt.inputs)):
            psbt.inputs[n].tap_key_sig = sign_input(
                psbt.tx, prevouts, n, WALLET_SECKEY)
        signed = psbt.finalize()
        verify_land(signed, prevouts, WALLET_SCRIPT, summary)
        # The estimate planned for worst-case material, so the paid rate
        # is at least the asked rate.
        self.assertGreaterEqual(summary.fee_sats, 2 * signed.vsize())
        self.assertLessEqual(summary.fee_sats, 2 * (signed.vsize() + 12))

    def test_psbt_round_trip(self):
        psbt, _ = self.build()
        again = Psbt.from_base64(psbt.to_base64())
        self.assertEqual(again.serialize(), psbt.serialize())
        self.assertEqual(again.inputs[0].tap_internal_key, WALLET_INTERNAL)

    def test_refusals(self):
        foreign = derived("tr(KEY)")
        with self.assertRaises(TemplateError):    # not the wallet's coin
            build_claim([coin(foreign.script, 50_000, 1)],
                        WALLET_SCRIPT, WALLET_INTERNAL, 2)
        with self.assertRaises(TemplateError):    # wrong internal key
            build_claim([coin(WALLET_SCRIPT, 50_000, 1)],
                        WALLET_SCRIPT, foreign.pubkey[1:], 2)
        with self.assertRaises(TemplateError):    # fee bound / dust
            self.build(values=(900,), fee_rate=2)
        with self.assertRaises(TemplateError):    # below-relay rate
            self.build(fee_rate=0.5)
        with self.assertRaises(TemplateError):    # nothing to claim
            build_claim([], WALLET_SCRIPT, WALLET_INTERNAL, 2)

    def test_verify_rejects_tampering(self):
        psbt, summary = self.build()
        prevouts = psbt.prevouts()

        tampered = Psbt.from_base64(psbt.to_base64()).tx
        tampered.outputs[1] = TxOut(tampered.outputs[1].value,
                                    derived("tr(KEY)").script)
        with self.assertRaises(TemplateError):    # change leaves the wallet
            verify_land(tampered, prevouts, WALLET_SCRIPT, summary)

        tampered = Psbt.from_base64(psbt.to_base64()).tx
        tampered.outputs[0], tampered.outputs[1] = (tampered.outputs[1],
                                                    tampered.outputs[0])
        with self.assertRaises(TemplateError):    # envelope not first
            verify_land(tampered, prevouts, WALLET_SCRIPT, summary)

        tampered = Psbt.from_base64(psbt.to_base64()).tx
        tampered.outputs[0] = TxOut(0, send_script([(5, 1)]))
        with self.assertRaises(TemplateError):    # edicts in the envelope
            verify_land(tampered, prevouts, WALLET_SCRIPT, summary)

        wrong = ClaimSummary(summary.inputs, summary.input_sats,
                             summary.change_sats - 1, summary.fee_sats + 1)
        with self.assertRaises(TemplateError):    # summary disagrees
            verify_land(psbt.tx, prevouts, WALLET_SCRIPT, wrong)


class CollectTests(unittest.TestCase):
    ORCHIDS = WALLET_SCRIPT

    def build_for(self, desc: str, fee_rate=3, values=(60_000, 40_000),
                  with_prev=False):
        inputs = []
        d = derived(desc)
        for n, v in enumerate(values):
            kw = {"pubkey": d.pubkey, "origin": d.origin}
            if with_prev:
                prev, txid = self.prev_paying(TxOut(v, d.script))
                inputs.append(FundingInput(txid, 0, TxOut(v, d.script),
                                           prev_tx=prev, **kw))
            else:
                inputs.append(coin(d.script, v, n + 1, **kw))
        return build_collect(inputs, self.ORCHIDS, d.script, fee_rate)

    @staticmethod
    def prev_paying(utxo: TxOut):
        from claimstones.bitcoin.psbt import Transaction
        from claimstones.bitcoin.txparse import TxIn
        prev = Transaction(inputs=[TxIn(bytes(32), 0xFFFFFFFF, b"\x51", 0)],
                           outputs=[utxo])
        return prev.serialize(), prev.txid()

    def test_template_across_script_types(self):
        for desc in ("tr(KEY)", "wpkh(KEY)", "sh(wpkh(KEY))", "pkh(KEY)"):
            with self.subTest(desc=desc):
                psbt, summary = self.build_for(desc, with_prev=True)
                self.assertEqual(len(psbt.tx.outputs), 3)
                self.assertEqual(psbt.tx.outputs[1].value, CARRIER_SATS)
                self.assertEqual(summary.input_sats,
                                 summary.return_sats + summary.carrier_sats
                                 + summary.fee_sats)
                self.assertTrue(all(inp.non_witness_utxo for inp in psbt.inputs))

    def test_psbt_carries_what_signers_need(self):
        psbt, _ = self.build_for("tr(KEY)")
        again = Psbt.from_base64(psbt.to_base64())
        d = derived("tr(KEY)")
        xonly = d.pubkey[1:]
        self.assertEqual(again.inputs[0].tap_internal_key, xonly)
        value = again.inputs[0].tap_bip32_derivation[xonly]
        self.assertEqual(value[:5], b"\x00" + bytes.fromhex("aabbccdd"))
        h = 0x80000000
        path = [int.from_bytes(value[5 + 4 * i:9 + 4 * i], "little")
                for i in range(5)]
        self.assertEqual(path, [86 + h, 0 + h, 0 + h, 0, 0])

        psbt, _ = self.build_for("sh(wpkh(KEY))")
        again = Psbt.from_base64(psbt.to_base64())
        d = derived("sh(wpkh(KEY))")
        self.assertEqual(again.inputs[0].redeem_script[:2], b"\x00\x14")
        self.assertIn(d.pubkey, again.inputs[0].bip32_derivation)

    def test_refusals(self):
        d_tr, d_w = derived("tr(KEY)"), derived("wpkh(KEY)")
        with self.assertRaises(TemplateError):    # two funding scripts
            build_collect([coin(d_tr.script, 50_000, 1),
                           coin(d_w.script, 50_000, 2)],
                          self.ORCHIDS, d_tr.script, 3)
        with self.assertRaises(TemplateError):    # funding == orchid address
            build_collect([coin(self.ORCHIDS, 50_000, 1)],
                          self.ORCHIDS, self.ORCHIDS, 3)
        with self.assertRaises(TemplateError) as ctx:   # legacy without prev tx
            self.build_for("pkh(KEY)", with_prev=False)
        self.assertIn("previous transaction", str(ctx.exception))
        # Dust: learn the (amount-independent) fee, then fund one sat
        # short of a spendable return. sh(wpkh)'s 540-sat dust bound sits
        # above the fee bound, so the dust refusal is the one that fires.
        _, s = self.build_for("sh(wpkh(KEY))", fee_rate=1, values=(50_000,),
                              with_prev=True)
        short = CARRIER_SATS + s.fee_sats + 540 - 1
        with self.assertRaises(TemplateError) as ctx:
            self.build_for("sh(wpkh(KEY))", fee_rate=1, values=(short,),
                           with_prev=True)
        self.assertIn("dust", str(ctx.exception))
        with self.assertRaises(TemplateError):    # fee over a fifth
            self.build_for("tr(KEY)", values=(2_000, 2_000), fee_rate=10)

    def test_wrong_prev_tx_refused(self):
        d = derived("pkh(KEY)")
        prev, _ = self.prev_paying(TxOut(60_000, d.script))
        wrong_txid = bytes([9]) * 32
        with self.assertRaises(TemplateError):
            build_collect([FundingInput(wrong_txid, 0, TxOut(60_000, d.script),
                                        prev_tx=prev, pubkey=d.pubkey)],
                          self.ORCHIDS, d.script, 3)

    def test_verify_rejects_tampering(self):
        psbt, summary = self.build_for("wpkh(KEY)")
        prevouts = psbt.prevouts()
        verify_collect(psbt.tx, prevouts, self.ORCHIDS, summary)

        tampered = Psbt.from_base64(psbt.to_base64()).tx
        tampered.outputs[1], tampered.outputs[2] = (tampered.outputs[2],
                                                    tampered.outputs[1])
        with self.assertRaises(TemplateError):    # orchids first, or gone
            verify_collect(tampered, prevouts, self.ORCHIDS, summary)

        tampered = Psbt.from_base64(psbt.to_base64()).tx
        tampered.outputs[1] = TxOut(CARRIER_SATS + 1,
                                    tampered.outputs[1].script_pubkey)
        with self.assertRaises(TemplateError):    # carrier is exactly 330
            verify_collect(tampered, prevouts, self.ORCHIDS, summary)

        tampered = Psbt.from_base64(psbt.to_base64()).tx
        tampered.outputs[2] = TxOut(tampered.outputs[2].value, WALLET_SCRIPT)
        wrong = CollectSummary(summary.inputs, summary.input_sats,
                               summary.carrier_sats, summary.return_sats,
                               summary.fee_sats)
        with self.assertRaises(TemplateError):    # return leaves the funder
            verify_collect(tampered, prevouts, self.ORCHIDS, wrong)

    def test_orchids_may_take_any_single_key_script(self):
        d = derived("wpkh(KEY)")
        funder = derived("tr(KEY)")
        psbt, summary = build_collect(
            [coin(funder.script, 50_000, 1, pubkey=funder.pubkey)],
            d.script, funder.script, 3)
        verify_collect(psbt.tx, psbt.prevouts(), d.script, summary)


class ScriptTypeTests(unittest.TestCase):
    def test_the_four_types(self):
        self.assertEqual(script_type(WALLET_SCRIPT), "tr")
        self.assertEqual(script_type(derived("wpkh(KEY)").script), "wpkh")
        self.assertEqual(script_type(derived("sh(wpkh(KEY))").script), "sh(wpkh)")
        self.assertEqual(script_type(derived("pkh(KEY)").script), "pkh")
        with self.assertRaises(TemplateError):
            script_type(b"\x6a\x5f")   # an OP_RETURN funds nothing


if __name__ == "__main__":
    unittest.main()
