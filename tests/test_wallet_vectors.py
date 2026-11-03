"""The wallet's key derivation and signing, pinned so that the browser
self-test (wallet/selftest.html) and the server agree byte for byte.

Wallet code -> secret (bech32m, hrp "orchid"); master = HMAC-SHA512(
"orchids/1", secret); key = HMAC-SHA512(master, "bitcoin/0")[:32] mod n;
address = P2TR of the BIP-86 tweaked key. Signatures use SIGHASH_DEFAULT
with a zero aux (the wallet uses random aux in production)."""

from __future__ import annotations

import hashlib
import hmac
import unittest

from claimstones.bitcoin import bech32, secp
from claimstones.bitcoin.psbt import Psbt, Transaction, sign_input, taproot_sighash
from claimstones.bitcoin.txparse import TxIn, TxOut
from claimstones.protocol.codec import land_script

CODE = "orchid1qqqsyqcyq5rqwzqfpg9scrgwpugpzysnzs23v9ccrydpk8qarc0sa7d8kr"
SK = "0b17598d3e2e53720ec1e185de3fb4e6bd9612f84d5c6c29cdc3140083a70dc5"
INTERNAL = "6dcba80efa68aea773ec371d86682e916539c817abc5dd53f8dc700a681db6e5"
OUTPUT_KEY = "f4b423a169aed62f2bd8aa0e106a334833cd34b1dec818b16665227364d4ded0"
ADDRESS = "bcrt1p7j6z8gtf4mtz727c4g8pq63nfqeu6d93mmyp3vtxv538xex5mmgq59vmxx"
SIGHASHES = ["7c6a74c3c0c106fabbbaf8c8962fdfdf87826c8c14ec1a54e4a3045df88c1eaf",
             "21a9b535503251af36f4ea27c92d50a32072f34c0505f7e9d9770ba670061aed"]
SIGS = ["6cf76248ef03ce07060381f852933804568aec83b05e7589803fc2ae4f2534481683c9462469a12729b52164f55c524e8f3cd7786990aa549beeb3cc7c0366c1",
        "6184b6f3e8e01eefd69fafd45a47afe96c77106e6820ddb7ef80d4a4ae74ac980124e8d1efca84cd0f3ef57cc7f92200efa9e49d0da1c6b837871011ae5582d5"]
TXID = "ef45b316519ecc8227b4c5135fa856fc9cd0b2909aec2794f82a324f41dc7179"


def garden_key(secret: bytes, garden: str = "bitcoin", index: int = 0) -> int:
    master = hmac.new(b"orchids/1", secret, hashlib.sha512).digest()
    child = hmac.new(master, f"{garden}/{index}".encode(), hashlib.sha512).digest()
    return int.from_bytes(child[:32], "big") % secp.N


class WalletVectorTests(unittest.TestCase):
    def test_derivation(self):
        secret = bech32.decode_wallet_code(CODE)
        self.assertEqual(secret, bytes(range(32)))
        sk = garden_key(secret)
        self.assertEqual(f"{sk:064x}", SK)
        internal = secp.pubkey(sk)
        self.assertEqual(internal.hex(), INTERNAL)
        out_key = secp.taproot_output_key(internal)
        self.assertEqual(out_key.hex(), OUTPUT_KEY)
        self.assertEqual(bech32.encode_segwit("bcrt", 1, out_key), ADDRESS)

    def test_land_signatures(self):
        sk = int(SK, 16)
        spk = bech32.address_to_script("bcrt", ADDRESS)
        tx = Transaction(inputs=[TxIn(bytes([0xAA]) * 32, 0, b"", 0xFFFFFFFD),
                                 TxIn(bytes([0xBB]) * 32, 1, b"", 0xFFFFFFFD)],
                         outputs=[TxOut(0, land_script()), TxOut(49_634, spk)])
        prevouts = [TxOut(20_000, spk), TxOut(30_000, spk)]
        self.assertEqual(tx.txid_hex, TXID)
        for n in range(2):
            self.assertEqual(taproot_sighash(tx, prevouts, n).hex(), SIGHASHES[n])
            self.assertEqual(sign_input(tx, prevouts, n, sk).hex(), SIGS[n])
        psbt = Psbt.from_tx(tx)
        for n, inp in enumerate(psbt.inputs):
            inp.witness_utxo = prevouts[n]
            inp.tap_key_sig = bytes.fromhex(SIGS[n])
        signed = psbt.finalize()
        self.assertEqual(signed.txid_hex, TXID)


if __name__ == "__main__":
    unittest.main()
