"""secp256k1/Schnorr (BIP-340 vectors), bech32m (BIP-350 vector), the
wallet code, and PSBT/sighash round trips (bitcoin/secp.py, bech32.py,
psbt.py)."""

from __future__ import annotations

import unittest

from claimstones.bitcoin import bech32, secp
from claimstones.bitcoin.psbt import (
    SIGHASH_ALL, Psbt, PsbtError, Transaction, sign_input, taproot_sighash, verify_input,
)
from claimstones.bitcoin.txparse import TxIn, TxOut

# BIP-340 test vectors 0 and 1
VECTORS = [
    ("0000000000000000000000000000000000000000000000000000000000000003",
     "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
     "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"),
    ("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "0000000000000000000000000000000000000000000000000000000000000001",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE3341"
     "8906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A"),
]


class SchnorrTests(unittest.TestCase):
    def test_bip340_vectors(self):
        for sk, pk, aux, msg, sig in VECTORS:
            seckey = int(sk, 16)
            self.assertEqual(secp.pubkey(seckey).hex().upper(), pk)
            got = secp.schnorr_sign(bytes.fromhex(msg), seckey, bytes.fromhex(aux))
            self.assertEqual(got.hex().upper(), sig)
            self.assertTrue(secp.schnorr_verify(bytes.fromhex(msg), bytes.fromhex(pk), got))
            bad = bytearray(got)
            bad[40] ^= 1
            self.assertFalse(secp.schnorr_verify(bytes.fromhex(msg), bytes.fromhex(pk), bytes(bad)))

    def test_taproot_tweak_matches_signing_key(self):
        seckey = 0xC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9
        internal = secp.pubkey(seckey)
        output_key = secp.taproot_output_key(internal)
        msg = b"\x07" * 32
        sig = secp.schnorr_sign(msg, secp.taproot_tweak_seckey(seckey))
        self.assertTrue(secp.schnorr_verify(msg, output_key, sig))
        self.assertFalse(secp.schnorr_verify(msg, internal, sig))

    def test_point_at_infinity_and_mul(self):
        self.assertIsNone(secp.point_mul(secp.N, secp.G))
        self.assertEqual(secp.point_mul(2, secp.G), secp.point_add(secp.G, secp.G))


class Bech32Tests(unittest.TestCase):
    def test_bip350_p2tr_vector(self):
        program = bytes.fromhex("79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
        addr = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
        self.assertEqual(bech32.encode_segwit("bc", 1, program), addr)
        self.assertEqual(bech32.decode_segwit("bc", addr), (1, program))
        self.assertEqual(bech32.address_to_script("bc", addr), bech32.p2tr_script(program))
        self.assertEqual(bech32.script_to_address("bc", bech32.p2tr_script(program)), addr)

    def test_bip173_v0_vector(self):
        addr = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        v, prog = bech32.decode_segwit("bc", addr)
        self.assertEqual((v, len(prog)), (0, 20))
        self.assertEqual(bech32.encode_segwit("bc", 0, prog), addr)

    def test_wrong_network_and_checksum(self):
        addr = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
        with self.assertRaises(bech32.Bech32Error):
            bech32.decode_segwit("bcrt", addr)
        with self.assertRaises(bech32.Bech32Error):
            bech32.decode_segwit("bc", addr[:-1] + "1")

    def test_wallet_code_round_trip(self):
        secret = bytes(range(32))
        code = bech32.encode_wallet_code(secret)
        self.assertTrue(code.startswith("orchid1"))
        self.assertEqual(len(code), 65)
        self.assertEqual(bech32.decode_wallet_code(code.upper()), secret)
        with self.assertRaises(bech32.Bech32Error):
            bech32.decode_wallet_code("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0")


def make_land_tx(seckey: int, n_inputs: int = 2):
    output_key = secp.taproot_output_key(secp.pubkey(seckey))
    spk = bech32.p2tr_script(output_key)
    tx = Transaction(inputs=[TxIn(bytes([i + 1]) * 32, i, b"", 0xFFFFFFFD) for i in range(n_inputs)],
                     outputs=[TxOut(0, bytes.fromhex("6a5f021c00")), TxOut(9_000, spk)])
    prevouts = [TxOut(5_000, spk) for _ in range(n_inputs)]
    return tx, prevouts


class PsbtTests(unittest.TestCase):
    def test_transaction_round_trip_and_txid(self):
        tx, _ = make_land_tx(5)
        raw = tx.serialize()
        self.assertEqual(Transaction.parse(raw).serialize(), raw)
        tx.inputs[0].witness = [b"\x11" * 64]
        tx.inputs[1].witness = [b"\x22" * 64]
        raw_w = tx.serialize()
        self.assertEqual(raw_w[4:6], b"\x00\x01")
        again = Transaction.parse(raw_w)
        self.assertEqual(again.inputs[1].witness, [b"\x22" * 64])
        self.assertEqual(again.txid(), tx.txid())
        self.assertEqual(tx.txid(), Transaction.parse(raw).txid())  # witness does not change the txid
        self.assertEqual(tx.weight(), len(raw) * 3 + len(raw_w))

    def test_sign_verify_and_psbt_round_trip(self):
        seckey = 12345
        tx, prevouts = make_land_tx(seckey)
        psbt = Psbt.from_tx(tx)
        for inp, prev in zip(psbt.inputs, prevouts):
            inp.witness_utxo = prev
            inp.tap_internal_key = secp.pubkey(seckey)
        b64 = psbt.to_base64()
        back = Psbt.from_base64(b64)
        self.assertEqual(back.tx.serialize(), tx.serialize())
        self.assertEqual(back.inputs[1].witness_utxo, prevouts[1])
        self.assertEqual(back.inputs[0].tap_internal_key, secp.pubkey(seckey))
        with self.assertRaises(PsbtError):
            back.finalize()                       # unsigned
        for n in range(2):
            back.inputs[n].tap_key_sig = sign_input(tx, prevouts, n, seckey)
        self.assertTrue(verify_input(tx, prevouts, 0, back.inputs[0].tap_key_sig))
        signed = back.finalize()
        self.assertEqual(signed.inputs[0].witness, [back.inputs[0].tap_key_sig])
        self.assertEqual(signed.txid(), tx.txid())
        # a signature made for another input, or another key, is refused
        back.inputs[0].tap_key_sig = sign_input(tx, prevouts, 1, seckey)
        with self.assertRaises(PsbtError):
            back.finalize()
        back.inputs[0].tap_key_sig = sign_input(tx, prevouts, 0, seckey + 1)
        with self.assertRaises(PsbtError):
            back.finalize()

    def test_sighash_all_variant(self):
        tx, prevouts = make_land_tx(77)
        sig = sign_input(tx, prevouts, 0, 77, hash_type=SIGHASH_ALL)
        self.assertEqual(len(sig), 65)
        self.assertTrue(verify_input(tx, prevouts, 0, sig))
        self.assertNotEqual(taproot_sighash(tx, prevouts, 0), taproot_sighash(tx, prevouts, 0, SIGHASH_ALL))
        self.assertNotEqual(taproot_sighash(tx, prevouts, 0), taproot_sighash(tx, prevouts, 1))

    def test_sighash_commits_to_outputs_and_prevout_values(self):
        tx, prevouts = make_land_tx(3)
        h = taproot_sighash(tx, prevouts, 0)
        tx.outputs[1].value += 1
        self.assertNotEqual(h, taproot_sighash(tx, prevouts, 0))
        tx.outputs[1].value -= 1
        prevouts[1].value += 1
        self.assertNotEqual(h, taproot_sighash(tx, prevouts, 0))

    def test_parse_rejects_garbage(self):
        for bad in [b"", b"psbt\xfe", b"psbt\xff\x00", b"psbt\xff\x01\x00\x00\x00"]:
            with self.assertRaises(PsbtError):
                Psbt.parse(bad)
        with self.assertRaises(PsbtError):
            Psbt.from_base64("not base64!")


if __name__ == "__main__":
    unittest.main()
