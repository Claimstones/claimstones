# Spec 4 — Indexer

One daemon, two halves, one pass (decided 2026-10-28, specified
2026-11-13). The **birth half** holds every unspent output's runs —
its sats grouped by birth block, with `PLAIN` for sats whose latent
value was already landed. The **ledger half** holds every unspent
output's orchid balance in seeds. Both are written by the same
per-transaction step, from the same input stream, and undone by the
same undo log. This replaces the 2026-09-17 draft, whose state was the
balance alone and whose land step ran a lineage walk; the walk is now
the independent verification procedure (`land.md` §Where the runs come
from), not the consensus rule.

Basis in code: `indexer/birth.py` (`BirthIndexer`, the birth half as
built and in production), `indexer/replay.py` (the 2026-09-17
single-asset allocation engine, ord's rune_updater port, which the
ledger half lifts), `protocol/codec.py` (`decipher`). Allocation rules:
`envelope.md`. Valuation: `constants.md` §Valuation, `land.md`
§Valuation sequence.

## State

Consensus state at height H is the set of unspent outputs, each with:

- `script` — its scriptPubKey (what an address encodes);
- `runs` — ordered `(birth height | PLAIN, sat count)` pairs, FIFO
  over the output's sats, adjacent equal heights merged;
- `balance` — orchid seeds, an integer ≥ 0; absent means 0.

Nothing else is consensus state. Two honest indexers hold
byte-identical state at every height, and the state is fingerprinted
by two digests: the birth digest over `(txid, vout, script, runs)` in
key order (`snapshot_digest.py`, the same digest the fastbuild
acceptance gate and the land-free snapshot use) and the ledger digest
over `(txid, vout, balance)` in key order for every nonzero balance.
Two digests rather than one so that the land-free snapshot's figure
stays comparable with every later birth index.

Reference layout (LMDB, `store_lmdb.py`): `utxo` (outpoint → script +
runs), `addr` (script hash → outpoints), `bal` (outpoint → seeds, only
nonzero), `work` (height → W(h)), `land` (txid → height, amount,
cenotaph), `undo` (per block), `meta`. `bal` is its own sub-database
so the 450 M-row `utxo` table keeps schema 3 and the land-free
snapshot needs no migration.

## Input

Raw blocks from a full node, in height order, from genesis. No
txindex: every input is resolved from the index's own rows, and an
input the index does not have is a fault (`MissingInput`), never a
lookup — a row that is missing is the 2026-09-18 damage signature
(`../birth-index-check.md`), and the daemon stops rather than invent
one.

The block header supplies `nBits`; the chain name comes from the
node's `getblockchaininfo` once and is recorded in the index, which
then refuses a node on another chain. `ACTIVATION_HEIGHT` is the
constant in `protocol/codec.py` for that chain (`constants.md`),
never an option.

## Determinism — the per-transaction step

A block is connected by applying the step to each transaction in
block order, the coinbase last: the coinbase's stream holds the other
transactions' fee tails, and nothing in a block can spend a coinbase
output, so placing it last is unobservable. Per transaction:

1. **Take the inputs.** For each input, in input order, read and
   delete its row. The **input stream** is the inputs' runs
   concatenated in that order; the **pot** is the sum of their
   balances. (A coinbase has no inputs: its stream is `s(h)` sats born
   at `h` followed by the block's fee tails in transaction order, and
   its pot is 0.)
2. **Read the envelope.** If `height ≥ ACTIVATION_HEIGHT` and an
   output script begins `6a 5f`, `decipher` yields an `Envelope`, a
   `Cenotaph`, or nothing (`envelope.md`). Below activation, or with
   `ACTIVATION_HEIGHT = None`, there is no envelope and steps 3 and 5
   reduce to the default rule.
3. **Land.** If the artifact carries LAND — an `Envelope` or a
   `Cenotaph` alike — compute `amount` over the input stream at `W(c)`
   exactly as `land.md` §Valuation sequence (one floor, `c` this
   block). If `0 < amount ≤ u128 max`, add it to the pot; otherwise
   the land is void and the pot is unchanged (`land.md` rule 5,
   `constants.md` §Arithmetic width). Either way, and for a cenotaph
   too, every output run becomes a single `PLAIN` run of the output's
   sat count (rule 3): landedness travels forward with the sats and is
   the double-claim check. The land is recorded (§Derived) and
   `lands` counts it.
4. **Cut the runs.** The stream is cut across the outputs in output
   order by output value (`birth.py` `cut_runs`); the leftover is the
   fee tail that the coinbase will receive. A zero-value output gets an
   empty run list — it is still an outpoint.
5. **Allocate the pot** (`envelope.md` §Edicts and allocation; the
   algorithm is `replay.py` `_index_tx`, ord's rune_updater port):
   - `Envelope`: edicts in order — asset `(0, 0)` only, any other
     asset ID is a void edict; amount 0 = all remaining; output index
     = number of outputs splits across the non-OP_RETURN outputs; an
     edict never allocates more than the pot holds. What remains goes
     to the POINTER output, else the first non-OP_RETURN output, else
     it is burned. Seeds allocated to an OP_RETURN output burn.
   - `Cenotaph`: the whole pot burns, including a landed amount
     (`land.md` §Cenotaph interaction).
   - no envelope: the pot goes to the first non-OP_RETURN output, else
     burns.

   LAND and POINTER do not interact: the landed amount joins the pot
   before the edicts run, and the pointer takes what the edicts leave
   (`test_land_with_pointer_and_edict`: land 500, edict 120 to output
   1, pointer at 2 receives 380). Closed 2026-11-13, `land.md`
   §Closed.
6. **Write the outputs.** One row per output: script, the cut (or
   `PLAIN`) runs, and its allocated seeds if nonzero.

Two honest indexers that apply this step to the same chain produce
the same two digests at every height. The only arithmetic that
discards anything is the single `div` of step 3, and `land.md`
§Valuation sequence makes it independent of how the runs are visited.

## Work

`W(c)` is the input to step 3 and the index computes it itself:

- `w(h) = 2^256 div (target(nBits_h) + 1)`, from the header the block
  arrived in (`constants.md` §Work; `walker.py` `work_from_nbits`);
- `W(h) = W(h − 1) + w(h)`, stored per height in `work`.

An index built from genesis has `W(0) = w(0)`. An index that starts
above genesis — the land-free snapshot resumed with the fused code —
takes `W(start − 1)` once from the header chain it did not replay (the
node's `getblockheader` `chainwork`, which is the same sum), and never
asks again. Agreement with the node's `chainwork` at any later height
is a check the daemon may log, not an input. `work` is a chain fact,
recomputable from headers; it is kept so that landing needs no node
call, reorgs rewrite it by height, and `getlatent` and the planned
`getworktable` (`indexer-rpc.md`) read it instead of the node.

## Reorg

The undo log holds, per connected block, every outpoint the block
touched: a created outpoint (undo = delete it) or a spent one with its
full row — script, runs, balance (undo = restore it). An outpoint
created and spent inside one block cancels out of the log. Disconnect
restores both halves in one pass and deletes the block's `land` rows;
`work` rows above the fork are simply rewritten on reconnect. The log
is kept `undo_depth` blocks deep (1000 at present) and pruned below.

A land that moves to another block is recomputed there with the new
`W(c)` (`land.md` §Finality); vector 14 is the acceptance test, and
the regtest end-to-end reorgs across a land. Counters (`utxos`,
`runs`, `lands`, `minted`, `burned`) are adjusted from the undone rows;
they are reporting, never consensus.

The daemon commits whole block windows, so a reader's view is always
at a block boundary and `meta.height` names it exactly
(`../birth-index-check.md` §When it can run). A crash loses the
uncommitted window and nothing else.

## Derived, not consensus

- `land` — txid → `(height, amount, cenotaph)` for every transaction
  that carried LAND. Never read by the step (the double-claim check is
  `PLAIN`); serves `gettxstatus`, stats and the explorer.
- `work` — see §Work; recomputable from headers.
- Counters in `meta`: `utxos`, `runs`, `txs`, `lands`, `minted`,
  `burned`, `ledger_height` (the height from which balances exist).

## The ledger cannot be computed afterwards

Step 1 deletes spent rows and step 3 overwrites runs with `PLAIN`. A
birth index that passed `ACTIVATION_HEIGHT` without the ledger half
running can therefore never acquire balances: the input streams of the
lands it saw are gone. Balances exist only in an index that ran the
full step from the first envelope on. Operationally: the fused daemon
must be the one following the chain when the activation block
arrives, and the land-free snapshot (`../birth-index-check.md`) is the
state to resume from if it was not — it is a complete state at a
height below activation, so a replay from it with the fused code is a
fresh ledger.

## Test vectors (generate with the codec, never by hand)

Vectors 1–12 and 15 are unit tests on the step with a fixed `W` and
synthetic rows (`tests/test_replay.py` today; they move to the ledger
module with the build). The regtest end-to-end
(`tests/test_regtest_e2e.py`) exercises the marked ones against a
real node.

1. Plain transfer, one edict.
2. Transfer with no envelope: sweep to the first non-OP_RETURN
   output. *(e2e: the claim and collect templates land by this rule —
   claim to the change output, collect to the 330-sat carrier.)*
3. Pointer to the change output; unallocated follows the pointer.
   *(e2e: withdraw.)*
4. LAND on pre-existing sats from one birth block; amount matches the
   formula at W(c). *(e2e: the claimed amount equals `land_amount`
   over the input's runs at the node's chainwork — asserted once the
   ledger serves.)*
5. LAND on sats from several birth blocks; one floor.
6. LAND on inputs already landed: void; the transfer part executes.
7. LAND in a coinbase (degenerate land).
8. LAND with fee sats: the fee sats' value goes to the claimer.
9. Cenotaph with LAND: balances and latent value burned; a later land
   of the outputs is void.
10. Cenotaph without LAND: balances burned; a later land of the
    outputs is valid.
11. LIFT present: cenotaph.
12. Unknown asset ID in an edict: void edict.
13. Walk cutoff is not consensus: the index's amount equals a backward
    walk at `d = 0`; a walk at `d > 0` may return less, never more
    (`land.md` §Walk cutoff). A walker test, not an indexer test.
14. Reorg: the land moves one block; the amount is recomputed at the
    new W(c); state and both digests equal a fresh sync. *(e2e: the
    reorg across the land.)*
15. Runestone in the same transaction: both protocols independent.
