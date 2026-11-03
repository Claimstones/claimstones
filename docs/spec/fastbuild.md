# Spec 5 — Fast birth index build (draft)

**Status:** decided 2026-10-26: the full lookahead rebuild replaces the slow
forward replay for building from genesis ("a slow build is a showstopper for onboarding
new users"). Spill records fixed-width, serving store LMDB, numpy-first
arithmetic with a measured Rust trigger. The incremental
tip-following design of `indexer/birth.py` is unchanged and takes over at the tip;
this spec covers only the bulk build. Draft until the first phase lands.

## Why: what the measurements said

The 2026-10 master build was profiled and instrumented at height ~710 000.
Three findings drive this design:

1. **Random lookups dominate.** ~6 100 inputs per block each probe a 125 M-row
   mutable store. With a small WAL a probe costs 196 µs; against the 9 GB WAL that
   the 1 000-block commit windows spill, 1.65 ms — CPU inside SQLite's WAL-index
   search, with the NVMe idle. This is what dragged throughput from 1 765 tx/s
   (freshly checkpointed) to ~440 over days.
2. **Per-fragment Python is the floor.** With the WAL small, ~70 % of wall time is
   the run machinery: `cut_runs` 102 s, 648.8 million `list.append` calls (~3 055
   per tx), decode/encode 129 s, in a 396 s / 100-block profile. Flow-weighted
   stream lengths are ~100× the 13-runs/UTXO set average: consolidation
   transactions dominate the flow.
3. **At 50× speed the block source becomes the wall.** RPC fetch + hex is
   16 ms/block and parse 19 ms/block, trivial at 0.3 blk/s, the limit at 30 blk/s.

The design removes all three *structurally*: no lookups anywhere (the future is
known in advance), runs stay packed end to end (vectorized arithmetic), blocks come
from `blk*.dat` through a process pool.

## The central idea

A full replay is not a live system: every output's fate is already on disk. Learn,
before replaying, the chain position at which every output is spent. Then the
replay writes each output's runs once, sequentially, into the file for the epoch in
which it dies, and reads each file once, sequentially, when that epoch arrives.
Random I/O and hash lookups are not optimized — they are absent.

Three phases, each sequential, parallel, and restartable at its boundary; then the
existing incremental daemon follows the tip.

## Phase 0 — block source

- Scan `blk*.dat` once; record `(block hash → file, offset)`. Chain order comes
  from one batched `getblockhash` sweep against the node (the node stays the
  authority on the main chain, exactly as today).
- A reader streams blocks in height order; a **process pool** parses them
  (parsing is CPU-bound; threads share the GIL). Phases 0–1 use a slim parse:
  txid, input outpoints, output values and scripts, envelope presence — no
  witness decode.
- Phase 2 re-reads blocks through the same reader (values, scripts and tx
  structure are cheaper to re-read sequentially than to carry through the sort).

## Phase 1 — resolve: dense ids and death positions

Every output ever created gets a **creation id**: its ordinal position in chain
order (block, then tx, then vout) — a uint64, ~3.3 billion at the 2026 tip. The
phase is one big equi-join of creations against spends, done as a classic
partitioned hash join, all sequential I/O:

1. **Emit** two streams while scanning blocks: creation records and spend records
   (formats below).
2. **Partition** both by the top bits of the txid into 256 bucket file pairs
   (sequential appends, buffered).
3. **Join** each bucket in RAM, buckets in parallel across cores. Output per
   match: `(creation_id, spend_seq, spend_height)`, where spend_seq is the global
   input ordinal (block, tx, input position).
4. **Sort twice** (external merge, sequential):
   - by spend_seq → the **resolved-spends stream**: for each input of each tx of
     each block, in order, the creation id it consumes. This replaces every
     `take()` of the current design.
   - by creation_id, keeping spend_height → the **death array**:
     `death_height[creation_id]` as a flat uint32 file, unspent = 0xFFFFFFFE.
     Also emitted here: the **death histogram** (deaths per 1 000-block bin),
     which phase 2 uses to cut epochs that fit RAM.

BIP30 (heights 91 842 / 91 880): the duplicated coinbase txids appear twice as
creations of the same outpoint. Rule, mirroring today's `INSERT OR REPLACE`: a
spend joins to the latest creation that precedes it; the overwritten creation is
marked dead at the overwriting height with no spend. Handled as a named special
case in `resolve.py`, tested with those two heights.

## Phase 2 — replay with lookahead

Replay blocks in order. The run arithmetic is semantically identical to today
(the settled model: positional FIFO `(height, count)` runs, adjacent-only merge,
fee tails concatenated into the coinbase stream, unclaimed subsidy never born,
LAND collapse when ACTIVATION_HEIGHT is set). What changes is where bytes live:

- **Inputs**: the resolved-spends stream supplies each tx's input creation ids.
  The runs for an id are in the current epoch's RAM table — an array indexed by
  id offset, not a hash map — populated by outputs born this epoch plus the
  epoch's spill file loaded at its start.
- **Outputs**: each new output's death height is `death_height[creation_id]`,
  read as the array streams by. Route by death epoch:
  - dies this epoch → RAM table;
  - dies in later epoch e → append to spill file e;
  - never dies → append to the **final stream**: the finished index, written once.
- Epoch boundary: free the table, bulk-load spill e+1. Epoch lengths are cut
  adaptively from the death histogram to a ~20 GB RAM budget — never guessed.

Every byte is written once and read once, in order. No store in the loop, no
undo log (a crash restarts the phase from its last epoch checkpoint).

**Arithmetic core** (`runcore.py`, shared with the tip daemon): runs stay packed
(uint32 height / uint64 count pairs, today's schema-3 encoding) end to end.
Concatenation, cutting by output values, and boundary merge are numpy kernels —
`concatenate`, `cumsum`, `searchsorted`, slice — with the plain-tuple path kept
for streams under ~64 runs where numpy overhead loses. The profile says flow is
dominated by huge streams, numpy's best case.

**Rust trigger** (decided 2026-10-26): the
numpy kernel is benchmarked against real blocks first. If it sustains under
~20 M run-fragments/s, or the measured phase-2 projection for the full chain
exceeds 12 h, the kernel — and only the kernel — is ported behind the same
`runcore` interface (pyo3). Nothing else is allowed to depend on the choice.

**Measured 2026-10-29** (phase 2 over blocks 0..400 000, 3607 s): the kernel
sustains 32.6 Mfrag/s — the throughput bar passes. The full-chain projection
(~12–20 h) likely breaches, but the py-spy profile voids this trigger's
remedy: the kernel is 40 % of wall time, block parsing is ~51 % (`parse_tx`
39.7 % self), so a kernel-only port caps at ~1.5x. Decision (2026-10-29):
**no Rust for release 1** — the 710 222 gate projects to
~7.5–10.5 h on numpy, inside budget. If a tip-height build ever needs to be
faster, the lever is the replay-side parse, which amends this spec.

**Unspendable outputs** (OP_RETURN and friends): stored exactly as today
(an outpoint row with its script and its — usually empty — run list), so the
verification digests against the existing index match row for row.

## Phase 3 — load, and the tip daemon on LMDB

The final stream arrives ordered by creation id; one external sort by (txid,
vout) makes the bulk load append-ordered. The serving store is **LMDB**
(decided 2026-10-26), one environment, sub-databases:

- `utxo`: key txid(32)+vout(4 BE) → script_len(u32) + script + runs (schema-3
  pairs). LMDB bulk-loads sorted keys at sequential speed (`MDB_APPEND`).
  (Amended 2026-11-01, with the final-stream record: script_len was u16
  until the first tip-height build hit a >64 KiB scriptPubKey at height
  ~870–880k — legal on mainnet since the 2025 uncapped-OP_RETURN change.)
- `addr`: key sha256(script) → outpoint, dupsort — the address table, built in
  the same pass from a second sort of the final stream. (Amended 2026-10-29
  from raw script: scripts are unbounded and may be empty, LMDB keys are
  neither; lookups are exact-script, so the wallet hashes the query.)
- `undo`, `meta`: the tip daemon's undo log and counters, as today but as
  sub-databases.

From the tip onward the **existing incremental logic takes over** (hot dict,
undo, reorg disconnect), re-pointed at LMDB through the same store interface
`birth.py` already isolates. One block per ten minutes needs correctness, not
speed. LMDB's single-writer/many-reader model fits the daemon + read-only
wallet server exactly; no WAL, no checkpointing, readers never block the writer.

**Map size (learned 2026-11-02, on a trial copy):** the bulk load packs every
B-tree page 100% full (`MDB_APPEND`), and txids are uniform, so the daemon's
first months of random inserts split nearly every leaf they touch — ~40 MB of
genuinely new pages per block until the tree relaxes toward LMDB's ~70%
steady-state fill, an asymptote near 1.4x the loaded size. The daemon opens
the environment at **200 GiB** (`--map-gb`), not the load's 120. Hitting
`MDB_MAP_FULL` anyway is harmless and has a one-step runbook: the failed
transaction aborts (the db stays at the last committed block), restart with a
larger `--map-gb` and it resumes — proven on the trial copy at h=973 831.

## File formats (fixed-width, decided 2026-10-26)

All integers little-endian. "Fixed-width" means fixed-width fields with
self-describing counts; run pairs are today's 12-byte schema-3 encoding.

| file | record |
|---|---|
| creation (bucketed) | txid 32 + vout u32 + creation_id u64 = 44 B |
| spend (bucketed) | txid 32 + vout u32 + spend_seq u64 + spend_height u32 = 48 B |
| join output | creation_id u64 + spend_seq u64 + spend_height u32 = 20 B |
| resolved-spends stream | creation_id u64 per input, chain order = 8 B |
| death array | death_height u32, indexed by creation_id = 4 B |
| epoch spill | creation_id u64 + run_count u32 + run_count × 12 B |
| final stream | creation_id u64 + vout u32 + txid 32 + script_len u32 + script + run_count u32 + runs |

Volumes at the 2026 tip (~3.3 B outputs, ~3.2 B spends): buckets ~290 GB, join
output ~64 GB, resolved stream ~26 GB, death array ~13 GB, spills bounded by
total runs (~20 GB live at any height, written across epochs), final stream
~30 GB. **Peak scratch ~450 GB**; phases delete their inputs as they complete.
The scratch disk may be any fast disk, not necessarily the chain's.

## Verification

The abandoned master build is the reference: independent code, known-good at
**height 710 222** (committed clean, WAL checkpointed away, 2026-10-26).
Acceptance: fast-build to 710 222 → identical utxo count, run count, run bytes,
and a content digest over every (txid, vout, script, runs) row. Plus the
`gettxoutsetinfo muhash` outpoint/sat identity of `docs/birth-index-check.md`
at checkpoints, and the backward lineage walker as the per-UTXO spot check.
Same bit-for-bit index or it does not ship.

## Module layout

```
src/claimstones/indexer/
  birth.py            # tip daemon + store interface (unchanged semantics)
  runcore.py          # shared run arithmetic: packed arrays, concat/cut/merge
  fastbuild/
    __init__.py       # orchestrator: phases, manifests, resume
    blocks.py         # blk*.dat scan, height ordering, process-pool parse
    resolve.py        # phase 1: emit, partition, join, two sorts, histogram
    spill.py          # record formats above; epoch files; final stream
    replay.py         # phase 2: lookahead replay
    load.py           # phase 3: LMDB bulk load, addr table, tip handover
```

Each phase ends at a named on-disk artifact with a manifest (inputs, parameters,
digest); a crash resumes at the last completed phase.

## Build order

1. **`runcore.py`** — testable against today's `cut_runs` on real blocks
   (bit-identical outputs over a replayed range), and immediately benchmarkable:
   this run decides numpy vs Rust before anything depends on it.
2. **Phase 0 + 1** to height ~400 000 with timings; validates the join, BIP30,
   and the volume estimates at a tenth of the cost.
3. **Phase 2** over the same range; verify against the master build's history
   of report lines (utxo/run counts at matched heights).
4. Full run to 710 222; the bit-for-bit acceptance gate.
5. **Phase 3 + tip handover**; retire the SQLite store; the wallet server reads
   LMDB read-only.

## Budgets (estimates to be replaced by step-2 measurements)

| | target | honest worst case |
|---|---|---|
| phase 0+1 | 1–2 h | 4 h |
| phase 2 | 1–3 h | 6 h |
| phase 3 | < 30 min | 1 h |
| total | **3–5 h** | 12 h (the Rust trigger) |
| RAM peak | ~20 GB | epoch size is the lever |
| scratch disk | ~450 GB free | may be a separate disk |

What this buys beyond speed: the land-free snapshot stops being precious
(any state is recomputable overnight, so setting ACTIVATION_HEIGHT is no longer
guarded by one backup); a new wallet-server operator bootstraps next to their own
node in an afternoon, or verifies a downloaded snapshot by rebuilding it —
"check my index", not "trust my index"; new gardens and schema changes are cheap.
