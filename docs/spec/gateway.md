# Spec 7 — Public gateway (draft)

What stands between the internet and the indexer RPC. Spec 6's server is
built for wallets and operators — JSON-RPC, cookie auth, unbounded
answers — and is never exposed publicly. The gateway
(`claimstones.indexer.gateway`) is the only process a public edge may
reach: GET-only HTTP, two paths, bounded JSON answers shaped for a
website where a visitor pastes an address and reads what it holds.
Decided 2026-11-08; the edge proxy itself (TLS, per-IP rate limits,
response cache, bans) is deployment, not protocol, and lives outside
this repo.

Trust is unchanged from Spec 6: every answer is reproducible from public
chain data, and the gateway adds presentation, never authority. Anyone
can run the whole stack beside their own node.

## Transport

- `GET /v1/info` and `GET /v1/address/<target>`; JSON out, including
  errors (`{"error": msg}` with 400 bad input / 404 no such path /
  405 not GET / 502 index backend unavailable).
- No auth: boundedness and the edge's rate limits are the protection.
  Default bind 127.0.0.1:7333, one digit off the RPC it fronts; deployed
  it binds the interface the edge proxy reaches (a tailnet address).
- `Cache-Control: public, max-age=60` and `Access-Control-Allow-Origin: *`
  on every answer: the edge may cache and any frontend may call.
- A 502 tells the edge to serve its cached copy; `height` and `as_of`
  in every 200 are what lets it mark that copy "as of block X" instead
  of erroring during an outage.

## `<target>`

Whatever a visitor pastes: bech32/bech32m, legacy base58 (`1...`/`3...`
on mainnet, `m...`/`n...`/`2...` on the test networks), or a raw
scriptPubKey in hex. The address must be for the index's own chain
(`chain` of `getindexinfo`, asked once): a `tb1...` on a mainnet
gateway, or a `bc1...` on a signet one, is a 400 naming the network,
never an empty answer for the same script on the wrong chain. Hex
carries no network and is looked up as given. The indexer RPC itself
stays bech32/hex (Spec 6); friendliness is this layer's job.

## Answers

`/v1/info`: Spec 6 `getindexinfo` plus `as_of` (unix time of the
answer).

`/v1/address/<target>`:

```json
{"target": "...", "script": "...", "height": 975041,
 "chainwork": "...", "seeds": 101, "settled_seeds": 310000,
 "sats": 123456, "latent_sats": 123456, "utxo_count": 731,
 "utxos": [...at most --max-utxos rows...], "truncated": true,
 "as_of": 1794120000}
```

Totals (`seeds`, `settled_seeds`, `sats`, `latent_sats`, `utxo_count`)
are always exact; only the `utxos` rows are capped (default 100), and
`truncated` says when they were. `seeds` is Spec 6 `getlatent`'s
valuation seeds, the orchids still to be claimed; `settled_seeds` the
orchids already on the script (Spec 6 `getorchidbalance`: landed and
received), also per row, and `null` on an index without a ledger half
(not 0). The two are asked one after the other, so across a new block
they can be one block apart. The frontend's wording contract is
`wallet-policy.md` ("est. orchids to be landed", never "value").

## What it will not do

No mutation of any kind — `sendrawtransaction` stays behind the RPC for
wallets; the public site reads. No pagination, no filters, no query
language: a caller who needs more than the capped rows is a wallet or
an operator, and Spec 6 is their door.
