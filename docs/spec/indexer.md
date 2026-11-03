# Spec 4 — Indexer (draft)

Basis: `src/claimstones/indexer/replay.py` (2026-09-17: single-asset per-outpoint balances, the land step behind an injected valuer, a land-txid table for the double-claim check). Reorg undo is specified below but not yet built (scheduled under hardening, build order step 5, decided 2026-09-17).

Adopted 2026-10-25 (proposed 2026-09-17): step (4) below, the valuation walk, is replaced by a lookup in the forward birth index built by the same replay (`src/claimstones/indexer/birth.py`; bulk build in `fastbuild.md`). The birth index is consensus state alongside the balances, its undo log is the reorg undo, and the walk cutoff left consensus (`land.md` §Open items, closed). Whether a built birth index can be trusted is settled by the identity in `docs/birth-index-check.md`, against the node's UTXO set.

## Determinism

- State = per-outpoint orchid balance in seeds (integer). Nothing else is consensus state; latent value is computed on demand by the walk (`land.md`).
- Input: raw blocks from a full node with txindex, from `ACTIVATION_HEIGHT`.
- Per transaction, in block order: (1) locate the envelope; (2) classify cenotaph or valid; (3) sum input balances; (4) if LAND, run the valuation walk at W(c) and add the amount; (5) apply edicts, pointer, default; (6) on cenotaph, burn everything.
- Two honest indexers must produce byte-identical state at every height.

## Reorg

Undo log per block: outpoints created and spent, with balances. On disconnect, revert; on reconnect, replay. Land amounts are recomputed at the new inclusion block.

## Caches (not consensus)

- Resolved lineage ranges and per-txid heights: chain facts, invalidated only for reorged blocks.
- Work per UTXO for discovery (`indexer-rpc.md`), stored in hashes and converted at W(now) for display.

## Test vectors (generate with the codec)

1. Plain transfer, one edict.
2. Transfer with no envelope: sweep to the first non-OP_RETURN output.
3. Pointer to the change output; unallocated follows the pointer.
4. LAND on pre-existing sats from one birth block; amount matches the formula at W(c).
5. LAND on sats from several birth blocks; one floor.
6. LAND on inputs already landed: void; the transfer part executes.
7. LAND in a coinbase (degenerate land).
8. LAND with fee sats: the fee sats' value goes to the claimer.
9. Cenotaph with LAND: balances and latent value burned; a later land of the outputs is void.
10. Cenotaph without LAND: balances burned; a later land of the outputs is valid.
11. LIFT present: cenotaph.
12. Unknown asset ID in an edict: void edict.
13. Walk cutoff: a dusty old range is dropped; the amount is unaffected.
14. Reorg: the land moves one block; the amount is recomputed; state equals a fresh sync.
15. Runestone in the same transaction: both protocols independent.
