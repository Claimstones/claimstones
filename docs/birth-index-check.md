# Birth index integrity check

The birth index is consensus state, so it is trusted by verification, not
provenance. A full rebuild is an overnight fastbuild run (`spec/fastbuild.md`;
the 2026-10-29 measurement projects 12–20 hours for the full chain on one
machine), so no copy of the file is precious — what matters is that any copy,
fresh or old, downloaded or built, can be checked against an independent
witness.

The need for that check was learned the hard way, on the earlier SQLite store:
on 2026-09-18 a `FAULTY_HARDWARE_CORRUPTED_PAGE` bugcheck removed rows from the
database, `PRAGMA quick_check` passed, and the loss only surfaced five days
later at block 629 743 as a `MissingInput`. The file was well formed; the rows
were simply wrong. The store is LMDB now, and LMDB's copy-on-write means a
crash loses the uncommitted window and never tears the file — but a memory
fault writes wrong bytes as happily as right ones, and no storage engine's own
tooling can see semantic damage. Only an outside witness can.

The node's UTXO set is that witness, and the check is one arithmetic identity.

## The invariant

Every sat in an unspent output is covered by exactly one run. For the index at
height H:

    Σ (run sat counts over all rows)
      − sats in outputs Core treats as provably unspendable
      − the genesis coinbase (5 000 000 000 sats)
      = Core's gettxoutsetinfo total_amount at H

The two subtractions are not fudge factors; they are outputs the index holds on
purpose and Core's chainstate never does:

- **Provably unspendable.** The indexer stores every output of every
  transaction, because a zero-value or unspendable outpoint is still an
  outpoint. Core applies `IsUnspendable()` and keeps these out of the UTXO set
  entirely: a script whose first byte is `OP_RETURN` (`0x6a`), or a script
  longer than `MAX_SCRIPT_SIZE` (10 000 bytes). Both are computable from the
  index's own script field, so the correction needs no outside data.
- **The genesis coinbase.** Core never adds it. The index does — verified on
  the mainnet builds: `runs=[(0, 5000000000)]`, P2PK script.

BIP-30's duplicate coinbases (heights 91 842 and 91 880) need no correction:
the store overwrites by key at exactly those heights (`BIP30_HEIGHTS`), which
leaves one instance, matching Core.

## When it can run

**At any committed height — a stop is no longer required.** An LMDB read
transaction pins the last committed state, and the daemon commits whole block
windows: outputs created or spent inside the current window live in the
daemon's RAM and are invisible to readers until the window commits. So a
reader's view is always at a block boundary, and `meta.height` read inside the
same transaction names it exactly. (This retires the old SQLite procedure of
stopping the indexer to flush a write-back window; the check below opens one
transaction and holds it for the whole scan.)

**At a height Core can speak for.** `gettxoutsetinfo` measures Core's tip, not
an arbitrary height. Two ways to line them up:

- **With `-coinstatsindex=1` on the node** (the repeatable path):
  `gettxoutsetinfo muhash <height>` answers at any height, so the check runs
  whenever you like against whatever height the index view is at. Costs one
  extra index build on the node, once.
- **Without it:** stop the daemon at the tip (write its stop file,
  `cs_indexer.stop` beside the database), then call `gettxoutsetinfo`. Its
  reply names the `bestblock` it actually measured; if a block arrived
  mid-scan, compare against that height instead and repeat if it moved.

## The check

One sequential cursor pass over the `utxo` sub-database computes every term at
once, inside a single read transaction so the height cannot move under it:

```python
# verify_birth_index.py <path-to-birth.lmdb>
import struct, sys

from claimstones.indexer.birth import decode_runs
from claimstones.indexer.store_lmdb import LmdbBirthStore

GENESIS = bytes.fromhex(
    "4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b")[::-1]

store = LmdbBirthStore(sys.argv[1], readonly=True)
rows = total = excluded = 0
with store.env.begin() as txn:                  # one view, one height
    height = int(txn.get(b"height", db=store.d_meta))
    meta_utxos = int(txn.get(b"utxos", db=store.d_meta))
    for key, value in txn.cursor(db=store.d_utxo):
        n = struct.unpack_from("<I", value)[0]  # script_len header
        script, runs = value[4:4 + n], value[4 + n:]
        sats = sum(c for _, c in decode_runs(runs))
        rows += 1
        total += sats
        if (script and script[0] == 0x6A) or len(script) > 10000:
            excluded += sats                    # Core: IsUnspendable()
        elif key == GENESIS + b"\x00\x00\x00\x00":
            excluded += sats                    # Core omits the genesis coinbase
print("height    ", height)
print("rows      ", rows)
print("meta.utxos", meta_utxos)
print("total sats", total)
print("excluded  ", excluded)
print("expect    ", total - excluded, "== gettxoutsetinfo total_amount * 1e8")
```

Then, on the node:

```
bitcoin-cli gettxoutsetinfo
```

and compare `total_amount` (BTC) against `expect` (sats). `rows` against
`meta.utxos` is a second, weaker check that needs no node at all — it catches
counter drift but not wrong runs.

Expect tens of minutes: the scan is disk-bound, one sequential pass over the
`utxo` sub-database (on the order of 100 GB at the 2026 tip). Against an
overnight rebuild, let alone against trusting a damaged index, that is free.

## Cheap variants, safe to run against the live daemon

Neither is a substitute for the identity above, but both run in seconds and
are worth having when something looks wrong:

- **Forward sample.** Draw N random rows, ask Core `gettxout <txid> <vout>`,
  and check the row's run total equals the output's value. Catches wrong sat
  counts. A row below the index height may have been spent since, so treat a
  null reply as a skip, not a failure.
- **Reverse sample.** Draw N outputs Core says are unspent, and require the
  index to hold them. This is the direction that finds **missing** rows.

The asymmetry is the point, and it is why the 2026-09-18 damage went
unnoticed: sampling the index can never reveal a row that is not there. Only
the reverse direction, or the full identity, can.

## Reading a mismatch

- **Index short of Core** — rows or runs lost. This is the hardware-corruption
  signature. Resuming does not repair it: the undo log reaches back 2016
  blocks and repairs reorgs, not damage. In practice the file is finished and
  a fresh fastbuild replaces it overnight.
- **Index over Core** — extra or double-counted rows, or a spend that was not
  applied.

Either way the figure localises nothing; it only says whether the file can be
trusted. That is the question worth answering before the index becomes
consensus state (`spec/indexer.md`).

## When to run it

- At the end of every full build, before the index is used for anything —
  alongside the bit-for-bit acceptance digest of `spec/fastbuild.md`
  §Verification.
- After any bugcheck, power loss or unclean shutdown — the 09-18 case, where
  the answer would have arrived in minutes instead of five days.
- Before promoting a build to be the one the RPC serves.
- On the land-free snapshot, below, which is the one copy whose height
  matters for years.

## The land-free snapshot

`ACTIVATION_HEIGHT` is still `[decide]` (`spec/constants.md`), and in the code
it is `ACTIVATION_HEIGHT = None` in `protocol/codec.py` — one definition, read
by both indexers and reported by `getindexinfo` as `null`
(`spec/indexer-rpc.md`), and a constant rather than a command-line option,
because a per-run value would defeat the only thing the constant exists for.
While it is `None`, `decipher` is never called and no run is ever marked
`PLAIN`, so **the index is a pure function of the Bitcoin chain**. Two
consequences are worth having: a content hash over its canonical rows means
something, and somebody else's rebuild is a genuine independent witness.

Giving the constant a number ends that permanently. `PLAIN` is not an
annotation on the landed output: the indexer cuts the input run stream
positionally across the outputs, so a `PLAIN` run enters the stream of every
later spend and travels forward with the sats
(`test_land_marks_outputs_plain` asserts exactly this). It cannot be
subtracted out afterwards, and it cannot be recomputed by overlaying a list of
lands without replaying the whole forward walk — which is the indexer again.
After the first land there is no land-free index except a rebuild from
genesis.

**So take a snapshot before activation.** The fastbuild changed its economics
— a rebuild is an overnight run, not a lost week — but not its point: the
snapshot is the pinned, verified, hashed artifact at a known height that later
audits compare against, and restarting from a copy plus a replay of the blocks
since is minutes against hours. One snapshot, taken as late as possible, is
the whole plan: the daemon gates landing on `height >= activation_height`, so
every block committed before the stop is a block a rebuild never has to
replay.

The window closes once. After activation the live database carries `PLAIN`,
and no second land-free snapshot can be taken without running a parallel
land-free build.

### Procedure

1. **Stop the daemon gracefully.** Write the stop file (`cs_indexer.stop`
   beside the database), with a line saying why — it is echoed into the log.
   The daemon finishes the window it is on, commits it and exits; the log ends
   with `stopped at h=<height>`. Record the height. A kill is not a
   substitute: LMDB cannot tear, but a kill loses the blocks since the last
   commit and leaves the height to be discovered instead of chosen.
2. **Copy the environment.** The database directory holds one data file
   (`data.mdb`; `lock.mdb` is scratch and recreated on open). Either copy
   `data.mdb` into a directory named for the height, or write a compacted
   copy, which repacks the tree and drops free pages — a smaller archive with
   identical rows:

   ```python
   import lmdb
   env = lmdb.open("birth.lmdb", readonly=True, max_dbs=4)
   env.copy("birth-landfree-<height>", compact=True)   # target dir: empty, exists
   ```

3. **Verify the copy**, not the original: the identity above against
   `gettxoutsetinfo muhash <height>`, and `rows` against `meta.utxos`.
4. **Hash it.** A digest over the canonical rows — `(txid, vout, script,
   runs)` in key order, which is exactly the `utxo` sub-database's cursor
   order — not over `data.mdb`, whose page layout differs between two correct
   environments (a compacted copy and its source hold the same rows in
   different pages). This is the same digest the fastbuild acceptance gate
   uses (`spec/fastbuild.md` §Verification), and it is the fingerprint that
   makes damage detectable years later. The sat-total identity is not a
   substitute: a total admits compensating errors, one row high and another
   low.
5. **Make it read-only and leave it.** Write-protect the directory at the
   filesystem level. Readers must then open with `readonly=True, lock=False` —
   a plain read-only open still creates `lock.mdb` beside the data, which is a
   write to the artifact.
6. **Restart the daemon**: delete the stop file and start it again; it
   resumes from `meta.height`.
