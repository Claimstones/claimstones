# Release 1 scope — Orchids on Bitcoin

**Status:** decided 2026-09-16. This document and the specification set under `docs/spec/` are the authority for release 1; where an older document disagrees, these win.

## What release 1 is

One token (orchids, 100 seeds each) on one garden (bitcoin), one valuation rule (claim-time W, one constant A_0), a self-custodial reference wallet (a CLI), and the indexer with its RPC: the daemon keeps the index at the tip and serves it read-only in bitcoind's idiom. Claimstones is the core stack: wallets are front ends, any number of them can be made against the indexer RPC — or against a bitcoin node alone (`spec/indexer-rpc.md`, the node-only guarantee) — and this repository carries one. Signet and regtest first; mainnet only after the safety checklist in `spec/wallet-policy.md` is green.

## In

| area | included |
|---|---|
| protocol | single asset, constants hard-coded; envelope `OP_RETURN OP_PUSHNUM_15`; tags BODY, POINTER, LAND, NOP; LIFT reserved (even, cenotaph); land from any sats incl. degenerate coinbase land; runes edicts verbatim; cenotaph/void failure tiers; claim-time W valuation |
| indexer | deterministic replay from public chain data, reorg undo, land valuation with the consensus walk cutoff |
| wallet | Check (watch-only, addresses + xpub/descriptor), Claim, Claim and collect via external PSBT, Send, Receive, Withdraw bitcoin, balances latent/landed/pending, wallet-code backup — the feature set of `spec/wallet-policy.md` §What a wallet can do, as a CLI |
| server | the indexer RPC (`spec/indexer-rpc.md`, decided 2026-11-03): read-only index queries in bitcoind's idiom — getindexinfo, getbirthruns, getaddressutxos, getlatent — plus sendrawtransaction relay; cookie auth; no keys, no quotes, no custody, nothing a wallet must trust for funds |

## Out (deferred, design notes only)

Lift semantics and honoring rules, standby registry, work-priced notations, etchings and family assets, Level 2 swaps (`spec/swap-level2.md` holds notes), other gardens (the wallet data model is multi-garden; one garden ships), miner-facing coinbase-land UX. Also out (decided 2026-11-03): any consumer wallet front end — app, PWA, their security models, unlock and presentation. A consumer wallet is a separate future project; this repository specifies what a wallet must do and ships the CLI that does it. Also out (decided 2026-11-03): sponsored sends — a server adding its own sats to a user's transaction and charging orchids is gas abstraction, a product someone may build on a mature market, not protocol; nothing in release 1 charges orchids.

## Build order

1. **Check.** Server discovery from the existing walker, work cache per UTXO, xpub/descriptor derivation; wallet screen with "est. production cost" wording. *Built 2026-09-17:* `src/claimstones/server/` (`descriptors.py`, `check.py`, `app.py`: `GET /hello`, `POST /check`, static wallet) and `wallet/` (the Check page). Runs against Bitcoin Core with txindex; not yet exercised on regtest end to end.
2. **Claim in-app.** Wallet key and address; self-spend land tx; indexer credits after finality depth. *Built 2026-09-19:* `bitcoin/secp.py`, `bech32.py`, `psbt.py` (Schnorr, bech32m, transactions, PSBT, taproot sighash); `server/land.py` (`POST /build/land`, `POST /submit`, `GET /tx/{txid}`, `GET /balance/{address}`, a ledger of broadcast lands); `wallet/` (one taproot key per garden from a wallet code, PIN vault, Claim flow with the envelope check before signing, balances landed/pending/claimable). Exercised end to end in a browser against a fake node (`tools/wallet_stub_server.py`); the regtest run against Bitcoin Core is still to do (`docs/regtest.md`). Credit follows the server's ledger until the orchid index runs live next to the node.
3. **Send and Receive.** Sponsored template, PSBT verification in the wallet, countersign and broadcast, auto-land. *Built 2026-09-21:* `server/sponsor.py` (the server's key and pool), `server/send.py` (`POST /build/send`, `GET /pool`, intake lands), `server/orchids.py` (balances by replaying the server's own transactions through the indexer, decided 2026-09-21 as the stand-in for the live index), `/submit` countersigns; `wallet/` Send card with `verifySend`, Activity list with sends and receipts, Receive unchanged. Auto-land not built (not needed for release 1 wallets). Exercised against the stub server; the regtest send trial is still to do (`docs/regtest.md` §6).
4. **Claim via external PSBT.** Watched-address model, PSBT export by file and QR, broadcast of user-returned transactions.
5. **Hardening.** Fee parameters live, CPFP, pool landing on intake, regtest end-to-end suite, signet trial.

The wallet drives this order: server features exist because a wallet feature needs them. The "Built" notes above record a browser-wallet prototype and the thick wallet server (builder, sponsor, countersigner) that proved the flows against a stub node; both left the scope 2026-11-03. What replaced them: the server surface is the read-only indexer RPC of `spec/indexer-rpc.md` (built, with the `claimstones-cli` client), the wallet builds its own transactions from the templates in `spec/wallet-policy.md`, and the CLI reference wallet is the next build.

## Open design question (2026-09-17): forward birth index

The Check trial against a mainnet node showed the UTXO scan (`scantxoutset`) to be the wrong tool: it walks the whole UTXO set per request and stalls on a slow or damaged chainstate. The answer was a forward-replayed **birth index**: per unspent output, an ordered list of (birth height, sat count) runs, built once from genesis and updated per block. It would make Check a lookup, make the consensus land step a lookup, and let the same pass keep an address-to-outpoints index. Consequence to decide: the walk cutoff in `spec/land.md` is defined on the backward walk and cannot be reproduced forward; with the index the natural rule is exact valuation with one floor per land and no cutoff. The backward walker stays as an independent verifier.

Measurement prototype: `src/claimstones/indexer/birth.py` (tests in `tests/test_birth.py`), to be run next to the node once its chainstate reindex finishes. Numbers wanted before deciding: runs per UTXO, bytes per UTXO, blocks per second, database size at 400k blocks.

## Specification set

Each file lives under `docs/spec/` and is marked draft until a build round freezes it.

1. `constants.md` — A_0, seeds per orchid, the definition of W(c), the floor rule and arithmetic width, the non-monetary activation height.
2. `envelope.md` — magic, tag set v1, single asset ID, cenotaph and void rules. Replaces `docs/archive/l3r-claim-protocol.md` for release 1.
3. `land.md` — land validity, FIFO lineage walk with the coinbase fee tail, the consensus walk cutoff, cenotaph interaction, finality depth, mixed outputs.
4. `indexer.md` — deterministic replay, reorg undo, the test-vector list.
5. `fastbuild.md` — the fast birth-index build from genesis.
6. `indexer-rpc.md` — the indexer's JSON-RPC: every call a wallet makes, and the node-only-wallet guarantee.
7. `wallet-policy.md` — what any wallet must be able to do, the transaction templates, coin control over the UTXO states, envelope scan before signing, wallet code backup, mainnet safety checklist.
8. `swap-level2.md` — design notes only.

## Code fold-backs (follow-ups, not part of this docs step)

- **Done 2026-10-09.** A_0 changed to 3e13 seeds (decided 2026-10-09, `constants.md` §Monetary constant): `walker.py` A_0, `discover.py` wording, API spec examples.

- **Done 2026-09-17.** `src/claimstones/lineage/walker.py`: `A_BASE` becomes 5.6e13 seeds; `BASE_PER_ORCHID` becomes 100; the per-period rate table becomes a work table (w(h)/s(h)); valuation divides by W(c) supplied by the caller.
- **Done 2026-09-17.** `src/claimstones/cli/discover.py`: remove launch-height pinning and W₀ from the cache meta and report header; report work in hashes and orchids at W(tip); keep "est. production cost".
- **Done 2026-09-17.** `src/claimstones/protocol/codec.py`: v1 tag set, `OP_PUSHNUM_15` magic, single pointer, LAND/LIFT rules, encoder plus `land_script`/`send_script` builders; no `OP_PUSHNUM_13` material on any path. Uses the spec's tentative values (LIFT 26, LAND 28 with value 0, asset ID 0:0) until the `[decide]` markers in `spec/envelope.md` are closed.
- **Done 2026-09-17.** `src/claimstones/indexer/replay.py`: single-asset seed balances, the land step (valuation injected as a `Valuer`; `WalkerValuer` drives the lineage walker and refuses non-deterministic walks), void/cenotaph tiers, a `lands` table that stops later walks (double-claim check), activation height; mint/etch gone. `walker.py` gained a `landed` hook and a `deterministic` flag on results. Test vectors 1-12 and 15 of `spec/indexer.md` are in `tests/test_replay.py`.
- `src/claimstones/indexer/replay.py`: reorg undo per `spec/indexer.md` (undo log per block; land txids and walk-cache entries of disconnected blocks must be reverted). Until then a reorg needs a resync from the fork point.
