"""The regtest end-to-end: the whole stack against a real node.

A throwaway regtest bitcoind, a fresh LMDB birth index built from
genesis by the real ChainFollower, the real indexer RPC server in a
thread, and the wallet driven through its own CLI functions:

  receive -> check -> claim (the minted amount equals land_amount
  recomputed from the node's headers) -> a reorg across the land (the
  balance is undone and recomputed) -> external claim-and-collect signed
  by Bitcoin Core itself (the seeds land on the carrier) -> withdraw
  (orchids stay) -> Send (the recipient's carrier holds the seeds).

Test chains are active from height 1 (codec.activation_height, decided
2026-11-11), which is what lets the land count here at all.

Needs a bitcoind: found on PATH, or named by CLAIMSTONES_BITCOIND.
Skipped otherwise, so the ordinary suite stays node-free.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

BITCOIND = os.environ.get("CLAIMSTONES_BITCOIND") or shutil.which("bitcoind")

from claimstones.indexer.birth import BirthIndexer, RpcBlocks
from claimstones.indexer.cs_indexer import ChainFollower
from claimstones.indexer.ledger import land_amount
from claimstones.indexer.rpc import IndexQueries, make_server
from claimstones.indexer.store_lmdb import LmdbBirthStore
from claimstones.lineage.walker import Rpc, resolve_rpc_auth, work_from_nbits
from claimstones.wallet.check import run_check
from claimstones.wallet.cli import (
    do_balance, do_claim, do_collect, do_send, do_withdraw,
)
from claimstones.wallet.keys import WalletKey
from claimstones.wallet.rpc import IndexerRpc

YES = lambda _prompt: "y"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(BITCOIND, "no bitcoind (PATH or CLAIMSTONES_BITCOIND)")
class RegtestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cs-e2e-")
        rpcport = _free_port()
        cls.node_proc = subprocess.Popen(
            [BITCOIND, "-regtest", f"-datadir={cls.tmp}",
             f"-rpcport={rpcport}", "-listen=0", "-server=1",
             "-fallbackfee=0.0001"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cookie = os.path.join(cls.tmp, "regtest", ".cookie")
        url = f"http://127.0.0.1:{rpcport}"
        deadline = time.time() + 60
        while True:
            try:
                if os.path.exists(cookie):
                    cls.node = Rpc(url, resolve_rpc_auth(False, None, None,
                                                         cookie),
                                   wallet="miner", cookie_path=cookie)
                    cls.node("getblockcount")
                    break
            except Exception:
                pass
            if time.time() > deadline:
                raise RuntimeError("regtest bitcoind did not come up")
            time.sleep(0.2)
        cls.node("createwallet", "miner")
        cls.miner = cls.node("getnewaddress", wallet=True)
        # 101 blocks: one spendable coinbase. The chain stays far below
        # regtest's 150-block halving, where subsidy_sats (the mainnet
        # schedule) and the node would part ways.
        cls.node("generatetoaddress", 101, cls.miner)

        cls.store = LmdbBirthStore(os.path.join(cls.tmp, "birth.lmdb"),
                                   commit_every=1, map_mb=256)
        # cs_indexer.main records the chain it follows; this test builds
        # the follower directly, so it records it the same way.
        cls.store.begin()
        cls.store.set_meta("chain", "regtest")
        cls.store.commit()
        indexer = BirthIndexer(cls.store, undo_depth=100, chain="regtest")
        cls.follower = ChainFollower(cls.store, indexer, RpcBlocks(cls.node),
                                     cls.node, undo_depth=100,
                                     report=lambda *a, **k: None)
        cls.follower.sync_once()

        queries = IndexQueries(cls.store, node=cls.node)
        cls.httpd = make_server(queries, "127.0.0.1", 0, None)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.wrpc = IndexerRpc(f"http://127.0.0.1:"
                              f"{cls.httpd.server_address[1]}")
        cls.key = WalletKey.new()
        cls.address = cls.key.address("regtest")

    @classmethod
    def tearDownClass(cls):
        with contextlib.suppress(Exception):
            cls.httpd.shutdown()
        with contextlib.suppress(Exception):
            cls.node("stop")
            cls.node_proc.wait(timeout=30)
        with contextlib.suppress(Exception):
            cls.node_proc.kill()
        with contextlib.suppress(Exception):
            cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _mine(cls, n: int = 1) -> tuple[int, int]:
        cls.node("generatetoaddress", n, cls.miner)
        return cls.follower.sync_once()

    def _coins(self):
        return self.wrpc.getaddressutxos(self.key.script.hex())

    def _seeds(self, target=None) -> dict:
        return self.wrpc.getorchidbalance(target or self.key.script.hex())

    def _expected_amount(self, runs, height: int) -> int:
        """land.md §Valuation sequence from the node's own headers:
        W(c) = the block's chainwork, w(h) from each birth block's nBits."""
        def header(h):
            return self.node("getblockheader", self.node("getblockhash", h))
        stream = [(h, n) for h, n in runs if h != "plain"]
        chainwork = int(header(height)["chainwork"], 16)
        return land_amount(stream, chainwork,
                           lambda h: work_from_nbits(int(header(h)["bits"], 16)))

    def test_the_whole_flow(self):
        with self.subTest("receive and check"):
            self.node("sendtoaddress", self.address, "1.0", wallet=True)
            self._mine()
            report = run_check([self.address], self.wrpc, "regtest")
            self.assertEqual(len(report.utxos), 1)
            self.assertEqual(report.total_sats, 100_000_000)
            self.assertEqual(report.total_latent_sats, 100_000_000)
            self.assertGreater(report.total_seeds, 0)
            self.assertFalse(report.no_node)

        with self.subTest("balance is the wallet's own check"):
            out = io.StringIO()
            rc = do_balance(self.wrpc, self.key, "regtest", out)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("100,000,000 sats", out.getvalue())
            self.assertRegex(out.getvalue(), r"total orchids +0\.00\n")   # served, none yet

        with self.subTest("the server knows its chain"):
            info = self.wrpc.getindexinfo()
            self.assertEqual(info["chain"], "regtest")
            self.assertEqual(info["activation_height"], 1)

        with self.subTest("claim lands the coin"):
            (before,) = self._coins()               # the runs the land values
            self.assertEqual(self._seeds()["seeds"], 0)
            out = io.StringIO()
            rc = do_claim(self.wrpc, self.key, 1.0, out, ask=YES)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("claim sent: ", out.getvalue())
            self._mine()
            coins = self._coins()
            self.assertEqual(len(coins), 1)
            self.assertEqual(coins[0]["latent_sats"], 0)
            self.assertEqual(coins[0]["runs"],
                             [["plain", coins[0]["sats"]]])
            # vector 4 against a real node: the index's amount is the
            # valuation sequence over the input runs at the block's W
            expected = self._expected_amount(before["runs"],
                                             self.node("getblockcount"))
            self.assertGreater(expected, 0)
            bal = self._seeds()
            self.assertEqual(bal["seeds"], expected)
            self.assertEqual(bal["utxos"], [{"txid": coins[0]["txid"],
                                             "vout": coins[0]["vout"],
                                             "seeds": expected}])
            info = self.wrpc.getindexinfo()
            self.assertEqual((info["lands"], info["minted"], info["burned"]),
                             (1, expected, 0))
            self.assertEqual(info["ledger_height"], 0)
            out = io.StringIO()
            self.assertEqual(do_balance(self.wrpc, self.key, "regtest", out), 0)
            self.assertIn("total orchids", out.getvalue())
            self.assertNotIn("not served", out.getvalue())
            claimed = expected

        with self.subTest("a reorg across the land"):
            tip = self.node("getblockcount")
            self.node("invalidateblock", self.node("getblockhash", tip))
            # The claim returns to the mempool and is mined again into
            # the replacement chain; the follower must undo and reapply.
            # A fresh coinbase address, or the instant re-mine rebuilds
            # the byte-identical block the node just marked invalid.
            other = self.node("getnewaddress", wallet=True)
            self.node("generatetoaddress", 2, other)
            undone, connected = self.follower.sync_once()
            self.assertGreaterEqual(undone, 1)
            self.assertEqual(connected, 2)
            coins = self._coins()
            self.assertEqual(len(coins), 1)
            self.assertEqual(coins[0]["latent_sats"], 0)
            # vector 14: the balance was undone with its block and
            # recomputed at the replacement block's W (the same height
            # here, so the same amount; the undo path is what is tested)
            self.assertEqual(self._seeds()["seeds"],
                             self._expected_amount(before["runs"], tip))
            self.assertEqual(self.wrpc.getindexinfo()["lands"], 1)

        with self.subTest("external claim-and-collect, signed and"
                          " broadcast by Core"):
            funding = self.node("getnewaddress", "", "bech32", wallet=True)
            self.node("sendtoaddress", funding, "0.5", wallet=True)
            self._mine()
            folder = os.path.join(self.tmp, "collect")
            os.makedirs(folder, exist_ok=True)
            out = io.StringIO()
            rc = do_collect(self.wrpc, self.key, "regtest", funding, 2.0,
                            out_dir=folder, out=out)
            self.assertEqual(rc, 0, out.getvalue())
            (psbt_file,) = os.listdir(folder)
            with open(os.path.join(folder, psbt_file), encoding="ascii") as f:
                unsigned = f.read().strip()

            # The signing wallet broadcasts (decided 2026-11-13); Core
            # plays it here, as Sparrow did on signet.
            signed = self.node("walletprocesspsbt", unsigned, wallet=True)
            self.assertTrue(signed["complete"])
            final = self.node("finalizepsbt", signed["psbt"])
            self.assertTrue(final["complete"])
            self.node("sendrawtransaction", final["hex"])
            self._mine()

            coins = self._coins()
            self.assertEqual(len(coins), 2)     # the change + the carrier
            self.assertIn(330, [c["sats"] for c in coins])
            self.assertTrue(all(c["latent_sats"] == 0 for c in coins))
            # The returned coin sits on the funding address, landed.
            returned = self.wrpc.getaddressutxos(funding)
            self.assertEqual(len(returned), 1)
            self.assertEqual(returned[0]["latent_sats"], 0)
            # The collect's seeds land on the 330-sat carrier (the first
            # non-OP_RETURN output), not on the return to the funder.
            (carrier,) = [c for c in coins if c["sats"] == 330]
            collected = self._seeds({"txid": carrier["txid"],
                                     "vout": carrier["vout"]})["seeds"]
            self.assertGreater(collected, 0)
            self.assertEqual(self._seeds(funding)["seeds"], 0)
            self.assertEqual(self._seeds()["seeds"], claimed + collected)
            self.assertEqual(self.wrpc.getindexinfo()["lands"], 2)

        with self.subTest("withdraw moves only bitcoin"):
            dest = self.node("getnewaddress", "", "bech32", wallet=True)
            out = io.StringIO()
            rc = do_withdraw(self.wrpc, self.key, "regtest", dest, 30_000,
                             1.0, out, ask=YES)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("only bitcoin leaves", out.getvalue())
            self.assertNotIn("CAUTION", out.getvalue())   # all claimed
            self._mine()
            got = self.wrpc.getaddressutxos(dest)
            self.assertEqual([c["sats"] for c in got], [30_000])
            self.assertEqual(got[0]["latent_sats"], 0)    # plain travelled
            # only bitcoin left: the orchids followed the pointer home
            self.assertEqual(self._seeds(dest)["seeds"], 0)
            self.assertEqual(self._seeds()["seeds"], claimed + collected)

        with self.subTest("send pays orchids to another address"):
            recipient = self.node("getnewaddress", "", "bech32", wallet=True)
            out = io.StringIO()
            rc = do_send(self.wrpc, self.key, "regtest", recipient,
                         "1.00", 1.0, out, ask=YES)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("sent: ", out.getvalue())
            self._mine()
            theirs = self._seeds(recipient)
            self.assertEqual(theirs["seeds"], 100)          # 1.00 orchid
            self.assertEqual(len(theirs["utxos"]), 1)
            (carrier,) = self.wrpc.getaddressutxos(recipient)
            self.assertEqual(carrier["sats"], 330)
            self.assertEqual(carrier["latent_sats"], 0)
            self.assertEqual(self._seeds()["seeds"], claimed + collected - 100)
            info = self.wrpc.getindexinfo()
            self.assertEqual(info["burned"], 0)
            self.assertEqual(info["minted"], claimed + collected)


if __name__ == "__main__":
    unittest.main()
