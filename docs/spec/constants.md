# Spec 1 — Constants and arithmetic (draft)

Authority: `docs/release-1-scope.md` (decided 2026-09-16). Marked **[decide]** where a value is still open.

## Monetary constant

| name | value | meaning |
|---|---|---|
| `SEEDS_PER_ORCHID` | 100 | display convention; the ledger counts seeds |
| `A_0` | 30 000 000 000 000 seeds (3e13) | about 1 orchid (108 seeds) per kWh of electricity at 10 J/TH at 2026 chainwork. Decided 2026-10-09, replacing the 5.6e13 of 2026-09-16: puts everyday prices in a two-digit orchid range at 2026 production cost, and anchors the orchid near one kWh of mining electricity |

There is no W_0 and no launch height in the monetary rule.

## Work

- `w(h)` = expected hashes of block h, computed from nBits exactly as bitcoin computes chainwork: `w = 2^256 div (target + 1)`. Decode nBits directly (mantissa/exponent); never use the float `difficulty` field.
- `W(c)` = cumulative chainwork including block c, i.e. the sum of w(h) for h ≤ c. Equals Bitcoin Core's `chainwork` field for block c.
- `s(h)` = block subsidy in sats: `5_000_000_000 >> (h div 210000)`, a **truncating** shift as in bitcoin, zero from halving 33 (height 6 930 000) because `5_000_000_000 < 2^33`. So `s(h)` takes exactly 33 nonzero values, and from halving 10 they are not of the form `5^10 · 2^k`: `s = 4 882 812 = 2^2 · 3 · 11 · 71 · 521`.

## Valuation

For a land included in block c, with input sats grouped by birth block h_i (n_i sats each):

    amount(seeds) = floor( A_0 · Σ_i n_i · w(h_i) / s(h_i)  /  W(c) )

`w(h)/s(h)` is a block's work shared out among the sats it created, and it is usually fractional — `w(1)/s(1) = 0.859` hashes per sat — so it cannot be evaluated in integers directly. Scale it by

    L = 883 562 550 452 072 936 642 907 668 473 515 000 000 000
      = 2^9 · 3^2 · 5^10 · 7^2 · 11 · 13 · 17 · 23 · 37 · 53 · 71 · 149 · 173 · 223 · 313 · 521 · 2879 · 19073

the least common multiple of the 33 nonzero subsidies. `L div s(h)` is therefore exact for every one of them, with no remainder to specify. Compute

    N = Σ_i n_i · w(h_i) · (L div s(h_i))
    amount = (A_0 · N) div (W(c) · L)

with arbitrary-precision integers. Every term of N is exact and N is an integer sum, so no range is rounded on its own and the `div` in `amount` is the **only** floor in the valuation. `L` cancels and does not affect the amount; it exists so that nothing is discarded before the sum. Ranges dropped by the walk cutoff (`land.md`) contribute nothing to N.

Adopted 2026-10-25, replacing `D = 5_000_000_000 · 2^33`. `D` was documented as making `D / s(h)` an integer for every `s(h) > 0`; it does not — it is exact for 13 of the 33 subsidies (halvings 0–9, plus the tail values 4, 2, 1), and truncates for halvings 10–29, heights 2 100 000 to 6 299 999. The shift bought no divisibility at all (`5_000_000_000` alone is exact for the same 13, since the pre-halving-10 subsidies are nested), only about 33 bits of accuracy in a quotient that was never exact: worst relative error 5.7e-15. That error was deterministic and so could not split consensus, but the spec's claim of exactness invited an implementer to evaluate the rule above as an exact rational — which is what this section describes — and so to disagree with the reference. `L` makes the claim true instead of qualifying it. Amounts are unchanged for every height on the chain today: below halving 10 both constants divide exactly, so `N/L` and `N/D` are the same rational and floor alike.

## After the last halving

`s(h) = 0` from height 6 930 000 (halving 33, about the year 2140 — the chain has averaged 9.63 min per block since genesis, so the nominal schedule is the late estimate). Those blocks mint no sats, so their work has no carrier: `w(h)` enters `W(c)` and no land can ever claim it. **Normative: a birth height at or above 6 930 000 contributes nothing to `N`.** A coinbase LAND in block 6 929 999 mints `A_0 · w(h) / W(c)`; the same transaction one block later mints zero.

Two consequences, both accepted:

- The identity that makes `A_0` the supply holds only up to 6 929 999. Block h mints exactly `s(h)` sats, each carrying `w(h)/s(h)`, so `Σ (every sat's share) = Σ_h w(h) = W` — a single land of the whole sat supply at `W(c)` mints exactly `A_0`. After halving 33, `W(c)` keeps growing against a frozen numerator, so the claimable share of `A_0` decays toward zero. (Issuance was never hard-capped at `A_0` in the other direction either: lands happen at different `W`, and landing every sat in its own birth block would have minted about 45 × `A_0` on the chain to date. Early landing is paid for out of later landers' share, which is the intended incentive.)
- The carrier thins before it disappears. Halving 30 (height 6 300 000, ~2128) mints 4 sats per block, halving 31 two, halving 32 one. For the ~630 000 blocks between 2128 and 2140 a single satoshi carries an entire block's work, and burning it destroys that work permanently.

Decided 2026-10-25: leave both alone. Nothing is claimable after halving 33, and nothing special happens at halvings 30–32. Two alternatives were considered and rejected for now:

- **B** — a coinbase LAND claims `w(h)` for its own block directly, with no sats in the calculation. Numerically this is already what a coinbase LAND does (`s(h)` sats × `w(h)/s(h)` = `w(h)`), so it reads as one rule with two carriers. It needs a per-height double-claim marker, because with no sats there are no runs to mark `PLAIN`, and it is use-it-or-lose-it: no carrier means nobody downstream can land it later.
- **C** — rebirth the coinbase output sats at `h`, taking `s(h)` as the coinbase's total output value. Keeps a real, spendable, later-landable carrier and preserves the identity, but overwrites the fee sats' own lineage (which `land.md` §Coinbase land keeps), would be the first rule under which a sat can *gain* value, and still fails for a zero-fee block.

The deciding argument is forward compatibility rather than the 113 years of notice. A is the only one of the three that stays open in both directions: a later generation can add B or C without invalidating anything minted under A, whereas adopting B or C now could not be retracted once holders had priced it in. Choosing the conservative base is therefore not a deferral of the decision but the one choice that preserves it.

## Arithmetic width

`A_0 · N` exceeds u128 (`L` is 140 bits, so N runs to roughly 200). Normative: intermediates at arbitrary precision; only wire-encoded values (edict amounts) are bound to u128. A land whose computed amount exceeds u128 max is void (decided 2026-10-09: void, not cap — a cap would mint a wrong amount silently); unreachable with these constants.

## Non-monetary constants

| name | value | meaning |
|---|---|---|
| `ACTIVATION_HEIGHT` | **[decide]** per network | indexers ignore envelopes before it; exists only so all indexers start from the same state. Deliberately left open 2026-10-09: all mainnet activity so far (the block-967875 land, the sponsor pool lands) counts as testing and need not be below the final value. In code it is `ACTIVATION_HEIGHT = None` in `protocol/codec.py` — one definition, read by both indexers and reported by `getindexinfo` as `null` (`indexer-rpc.md`), and a constant rather than a flag, since a per-run option would defeat the only thing it exists for. **Fixing the value is irreversible for the birth index: take the land-free snapshot first** (`../birth-index-check.md`). `PLAIN` propagates forward through every later spend, so after the first land only a rebuild from genesis — an overnight fastbuild run (`fastbuild.md`) — recovers a land-free index. **Decided 2026-11-11 for test chains:** testnet, signet and regtest are active from height 1 (`codec.activation_height(chain)`, the chain taken from the node's `getblockchaininfo` and recorded in the index, never a flag) — a test network exists to exercise the protocol, and a land that cannot count tests nothing. The [decide] that remains is mainnet's number |
| `LAND_FINALITY` | 6 | confirmations before a wallet or server treats a land as settled (policy floor, not consensus). Decided 2026-09-21: kept at 6; a claim is a one-time operation so a few extra minutes of "pending" is not a cost worth trading against reorg safety, and coinbase sats cannot be spent (so cannot be landed) before their own 100-block maturity regardless. |
| `DUST_SEEDS` | 1 | default walk cutoff for a verifying walker (`land.md` §Walk cutoff). **Not consensus** since 2026-10-25: the valuation is exact over all runs, and a consensus answer requires the cutoff off (`d = 0`). A positive value only under-mints, and trades accuracy for walk speed at the verifier's own choice |

## Reference numbers (2026-08-14, height 962424)

| quantity | value |
|---|---|
| W(tip) | 9.8846e28 hashes |
| w(tip) | 5.48e23 hashes |
| seeds per block | 1.66e8 (1.66e6 orchids) |
| seeds per 2026 subsidy sat | about 0.53 |
| decay of unclaimed value | −5.5e-6 per block, about −2.8 % per month |
