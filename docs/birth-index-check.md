# Birth index integrity check

The birth index is the one file in this project that cannot be re-derived cheaply: a
full build is seven to eight days (measured 2026-10-24: ~0.5 blk/s near the tip,
and 47% of the chain's 1.47 billion transactions sit above height 763 000). It is also the file most exposed to silent damage, and
SQLite's own tools cannot see that damage — on 2026-09-18 a `FAULTY_HARDWARE_CORRUPTED_PAGE`
bugcheck removed rows from `birth.sqlite`, `PRAGMA quick_check` passed, and the loss
only surfaced five days later at block 629 743 as a `MissingInput`. The file was
well formed; the rows were simply wrong.

So the index has to be checked against an independent witness. The node's UTXO set is
that witness, and the check is one arithmetic identity.

## The invariant

Every sat in an unspent output is covered by exactly one run. For the index at
height H:

    Σ (run sat counts over all rows)
      − sats in outputs Core treats as provably unspendable
      − the genesis coinbase (5 000 000 000 sats)
      = Core's gettxoutsetinfo total_amount at H

The two subtractions are not fudge factors; they are outputs the index holds on
purpose and Core's chainstate never does:

- **Provably unspendable.** `BirthIndexer._place` stores every output of every
  transaction, because a zero-value or unspendable outpoint is still an outpoint.
  Core applies `IsUnspendable()` and keeps these out of the UTXO set entirely:
  a script whose first byte is `OP_RETURN` (`0x6a`), or a script longer than
  `MAX_SCRIPT_SIZE` (10 000 bytes). Both are computable from the index's own
  `script` column, so the correction needs no outside data.
- **The genesis coinbase.** Core never adds it. The index does — verified on the
  mainnet build: `runs=[(0, 5000000000)]`, P2PK script.

BIP-30's duplicate coinbases (heights 91 842 and 91 880) need no correction: the
index overwrites by primary key at exactly those heights (`BIP30_HEIGHTS`), which
leaves one instance, matching Core.

## When it can run

**With the indexer stopped, or immediately after a commit.** Outputs created and
spent inside the write-back window live in `store.hot` and are not in the table
yet; the `unflushed` figure on each progress line says how many. A check run against a
mid-window index will report a shortfall that is not damage.

**At a height Core can speak for.** `gettxoutsetinfo` measures Core's tip, not an
arbitrary height. Two ways to line them up:

- **With `-coinstatsindex=1` on the node** (the repeatable path):
  `gettxoutsetinfo muhash <height>` answers at any height, so the check runs
  whenever you like against whatever height the index is at. Costs one extra
  index build on the node, once.
- **Without it:** stop the indexer, read Core's tip T, run the indexer with
  `--end T` so it lands exactly on T, then call `gettxoutsetinfo`. Its reply names
  the `bestblock` it actually measured; if a block arrived mid-scan, compare against
  that height instead and repeat if it moved.

## The check

Run with the indexer stopped. One full scan of the `utxo` table computes every
term at once:

```python
# verify_birth_index.py
import sqlite3, sys
sys.path.insert(0, r"C:\apps\claimstones\src")
from claimstones.indexer.birth import decode_runs

GENESIS = bytes.fromhex(
    "4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b")[::-1]
db = sqlite3.connect("file:C:/apps/claimstones/birth.sqlite?mode=ro", uri=True)

rows = total = excluded = 0
for txid, vout, runs, script in db.execute(
        "SELECT txid, vout, runs, script FROM utxo"):
    n = sum(c for _, c in decode_runs(runs))
    rows += 1
    total += n
    if (script and script[0] == 0x6A) or len(script) > 10000:
        excluded += n                      # Core: IsUnspendable()
    elif txid == GENESIS and vout == 0:
        excluded += n                      # Core omits the genesis coinbase
print("rows      ", rows)
print("meta.utxos", db.execute(
    "SELECT value FROM meta WHERE key='utxos'").fetchone()[0])
print("total sats", total)
print("excluded  ", excluded)
print("expect    ", total - excluded, "== gettxoutsetinfo total_amount * 1e8")
```

Then, on the node:

```
bitcoin-cli -datadir=D:\Bitcoin_core gettxoutsetinfo
```

and compare `total_amount` (BTC) against `expect` (sats). `rows` against
`meta.utxos` is a second, weaker check that needs no node at all — it catches
counter drift but not wrong runs.

Expect tens of minutes: the scan is disk-bound over every row (55.5 M rows and
22 GB at height 490 000). Against a week-long build that is free.

## Cheap variants, safe to run mid-build

Neither is a substitute for the identity above, but both run in seconds against a
live index and are worth having when something looks wrong:

- **Forward sample.** Draw N random rows, ask Core `gettxout <txid> <vout>`, and
  check the row's run total equals the output's value. Catches wrong sat counts.
  A row below the index height may have been spent since, so treat a null reply as
  a skip, not a failure.
- **Reverse sample.** Draw N outputs Core says are unspent, and require the index
  to hold them. This is the direction that finds **missing** rows.

The asymmetry is the point, and it is why the 2026-09-18 damage went unnoticed:
sampling the index can never reveal a row that is not there. Only the reverse
direction, or the full identity, can.

## Reading a mismatch

- **Index short of Core** — rows or runs lost. This is the hardware-corruption
  signature. Resuming does not repair it: the undo log only reaches back 2016
  blocks, so in practice the file is finished and the build restarts from 0.
- **Index over Core** — extra or double-counted rows, or a spend that was not
  applied.

Either way the figure localises nothing; it only says whether the file can be
trusted. That is the question worth answering before the index becomes consensus
state (`spec/indexer.md`).

## When to run it

- At the end of every full build, before the index is used for anything.
- After any bugcheck, power loss or unclean shutdown during a build — the 09-18
  case, where the answer would have arrived in minutes instead of five days.
- Before promoting a build to be the wallet's source of truth.
- On the land-free snapshot, below, which is the one copy that has to stay
  trustworthy for years.

## The land-free snapshot

`ACTIVATION_HEIGHT` is still `[decide]` (`spec/constants.md`), and in the code it is
`ACTIVATION_HEIGHT = None` in `protocol/codec.py` — one definition, read by both
indexers and advertised by the server, and a constant rather than a command-line
option, because a per-run value would defeat the only thing the constant exists for.
While it is `None`, `decipher` is never called and no run is ever marked `PLAIN`, so
**the index is a pure function of the Bitcoin chain**. Two consequences are worth
having: a content hash over its canonical rows means something, and somebody else's
rebuild is a genuine independent witness — which, on non-ECC memory with no UPS, is
the only independent witness available.

Giving the constant a number ends that permanently. `PLAIN` is not an annotation on
the landed output: `_place` cuts the input run stream positionally across the outputs,
so a `PLAIN` run enters the stream of every later spend and travels forward with the
sats (`test_land_marks_outputs_plain` asserts exactly this). It cannot be subtracted
out afterwards, and it cannot be recomputed by overlaying a list of lands without
replaying the whole forward walk — which is the indexer again. After the first land
there is no land-free index except a rebuild from genesis.

**So take a snapshot before activation.** A copy of the database stopped cleanly at any
committed height below activation is land-free by construction, and the indexer resumes
from `meta.height`, so a land-free index at any later date is that copy plus a replay of
the blocks since — roughly a day and a quarter per year of chain at current rates,
against seven to eight days from genesis.

The window closes once. After activation the live database carries `PLAIN`, and no
second land-free snapshot can be taken without running a parallel land-free build. One
snapshot, taken as late as possible, is the whole plan — `_place` gates on
`height >= activation_height`, so every block committed before the stop is a block the
rebuild never has to replay.

### Procedure

1. **Stop the build gracefully.** Write the stop file (`D:\claimstones\logs\birth.stop`),
   with a line saying why — it is echoed into the log. The indexer finishes the block it
   is on, commits it and exits; measured at 49 s. A kill is not a substitute: it loses
   the blocks since the last commit, and a kill during a write is how a torn file
   happens.
2. **Confirm it closed cleanly.** The run's `.err` file must be empty, the log must end
   with `stopped at <h>`, and `birth.sqlite-wal` and `birth.sqlite-shm` must be gone.
   Record the height.
3. **Checkpoint if a WAL survived.** `PRAGMA wal_checkpoint(TRUNCATE)` — about 92 s on a
   30 GB database — then remove the sidecars, so the snapshot is one file. A database
   plus its WAL is two files that have to stay together forever.
4. **Copy it**, with the height in the name: `birth-landfree-<height>.sqlite`.
5. **Verify the copy**, not the original: the identity above against
   `gettxoutsetinfo muhash <height>`, and the row count against Core's `txouts`. Both
   matched exactly at height 644 000.
6. **Hash it.** A digest over the canonical rows — ordered by `(txid, vout)`, each row
   in the existing fixed-width run encoding — not over the SQLite file, whose free pages
   and layout differ between two correct databases. This is the fingerprint that makes
   damage detectable years later, and the sat-total identity is not a substitute: a
   total admits compensating errors, one row high and another low.
7. **Make it read-only and leave it.** Opening a WAL-mode database read-write creates
   `-wal` and `-shm` just by opening it, which is a write to the artifact. Either clear
   the write bit at the filesystem level, or open it only as
   `file:...?mode=ro&immutable=1`, or set `journal_mode=delete` on the copy once so
   reads never need a sidecar.
8. **Restart the build**: delete the stop file; the scheduled task picks it up within
   five minutes.
