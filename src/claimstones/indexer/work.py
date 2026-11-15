"""The work table for an index that starts above genesis
(docs/spec/indexer.md §Work).

The fused indexer computes W(h) = W(h-1) + w(h) from each block's own
header as it connects it. An index resumed from a snapshot -- the
land-free birth index at 975 433 -- never saw the headers below its
start, yet a land values runs born at any height, so w(h) must exist
for every h. This fills the gap from the node's headers, once:

  - nBits is constant inside a retarget period on main, signet and
    regtest (no minimum-difficulty rule there), so one header per period
    gives every w(h);
  - the cumulative W(h) is written for every height 0..upto;
  - W(upto) must equal the node's `chainwork` at that height, exactly.
    It is the same sum over the same headers; a difference means the
    per-period assumption does not hold on this chain, and the daemon
    must not run on a wrong table.

Agreement with the node's chainwork at any later height is a check the
daemon may log, never an input (the spec).
"""

from __future__ import annotations

from claimstones.lineage.walker import RETARGET_PERIOD, work_from_nbits


def fill_work(store, call, upto: int, report=print,
              period: int = RETARGET_PERIOD) -> int:
    """Write W(h) for every h in 0..upto from one header per retarget
    period; check W(upto) against the node; commit. Returns rows written.
    `call(method, *args)` is the node RPC."""
    periods = upto // period + 1
    report(f"  work table: reading nBits for {periods} retarget periods "
           f"up to h={upto}", flush=True)
    bits = []
    for p in range(periods):
        header = call("getblockheader", call("getblockhash", p * period))
        bits.append(int(header["bits"], 16))
    node_chainwork = int(call("getblockheader",
                              call("getblockhash", upto))["chainwork"], 16)

    store.begin()
    chainwork = 0
    for h in range(upto + 1):
        chainwork += work_from_nbits(bits[h // period])
        store.put_work(h, chainwork)
    if chainwork != node_chainwork:
        store.rollback()
        raise RuntimeError(
            f"work table: W({upto}) computed from one nBits per period is "
            f"{chainwork:#x} but the node's chainwork is {node_chainwork:#x}; "
            f"nBits is not constant per period on this chain -- not filling")
    store.commit()
    report(f"  work table: {upto + 1} rows, W({upto}) matches the node",
           flush=True)
    return upto + 1
