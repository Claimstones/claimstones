"""The ledger half: pure functions for steps 3 and 5 of the indexer's
per-transaction step (docs/spec/indexer.md §Determinism).

Nothing here touches a store or a node. The caller -- BirthIndexer._place
-- hands in what it already has: the input run stream (before the cut),
the deciphered artifact, the pot of input balances, W(c) and a lookup
for w(h); it gets back the landed amount, the seeds per output and what
burned. That is what makes the two halves one pass: the stream the birth
half is about to cut across the outputs is the stream a land values.

    land_amount(stream, chainwork, work_of)   land.md §Valuation sequence
    allocate(outputs, artifact, pot)          envelope.md §Edicts and allocation
    apply(tx, artifact, stream, pot, ...)     the two composed, as _place calls them

The allocation is the 2026-09-17 engine of indexer/replay.py (ord's
rune_updater port, one asset) lifted out of its store. Vectors 1-12 and
15 of indexer.md are tests/test_ledger.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from claimstones.bitcoin.txparse import Tx, TxOut
from claimstones.indexer.birth import PLAIN, Run
from claimstones.lineage.walker import (SUBSIDY_SCALE, seeds_from_work,
                                       subsidy_sats, work_from_nbits)
from claimstones.protocol.codec import (ORCHID, U128_MAX, Artifact,
                                        Cenotaph, Envelope, is_op_return)

__all__ = ["Step", "stream_work", "land_amount", "allocate", "apply",
           "work_from_nbits"]

WorkOf = Callable[[int], int]     # height -> w(h), expected hashes of that block


# ---------------------------------------------------------------------------
# Step 3: the land amount
# ---------------------------------------------------------------------------

def stream_work(stream: Sequence[Run], work_of: WorkOf) -> int:
    """N of constants.md §Valuation over an input run stream:

        N = sum over runs (h, n) of  n * w(h) * (L div s(h))

    A PLAIN run carries nothing (landed already). A run born at or above
    halving 33 carries nothing (s(h) = 0, no carrier). Every term is an
    exact integer, so the order of the runs and whether equal heights
    were merged cannot change the sum -- land.md §Valuation sequence,
    "Order is not part of the sequence"."""
    total = 0
    for h, n in stream:
        if h == PLAIN or n <= 0:
            continue
        s = subsidy_sats(h)
        if s == 0:
            continue
        total += n * work_of(h) * (SUBSIDY_SCALE // s)
    return total


def land_amount(stream: Sequence[Run], chainwork: int, work_of: WorkOf) -> int:
    """Seeds minted by a land of `stream` in a block with cumulative
    chainwork `chainwork` (W(c)); 0 when the land is void.

        amount = (A_0 * N) div (W(c) * L)

    The one floor of the valuation. Void (land.md rule 5, constants.md
    §Arithmetic width): N = 0, or an amount above u128 max -- void, not
    capped, so a wrong amount is never minted silently."""
    n = stream_work(stream, work_of)
    if n == 0:
        return 0
    amount = seeds_from_work(n, chainwork)
    return amount if amount <= U128_MAX else 0


# ---------------------------------------------------------------------------
# Step 5: allocation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """What the ledger half decided for one transaction."""
    land: bool          # a LAND tag was present (valid or cenotaph):
                        # the birth half makes every output run PLAIN
    landed: int         # seeds the land added to the pot (0: no LAND, or void)
    seeds: list[int]    # per output; seeds for an OP_RETURN output are
                        # already counted in `burned` and appear here as 0
    burned: int


def allocate(outputs: Sequence[TxOut], artifact: Optional[Artifact],
             pot: int) -> tuple[list[int], int]:
    """Place `pot` seeds over `outputs` as envelope.md §Edicts and
    allocation says; returns (seeds per output, burned).

    Envelope: edicts in order (asset (0,0) only, others void; amount 0 =
    all remaining; output == len(outputs) splits across non-OP_RETURN
    outputs; never more than the pot holds), then the remainder to the
    POINTER output, else the first non-OP_RETURN output, else burned.
    Cenotaph: everything burns. No envelope: the remainder rule alone.
    Seeds that land on an OP_RETURN output burn."""
    seeds = [0] * len(outputs)
    burned = 0
    unallocated = pot

    if isinstance(artifact, Cenotaph):
        return seeds, pot

    if isinstance(artifact, Envelope):
        for edict in artifact.edicts:
            if edict.id != ORCHID:
                continue                        # unknown asset: void edict
            balance = unallocated
            output = edict.output
            if output == len(outputs):
                destinations = [i for i, o in enumerate(outputs)
                                if not is_op_return(o.script_pubkey)]
                if destinations:
                    if edict.amount == 0:
                        share, rest = divmod(balance, len(destinations))
                        for i, idx in enumerate(destinations):
                            give = share + 1 if i < rest else share
                            if give > 0:
                                balance -= give
                                seeds[idx] += give
                    else:
                        for idx in destinations:
                            give = min(edict.amount, balance)
                            if give > 0:
                                balance -= give
                                seeds[idx] += give
            else:
                give = balance if edict.amount == 0 else min(edict.amount, balance)
                if give > 0:
                    balance -= give
                    seeds[output] += give
            unallocated = balance

    vout = artifact.pointer if isinstance(artifact, Envelope) else None
    if vout is None:
        for i, o in enumerate(outputs):
            if not is_op_return(o.script_pubkey):
                vout = i
                break
    if vout is not None:
        seeds[vout] += unallocated
    else:
        burned += unallocated

    for i, o in enumerate(outputs):
        if seeds[i] and is_op_return(o.script_pubkey):
            burned += seeds[i]
            seeds[i] = 0
    return seeds, burned


# ---------------------------------------------------------------------------
# Steps 3 + 5 as _place calls them
# ---------------------------------------------------------------------------

def apply(tx: Tx, artifact: Optional[Artifact], stream: Sequence[Run],
          pot: int, chainwork: int, work_of: WorkOf) -> Step:
    """The ledger's part of one transaction. `stream` is the input run
    stream before the cut (for a coinbase: subsidy + fee tails); `pot`
    the sum of the inputs' balances; `artifact` what decipher returned,
    or None below activation / without an envelope."""
    land = artifact is not None and artifact.land
    landed = land_amount(stream, chainwork, work_of) if land else 0
    seeds, burned = allocate(tx.outputs, artifact, pot + landed)
    return Step(land=land, landed=landed, seeds=seeds, burned=burned)
