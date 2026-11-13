# Spec 7 — Wallet policy (draft)

## Principles

Claimstones is the core stack; wallets are front ends, and any number of them can exist. This spec states what every wallet must be able to do and the rules that keep it safe — not how one looks, stores its secret at rest, or authenticates its user; those belong to each front end. The reference wallet for this repository is a CLI (planned, an upcoming commit). Safety before features: the wallet never emits a near-cenotaph and never signs what it has not decoded.

## What a wallet can do

The release-1 feature set, built on the index queries of `indexer-rpc.md` (or a wallet's own node and walker) and the templates below:

- **Receive orchids and bitcoin.** One taproot address per garden, reused. Bitcoin sent to the address becomes claimable orchids; orchids sent by another wallet arrive as settled balance.
- **Send orchids.** The send template below, built and signed by the wallet; the miner fee comes from the wallet's own sats. Nothing in release 1 charges orchids. (The sponsored send of the retired server — a sponsor adding its sats and charging an orchid fee — left the project 2026-11-03; it is a product a wallet-and-server operator may build on a mature market, not protocol.)
- **Discover and claim** latent orchids on the wallet address (Check + Claim).
- **Check any bitcoin**: any address, xpub or descriptor, watch-only; per UTXO: sats, unclaimed sats, "est. orchids to be landed" at W(tip).
- **Claim and collect via external PSBT** for coins the wallet does not hold keys for. **Decided 2026-10-04: harvest model**, extended 2026-10-30 to collect: one transaction lands the coins' latent value and moves any orchids already on them to the wallet address, on `carrier_sats` (330, §Transaction templates), and returns the bitcoin to the address it came from: the user harvests orchids without disturbing their holdings, and the returned coins are clean. The confirmation shown before signing must state plainly what the signer pays: the miner fee plus 330 sats. Template: §Transaction templates; checks: `verifyCollect` below. This supersedes the watched-address model in `release-1-scope.md`; no watched addresses are needed, the orchids arrive as ordinary wallet balance.
- **Withdraw bitcoin** (decided 2026-10-04). The use case: a user moves bitcoin onto the wallet address only to claim, then sends it back to their regular bitcoin wallet. The withdraw transaction always carries an envelope with a POINTER to the wallet's own change output, so every settled orchid stays; only plain bitcoin leaves. The envelope is mandatory: without one, `envelope.md` sweeps the whole orchid balance to the first non-OP_RETURN output — the destination. Server builds, wallet verifies (`verifyWithdraw`: pointer at the wallet's own output, no edicts to foreign outputs, no LAND, sats out equal amount + fee) and signs. Caution the envelope cannot cover: *latent* value rides the sats themselves (FIFO lineage), so withdrawing unclaimed sats gives their latent value to the destination — when claimable > 0 the wallet must ask "claim first?" before confirming.
- **Backup and restore by wallet code** (below). No seed phrase.

Release 2, design notes only: **Exchange** (`swap-level2.md`) — quotes worded as amounts per §Wording, suggestive only, never "value"; countersign the server's matched PSBT within `quote_ttl_seconds`; both legs decoded, refuse LAND in a swap.

## Keys and address

- One taproot key per garden, one address, reused. HD derivation under the hood so rotation can be added later without migration.
- **Backup = wallet code**: the key as one short string. Restore from the code alone recovers everything. No seed phrase.

As built 2026-09-19 (`src/claimstones/bitcoin/secp.py` and `bech32.py`; vectors in `tests/test_wallet_vectors.py`):

- **Wallet secret**: 32 random bytes. **Wallet code** = the secret in bech32m under the human-readable part `orchid` (65 characters, `orchid1...`).
- **Derivation**: `master = HMAC-SHA512(key "orchids/1", secret)`; garden key `= HMAC-SHA512(master, "<garden>/<index>")[0:32] mod n`, garden `bitcoin`, index 0. Rotation later = index 1, same code.
- **Address**: BIP-86 taproot of that key (tweak with no script tree), one address per garden, bech32m for the server's network (`bcrt1p`, `tb1p`, `bc1p`). The wallet refuses to talk to a server on another network.
- **Signing**: BIP-341 key path, SIGHASH_DEFAULT (64-byte Schnorr signatures, random aux).

How the secret is stored at rest and unlocked is each front end's business and out of this spec's scope; the only policy is that the wallet code alone must restore the wallet.

## Transaction templates

The wallet builds every transaction itself from these templates (decided 2026-11-03, replacing the retired server's builder; the first CLI-wallet build freezes them). `carrier_sats` = 330: the sats that ride under a token output, taproot dust plus headroom. Every input has sequence `0xfffffffd`.

**Claim** (the wallet's own coins): exactly two outputs. Output 0 `OP_RETURN OP_PUSHNUM_15` with LAND only — no pointer, no edicts; output 1 the wallet's own address, all input sats minus the miner fee. The default rule (`envelope.md`: unallocated balance to the first non-OP_RETURN output) puts the minted orchids on output 1.

**Send**: exactly three outputs. Output 0 the envelope with exactly one edict — asset 0:0, the amount, to output 2 — and no LAND; output 1 the wallet's own change, all input sats minus `carrier_sats` and the miner fee (the orchid remainder lands here by the default rule; a POINTER to 1 is permitted and redundant); output 2 the recipient, exactly `carrier_sats`. Inputs are the wallet's own: settled orchid UTXOs covering the amount, plus plain UTXOs as the fee needs.

**Claim and collect** (external coins, the harvest model — not built):

| output | pays | sats | orchids |
|---|---|---|---|
| 0 | `OP_RETURN OP_PUSHNUM_15`, LAND only: no pointer, no edicts | 0 | — |
| 1 | the orchid wallet | `carrier_sats` | all of them: the inputs' balance plus the landed amount, by the default rule |
| 2 | `return_to`, the address of one of the inputs (default the largest) | input sats − carrier − miner fee | none |

LAND is always present: sats landed before add nothing (`land.md`), so the one template covers claim only, collect only and both. The carrier comes first so the default rule puts every orchid on output 1 without a pointer, and keeps doing so if a fee bump removes output 2. The PSBT carries per input `witness_utxo`, `non_witness_utxo` for non-taproot inputs (hardware signers want the previous transaction for segwit v0 and legacy), and the key origin (`bip32_derivation`, `tap_bip32_derivation`) when the input descriptor gave one as `[fingerprint/path]` — a bare xpub has no origin and a hardware signer cannot find its key from it. There is no expiry: the landed amount is fixed at the inclusion block, so the PSBT fails only if an input is spent first. The owner sets the fee rate and can raise it afterwards in their own wallet by CPFP spending output 2, which carries no orchids: the parent is never touched, so this is safe in any wallet. RBF is not (signet, 2026-11-13): a wallet that did not build the transaction does not bump it, it rebuilds it from the payments it recognises — Sparrow's "Increase fee" dropped the 0-sat OP_RETURN, added an input and a change output, and reordered; no envelope, no LAND, the 330 sats arrived as plain bitcoin and the latent value stayed on the return coin. Nothing is lost but the fee, and nothing warned, because the signing wallet broadcast. The exported-PSBT guidance is therefore CPFP only, with the RBF warning spelled out (§Wording). **[decide]** inputs from several source addresses: one return output, or one transaction per source address. Collecting (as opposed to claiming) waits on orchid balances per outpoint from the index (`indexer-rpc.md` §Planned); until then the external form can only claim.

**Withdraw**: §What a wallet can do — the mandatory POINTER envelope at the wallet's own change output; only plain bitcoin leaves.

## Coin control

Every UTXO has a state: **tokens** (orchid balance), **latent** (unlanded work), **both**, or **plain**. Rules:

- Never auto-select a token-bearing UTXO as a sat input; an unaware spend sweeps the balance by the default rule.
- Every transaction is one of §Transaction templates — never a freehand construction. Send inputs: settled orchid UTXOs, largest first, until the amount is covered; plain UTXOs for the fee.
- Show latent and landed balances separately, with pending in between.

## Envelope scan before signing

Decode every envelope in any PSBT before signing — including the wallet's own builds, where the scan is the self-check that the builder produced the template it meant to. Refuse to sign on any unexpected tag, edict, pointer, or sat outflow. This is the multi-party footgun defence and is not optional. A failed check means refusal — never a signed transaction after a failed decode. A failed check means refusal — never a signed transaction after a failed decode. The checks run when the PSBT arrives and again right before signing.

For the claim (`verifyLand`): exactly two outputs; output 0 the only OP_RETURN, decoding to LAND alone (no flaw, no LIFT, no edicts, no unknown even tag, pointer absent or 1); output 1 pays the wallet's own script; every input has a `witness_utxo` paying the wallet's own script and is named in the summary; input sats, change and fee equal the summary; the fee is at most a fifth of the input sats.

For the send (`verifySend`, rewritten 2026-11-03 for the wallet-built template): exactly three outputs; output 0 the only OP_RETURN, decoding without flaw, no LAND, no LIFT, pointer absent or 1; exactly one edict, for asset 0:0, paying exactly the typed amount to output 2; output 1 pays the wallet's own script all input sats minus `carrier_sats` and the miner fee (no other sat outflow); output 2 pays the recipient script the wallet derived from the address the user typed, exactly `carrier_sats`; every input is the wallet's own, with a `witness_utxo`, and the inputs' settled orchids cover the amount.

For the external claim and collect (2026-10-30, `verifyCollect`, not built; template in §Transaction templates): exactly three outputs; output 0 the only OP_RETURN, decoding to LAND alone as for the claim; output 1 pays the wallet's own script exactly `carrier_sats`; output 2 pays the script of one of the inputs; no input pays the wallet's own script, and every input is named in the summary with its `witness_utxo`; input sats, return sats and fee equal the summary; the fee is at most a fifth of the input sats. The wallet signs nothing here. The check runs when the wallet builds the PSBT; the signing wallet broadcasts and nothing comes back (decided 2026-11-13: a wallet that can sign a PSBT can broadcast it, and a bring-it-back step was a second file nobody would carry). What protects the user is the plan shown before signing and the fee guidance with it (§Wording).

## Wording

Orchids are amounts (decided 2026-11-13): "est. orchids to be landed", never "value", "worth" or "production cost" — that phrase belongs to the issuance function alone (`constants.md`). A plan reads: sats in and unclaimed, the orchids to be landed (valued at height N), what is paid, what returns. Unclaimed orchids "do not expire, but shrink about 3 % a month". Claiming "does not spend your bitcoin; you pay a network fee". On a withdraw: "your orchids stay in this wallet; only bitcoin leaves". With an exported claim-and-collect PSBT: "to raise the fee later, use CPFP in your other wallet: spend the returned coin at a higher rate" and "do not use Increase fee (RBF): a wallet that did not build this transaction rebuilds it and drops the envelope — the claim silently does not happen".

Sats a walk left unresolved (2026-09-21): "not claimable" when the consensus walk cutoff dropped them (a claim goes ahead; at most the shown upper bound is forgone), "not claimable yet" when the server's walk budget ran out (the UTXO waits until it has been walked). The "yet" is the difference: the first case never changes, the second does.

## Mainnet safety checklist

- Regtest end to end: check, claim in-app, send, receive, external claim and collect, withdraw, reorg during a land.
- Sparrow: import a claim-and-collect PSBT, sign, broadcast; CPFP the returned coin and the parent confirms unchanged. "Increase fee" is known to rebuild (signet 2026-11-13, envelope dropped): the guidance warns against it, and the wallet must show that warning with every exported PSBT.
- Signet trial with real fee bumps — done 2026-11-13 (`ops/signet`): claim, Sparrow collect, CPFP; the RBF finding above.
- Codec test vectors green; strict construction (no cenotaph ever emitted) fuzzed.
- Restore from the wallet code alone tested.
