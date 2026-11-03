"""The consensus walk is cache-independent; a cut-off walk is not.

docs/spec/land.md defines valuation over all runs with one floor, and
makes the walk cutoff a verifier's own speed-for-accuracy choice rather
than part of the protocol (decided 2026-10-25). These tests pin both
halves of that, on a synthetic chain small enough to reason about:

    height   2 016   coinbase A, 50 BTC, in a low-difficulty period
    height 100 800   coinbase B, 50 BTC, in a high-difficulty period
    height 110 880   tx T spends A:0 and B:0 into one 100 BTC output

W is chosen so that at DUST_SEEDS = 1 the walk keeps B and drops A: a
sat born at 2 016 carries almost no work, which is the real shape of the
chain and the whole reason the cutoff exists.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from claimstones.lineage.walker import (A_0, RETARGET_PERIOD, SUBSIDY_SCALE,
                                        Cache, LineageWalker, WorkTable)

LOW_NBITS = 0x1D00FFFF       # difficulty 1
HIGH_NBITS = 0x1900FFFF      # target 256^4 smaller, so vastly more work
HIGH_FROM = 30               # retarget periods at or above this are HIGH

H_A, H_B, H_T = 2_016, 100_800, 110_880
TIP = H_T
COIN = 5_000_000_000         # 50 BTC in sats, the subsidy at these heights


def nbits_of(period: int) -> int:
    return HIGH_NBITS if period >= HIGH_FROM else LOW_NBITS


class FakeChain:
    """Just enough node to walk the chain above. Called like Rpc, and
    read-only, so the walker's prefetch threads can share it."""

    def __init__(self):
        self.height_of_hash = {f"bh{h}": h for h in (H_A, H_B, H_T)}
        cb = [{"coinbase": "00"}]
        self.txs = {
            "A": {"txid": "A", "vin": cb, "blockhash": f"bh{H_A}",
                  "vout": [{"value": Decimal(50)}]},
            "B": {"txid": "B", "vin": cb, "blockhash": f"bh{H_B}",
                  "vout": [{"value": Decimal(50)}]},
            # no fee, so the input stream exactly fills the one output and
            # no fee tail is ever walked
            "T": {"txid": "T", "blockhash": f"bh{H_T}",
                  "vin": [{"txid": "A", "vout": 0}, {"txid": "B", "vout": 0}],
                  "vout": [{"value": Decimal(100)}]},
        }

    def __call__(self, method, *args, **kw):
        if method == "getrawtransaction":
            return self.txs[args[0]]
        if method == "getblockheader":
            h = self.height_of_hash[args[0]]
            return {"height": h, "bits": f"{nbits_of(h // RETARGET_PERIOD):08x}"}
        if method == "getblockhash":
            return f"bh{args[0]}"
        raise AssertionError(f"the fake chain was asked for {method}")


class WalkCacheTests(unittest.TestCase):

    def setUp(self):
        self.chain = FakeChain()
        self.dir = tempfile.mkdtemp(prefix="walkcache")
        self.n = 0
        # W such that A sits a thousandfold below one seed while B is far
        # above it, so the two coinbases land on opposite sides of the
        # cutoff with room to spare.
        table = self.table(self.fresh_cache())
        self.W = 1000 * A_0 * COIN * table.maxrate(H_A) // SUBSIDY_SCALE
        self.assertLess(self.seeds_of(COIN, table.maxrate(H_A)), 1,
                        "A should be below one seed")
        self.assertGreater(self.seeds_of(COIN, table.maxrate(H_B)), 1,
                           "B should be well above one seed")

    def seeds_of(self, size, rate):
        return A_0 * size * rate / (self.W * SUBSIDY_SCALE)

    def fresh_cache(self):
        """A cache of its own, with the nbits table filled in directly so
        WorkTable has nothing to fetch (and prints nothing)."""
        self.n += 1
        cache = Cache(os.path.join(self.dir, f"c{self.n}.sqlite"), fresh=True)
        for p in range(TIP // RETARGET_PERIOD + 1):
            cache.put_nbits(p, nbits_of(p))
        cache.commit()
        return cache

    def table(self, cache):
        return WorkTable(self.chain, cache, TIP)

    def walk(self, cache, dust):
        walker = LineageWalker(self.chain, cache, self.table(cache),
                               max_nodes=1 << 62, dust=dust, tip=TIP,
                               chainwork=self.W)
        return walker.walk_utxo("T", 0, 2 * COIN)

    # -- the chain behaves as the comment claims -------------------------

    def test_without_a_cutoff_both_coinbases_resolve(self):
        res = self.walk(self.fresh_cache(), dust=0)
        self.assertEqual(dict(res.birth), {H_A: COIN, H_B: COIN})
        self.assertEqual(res.unresolved, 0)

    def test_the_cutoff_drops_the_ancient_coinbase(self):
        res = self.walk(self.fresh_cache(), dust=1)
        self.assertEqual(dict(res.birth), {H_B: COIN})
        self.assertEqual(res.unresolved, COIN)
        self.assertTrue(res.deterministic)   # a cutoff decision, not a budget

    def test_a_cold_walk_is_reproducible(self):
        a = self.walk(self.fresh_cache(), dust=1)
        b = self.walk(self.fresh_cache(), dust=1)
        self.assertEqual(dict(a.birth), dict(b.birth))

    # -- what the protocol relies on -------------------------------------

    def test_the_consensus_walk_is_cache_independent(self):
        """dust=0 is the consensus walk (land.md: a consensus answer
        requires d = 0). Prime a cache, then walk the same outpoint on
        it; the answer must be the cold one. Here the memo tables are
        what they claim to be -- a pure speed-up."""
        cache = self.fresh_cache()
        primed = self.walk(cache, dust=0)
        warm = self.walk(cache, dust=0)
        cold = self.walk(self.fresh_cache(), dust=0)
        self.assertEqual(dict(warm.birth), dict(cold.birth))
        self.assertEqual(dict(warm.birth), {H_A: COIN, H_B: COIN})
        self.assertEqual(warm.unresolved, 0)
        self.assertEqual(dict(primed.birth), dict(cold.birth))

    def test_a_positive_cutoff_is_cache_dependent(self):
        """Which is exactly why it is not consensus.

        Walk once with no cutoff, memoizing the ancient range as fully
        resolved, then walk the same outpoint at dust=1 on that warm
        cache. The answers differ: `lineage` is keyed (txid, vout, lo,
        hi) with neither dust nor chainwork, and _expand consults it
        before below_dust runs, so a range an earlier walk happened to
        resolve comes back exact and the cutoff never tests it.

        Reordering below_dust ahead of the memo would not fix it -- the
        memo that does the damage is on the root range, and a root frame
        has bound None, so the cutoff is guarded out. The fix is not to
        rely on it: at dust=0, which the protocol requires, the question
        does not arise (see the test above).

        Asserted rather than left as an expected failure, because a
        warm-vs-cold difference here is now permitted behaviour in a
        parameter outside consensus, and silently losing it would hide
        the reason the cutoff was demoted.
        """
        cache = self.fresh_cache()
        self.walk(cache, dust=0)
        warm = self.walk(cache, dust=1)
        cold = self.walk(self.fresh_cache(), dust=1)
        self.assertEqual(dict(cold.birth), {H_B: COIN})
        self.assertEqual(dict(warm.birth), {H_A: COIN, H_B: COIN})
        self.assertNotEqual(dict(warm.birth), dict(cold.birth))

    def test_the_cutoff_only_ever_under_mints(self):
        """land.md: a positive dust is safe because it errs downwards.
        A cut walk must never report more birth sats than dust=0."""
        exact = self.walk(self.fresh_cache(), dust=0)
        cut = self.walk(self.fresh_cache(), dust=1)
        self.assertLessEqual(sum(cut.birth.values()), sum(exact.birth.values()))
        self.assertEqual(cut.unresolved, COIN)


if __name__ == "__main__":
    unittest.main()
