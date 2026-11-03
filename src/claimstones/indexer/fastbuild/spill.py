"""Phase 2's files: epoch cutting, epoch spills, and the final stream.

Epochs partition the height range so that each epoch's dying population —
every output whose death height falls inside it — fits the RAM budget.
They are cut from phase 1's death histogram (deaths per HIST_BIN blocks),
so the bin is the resolution floor. An output created before its death
epoch waits in that epoch's spill file; one that never dies goes straight
to the final stream, the finished index content, written exactly once.

Record formats (spec 9, fixed-width fields, little-endian):

  epoch spill   creation_id u64 + run_count u32 + run_count x 12 B
  final stream  creation_id u64 + vout u32 + txid 32 + script_len u32
                + script + run_count u32 + run_count x 12 B

script_len was u16 until 2026-11-01: the 2025 uncapped-OP_RETURN change
put scriptPubKeys past 64 KiB on mainnet (first hit ~height 870-880k).
"""

from __future__ import annotations

import os
import struct

FINAL_HEAD = struct.Struct("<QI32sI")    # creation_id, vout, txid, script_len
RUN_COUNT = struct.Struct("<I")
SPILL_HEAD = struct.Struct("<QI")        # creation_id, run_count
RUN_SIZE = 12


def cut_epochs(hist: list[int], bin_size: int, end: int,
               budget_bytes: int, per_death: int = 40) -> list[tuple[int, int]]:
    """Half-open height ranges [(h0, h1), ...] covering 0..end inclusive.

    Greedy: a new epoch starts when the dying population's bookkeeping
    (id array, slot table, in-flight runs — per_death bytes a head) would
    outgrow the budget. A single bin over budget still becomes one epoch:
    the histogram cannot see inside a bin."""
    epochs: list[tuple[int, int]] = []
    start = acc = 0
    for i, deaths in enumerate(hist):
        if acc and (acc + deaths) * per_death > budget_bytes:
            epochs.append((start * bin_size, i * bin_size))
            start, acc = i, 0
        acc += deaths
    epochs.append((start * bin_size, end + 1))
    return epochs


class FinalWriter:
    """The finished index content, in creation-id order, written once.

    `resume` is (bytes, count, run_count) from a phase-2 checkpoint: the
    .tmp file is truncated back to that offset (a crash may have written
    past it) and writing continues from there."""

    def __init__(self, path: str, resume: tuple[int, int, int] | None = None):
        self.path = path
        if resume:
            self.bytes, self.count, self.run_count = resume
            if os.path.getsize(path + ".tmp") < self.bytes:
                raise RuntimeError(f"{path}.tmp is shorter than its "
                                   f"checkpoint — delete the checkpoint "
                                   f"to start over")
            with open(path + ".tmp", "r+b") as f:
                f.truncate(self.bytes)
            self.f = open(path + ".tmp", "ab", buffering=1 << 20)
        else:
            self.f = open(path + ".tmp", "wb", buffering=1 << 20)
            self.bytes = self.count = self.run_count = 0

    def write(self, cid: int, vout: int, txid: bytes,
              script: bytes, runs: bytes) -> None:
        n = len(runs) // RUN_SIZE
        rec = (FINAL_HEAD.pack(cid, vout, txid, len(script)) + script
               + RUN_COUNT.pack(n) + runs)
        self.f.write(rec)
        self.bytes += len(rec)
        self.count += 1
        self.run_count += n

    def flush(self) -> None:
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self) -> None:
        self.f.close()
        os.replace(self.path + ".tmp", self.path)


def iter_final(path: str):
    """Yield (creation_id, vout, txid, script, runs) — phase 3's reader."""
    with open(path, "rb", buffering=1 << 20) as f:
        while True:
            head = f.read(FINAL_HEAD.size)
            if not head:
                return
            cid, vout, txid, slen = FINAL_HEAD.unpack(head)
            script = f.read(slen)
            n, = RUN_COUNT.unpack(f.read(RUN_COUNT.size))
            yield cid, vout, txid, script, f.read(n * RUN_SIZE)


class SpillSet:
    """Per-epoch spill files for outputs that die beyond the current epoch.

    Writers open lazily in append mode. An epoch's file is read back when
    its epoch arrives but deleted only by `drop`, at the NEXT checkpoint:
    until then it is what a resume of the current epoch rebuilds its table
    from. `resume` is the checkpoint's {epoch: bytes}: every file is
    truncated back to its recorded size (a crash may have appended past
    it), and files the checkpoint never saw are removed."""

    def __init__(self, dirpath: str, resume: dict[int, int] | None = None,
                 resume_total: int = 0):
        self.dir = dirpath
        self.files: dict[int, object] = {}
        self.sizes: dict[int, int] = {}
        self.bytes = resume_total
        os.makedirs(dirpath, exist_ok=True)
        if resume is None:
            for name in os.listdir(dirpath):          # fresh start
                os.remove(os.path.join(dirpath, name))
            return
        self.sizes = dict(resume)
        for name in os.listdir(dirpath):
            p = os.path.join(dirpath, name)
            want = self.sizes.get(int(name[6:10]), 0)
            if want == 0:
                os.remove(p)
            elif os.path.getsize(p) < want:
                raise RuntimeError(f"{p} is shorter than its checkpoint — "
                                   f"delete the checkpoint to start over")
            else:
                with open(p, "r+b") as f:
                    f.truncate(want)

    def path(self, e: int) -> str:
        return os.path.join(self.dir, f"epoch-{e:04d}.spill")

    def write(self, e: int, cid: int, runs: bytes) -> None:
        f = self.files.get(e)
        if f is None:
            f = self.files[e] = open(self.path(e), "ab", buffering=1 << 20)
        rec = SPILL_HEAD.pack(cid, len(runs) // RUN_SIZE) + runs
        f.write(rec)
        self.bytes += len(rec)
        self.sizes[e] = self.sizes.get(e, 0) + len(rec)

    def load(self, e: int):
        """Yield (creation_id, runs) for epoch e; the file stays on disk
        until `drop` — a resume of this epoch reads it again."""
        f = self.files.pop(e, None)
        if f is not None:
            f.close()
        p = self.path(e)
        if not os.path.exists(p):
            return
        with open(p, "rb", buffering=1 << 20) as f:
            while True:
                head = f.read(SPILL_HEAD.size)
                if not head:
                    break
                cid, n = SPILL_HEAD.unpack(head)
                yield cid, f.read(n * RUN_SIZE)

    def drop(self, e: int) -> None:
        """Delete a consumed epoch's file (after the next checkpoint)."""
        self.sizes.pop(e, None)
        if os.path.exists(self.path(e)):
            os.remove(self.path(e))

    def flush(self) -> None:
        for f in self.files.values():
            f.flush()
            os.fsync(f.fileno())

    def close(self) -> None:
        for f in self.files.values():
            f.close()
        self.files.clear()
