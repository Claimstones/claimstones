"""Check, watch-only (wallet/check.py, wallet/cli.py): the gap-limit
walk against a fake indexer RPC, valuation and the no-node fallback,
dedup of a coin pasted twice over, and the report wording."""

from __future__ import annotations

import io
import unittest

from claimstones.bitcoin.descriptors import GAP_LIMIT, parse_line
from claimstones.wallet.check import orchids, run_check
from claimstones.wallet.cli import do_check, print_report
from claimstones.wallet.rpc import NO_NODE, WalletRpcError

BIP84_ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1AD"
              "qtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs")
BIP84_FIRST = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"


def utxo(salt: int, sats: int, plain: int = 0) -> dict:
    runs = [[100, sats - plain]] + ([["plain", plain]] if plain else [])
    return {"txid": f"{salt:02x}" * 32, "vout": salt, "runs": runs,
            "sats": sats, "latent_sats": sats - plain}


class FakeRpc:
    """Coins keyed by script hex; seeds valued at one per ten latent sats."""

    def __init__(self, coins: dict[str, list[dict]], node: bool = True):
        self.coins = coins
        self.node = node
        self.latent_calls = 0

    def getaddressutxos(self, script_hex: str) -> list[dict]:
        return [dict(u) for u in self.coins.get(script_hex, [])]

    def getlatent(self, script_hex: str) -> dict:
        if not self.node:
            raise WalletRpcError(NO_NODE, "getlatent needs the host node")
        self.latent_calls += 1
        utxos = [dict(u, seeds=u["latent_sats"] // 10)
                 for u in self.coins.get(script_hex, [])]
        return {"valued_at_height": 972_618, "chainwork": "9c",
                "seeds": sum(u["seeds"] for u in utxos), "utxos": utxos}


def zpub_scripts():
    receive, change = parse_line(BIP84_ZPUB)
    return receive, change


class RunCheckTests(unittest.TestCase):
    def test_single_address(self):
        receive, _ = zpub_scripts()
        script = receive.derive(0).script.hex()
        rpc = FakeRpc({script: [utxo(1, 50_000)]})
        report = run_check([BIP84_FIRST], rpc)
        self.assertEqual(len(report.utxos), 1)
        self.assertEqual(report.branches, 1)
        self.assertEqual(report.scripts, 1)
        self.assertEqual(report.total_sats, 50_000)
        self.assertEqual(report.total_seeds, 5_000)
        self.assertEqual(report.valued_at_height, 972_618)
        self.assertFalse(report.no_node)
        self.assertEqual(report.utxos[0].address, BIP84_FIRST)

    def test_zpub_walks_both_branches(self):
        receive, change = zpub_scripts()
        rpc = FakeRpc({
            receive.derive(0).script.hex(): [utxo(1, 50_000)],
            receive.derive(15).script.hex(): [utxo(2, 30_000, plain=10_000)],
            change.derive(0).script.hex(): [utxo(3, 7_000)],
        })
        report = run_check([BIP84_ZPUB], rpc)
        self.assertEqual(len(report.utxos), 3)
        self.assertEqual(report.branches, 2)
        # receive: hit at 15 extends the walk to 36; change: one window
        # past the hit at 0.
        self.assertEqual(report.scripts, (15 + GAP_LIMIT + 1) + (GAP_LIMIT + 1))
        self.assertEqual(report.total_sats, 87_000)
        self.assertEqual(report.total_latent_sats, 77_000)
        self.assertEqual(report.total_seeds, 7_700)
        self.assertEqual(rpc.latent_calls, 3)

    def test_coin_pasted_twice_counts_once(self):
        receive, _ = zpub_scripts()
        rpc = FakeRpc({receive.derive(0).script.hex(): [utxo(1, 50_000)]})
        report = run_check([BIP84_FIRST, BIP84_ZPUB], rpc)
        self.assertEqual(len(report.utxos), 3 - 2)
        self.assertEqual(report.total_sats, 50_000)
        self.assertEqual(report.branches, 3)   # the address + two branches

    def test_without_a_node_the_coins_still_show(self):
        receive, _ = zpub_scripts()
        rpc = FakeRpc({receive.derive(0).script.hex(): [utxo(1, 50_000)]},
                      node=False)
        report = run_check([BIP84_FIRST], rpc)
        self.assertTrue(report.no_node)
        self.assertIsNone(report.total_seeds)
        self.assertIsNone(report.utxos[0].seeds)
        self.assertEqual(report.total_sats, 50_000)

    def test_orchids_formatting(self):
        self.assertEqual(orchids(929), "9.29")
        self.assertEqual(orchids(100), "1.00")
        self.assertEqual(orchids(5), "0.05")
        self.assertEqual(orchids(0), "0.00")
        self.assertEqual(orchids(123_456_789), "1,234,567.89")


class ReportTests(unittest.TestCase):
    def test_wording(self):
        receive, _ = zpub_scripts()
        rpc = FakeRpc({receive.derive(0).script.hex(): [utxo(1, 50_000)]})
        out = io.StringIO()
        print_report(run_check([BIP84_FIRST], rpc), out)
        text = out.getvalue()
        self.assertIn("est. orchids to be landed 50.00", text)   # 5000 seeds
        self.assertIn("shrink about 3 % a month", text)
        # Orchids are amounts, never a "worth" (wallet-policy §Wording).
        self.assertNotIn("worth", text)
        self.assertNotIn("production cost", text)

    def test_no_node_wording(self):
        receive, _ = zpub_scripts()
        rpc = FakeRpc({receive.derive(0).script.hex(): [utxo(1, 50_000)]},
                      node=False)
        out = io.StringIO()
        print_report(run_check([BIP84_FIRST], rpc), out)
        text = out.getvalue()
        self.assertIn("no host node", text)
        self.assertNotIn("est. orchids to be landed 5", text)

    def test_do_check_refuses_bad_input(self):
        rc = do_check(FakeRpc({}), "main", ["xprv9s21ZrQH143K3QTDL4LXw2F7HEK3"
                                            "wJUD2nW2nRk4stbPy6cq3jPPqjiChkVv"
                                            "vNKmPGJxWUtg6LnF5kejMRNNU3TGtRBe"
                                            "Jgk33yuGBxrMPHi"], io.StringIO())
        self.assertEqual(rc, 1)

    def test_do_check_empty_result(self):
        out = io.StringIO()
        rc = do_check(FakeRpc({}), "main", [BIP84_FIRST], out)
        self.assertEqual(rc, 0)
        self.assertIn("0 UTXOs found", out.getvalue())


if __name__ == "__main__":
    unittest.main()
