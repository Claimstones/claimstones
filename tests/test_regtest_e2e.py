"""The regtest end-to-end: the whole stack against a real node.

A throwaway regtest bitcoind, a fresh LMDB birth index built from
genesis by the real ChainFollower, the real indexer RPC server in a
thread, and the wallet driven through its own CLI functions:

  receive -> check -> claim -> a reorg across the land -> external
  claim-and-collect signed by Bitcoin Core itself -> withdraw ->
  Send's documented refusal (the ledger half is not built yet).

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
from claimstones.indexer.rpc import IndexQueries, make_server
from claimstones.indexer.store_lmdb import LmdbBirthStore
from claimstones.lineage.walker import Rpc, resolve_rpc_auth
from claimstones.wallet.check import run_check
from claimstones.wallet.cli import (
    do_claim, do_collect, do_relay, do_send, do_withdraw,
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

        with self.subTest("the server knows its chain"):
            info = self.wrpc.getindexinfo()
            self.assertEqual(info["chain"], "regtest")
            self.assertEqual(info["activation_height"], 1)

        with self.subTest("claim lands the coin"):
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

        with self.subTest("external claim-and-collect, signed by Core"):
            funding = self.node("getnewaddress", "", "bech32", wallet=True)
            self.node("sendtoaddress", funding, "0.5", wallet=True)
            self._mine()
            folder = os.path.join(self.tmp, "collect")
            os.makedirs(folder, exist_ok=True)
            out = io.StringIO()
            rc = do_collect(self.wrpc, self.key, "regtest", funding, 2.0,
                            out_dir=folder, out=out)
            self.assertEqual(rc, 0, out.getvalue())
            record, psbt_file = sorted(os.listdir(folder))
            with open(os.path.join(folder, psbt_file), encoding="ascii") as f:
                unsigned = f.read().strip()

            signed = self.node("walletprocesspsbt", unsigned, wallet=True)
            self.assertTrue(signed["complete"])
            signed_path = os.path.join(folder, "signed.psbt")
            with open(signed_path, "w", encoding="ascii") as f:
                f.write(signed["psbt"])
            out = io.StringIO()
            rc = do_relay(self.wrpc, os.path.join(folder, record),
                          signed_path, out=out)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("collect sent: ", out.getvalue())
            self._mine()

            coins = self._coins()
            self.assertEqual(len(coins), 2)     # the change + the carrier
            self.assertIn(330, [c["sats"] for c in coins])
            self.assertTrue(all(c["latent_sats"] == 0 for c in coins))
            # The returned coin sits on the funding address, landed.
            returned = self.wrpc.getaddressutxos(funding)
            self.assertEqual(len(returned), 1)
            self.assertEqual(returned[0]["latent_sats"], 0)

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

        with self.subTest("send refuses until the ledger half exists"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = do_send(self.wrpc, self.key, "regtest", self.miner,
                             "1.00", 1.0, io.StringIO(), ask=YES)
            self.assertEqual(rc, 1)
            self.assertIn("cannot serve settled orchid balances",
                          err.getvalue())


if __name__ == "__main__":
    unittest.main()
