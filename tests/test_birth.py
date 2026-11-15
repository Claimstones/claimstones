"""Tests for the birth index (indexer/birth.py): run arithmetic, the
indexer on the LMDB store (the only store it runs on since 2026-11-13),
and the RPC waiting policy. The SQLite BirthStore's own tests
(migration, schema) stay; it is the retired prototype's store."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

import claimstones.indexer.birth as birth

from claimstones.bitcoin.txparse import Block, Tx, TxIn, TxOut
from claimstones.indexer.store_lmdb import LmdbBirthStore
from claimstones.indexer.birth import (
    PLAIN,
    RUN_SIZE,
    BirthIndexer,
    BirthStore,
    MissingInput,
    RpcBlocks,
    cut_runs,
    decode_runs,
    encode_runs,
    extend_merged,
    merge_runs,
    transient_rpc,
    wait_for_rpc,
)
from claimstones.lineage.walker import RpcError, RpcMethodMissing, subsidy_sats
from claimstones.protocol.codec import land_script

P2WPKH = bytes([0x00, 0x14]) + b"\x11" * 20
S = subsidy_sats(0)   # 50 BTC in sats for every height used here


def tid(n: int) -> bytes:
    return n.to_bytes(4, "little") + b"\x00" * 28


def tx(n: int, inputs=(), outputs=(), coinbase=False) -> Tx:
    ins = [TxIn(prev, vout, b"", 0xFFFFFFFF) for prev, vout in inputs]
    if coinbase:
        ins = [TxIn(b"\x00" * 32, 0xFFFFFFFF, b"", 0xFFFFFFFF)]
    outs = [TxOut(v, s if s is not None else P2WPKH) for v, s in outputs]
    return Tx(tid(n), ins, outs, coinbase, 400)


def coinbase(n: int, height: int, claim: int | None = None, extra=()) -> Tx:
    value = S if claim is None else claim
    return tx(n, outputs=[(value, None), *extra], coinbase=True)


class RunTests(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(merge_runs([(5, 3), (5, 4), (0, 0), (7, 1), (7, 2), (5, 1)]), [(5, 7), (7, 3), (5, 1)])

    def test_extend_merged(self):
        s = [(1, 5)]
        extend_merged(s, [(1, 2), (3, 1)])
        extend_merged(s, [])
        extend_merged(s, [(3, 4)])
        self.assertEqual(s, [(1, 7), (3, 5)])

    def test_cut_exact(self):
        pieces, left = cut_runs([(1, 10), (2, 10)], [5, 10, 5])
        self.assertEqual(pieces, [[(1, 5)], [(1, 5), (2, 5)], [(2, 5)]])
        self.assertEqual(left, [])

    def test_cut_leftover_and_zero_size(self):
        pieces, left = cut_runs([(1, 10), (2, 10)], [0, 12])
        self.assertEqual(pieces, [[], [(1, 10), (2, 2)]])
        self.assertEqual(left, [(2, 8)])

    def test_cut_short_stream(self):
        pieces, left = cut_runs([(1, 4)], [3, 3])
        self.assertEqual(pieces, [[(1, 3)], [(1, 1)]])
        self.assertEqual(left, [])

    def test_encode_round_trip(self):
        runs = [(0, 1), (PLAIN, 5_000_000_000), (840_000, 123), (2 ** 31, 21_000_000 * 10 ** 8)]
        self.assertEqual(decode_runs(encode_runs(runs)), runs)
        self.assertEqual(len(encode_runs(runs)), 4 * RUN_SIZE)
        self.assertEqual(encode_runs([]), b"")
        self.assertEqual(decode_runs(b""), [])
        self.assertEqual(encode_runs([(PLAIN, 1)])[:4], b"\xff\xff\xff\xff")

    def test_migration_from_schema_2(self):
        def old_varint(v):
            out = bytearray()
            while v >= 0x80:
                out.append((v & 0x7F) | 0x80)
                v >>= 7
            out.append(v)
            return bytes(out)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.sqlite")
            s = BirthStore(path)
            old = old_varint(0) + old_varint(7) + old_varint(841) + old_varint(300)   # PLAIN 7, h=840 300
            s.db.execute("INSERT INTO utxo VALUES (?, ?, ?, ?)", (tid(1), 0, P2WPKH, old))
            s.set_meta("schema", "2")
            s.close()
            s = BirthStore(path)
            self.assertEqual(s.get_meta("schema"), "3")
            self.assertEqual(s.get(tid(1), 0), [(PLAIN, 7), (840, 300)])
            s.close()


class IndexerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._extra = []
        self.store = self._open("db", commit_every=1)
        self.ix = BirthIndexer(self.store)

    def tearDown(self):
        for s in self._extra:
            s.env.close()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def _open(self, name: str, **kw) -> LmdbBirthStore:
        s = LmdbBirthStore(os.path.join(self._tmp.name, name),
                           map_mb=64, **kw)
        self._extra.append(s)
        return s

    def block(self, height: int, txs: list[Tx]) -> None:
        self.ix.connect_block(Block(height, tid(200 + height), 0, txs))

    def runs(self, n: int, vout: int):
        return self.store.get(tid(n), vout)


class IndexerTests(IndexerCase):
    def test_coinbase_subsidy_born_at_height(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1)])
        self.assertEqual(self.runs(1, 0), [(0, S)])
        self.assertEqual(self.runs(2, 0), [(1, S)])
        self.assertEqual(self.store.stats["utxos"], 2)
        self.assertEqual(self.store.stats["runs"], 2)

    def test_unclaimed_subsidy_is_never_born(self):
        self.block(0, [coinbase(1, 0, claim=S - 1000)])
        self.assertEqual(self.runs(1, 0), [(0, S - 1000)])

    def test_fifo_cut_and_fee_tail_to_coinbase(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1)])
        # spend both coinbases: outputs 60 BTC, 30 BTC, fee 10 BTC
        spend = tx(3, inputs=[(tid(1), 0), (tid(2), 0)], outputs=[(S + S // 5, None), (3 * S // 5, None)])
        self.block(2, [coinbase(4, 2, claim=S + S // 5), spend])
        self.assertEqual(self.runs(3, 0), [(0, S), (1, S // 5)])
        self.assertEqual(self.runs(3, 1), [(1, 3 * S // 5)])
        # coinbase: subsidy at 2, then the fee tail (the last 10 BTC born at 1)
        self.assertEqual(self.runs(4, 0), [(2, S), (1, S // 5)])
        self.assertIsNone(self.runs(1, 0))
        self.assertIsNone(self.runs(2, 0))
        self.assertEqual(self.store.stats["utxos"], 3)

    def test_fee_tails_in_block_order_and_merge(self):
        self.block(0, [coinbase(1, 0)])
        a = tx(2, inputs=[(tid(1), 0)], outputs=[(S // 2, None), (S // 4, None)])   # fee S/4 born 0
        b = tx(3, inputs=[(tid(2), 1)], outputs=[(S // 8, None)])                    # fee S/8 born 0
        self.block(1, [coinbase(4, 1, claim=S + S // 4 + S // 8), a, b])
        self.assertEqual(self.runs(4, 0), [(1, S), (0, S // 4 + S // 8)])   # tails merged

    def test_same_block_spend(self):
        self.block(0, [coinbase(1, 0)])
        a = tx(2, inputs=[(tid(1), 0)], outputs=[(S, None)])
        b = tx(3, inputs=[(tid(2), 0)], outputs=[(S, None)])
        self.block(1, [coinbase(4, 1), a, b])
        self.assertEqual(self.runs(3, 0), [(0, S)])
        self.assertIsNone(self.runs(2, 0))

    def test_zero_value_output_is_spendable(self):
        self.block(0, [coinbase(1, 0)])
        a = tx(2, inputs=[(tid(1), 0)], outputs=[(0, None), (S, None)])
        self.block(1, [coinbase(3, 1), a])
        self.assertEqual(self.runs(2, 0), [])
        b = tx(4, inputs=[(tid(2), 0), (tid(2), 1)], outputs=[(S, None)])
        self.block(2, [coinbase(5, 2), b])
        self.assertEqual(self.runs(4, 0), [(0, S)])
        self.assertIsNone(self.runs(2, 0))

    def test_repair_undoes_half_applied_block(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1)])
        # a block whose second tx fails after the first spent a coinbase
        bad = [coinbase(3, 2), tx(4, inputs=[(tid(1), 0)], outputs=[(S, None)]),
               tx(5, inputs=[(tid(99), 0)], outputs=[(1, None)])]
        with self.assertRaises(MissingInput):
            self.block(2, bad)
        self.store.commit()                       # simulate the old crash path
        self.assertIsNone(self.runs(1, 0))        # half-applied
        self.assertEqual(self.store.repair(), 1)
        self.assertEqual(self.runs(1, 0), [(0, S)])
        self.assertIsNone(self.runs(4, 0))
        self.assertEqual(self.store.last_height(), 1)

    def test_address_table_lookup_and_undo(self):
        other = bytes([0x00, 0x14]) + b"\x22" * 20
        self.block(0, [coinbase(1, 0)])
        a = tx(2, inputs=[(tid(1), 0)], outputs=[(S // 2, None), (S // 4, other), (S // 4, None)])
        self.block(1, [coinbase(3, 1), a])
        mine = self.store.by_script(P2WPKH)
        self.assertEqual([(t, v) for t, v, _ in mine], [(tid(2), 0), (tid(2), 2), (tid(3), 0)])
        self.assertEqual(self.store.by_script(other), [(tid(2), 1, [(0, S // 4)])])
        self.assertEqual(self.store.stats["script_bytes"], 4 * 22)
        self.ix.disconnect_block(1)
        self.assertEqual(self.store.by_script(other), [])
        self.assertEqual([(t, v) for t, v, _ in self.store.by_script(P2WPKH)], [(tid(1), 0)])
        self.assertEqual(self.store.stats["script_bytes"], 22)

    def test_bip30_duplicate_coinbase_overwrites_only_at_known_heights(self):
        # starting above genesis and skipping heights: give the indexer
        # the parents' W, as fill_work would
        for h in (91841, 91879, 91880):
            self.store.put_work(h, 2 * (h + 1))
        self.block(91842, [coinbase(1, 91842)])
        self.block(91880, [coinbase(1, 91880)])            # same txid again
        self.assertEqual(self.runs(1, 0), [(91880, S)])
        self.assertEqual(self.store.stats["utxos"], 1)
        with self.assertRaises(Exception):                 # elsewhere a duplicate is an error
            self.block(91881, [coinbase(1, 91881)])

    def test_sqlite_store_is_refused(self):
        # LMDB only since 2026-11-13: the prototype's store has no ledger
        with self.assertRaises(TypeError):
            BirthIndexer(BirthStore(":memory:"))

    def test_schema_mismatch_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.sqlite")
            s = BirthStore(path)
            s.set_meta("schema", "1")
            s.close()
            with self.assertRaises(RuntimeError):
                BirthStore(path)

    def test_missing_input_raises(self):
        with self.assertRaises(MissingInput):
            self.block(0, [coinbase(1, 0), tx(2, inputs=[(tid(99), 0)], outputs=[(1, None)])])

    def test_land_marks_outputs_plain(self):
        self.ix.activation_height = 0        # the constant ships undecided
        self.block(0, [coinbase(1, 0)])
        land = tx(2, inputs=[(tid(1), 0)], outputs=[(S - 1000, None), (0, land_script())])
        self.block(1, [coinbase(3, 1, claim=S + 1000), land])
        self.assertEqual(self.runs(2, 0), [(PLAIN, S - 1000)])
        self.assertEqual(self.runs(3, 0), [(1, S), (0, 1000)])   # fee sats were not landed by the output cut
        self.assertEqual(self.store.stats["lands"], 1)
        # plain sats stay plain through later spends and merge with other plain sats
        spend = tx(4, inputs=[(tid(2), 0)], outputs=[(S - 2000, None)])
        self.block(2, [coinbase(5, 2), spend])
        self.assertEqual(self.runs(4, 0), [(PLAIN, S - 2000)])

    def test_undecided_activation_lands_nothing(self):
        # ACTIVATION_HEIGHT is None until it is decided, and while it is the
        # index must be a pure function of the chain: no envelope counts at
        # any height, so no run is ever marked PLAIN. This is what the
        # land-free snapshot (docs/birth-index-check.md) depends on.
        self.assertIsNone(self.ix.activation_height)
        self.block(0, [coinbase(1, 0)])
        land = tx(2, inputs=[(tid(1), 0)], outputs=[(S, None), (0, land_script())])
        self.block(1, [coinbase(3, 1), land])
        self.assertEqual(self.runs(2, 0), [(0, S)])
        self.assertEqual(self.store.stats["lands"], 0)

    def test_land_before_activation_is_ignored(self):
        self.ix.activation_height = 5
        self.block(0, [coinbase(1, 0)])
        land = tx(2, inputs=[(tid(1), 0)], outputs=[(S, None), (0, land_script())])
        self.block(1, [coinbase(3, 1), land])
        self.assertEqual(self.runs(2, 0), [(0, S)])
        self.assertEqual(self.store.stats["lands"], 0)

    def test_disconnect_restores_state(self):
        self.block(0, [coinbase(1, 0)])
        self.block(1, [coinbase(2, 1)])
        before = dict(self.store.stats)
        spend = tx(3, inputs=[(tid(1), 0), (tid(2), 0)], outputs=[(S, None), (S // 2, None)])
        self.block(2, [coinbase(4, 2, claim=S + S // 2), spend])
        self.assertEqual(self.store.last_height(), 2)
        self.ix.disconnect_block(2)
        self.assertEqual(self.store.last_height(), 1)
        self.assertEqual(self.runs(1, 0), [(0, S)])
        self.assertEqual(self.runs(2, 0), [(1, S)])
        self.assertIsNone(self.runs(3, 0))
        self.assertIsNone(self.runs(4, 0))
        state = {k: v for k, v in self.store.stats.items() if k != "txs"}   # txs is a throughput counter
        self.assertEqual(state, {k: v for k, v in before.items() if k != "txs"})


class WaitBase(unittest.TestCase):
    """Fakes the clock and captures the log so waiting is observable."""

    def setUp(self):
        self.slept = []
        self.real_sleep = birth.time.sleep
        birth.time.sleep = self.slept.append
        self.log = io.StringIO()
        self._redirect = contextlib.redirect_stdout(self.log)
        self._redirect.__enter__()

    def tearDown(self):
        self._redirect.__exit__(None, None, None)
        birth.time.sleep = self.real_sleep


class WaitForRpcTests(WaitBase):
    """A node restart must not end a multi-day run: the blocks applied since
    the last commit live only in the running process."""

    def test_waits_out_an_unreachable_node_then_returns(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 4:
                raise RuntimeError("cannot reach RPC at http://127.0.0.1:8332: refused")
            return "block data"

        self.assertEqual(wait_for_rpc(fn, "fetching blocks 1..64"), "block data")
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.slept, [5.0, 10.0, 20.0])   # backoff, capped later at 60
        log = self.log.getvalue()
        self.assertIn("fetching blocks 1..64", log)       # the range is named
        self.assertIn("node back after 35s", log)         # and the recovery

    def test_outage_is_logged_once_then_every_five_minutes(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 30:
                raise RuntimeError("cannot reach RPC at http://127.0.0.1:8332: timed out")
            return 1

        wait_for_rpc(fn, "x")
        # 29 failures spanning ~25 min of fake time, but a quiet log
        self.assertEqual(self.log.getvalue().count("node unavailable"), 6)

    def test_backoff_caps_and_waits_indefinitely_by_default(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 40:
                raise RuntimeError("cannot reach RPC at http://127.0.0.1:8332: timed out")
            return 1

        self.assertEqual(wait_for_rpc(fn, "x"), 1)
        self.assertEqual(max(self.slept), 60.0)
        self.assertEqual(len(calls), 40)                  # no budget: kept waiting

    def test_wait_max_gives_up_with_the_cause(self):
        def fn():
            raise RuntimeError("cannot reach RPC at http://127.0.0.1:8332: refused")

        with self.assertRaises(RuntimeError) as cm:
            wait_for_rpc(fn, "fetching blocks 1..64", wait_max=30)
        self.assertIn("still unavailable", str(cm.exception))
        self.assertIn("cannot reach RPC", str(cm.exception))

    def test_warmup_and_catch_up_are_waited_out(self):
        for e in (RpcError("getblockhash", -28, "Loading block index..."),
                  RpcError("getblockhash", -8, "Block height out of range")):
            calls = []

            def fn(e=e):
                calls.append(1)
                if len(calls) < 2:
                    raise e
                return "ok"

            self.assertEqual(wait_for_rpc(fn, "x"), "ok")

    def test_real_faults_are_not_waited_out(self):
        for e in (RuntimeError("RPC authentication failed (HTTP 401)"),
                  RpcMethodMissing("getblock", -32601, "method not found"),
                  RpcError("getblock", -5, "Block not found"),
                  MissingInput("spends unknown abcd:0"),
                  ValueError("truncated block")):
            with self.assertRaises(type(e)):
                wait_for_rpc(lambda e=e: (_ for _ in ()).throw(e), "x")
            self.assertEqual(self.slept, [])   # never even paused

    def test_transient_predicate(self):
        self.assertTrue(transient_rpc(RuntimeError("cannot reach RPC at x: refused")))
        self.assertTrue(transient_rpc(RuntimeError("RPC batch: backend returned non-JSON response")))
        self.assertTrue(transient_rpc(RpcError("m", -28, "Loading block index...")))
        self.assertFalse(transient_rpc(RuntimeError("RPC authentication failed (HTTP 401)")))
        self.assertFalse(transient_rpc(RpcError("m", -5, "Block not found")))
        self.assertFalse(transient_rpc(MissingInput("x")))


class RpcBlocksWaitTests(WaitBase):
    """The wait belongs around the fetch only: a block that arrives and will
    not parse is a real fault and must still stop the run."""

    class FakeRpc:
        def __init__(self, fail_times=0):
            self.fail_times = fail_times
            self.timeouts = []

        def batch(self, calls, timeout=600):
            self.timeouts.append(timeout)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise RuntimeError("cannot reach RPC at http://127.0.0.1:8332: refused")
            if calls[0][0] == "getblockhash":
                return ["hash%d" % c[1] for c in calls]
            return ["00" for _ in calls]        # arrives, but is not a block

    def test_fetch_waits_out_the_outage_then_parse_failure_still_raises(self):
        rpc = self.FakeRpc(fail_times=2)
        with self.assertRaises(Exception) as cm:
            RpcBlocks(rpc, chunk=4)._fetch_chunk(0, 3)
        self.assertNotIsInstance(cm.exception, RuntimeError)   # not the RPC path
        self.assertEqual(self.slept, [5.0, 10.0])              # both outages waited out
        self.assertIn("fetching blocks 0..3", self.log.getvalue())

    def test_timeout_is_passed_to_both_batches(self):
        rpc = self.FakeRpc()
        with self.assertRaises(Exception):
            RpcBlocks(rpc, chunk=2, timeout=77)._fetch_chunk(0, 1)
        self.assertEqual(rpc.timeouts, [77, 77])   # getblockhash and getblock alike


if __name__ == "__main__":
    unittest.main()
