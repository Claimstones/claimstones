"""Tests for the tip daemon's follow loop (indexer/cs_indexer.py).

A FakeNode serves a scripted chain through the same two surfaces the
real node does (the RPC calls and the block iterator), so every cycle
the follower runs here is the cycle it runs in production: reorg check
against recorded hashes, then extend. The reorg test is differential —
after unwinding and reconnecting, the store must equal a fresh sync of
the new chain."""

from __future__ import annotations

import os
import tempfile
import unittest

import pytest

pytest.importorskip("lmdb")

from claimstones.bitcoin.txparse import Block
from claimstones.indexer.birth import BirthIndexer
from claimstones.indexer.cs_indexer import ChainFollower, hash_key
from claimstones.indexer.store_lmdb import LmdbBirthStore
from tests.test_birth import P2WPKH, S, coinbase, tid, tx
from tests.test_store_lmdb import lmdb_utxo_rows

OTHER = bytes([0x00, 0x14]) + b"\x44" * 20


def block(height: int, txs, salt: int = 5000) -> Block:
    return Block(height, tid(salt + height), 0, txs)


def chain_a() -> list[Block]:
    return [
        block(0, [coinbase(1, 0)]),
        block(1, [coinbase(2, 1)]),
        block(2, [coinbase(3, 2),
                  tx(4, inputs=[(tid(1), 0)],
                     outputs=[(S // 2, None), (S // 2, OTHER)])]),
        block(3, [coinbase(5, 3)]),
        block(4, [coinbase(6, 4),
                  tx(7, inputs=[(tid(4), 0)], outputs=[(S // 2, OTHER)])]),
    ]


def branch_b() -> list[Block]:
    """Replaces heights 3..4 of chain A and goes one block further."""
    return [
        block(3, [coinbase(8, 3),
                  tx(9, inputs=[(tid(4), 1)], outputs=[(S // 2, None)])],
              salt=7000),
        block(4, [coinbase(10, 4)], salt=7000),
        block(5, [coinbase(11, 5)], salt=7000),
    ]


class FakeNode:
    """A scripted chain behind the follower's two injection points."""

    def __init__(self, blocks: list[Block]):
        self.chain = list(blocks)

    def call(self, method: str, *a):
        if method == "getblockcount":
            return len(self.chain) - 1
        if method == "getbestblockhash":
            return self.chain[-1].hash_hex
        if method == "getblockhash":
            return self.chain[a[0]].hash_hex
        raise AssertionError(f"unexpected rpc {method}")

    def iter(self, start: int, end: int):
        for h in range(start, end + 1):
            yield self.chain[h]


class FollowerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._stores = []

    def tearDown(self):
        for s in self._stores:
            s.env.close()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def follower(self, node: FakeNode, name: str = "db",
                 undo_depth: int = 1000, **kw) -> ChainFollower:
        store = LmdbBirthStore(os.path.join(self._tmp.name, name),
                               commit_every=1, map_mb=64)
        self._stores.append(store)
        ix = BirthIndexer(store, undo_depth=undo_depth)
        return ChainFollower(store, ix, node, node.call,
                             undo_depth=undo_depth,
                             report=lambda *a, **k: None, **kw)


class FollowerTests(FollowerCase):
    def test_catch_up_then_idle(self):
        node = FakeNode(chain_a())
        f = self.follower(node)
        self.assertEqual(f.sync_once(), (0, 5))
        self.assertEqual(f.store.last_height(), 4)
        self.assertEqual(f.store.get(tid(7), 0), [(0, S // 2)])
        self.assertIsNone(f.store.get(tid(4), 0))
        self.assertEqual(f.sync_once(), (0, 0))        # nothing new

    def test_recorded_hashes_match_the_chain(self):
        node = FakeNode(chain_a())
        f = self.follower(node)
        f.sync_once()
        for b in node.chain:
            self.assertEqual(f.store.get_meta(hash_key(b.height)), b.hash_hex)

    def test_hash_records_pruned_to_undo_depth(self):
        node = FakeNode(chain_a())
        f = self.follower(node, undo_depth=3)
        f.sync_once()
        self.assertIsNone(f.store.get_meta(hash_key(0)))
        self.assertIsNone(f.store.get_meta(hash_key(1)))
        for h in (2, 3, 4):
            self.assertEqual(f.store.get_meta(hash_key(h)),
                             node.chain[h].hash_hex)

    def test_reorg_equals_fresh_sync(self):
        node = FakeNode(chain_a())
        f = self.follower(node)
        f.sync_once()
        node.chain = chain_a()[:3] + branch_b()        # the network reorgs
        self.assertEqual(f.sync_once(), (2, 3))
        self.assertEqual(f.store.last_height(), 5)

        fresh = self.follower(FakeNode(chain_a()[:3] + branch_b()), "db2")
        fresh.sync_once()
        self.assertEqual(lmdb_utxo_rows(f.store), lmdb_utxo_rows(fresh.store))
        drop = lambda s: {k: v for k, v in s.items() if k != "txs"}
        self.assertEqual(drop(f.store.stats), drop(fresh.store.stats))
        for script in (P2WPKH, OTHER):
            self.assertEqual(f.store.by_script(script),
                             fresh.store.by_script(script))
        for h in (3, 4, 5):
            self.assertEqual(f.store.get_meta(hash_key(h)),
                             node.chain[h].hash_hex)

    def test_node_behind_us_is_left_alone(self):
        node = FakeNode(chain_a())
        f = self.follower(node)
        f.sync_once()
        node.chain = chain_a()[:3]      # the node restarted into catch-up
        before = lmdb_utxo_rows(f.store)
        self.assertEqual(f.sync_once(), (0, 0))
        self.assertEqual(f.store.last_height(), 4)
        self.assertEqual(lmdb_utxo_rows(f.store), before)

    def test_handover_db_without_hashes_extends_cleanly(self):
        # the production db has no hash records at all; the first cycle
        # must extend without attempting a single disconnect
        node = FakeNode(chain_a())
        f = self.follower(node)
        f.sync_once()
        for h in range(5):
            f.store.del_meta(hash_key(h))
        node.chain = chain_a() + [block(5, [coinbase(12, 5)])]
        self.assertEqual(f.sync_once(), (0, 1))
        self.assertEqual(f.store.last_height(), 5)

    def test_reorg_deeper_than_undo_depth_refuses(self):
        node = FakeNode(chain_a())
        f = self.follower(node, undo_depth=1)
        f.sync_once()
        # hashes exist only for h=3,4 (pruned to depth 1 would leave 4;
        # depth 1 prunes h-1 at each connect, so only h=4 remains) —
        # rebuild deeper records by hand to trip the guard
        for h in (3, 4):
            f.store.set_meta(hash_key(h), node.chain[h].hash_hex)
        node.chain = chain_a()[:3] + branch_b()
        with self.assertRaises(RuntimeError):
            f.sync_once()
        f.store.rollback()

    def test_stop_file_commits_and_exits(self):
        node = FakeNode(chain_a())
        stop = os.path.join(self._tmp.name, "stop")
        f = self.follower(node, stop_path=stop)
        with open(stop, "w") as fh:
            fh.write("test stop")
        with self.assertRaises(SystemExit):
            f.sync_once()
        self.assertEqual(f.store.last_height(), 0)     # one block, committed
        self.assertFalse(f.store.in_tx)
        os.remove(stop)
        self.assertEqual(f.sync_once(), (0, 4))        # resumes cleanly
        self.assertEqual(f.store.last_height(), 4)
