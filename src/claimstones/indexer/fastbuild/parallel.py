"""Parallel block parse: a process pool runs ahead of the replay.

The replay is a fold over chain order — every block mutates state the
next block depends on — so it cannot be distributed. The parse in front
of it is stateless: any block parses alone. This module parallelizes
exactly that producer and nothing else.

Two rules make the pool pay:

  - workers return PACKED ARRAYS, never Python object trees: pickling a
    block's tx objects costs more than parsing them. A packed block is
        (txids 32*n_tx, n_in u32[n_tx], n_out u32[n_tx],
         values u64[outputs], soff u32[outputs+1], scripts bytes)
    in block order, and the consumer slices it.
  - backpressure: one new task is submitted per result consumed, with a
    bounded in-flight window, so a fast pool can never pile unconsumed
    blocks into RAM (Pool.imap would).

Workers read the blk files themselves (dir + xor key from the
initializer, file/offset/size per task), so no raw bytes cross the
process boundary either.
"""

from __future__ import annotations

import os
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from typing import Iterator

import numpy as np

from claimstones.bitcoin.txparse import parse_block
from claimstones.indexer.fastbuild.blocks import BlockIndex, unxor

Packed = tuple[bytes, np.ndarray, np.ndarray, np.ndarray, np.ndarray, bytes]


def pack(block) -> Packed:
    """One block's parse as flat buffers, in block order."""
    txs = block.txs
    outs = [o for t in txs for o in t.outputs]
    soff = np.zeros(len(outs) + 1, np.uint32)
    np.cumsum(np.fromiter((len(o.script_pubkey) for o in outs),
                          np.uint32, len(outs)), out=soff[1:])
    return (b"".join(t.txid for t in txs),
            np.fromiter((len(t.inputs) for t in txs), np.uint32, len(txs)),
            np.fromiter((len(t.outputs) for t in txs), np.uint32, len(txs)),
            np.fromiter((o.value for o in outs), np.uint64, len(outs)),
            soff,
            b"".join(o.script_pubkey for o in outs))


def serial_blocks(idx: BlockIndex, start: int,
                  end: int) -> Iterator[tuple[int, Packed]]:
    """The no-pool producer: same interface, one process."""
    for height in range(start, end + 1):
        yield height, pack(parse_block(idx.raw(height), height))


# ---- worker side ----------------------------------------------------------

_DIR = ""
_KEY = b"\x00" * 8
_HANDLES: dict[int, object] = {}


def _init(blocks_dir: str, key: bytes) -> None:
    global _DIR, _KEY
    _DIR, _KEY = blocks_dir, key


def _parse(task: tuple[int, int, int, int]) -> Packed:
    height, file_no, offset, size = task
    fh = _HANDLES.get(file_no)
    if fh is None:
        if len(_HANDLES) > 4:                 # heights arrive mostly in order
            _HANDLES.pop(next(iter(_HANDLES))).close()
        fh = _HANDLES[file_no] = open(
            os.path.join(_DIR, f"blk{file_no:05d}.dat"), "rb")
    fh.seek(offset)
    return pack(parse_block(unxor(fh.read(size), _KEY, offset), height))


def pooled_blocks(idx: BlockIndex, start: int, end: int, workers: int,
                  window_per_worker: int = 12) -> Iterator[tuple[int, Packed]]:
    """Packed blocks in height order, parsed by a process pool."""
    window = max(workers * window_per_worker, workers + 1)
    it = iter(range(start, end + 1))
    pending: deque = deque()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(idx.blocks_dir, idx.key)) as ex:

        def submit(h: int) -> None:
            _, file_no, offset, size = idx.entry(h)
            pending.append((h, ex.submit(_parse, (h, file_no, offset, size))))

        for h in it:
            submit(h)
            if len(pending) >= window:
                break
        for h_next in it:
            h, fut = pending.popleft()
            yield h, fut.result()
            submit(h_next)
        while pending:
            h, fut = pending.popleft()
            yield h, fut.result()
