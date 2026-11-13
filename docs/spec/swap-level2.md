# Spec 8 — Level 2 swaps (design notes only, not in release 1)

Goal: buy orchids with sats or sell orchids for sats in one atomic transaction, with the server as order book and matcher, never as custodian.

Notes carried over from the earlier docs:

- Both legs in one transaction with `SIGHASH_ALL` for both parties. `SIGHASH_SINGLE|ANYONECANPAY` listings are safe only on the ask side and only from a UTXO holding exactly the amount offered (see `docs/archive/l3-rune-design.md` §9 note).
- The envelope is authored by whoever builds the transaction; both wallets must decode it and check that they are paid (edicts by output index, pointer for the remainder).
- The server proposes a matched PSBT; each party has `quote_ttl_seconds` to accept; a refusal releases the other party's reservation.
- Fee: percent with a minimum, in orchids, via an edict to the server output as in sends; the sat side can also carry the miner fee.
- A LAND tag in a swap must be refused by both wallets unless explicitly quoted, since it would claim the counterparty's latent value.

Open: order-book API shape, price display (`wallet-policy.md` §Wording applies: amounts, never "value"), partial fills, who pays the miner fee.
