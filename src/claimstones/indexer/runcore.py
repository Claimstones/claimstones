"""Run arithmetic on packed run lists — the shared core of the indexers.

A run list is the schema-3 encoding birth.py stores: consecutive 12-byte
pairs (uint32 birth height, uint64 sat count), little-endian, in the
output's FIFO sat order, adjacent pairs never sharing a height. This module
does the three operations of the lineage walk directly on those bytes:

    concat(parts)       input lists joined in input order, merged only at
                        the boundaries (extend_merged's contract)
    cut(stream, sizes)  the stream sliced across outputs by value, FIFO,
                        plus the leftover fee tail (cut_runs's contract)
    total(stream)       sats in a stream

The contract is bit-exactness with birth.py's tuple implementation:
for all inputs, concat/cut here encode to the same bytes as
extend_merged/cut_runs over decode_runs of the same inputs. The replay
verifier (fastbuild/verify_runcore.py) proves it over the real chain;
tests/test_runcore.py fuzzes the edges.

Why bytes instead of tuples: the 2026-10-26 profile showed ~70% of build
wall time in the tuple machinery, with 648.8 million list.append calls per
100 tip-era blocks — flow is dominated by consolidation streams thousands
of runs long. Here a small stream (the common case) is handled by a struct
loop that never builds tuples, and a big stream by numpy slicing
(cumsum + searchsorted + memcpy), so cost scales with the number of
outputs, not the number of runs.

decided 2026-10-26 (fastbuild.md): numpy first; if the replay benchmark
sustains under ~20M fragments/s or phase 2 projects past 12 h, this
module's internals move to Rust behind the same four names.
"""

from __future__ import annotations

import struct
from typing import Optional

try:
    import numpy as _np
except ImportError:                      # the scalar path carries everything
    _np = None

RUN = struct.Struct("<IQ")               # (height, count)
RUN_SIZE = RUN.size                      # 12
PLAIN = 0xFFFFFFFF

# Above this many runs the numpy path wins; below it, numpy's per-call
# overhead loses to the struct loop. Order of magnitude from the replay
# benchmark; exactness does not depend on it.
NUMPY_MIN_RUNS = 64

_DT = _np.dtype([("h", "<u4"), ("n", "<u8")]) if _np is not None else None


def make_run(height: int, count: int) -> bytes:
    return RUN.pack(height, count) if count > 0 else b""


def total(stream: bytes) -> int:
    """Sats in a stream."""
    pairs = len(stream) // RUN_SIZE
    if _np is not None and pairs >= NUMPY_MIN_RUNS:
        return int(_np.frombuffer(stream, _DT)["n"].sum())
    return sum(n for _, n in RUN.iter_unpack(stream))


def concat(parts: list[bytes]) -> bytes:
    """Join run lists in order, merging only where a boundary meets two
    runs of the same height. Empty parts vanish. Pure byte assembly: the
    only pairs ever unpacked are the two at each boundary."""
    chunks: list[bytes] = []
    carry: Optional[tuple[int, int]] = None      # last pair, held back
    for part in parts:
        if not part:
            continue
        h0, n0 = RUN.unpack_from(part, 0)
        if carry is not None:
            if carry[0] == h0:
                n0 += carry[1]
            else:
                chunks.append(RUN.pack(*carry))
        if len(part) == RUN_SIZE:
            carry = (h0, n0)
        else:
            chunks.append(RUN.pack(h0, n0))
            chunks.append(part[RUN_SIZE:-RUN_SIZE] if len(part) > 2 * RUN_SIZE
                          else b"")
            carry = RUN.unpack_from(part, len(part) - RUN_SIZE)
    if carry is not None:
        chunks.append(RUN.pack(*carry))
    return b"".join(chunks)


def cut(stream: bytes, sizes: list[int]) -> tuple[list[bytes], bytes]:
    """Slice a stream into FIFO pieces of the given sizes; the remainder is
    the leftover (the fee tail). A size of 0 yields an empty piece; a
    stream that runs short cuts the remaining pieces short and leaves an
    empty leftover — exactly cut_runs."""
    pairs = len(stream) // RUN_SIZE
    if _np is not None and pairs >= NUMPY_MIN_RUNS:
        return _cut_np(stream, sizes, pairs)
    return _cut_scalar(stream, sizes)


def _cut_scalar(stream: bytes, sizes: list[int]) -> tuple[list[bytes], bytes]:
    pieces: list[bytes] = []
    i = 0                                # byte offset of the current run
    off = 0                              # sats already taken from it
    end = len(stream)
    for size in sizes:
        need = size
        start = i
        head = b""
        if need > 0 and i < end and off:
            h, n = RUN.unpack_from(stream, i)
            avail = n - off
            if need >= avail:            # finish the split run
                head = RUN.pack(h, avail)
                need -= avail
                i += RUN_SIZE
                off = 0
                start = i
            else:                        # this piece lives inside one run
                pieces.append(RUN.pack(h, need))
                off += need
                continue
        while need > 0 and i < end:
            h, n = RUN.unpack_from(stream, i)
            if need >= n:
                need -= n
                i += RUN_SIZE
            else:
                pieces.append(head + stream[start:i] + RUN.pack(h, need))
                off = need
                need = 0
                break
        else:
            pieces.append(head + stream[start:i])
    if i >= end:
        return pieces, b""
    if off:
        h, n = RUN.unpack_from(stream, i)
        return pieces, RUN.pack(h, n - off) + stream[i + RUN_SIZE:]
    return pieces, stream[i:]


def _cut_np(stream: bytes, sizes: list[int], pairs: int) -> tuple[list[bytes], bytes]:
    arr = _np.frombuffer(stream, _DT)
    cum = _np.cumsum(arr["n"], dtype=_np.uint64)     # sats through run i
    stream_total = int(cum[-1])
    pieces: list[bytes] = []
    a = 0                                            # window start, in sats
    for size in sizes:
        if size <= 0:
            pieces.append(b"")
            continue
        b = a + size
        if a >= stream_total:
            pieces.append(b"")
            continue
        b_eff = min(b, stream_total)
        # runs overlapping [a, b_eff): i0 = first with cum > a,
        # i1 = first with cum >= b_eff (the run the window ends inside or at)
        i0 = int(_np.searchsorted(cum, a, side="right"))
        i1 = int(_np.searchsorted(cum, b_eff - 1, side="right"))
        piece = bytearray(stream[i0 * RUN_SIZE:(i1 + 1) * RUN_SIZE])
        pre_i0 = int(cum[i0 - 1]) if i0 else 0       # sats before run i0
        if a > pre_i0:                               # window starts mid-run
            h, n = RUN.unpack_from(piece, 0)
            RUN.pack_into(piece, 0, h, n - (a - pre_i0))
        if b_eff < int(cum[i1]):                     # window ends mid-run
            h, _ = RUN.unpack_from(piece, len(piece) - RUN_SIZE)
            pre_i1 = int(cum[i1 - 1]) if i1 else 0
            # the last run contributes the sats from max(a, pre_i1) to b_eff
            # (max, because when i0 == i1 the window also started inside it)
            RUN.pack_into(piece, len(piece) - RUN_SIZE, h, b_eff - max(a, pre_i1))
        pieces.append(bytes(piece))
        a = b
    if a >= stream_total:
        return pieces, b""
    i0 = int(_np.searchsorted(cum, a, side="right"))
    pre_i0 = int(cum[i0 - 1]) if i0 else 0
    leftover = bytearray(stream[i0 * RUN_SIZE:])
    if a > pre_i0:
        h, n = RUN.unpack_from(leftover, 0)
        RUN.pack_into(leftover, 0, h, n - (a - pre_i0))
    return pieces, bytes(leftover)
