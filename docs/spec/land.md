# Spec 3 — Land validity and lineage (draft)

Authority: manual §Decisions. Supersedes `docs/archive/claimstones.md` §3–§4 and §6 rule 2 wherever they mention W₀ or a launch rate.

## What a land is

A transaction whose envelope carries the LAND tag claims the latent value of **every** input sat, valued at the inclusion block (`constants.md` §Valuation), and adds the amount to the transaction's unallocated balance, which edicts, the pointer, or the default rule then place. The tag itself has no destination field.

Rules:

1. **Authority = spend authority.** Whoever can spend the inputs consented by signing.
2. **All inputs, all-or-nothing.** Granularity is chosen by choosing inputs. Fee sats count: value decouples from sats at the land.
3. **After a land, every output sat is plain.** Latent value is zero on all outputs of a land transaction, wherever the sats go.
4. **Post-land, the token balance is ordinary edict state.**
5. **A land of inputs whose latent value is zero is void** (skipped), not a cenotaph.
6. **Coinbase land.** A LAND tag in a coinbase transaction lands the subsidy sats at lineage length zero, valued at W(b) for that block. Fee sats in the coinbase resolve like any other sats — they keep their lineage and the miner collects their latent value (decided 2026-10-25: the alternative, fee sats arriving `PLAIN`, was built and indexed from genesis to height 345 072 and compared with this one at matched heights; it cut 36-39% of index runs but only ~15% of index size, the effect diluting with height rather than compounding, and it destroys a work-weighted upper bound of 2.71% of the claimable pool instead of transferring it). From halving 33 (height 6 930 000, about 2140) there are no subsidy sats and the block's own work is claimable by nobody; only its fee sats carry anything. Deliberate — `constants.md` §After the last halving.

## Lineage walk (normative)

Ordinal FIFO: inputs concatenate in input order into the input stream; outputs consume it in output order; the leftover tail is the fee and reappears in the block's coinbase after the subsidy sats, in transaction order.

The valuation walk for a sat range terminates at exactly one of:

- **a coinbase**: birth block known; the range contributes to N (`constants.md`); or
- **an earlier land transaction**: value 0. This is the double-claim check; no global claimed-sat set exists.

Coinbase resolution: the first s(h) sats of a coinbase's stream are born at h; the rest are fee tails, resolved by recursing into the block's paying transactions in transaction order.

## Valuation sequence (normative)

A land's amount is a function of its input sats' birth heights and its inclusion block, and of nothing else. The sequence is normative so that every implementation produces the same integer.

1. **Input run stream.** Concatenate the runs of the transaction's inputs, in input order. A run is `(birth height, sat count)`; the `PLAIN` height marks sats whose latent value was already landed.
2. **Drop what carries nothing.** A `PLAIN` run contributes zero. A run whose birth height h has `s(h) = 0` contributes zero (`constants.md` §After the last halving).
3. **Accumulate.** For each remaining run `(h, n)`, with `w`, `s` and `L` as in `constants.md` §Valuation:

        N += n · w(h) · (L div s(h))

   Every term is exact: `L div s(h)` has no remainder, by construction of `L`.
4. **One floor**, with c the inclusion block:

        amount = (A_0 · N) div (W(c) · L)

   This is the only division in the valuation that discards anything.
5. **Void cases.** `amount = 0` makes the land void and not a cenotaph (rule 5). An `amount` above u128 max is void (`constants.md` §Arithmetic width).

**Order is not part of the sequence.** Steps 1–3 add exact integers, so the result does not depend on the order the runs are visited, on how they are grouped by height, or on whether adjacent runs of equal height were merged. Two implementations that disagree about traversal still agree about `amount`. That is what allows a forward index and a backward walk to be the same protocol rather than two approximations of one.

## Where the runs come from

Two procedures, one answer.

- **Forward index.** Every indexer replays the chain once and holds, for each unspent output, its ordered runs. A land is then a lookup: read the inputs' rows and run the sequence above. Nothing is walked at land time. `PLAIN` propagation is the double-claim check — a land rewrites its outputs' runs to a single `PLAIN` run, and since a later spend reads those rows, landedness flows forward with the sats exactly as a birth height does. Adopted 2026-10-25, closing the open question of 2026-09-17.
- **Backward walk.** From each input, follow the ordinal FIFO backwards until every sat range terminates at a coinbase (birth height known) or at an earlier land (contributes zero). Implement iteratively with an explicit stack, memoizing resolved ranges per `(txid, vout, lo, hi)`. The multiset of `(birth height, sat count)` it yields is the index's run stream, so `N` and `amount` are the same.

The walk is normative as an **independent verification procedure**, not as the consensus rule. A node that only walks and a node that only indexes must agree, and they do, because step 3 is order-independent and both see the same sats.

## Walk cutoff (verification only, not consensus)

A walker may decline to expand a range that cannot carry a seed. Let `maxrate(H)` = max over h ≤ H of w(h)/s(h), precomputed per retarget period and compared cross-multiplied; every open range knows a height bound H (the height of the block containing the transaction being walked, since a sat's birth is ≤ H). A range of n sats with bound H carries less than `d` seeds if

    A_0 · n · maxrate(H) < d · W(c)

**A consensus answer requires `d = 0`.** Any positive `d` under-mints — the safe direction — and is a speed-for-accuracy trade each verifier may make for itself. It is not part of the protocol.

This was consensus until 2026-10-25, and the reason it stopped being so is worth keeping. With `d > 0` the walk's answer depends on its own memo tables: `lineage` is keyed `(txid, vout, lo, hi)` with neither `d` nor `W(c)` in the key, and `_expand` consults it before the cutoff runs, so a range that some earlier walk happened to resolve comes back exact and the cutoff never tests it. A node with a warm cache then mints more than a freshly synced one for the identical land. Reordering the cutoff ahead of the memo does not fix it: the damaging memo sits on the root range, and a root frame has no bound. At `d = 0` the problem dissolves — every range is expanded, the memo tables are a pure speed-up, and warm and cold walks agree exactly.

Measured on `e4f6f9ef4d82c9b040fa2f35f57a9b39bf6b59e6bc174e9c4d9db90547da791f:0` (19 runs, heights 44 686–399 795, 199 870 160 sats, at W(973 058)): the index values it at 929 seeds; the walk at `d = 1` returns 927, dropping 179 402 057 sats; at `d = 1/2` it returns 929. At `d = 0` warm and cold agree exactly. The gap at `d = 1` is the cutoff working as designed, and is precisely why it cannot be consensus.

## Independent validation

Three tiers, as discussed 2026-10-09. Landing itself needs none of them: LAND carries no amount, and both the valuation and the double-claim check are local to the inputs, so a land is self-sovereign given a transaction source.

1. **Index server.** What release-1 wallets use.
2. **Backward balance trace plus the walk.** Trustless self-validation of orchids received: every balance path terminates at a land or a coinbase, and each land is re-verified by walking it at `d = 0`. Cost grows with the coins' token history — practical at launch, unbounded after long circulation.
3. **Sender-carried trace** (client-side-validation, RGB-like). A deferred design option, not release 1.

## Cenotaph interaction

- Cenotaph **with** LAND: the inputs' latent value is landed and burned together with all input balances. Sats become plain.
- Cenotaph **without** LAND: latent value is untouched (the walk sees no land); token balances burn.

## Finality

Consensus needs none: valuation is a function of the inclusion block. Policy: wallets and servers treat a land as settled after `LAND_FINALITY` confirmations. A reorg that moves the land to a different block changes W(c) and thus the amount; the indexer's reorg undo replays it.

## Mixed outputs

Outputs holding both orchid balance and unlanded sats are normal; every payment output has carrier sats. No rule is needed; wallet coin control distinguishes the states (`wallet-policy.md`).

## Open items

None.

## Closed

- **LAND and POINTER** (opened 2026-09-17: confirm there is no interaction when a land also spends orchid-bearing inputs). Closed 2026-11-13: none. The landed amount joins the unallocated pot before the edicts run, and the pointer receives what the edicts leave, whether that came from inputs or from the land — `indexer.md` §Determinism step 5; in code `replay.py` `_index_tx` steps (4) to (6), tested by `test_land_with_pointer_and_edict` (land 500, edict 120 to output 1, pointer at 2 receives 380).

- **Verification cost of deep lineages** (was [decide before mainnet], opened 2026-09-17). Closed 2026-10-25 by adopting the forward birth index: a land is a lookup, so no walk happens at land time and the hop count stops mattering for consensus. The alternatives considered and not taken were evidence-carrying lands (the wallet supplies the lineage, the indexer verifies it) and a per-land hop budget above which the land is void. A verifier that chooses to walk still pays the hop cost, which is why the cutoff survives as a verification parameter.
- **Walk cutoff versus the forward index** (was [decide with the index]). Closed 2026-10-25. A forward index cannot reproduce a cutoff defined on the backward walk, so one of them had to stop being consensus. The cutoff did: valuation is now exact, N over all runs with one floor per land, and `DUST_SEEDS` is a walker parameter with no consensus role. See §Walk cutoff and §Independent validation.
