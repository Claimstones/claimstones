# Claimstones

Claimstones is an open-source protocol and software project for work-landed tokens on existing proof-of-work blockchains, beginning with Bitcoin. It is not a company, token sale, investment, or promise of value. Nothing is deployed by this repository.

## The idea

A Claimstones token has no issuer, premine, or minting process. The host chain's already-performed proof of work gives its coins a latent value, which follows satoshi lineage from the coinbase outputs where the work entered. The holder of such coins can **land** that latent value — convert it into spendable token balance — by spending the satoshis in a transaction that carries the Claimstones envelope. After a land, the balance is ordinary token state carried by UTXOs and moved with runes-style edicts, and the underlying satoshis are plain.

Release 1 is one token (**orchids**, 100 seeds each) on one garden (bitcoin). Valuation is at claim time: a land mints seeds in proportion to the work behind the landed satoshis, divided by the work function W at the land's block, with the single constant `A_0` anchoring an orchid near one kWh of mining electricity at 2026 chainwork. Unclaimed latent value never expires, but shrinks as W grows — about 3 % a month at the current cadence.

The envelope reuses proven runes machinery — OP_RETURN payload, LEB128 integers, tags, edicts, pointers, cenotaph failure handling — under its own magic (`OP_RETURN OP_PUSHNUM_15`, disjoint from runes' `OP_PUSHNUM_13`), so the two protocols can never be mistaken for one another. Claimstones does not etch, premine, or mint runes.

State is reconstructed deterministically from public chain data by anyone: the indexer in this repository replays the chain from genesis and arrives at the same balances on every machine. `ACTIVATION_HEIGHT` is not yet set (`protocol/codec.py` holds `None`); until it is, no land counts and everything is testing.

## Reading order

1. `docs/release-1-scope.md` — what release 1 contains and the build order. This document and `docs/spec/` are the authority.
2. `docs/spec/constants.md` — A_0, seeds per orchid, W(c), the floor rule, activation height.
3. `docs/spec/envelope.md` — magic, tag set, cenotaph and void rules.
4. `docs/spec/land.md` — land validity, the FIFO lineage walk, finality.
5. `docs/spec/indexer.md` — deterministic replay, reorg undo, test vectors.
6. `docs/spec/fastbuild.md` — the fast birth-index build from genesis.
7. `docs/birth-index-check.md` — how a built index is verified against the node's own UTXO set.
8. `docs/spec/indexer-rpc.md` and `docs/spec/wallet-policy.md` — the JSON-RPC every wallet calls, and what any wallet must be able to do, with the transaction templates.

The specifications are drafts until a build round freezes them; open points are marked `[decide]`.

## What is in this tree

```text
src/claimstones/
  protocol/   envelope codec: tags, edicts, cenotaph rules (consensus)
  lineage/    satoshi FIFO lineage walker and latent-value arithmetic (consensus)
  indexer/    deterministic replay, the birth index, the fast build, the tip
              daemon, and the JSON-RPC server that makes the index queryable
  bitcoin/    keys, bech32m, transactions, PSBT, block parsing (no third-party crypto)
  cli/        claimstones-cli, bitcoin-cli for the indexer RPC
docs/         release-1 scope, the specification set, the index verification identity
data/         calibration datasets behind the constants
tests/        protocol, lineage, indexer, and RPC tests, including the spec's test vectors
```

Claimstones is the core stack; wallets are front ends, and any number of them can be made against the JSON-RPC in `docs/spec/indexer-rpc.md` — or against a Bitcoin node alone, since valuation is a pure function of chain data and the walker computes the identical number (the node-only guarantee in that spec). A reference wallet, a CLI, arrives in an upcoming commit.

## Running it

Python ≥ 3.10. The only dependencies are `numpy` and `lmdb`:

```
pip install -e .
pytest
```

Building the birth index from genesis takes a Bitcoin Core node's `blocks/blk*.dat` files and runs the three phases of `docs/spec/fastbuild.md` (each module under `src/claimstones/indexer/fastbuild/` prints its usage with `--help`): resolve, replay, load. The result is an LMDB database; the tip daemon then keeps it at the chain tip over RPC, and the RPC server makes it queryable in bitcoind's own idiom (`docs/spec/indexer-rpc.md`):

```
python -m claimstones.indexer.cs_indexer --rpc-cookie <datadir>/.cookie --db <path>/birth.lmdb
python -m claimstones.indexer.rpc --db <path>/birth.lmdb \
    --node-url http://127.0.0.1:8332 --node-cookie <datadir>/.cookie
claimstones-cli -rpccookie <path>/rpc.cookie getindexinfo
claimstones-cli -rpccookie <path>/rpc.cookie getlatent bc1p...
```

A built index is trusted by verification, not provenance: `docs/birth-index-check.md` states the identity against the node's `gettxoutsetinfo muhash` that any independently built index must satisfy — and a wallet that runs its own node needs no index at all, since the backward walker computes the identical valuation from chain data alone.

## Status and boundaries

Draft specifications, a working indexer, and consensus code with test vectors. No token exists yet: `ACTIVATION_HEIGHT` is unset, nothing is deployed, and estimated production cost is a cost, not a market valuation. Contributions should keep the protocol auditable from public chain data, the wallet self-custodial, and every constant decided in the open.

## License

MIT — see [LICENSE](LICENSE).
