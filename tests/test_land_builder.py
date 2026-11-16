"""The land builder (indexer/land_builder.py, Spec 8): the refusals it
adds in front of plan_collect, the answer's shape, the input cap, and
the HTTP surface. The indexer is a fake; the template and its
verification are the wallet's own code, checked again here on the
returned PSBT."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import unittest
from http.server import ThreadingHTTPServer

from claimstones.bitcoin.psbt import Psbt
from claimstones.indexer.land_builder import (MAX_BODY, Refused, Unavailable,
                                              build, make_handler)
from claimstones.wallet.land import CARRIER_SATS, CollectSummary, verify_collect
from claimstones.bitcoin.descriptors import parse_line
from claimstones.wallet.keys import WalletKey
from claimstones.wallet.rpc import RpcUnreachable

# As tests/test_wallet_collect.py: the test wallet, and an external wpkh
# address (BIP-84 test vector, first receive address) holding the coins.
KEY = WalletKey.from_code(
    "orchid1qqqsyqcyq5rqwzqfpg9scrgwpugpzysnzs23v9ccrydpk8qarc0sa7d8kr")
FUNDING = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
FUNDING_SCRIPT = parse_line(FUNDING, "main")[0].derive(0).script


def utxo(salt: int, sats: int, latent: int | None = None) -> dict:
    latent = sats if latent is None else latent
    return {"txid": f"{salt:064x}", "vout": salt, "sats": sats,
            "latent_sats": latent, "runs": []}


class FakeRpc:
    """getlatent values one seed per ten latent sats."""
    def __init__(self, coins: dict[str, list[dict]]):
        self.coins = coins

    def getaddressutxos(self, script_hex: str) -> list[dict]:
        return [dict(u) for u in self.coins.get(script_hex, [])]

    def getlatent(self, script_hex: str) -> dict:
        utxos = [dict(u, seeds=u["latent_sats"] // 10)
                 for u in self.coins.get(script_hex, [])]
        return {"valued_at_height": 980_100,
                "seeds": sum(u["seeds"] for u in utxos), "utxos": utxos}

ORCHIDS = KEY.address("main")
LEGACY = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"


class BuilderRpc(FakeRpc):
    def __init__(self, coins, height=980_100, activation=980_000,
                 chain="main", fee=3, unreachable=False):
        super().__init__(coins)
        self.info = {"chain": chain, "height": height,
                     "activation_height": activation}
        self.fee = fee
        self.unreachable = unreachable
        self.fee_calls = 0

    def getindexinfo(self):
        if self.unreachable:
            raise RpcUnreachable("cannot reach http://127.0.0.1:7332")
        return dict(self.info)

    def getfeerate(self, blocks=3):
        self.fee_calls += 1
        return {"fee_rate": self.fee, "blocks": blocks}


def rpc_with(*coins, **kw) -> BuilderRpc:
    return BuilderRpc({FUNDING_SCRIPT.hex(): list(coins)}, **kw)


def req(**over) -> dict:
    return {"address": FUNDING, "orchids_to": ORCHIDS, **over}


class BuildTests(unittest.TestCase):
    def test_answer_is_the_verified_template(self):
        rpc = rpc_with(utxo(1, 50_000), utxo(2, 30_000, latent=10_000))
        out = build(rpc, req())
        s = out["summary"]
        self.assertEqual(rpc.fee_calls, 1)          # default fee asked
        self.assertEqual(s["fee_rate"], 3)
        self.assertEqual((s["inputs"], s["input_sats"]), (2, 80_000))
        self.assertEqual(s["carrier_sats"], CARRIER_SATS)
        self.assertEqual(s["input_sats"],
                         s["carrier_sats"] + s["return_sats"] + s["fee_sats"])
        self.assertEqual(s["seeds"], 6_000)
        self.assertEqual((s["utxo_count"], s["capped"]), (2, False))
        psbt = Psbt.from_base64(out["psbt"])
        outpoints = tuple(f"{i.prev_txid[::-1].hex()}:{i.prev_vout}"
                          for i in psbt.tx.inputs)
        verify_collect(psbt.tx, psbt.prevouts(), KEY.script,
                       CollectSummary(outpoints, s["input_sats"],
                                      s["carrier_sats"], s["return_sats"],
                                      s["fee_sats"]))
        for inp in psbt.inputs:                     # short form only
            self.assertIsNotNone(inp.witness_utxo)
            self.assertIsNone(inp.non_witness_utxo)

    def test_given_fee_rate_is_used(self):
        rpc = rpc_with(utxo(1, 50_000))
        out = build(rpc, req(fee_rate=7))
        self.assertEqual(rpc.fee_calls, 0)
        self.assertEqual(out["summary"]["fee_rate"], 7)

    def test_cap_spends_the_largest_and_values_only_those(self):
        rpc = rpc_with(utxo(1, 10_000), utxo(2, 90_000), utxo(3, 50_000))
        s = build(rpc, req(), max_inputs=2)["summary"]
        self.assertEqual((s["inputs"], s["input_sats"]), (2, 140_000))
        self.assertEqual((s["utxo_count"], s["capped"]), (3, True))
        self.assertEqual(s["seeds"], 14_000)        # not the 10k coin's

    def test_refusals(self):
        coin = utxo(1, 50_000)
        cases = [
            (rpc_with(coin, activation=None), req(), "not set"),
            (rpc_with(coin, height=979_999), req(), "open at block 980,000"),
            (rpc_with(coin), req(orchids_to=FUNDING), "different address"),
            (rpc_with(coin), req(address=LEGACY), "legacy"),
            (rpc_with(coin), req(address=FUNDING_SCRIPT.hex()), "script hex"),
            (rpc_with(coin), req(orchids_to=""), "expected a bitcoin address"),
            (rpc_with(coin, chain="signet"), req(), "wrong network"),
            (rpc_with(coin), req(fee_rate=0.5), "fee_rate"),
            (rpc_with(coin), req(fee_rate="4"), "fee_rate"),
            (rpc_with(), req(), "no UTXOs"),
            (rpc_with(coin), ["not", "an", "object"], "JSON object"),
        ]
        for rpc, body, msg in cases:
            with self.subTest(msg=msg):
                with self.assertRaisesRegex(Refused, msg):
                    build(rpc, body)

    def test_backend_away(self):
        with self.assertRaises(Unavailable):
            build(rpc_with(utxo(1, 50_000), unreachable=True), req())


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rpc = rpc_with(utxo(1, 50_000))
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                      make_handler(rpc, 50, 3))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def call(self, path="/v1/land", body=None, method="POST"):
        data = None if body is None else (
            body if isinstance(body, bytes) else json.dumps(body).encode())
        r = urllib.request.Request(self.url + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as res:
                return res.status, json.loads(res.read()), res.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def test_post_builds(self):
        code, out, headers = self.call(body=req())
        self.assertEqual(code, 200)
        self.assertIn("psbt", out)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_errors(self):
        self.assertEqual(self.call(body=req(orchids_to=FUNDING))[0], 400)
        self.assertEqual(self.call(body=b"{nope")[0], 400)
        self.assertEqual(self.call(path="/v1/info", body=req())[0], 404)
        self.assertEqual(self.call(method="GET")[0], 405)
        self.assertEqual(self.call(body=b"x" * (MAX_BODY + 1))[0], 413)


if __name__ == "__main__":
    unittest.main()
