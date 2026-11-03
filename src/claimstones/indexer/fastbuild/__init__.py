"""Fast birth index build: the lookahead rebuild of docs/spec/fastbuild.md.

Phases, each a module, each ending at an on-disk artifact with a manifest:

    blocks.py    phase 0: the chain out of Core's blk*.dat files
    resolve.py   phase 1: dense creation ids, the spend join, death array
    spill.py     record formats, epoch files, the final stream
    replay.py    phase 2: the lookahead replay
    load.py      phase 3: LMDB bulk load and tip handover

The build never asks a question it can look up later: no store, no lookups,
every byte written once and read once, in order.
"""
