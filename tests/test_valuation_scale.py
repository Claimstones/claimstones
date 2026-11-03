"""The valuation scale L, and why it is the lcm and not a power of two.

docs/spec/constants.md evaluates floor(A_0 * sum n_i*w(h_i)/s(h_i) / W(c))
in integers by scaling w(h)/s(h) -- a fraction, below one for early
blocks -- by L. The spec's promise is that the `div` in `amount` is the
only floor in the whole valuation. That promise holds only while
`L div s(h)` has no remainder for every subsidy, so these tests rederive
L from the subsidy schedule rather than trusting the literal.

The previous scale, D = 5e9 << 33, was documented as having the same
property and did not: bitcoin halves with a truncating shift, so from
halving 10 the subsidies pick up prime factors (3, 11, 71, 521, ...)
that no scaling of 5e9 can carry. Those tests are here too, so the
reason for the change cannot quietly stop being true.
"""

from __future__ import annotations

import math
import os
import random
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from claimstones.lineage.walker import (A_0, HALVING_INTERVAL, SUBSIDY_SCALE,
                                        subsidy_sats)

OLD_SCALE = 5_000_000_000 << 33     # D, replaced 2026-10-25
HALVINGS = 33                       # 5e9 >> 33 == 0


def schedule():
    """Every nonzero subsidy, one per halving."""
    return [subsidy_sats(k * HALVING_INTERVAL) for k in range(HALVINGS)]


def amount(birth: Counter, work_of, chainwork: int, scale: int) -> int:
    """constants.md's valuation, with the scale as a parameter."""
    n = 0
    for height, count in birth.items():
        s = subsidy_sats(height)
        if s:
            n += count * work_of(height) * (scale // s)
    return A_0 * n // (chainwork * scale)


class ScaleTests(unittest.TestCase):

    def test_the_schedule_has_exactly_33_nonzero_subsidies(self):
        subs = schedule()
        self.assertEqual(len(subs), HALVINGS)
        self.assertEqual(subs[0], 5_000_000_000)
        self.assertEqual(subs[-1], 1, "the last subsidy is one sat")
        self.assertEqual(subsidy_sats(HALVINGS * HALVING_INTERVAL), 0)
        self.assertEqual(HALVINGS * HALVING_INTERVAL, 6_930_000)

    def test_the_scale_is_the_lcm_of_the_schedule(self):
        self.assertEqual(SUBSIDY_SCALE, math.lcm(*schedule()))

    def test_the_scale_divides_every_subsidy_exactly(self):
        for k, s in enumerate(schedule()):
            with self.subTest(halving=k, subsidy=s):
                self.assertEqual(SUBSIDY_SCALE % s, 0)

    def test_truncation_is_what_breaks_a_power_of_two_scale(self):
        """Why L and not D: the shift is truncating, so from halving 10 a
        subsidy is no longer 5^10 times a power of two."""
        # 5e9 = 2^9 * 5^10, so the tenth halving is the first shift
        # with something to drop: half a sat, and with it the clean
        # factorisation every earlier subsidy had.
        self.assertEqual(5_000_000_000 % 2 ** 9, 0)
        self.assertEqual(5_000_000_000 % 2 ** 10, 512)
        self.assertEqual(subsidy_sats(9 * HALVING_INTERVAL), 5 ** 10)
        self.assertEqual(subsidy_sats(10 * HALVING_INTERVAL), 4_882_812)
        self.assertEqual(4_882_812 * 2 + 1, 5 ** 10, "a bit was dropped")
        self.assertEqual(4_882_812 % 3, 0, "and a factor of three appeared")
        self.assertNotEqual(SUBSIDY_SCALE % 3, OLD_SCALE % 3)
        exact = [s for s in schedule() if OLD_SCALE % s == 0]
        self.assertEqual(len(exact), 13, "D was exact for 13 of 33")
        self.assertEqual(len([s for s in schedule()
                              if SUBSIDY_SCALE % s == 0]), HALVINGS)

    def test_the_change_of_scale_moves_no_amount_on_today_s_chain(self):
        """Below halving 10 both scales divide exactly, so the two
        evaluations are the same rational and floor alike. Pinned on the
        real 19-run lineage measured 2026-10-24 plus random lineages."""
        W = 104_820_700_000_000_000_000_000_000_000
        heights = [44_686, 104_184, 109_405, 131_112, 132_511, 135_574,
                   135_864, 155_481, 176_469, 206_544, 244_659, 341_782,
                   352_271, 399_795, 478_473, 493_281, 501_356, 700_000,
                   973_058]
        work_of = (lambda h: 2 ** 70 + h * 1_000_003)     # stands in for w(h)
        real = Counter({h: 10_000_000 + h for h in heights})
        self.assertEqual(amount(real, work_of, W, SUBSIDY_SCALE),
                         amount(real, work_of, W, OLD_SCALE))

        rng = random.Random(20261025)
        for _ in range(200):
            birth = Counter({rng.randrange(1, 10 * HALVING_INTERVAL):
                             rng.randrange(1, 5_000_000_000)
                             for _ in range(rng.randrange(1, 8))})
            with self.subTest(birth=dict(birth)):
                self.assertEqual(amount(birth, work_of, W, SUBSIDY_SCALE),
                                 amount(birth, work_of, W, OLD_SCALE))

    def test_the_cliff_at_the_last_halving_is_deliberate(self):
        """constants.md §After the last halving, decided 2026-10-25
        (option A): a block from halving 33 on mints no sats, so its
        work has no carrier and no land can claim it. Pinned because it
        is a cliff, not a limit -- the block before it pays in full."""
        W = 104_820_700_000_000_000_000_000_000_000
        w = lambda h: 2 ** 70
        last = HALVINGS * HALVING_INTERVAL - 1          # 6 929 999
        first_dead = HALVINGS * HALVING_INTERVAL        # 6 930 000

        # the last block with a subsidy mints one sat carrying all of w(h)
        self.assertEqual(subsidy_sats(last), 1)
        alive = amount(Counter({last: 1}), w, W, SUBSIDY_SCALE)
        self.assertEqual(alive, A_0 * 2 ** 70 // W)
        self.assertGreater(alive, 0)

        # one block later, the whole bitcoin supply lands for nothing
        dead = Counter({first_dead: 21_000_000 * 100_000_000})
        self.assertEqual(amount(dead, w, W, SUBSIDY_SCALE), 0)


if __name__ == "__main__":
    unittest.main()
