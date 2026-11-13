# Spec 6 — Indexer RPC (draft)

The indexer's reading face, in bitcoind's own idiom: `cs_indexer` keeps the birth index at the chain tip beside the node, and `claimstones.indexer.rpc` serves it over JSON-RPC the way bitcoind serves its chainstate — HTTP POST, one call per request, cookie authentication from a file beside the data, errors as `{code, message}` objects. Decided 2026-11-03, replacing the wallet-server API: the server that built transactions, quoted, sponsored and countersigned is gone from this project; what remains is a view of the index plus transaction relay. The server holds no keys, builds nothing, quotes nothing, and is never a custodian or an authority: every answer is reproducible from public chain data, and whether a served index is honest is settled by `../birth-index-check.md`, not by trusting the operator.

## Who needs it, and who does not

A wallet without its own Bitcoin node uses this API for its view — discovery, valuation, relay — and trusts it exactly as far as an Electrum user trusts an Electrum server: for what it *shows*, never for what it *signs* (`wallet-policy.md` §Envelope scan). Anyone can run their own: the daemon and this server read the same LMDB environment, the daemon the single writer, readers never blocked.

**The node-only wallet is a design guarantee, not a courtesy.** Consensus valuation is exact and a pure function of chain data (`land.md`, closed 2026-10-25), so a wallet with only a Bitcoin node (txindex) needs nothing here at all: it discovers its coins through the node, values them with the backward FIFO walker at `d = 0` — the identical number this index serves — and broadcasts through the node. The cost is walk time on deep lineages; the uncertainty is in cost, never in the amount. Nothing in this API may ever become necessary for correctness.

## Transport

- `POST /` with `{"method": "...", "params": [...], "id": any}`; the reply is `{"result": ..., "error": null, "id"}` or `{"result": null, "error": {"code", "message"}, "id"}`. Error messages name the call and the reason in plain words a wallet can show; no silent fallbacks.
- **Auth**: on start the server writes `rpc.cookie` beside the database — `__cookie__:<64 hex>`, bitcoind's convention — and requires it as HTTP Basic credentials (`--no-auth` for localhost experiments). The cookie is rotated on every start.
- **Port**: one digit off the node it serves beside, per network as Core does (decided 2026-11-12) — main **7332** (node 8332), testnet **17332** (18332), signet **37332** (38332), regtest **17443** (18443). The default is the mainnet port; the others are `--port`.
- The host node (`--node-url` + its auth) is optional: without it, `getlatent` and `sendrawtransaction` answer with an error naming the missing piece and the pure index lookups still serve.
- Seeds and sats travel as JSON numbers: a single land is bounded by `A_0` = 3e13 seeds and sats by 2.1e15, both inside JSON's exact-integer range (2^53). Chainwork is hex. txids are in display order (reversed), as everywhere in Core's RPC.

## Methods

### `getindexinfo`

The index's own state — height, counters, schema, the chain the index was built on, and that chain's activation height. The daemon records the chain (from its node's `getblockchaininfo`) in the index and refuses to follow a node on another one; the server reports it and the activation height the code holds for it (`constants.md`: test chains active from height 1; mainnet still [decide]). `activation_height` is `null` while mainnet's value is undecided; a wallet must treat `null` as "no envelope counts yet" and must not read it as 0, which is a real answer meaning active since genesis. The value reported is the constant the indexer compiles against, never a per-server setting: two servers on one chain that disagree here are running different code.

```json
{"height": 972618, "utxos": 191234567, "runs": 1283456789,
 "schema": "3", "chain": "main", "activation_height": null,
 "db_bytes": 73014444032}
```

### `getbirthruns <txid> <vout>`

One unspent output's runs, the index's native answer: ordered `[birth_height, sats]` pairs, with `"plain"` for sats already landed (`PLAIN` in the store). Error `-5` when the outpoint is unknown or spent. This is the call a wallet that values locally builds on.

```json
{"txid": "4a5e...", "vout": 0,
 "runs": [[100, 5000], [170, 2500], ["plain", 600]],
 "sats": 8100, "latent_sats": 7500}
```

### `getaddressutxos <address | script_hex>`

Every unspent output locked by a script, from the index's address table; the same shape as `getbirthruns`, one entry per outpoint. Addresses are bech32/bech32m with any HRP (`bc`, `tb`, `bcrt`); anything else is passed as scriptPubKey hex. Key derivation is the wallet's business: the wallet derives its addresses (or a watch-only set) and asks per script.

### `getlatent <address | script_hex | {"txid":..., "vout":...}>`

The runs valued in seeds at the node's current tip: `seeds = floor(A_0 · N / (W · L))` with N summed over all latent runs, one floor per the valuation rule (`constants.md`), W the node's cumulative chainwork at `valued_at_height`. Per-UTXO seeds use the same rule per outpoint, so the parts may sum below the whole by at most rounding. Needs the host node (error `-10` without one). The reply carries everything a client needs to recompute the number from `getbirthruns` and its own W — the server shows its work:

```json
{"valued_at_height": 972618, "chainwork": "0000...9c21",
 "seeds": 929, "utxos": [{"txid": "...", "vout": 0, "runs": [[100, 5000]],
                           "sats": 5000, "latent_sats": 5000, "seeds": 929}]}
```

Wording for display is the wallet's business (`wallet-policy.md` §Wording): "est. orchids to be landed", never "value" or "production cost".

### `sendrawtransaction <hex>`

Relay through the host node; the node's answer (txid or refusal) passes through. Error `-10` without a node, `-11` with the node's own message when it refuses.

### `help`

The method list with signatures.

## Planned, not yet served

- **`getorchidbalance <address | script_hex | {"txid":..., "vout":...}>`** — settled orchid balances from the ledger half (`indexer.md`), once it joins the daemon. Until then there are no balances to serve, and the method is absent rather than answering 0. Shape decided 2026-11-13, the one the CLI wallet already consumes: per-UTXO seeds for every unspent output of the script that holds a nonzero balance, and their sum.

  ```json
  {"height": 980123, "seeds": 12294,
   "utxos": [{"txid": "...", "vout": 1, "seeds": 12294}]}
  ```

  With it `getindexinfo` gains `minted`, `burned` and `ledger_height` (the height from which balances exist).
- **Confirmation status** for a relayed transaction (`gettxstatus`): mempool / confirming / settled / dropped, as the node sees it. **[decide]** whether a pure passthrough of the node's `getrawtransaction` verbosity is enough.
- **[decide]** a `getworktable` (nbits per retarget period) so a node-less client can recompute valuation end to end from headers it verifies itself.

## Running it

```
python -m claimstones.indexer.rpc --db <path>/birth.lmdb \
    --node-url http://127.0.0.1:8332 --node-cookie <datadir>/.cookie
claimstones-cli -rpccookie <path>/rpc.cookie getindexinfo
```

`claimstones-cli` (`src/claimstones/cli/rpc_cli.py`) is bitcoin-cli for this API: one call per invocation, parameters JSON where they parse as JSON, result on stdout, errors on stderr with exit code 1.
