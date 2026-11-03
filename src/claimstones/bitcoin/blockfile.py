"""Read-only, integrity-checked access to Pynode's block storage.

Files (all opened read-only; the node can keep running):

- headers.dat — 80 bytes per header, sequential from genesis. This is
  the canonical chain: hash-linked headers with proof of work. We use
  it as the source of truth for the block hash at every height.
- blocks.idx — 48 bytes per entry, append-only:
      hash (32, internal order) + offset (u64 LE) + size (u32 LE)
      + height (i32 LE)
- blocks.dat — raw blocks at the offsets given by the index.

The index cannot be trusted blindly: it may contain duplicate entries
for a height, entries whose size field includes trailing data, and
entries pointing at data that is not the canonical block at all (all
three were observed in practice). A height's entry is accepted only
if the 80-byte header at its offset hashes to the canonical hash for
that height from headers.dat.

Heights with no acceptable entry can be served from a supplement file
(cache/recovered_blocks.dat + .idx, same 48-byte index format) holding
externally fetched blocks; those are verified against the canonical
hash the same way, so their origin does not matter.
"""

from __future__ import annotations

import os
import struct

from claimstones.bitcoin.txparse import dsha256

PYNODE_DIR = r"C:\apps\Pynode"

ENTRY_SIZE = 48
HEADER_SIZE = 80


def load_index(path: str) -> dict[int, list[tuple[int, int]]]:
    """height -> [(offset, size), ...] in file order."""
    entries: dict[int, list[tuple[int, int]]] = {}
    with open(path, "rb") as f:
        while True:
            entry = f.read(ENTRY_SIZE)
            if len(entry) < ENTRY_SIZE:
                break
            offset, size, height = struct.unpack("<QIi", entry[32:48])
            entries.setdefault(height, []).append((offset, size))
    return entries


class BlockFile:
    def __init__(
        self,
        pynode_dir: str = PYNODE_DIR,
        supplement: str | None = None,
    ):
        self.pynode_dir = pynode_dir
        self.headers_path = os.path.join(pynode_dir, "headers.dat")
        self.blocks_path = os.path.join(pynode_dir, "blocks.dat")
        self.index_path = os.path.join(pynode_dir, "blocks.idx")
        self.supplement = supplement  # path base: <supplement>.dat / .idx

        self.canonical: dict[int, bytes] = {}  # height -> block hash (internal order)
        # height -> (source, offset, size); source 0 = blocks.dat, 1 = supplement
        self.by_height: dict[int, tuple[int, int, int]] = {}
        self.unresolved: list[int] = []  # heights with no canonical entry

        self._fh = open(self.blocks_path, "rb")
        self._sfh = None
        if supplement and os.path.exists(supplement + ".dat"):
            self._sfh = open(supplement + ".dat", "rb")

        self.reload()

    def _load_canonical(self) -> None:
        """Read headers.dat and verify its hash linkage as we go."""
        size = os.path.getsize(self.headers_path)
        n_headers = size // HEADER_SIZE
        with open(self.headers_path, "rb") as f:
            prev = None
            start = 0
            # headers.dat starts at genesis; skip straight to 840000
            if n_headers > 840_000:
                start = 839_999
                f.seek(start * HEADER_SIZE)
                prev = dsha256(f.read(HEADER_SIZE))
                start += 1
            for height in range(start, n_headers):
                header = f.read(HEADER_SIZE)
                if prev is not None and header[4:36] != prev:
                    raise ValueError(
                        f"headers.dat hash linkage broken at height {height}"
                    )
                prev = dsha256(header)
                self.canonical[height] = prev

    def reload(self) -> None:
        """(Re)load headers and index; select canonical entries."""
        self.canonical.clear()
        self._load_canonical()

        self.by_height.clear()
        self.unresolved = []

        candidates = load_index(self.index_path)
        supplement_candidates: dict[int, list[tuple[int, int]]] = {}
        if self.supplement and os.path.exists(self.supplement + ".idx"):
            supplement_candidates = load_index(self.supplement + ".idx")
            if self._sfh is None and os.path.exists(self.supplement + ".dat"):
                self._sfh = open(self.supplement + ".dat", "rb")

        for height in sorted(candidates.keys() | supplement_candidates.keys()):
            canonical = self.canonical.get(height)
            if canonical is None:
                continue
            chosen = None
            for source, fh, ents in (
                (0, self._fh, candidates.get(height, ())),
                (1, self._sfh, supplement_candidates.get(height, ())),
            ):
                for offset, size in ents:
                    fh.seek(offset)
                    if dsha256(fh.read(HEADER_SIZE)) == canonical:
                        chosen = (source, offset, size)
                        break
                if chosen:
                    break
            if chosen:
                self.by_height[height] = chosen
            else:
                self.unresolved.append(height)

    @property
    def min_height(self) -> int:
        return min(self.by_height)

    @property
    def max_height(self) -> int:
        return max(self.by_height)

    def read_block(self, height: int) -> bytes:
        """Read a block and verify its header against the canonical chain.

        The returned bytes may contain trailing data beyond the block
        (some index entries over-state the size); parse_block validates
        the transaction list against the header merkle root, so
        trailing bytes are harmless.
        """
        source, offset, size = self.by_height[height]
        fh = self._sfh if source == 1 else self._fh
        fh.seek(offset)
        data = fh.read(size)
        if len(data) != size:
            raise IOError(f"short read at height {height}: {len(data)}/{size}")
        if dsha256(data[:HEADER_SIZE]) != self.canonical[height]:
            raise ValueError(f"block at height {height} is not canonical")
        return data

    def close(self) -> None:
        self._fh.close()
        if self._sfh:
            self._sfh.close()
