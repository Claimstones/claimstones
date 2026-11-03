# Spec 2 — Envelope and tag set v1 (draft)

Replaces `docs/archive/l3r-claim-protocol.md` §1–§4 and §6–§7 for release 1. Everything not stated here follows ord's runestone machinery as ported in `src/claimstones/protocol/codec.py`.

## Location

- The envelope is the **first** output whose script begins `OP_RETURN OP_PUSHNUM_15` (`6a 5f`). Later matching outputs are ignored. Runestones (`6a 5d`) are a different protocol; neither indexer's state depends on the other's envelope.
- All instructions after the two opcodes must be data pushes; their concatenation is the payload. Non-push opcode or truncated push → cenotaph.
- Payload = LEB128 varints, each ≤ 19 bytes and ≤ u128 max. Varint error → cenotaph.
- Integers form tag/value pairs until `TAG_BODY`; everything after is edicts. Tag at payload end with no value → cenotaph.

## Tags

| tag | number | parity | values | release 1 |
|---|---|---|---|---|
| BODY | 0 | even | edicts follow | yes |
| POINTER | 22 | even | one output index | yes |
| LIFT | 26 | even | reserved | present → cenotaph |
| LAND | 28 | even | one value, must be 0 | yes |
| NOP | 127 | odd | any | ignored |

Tag numbers and the LAND value form were confirmed 2026-10-09 (spec freeze review). LAND keeps a mandatory value rather than a value-less form: the tag/value pairing stays verbatim runes machinery, and a future LAND variant with a nonzero value fails loudly (cenotaph) on v1 indexers. The codec (`src/claimstones/protocol/codec.py`, 2026-09-17) implements them: LIFT 26, LAND 28 carrying one value that must be 0 (any other value → cenotaph without land; a duplicate LAND → cenotaph with land), one POINTER (a second → cenotaph).

Unrecognized even tag → cenotaph; unrecognized odd tag → ignored. RULE (6), PARAMS (8) and CLAIM (24) from the older draft are **not** part of v1 and count as unrecognized even tags (cenotaph).

## Asset ID

Release 1 has one asset. Edicts keep the runes shape `(id_block_delta, id_tx, amount, output)` so the codec stays verbatim. The orchid asset ID is `(0, 0)` (confirmed 2026-10-09; runes' reserved meaning of (0,0) — "the rune etched here" — cannot arise since claimstones has no etching, and zeros are the cheapest delta encoding); any other ID in an edict makes that edict void (skipped), not a cenotaph, to keep upgrade room for family assets.

## Edicts and allocation

As runes: delta-encoded IDs; amount 0 = all remaining; output index equal to the number of outputs = split evenly across non-OP_RETURN outputs; output index beyond that → cenotaph. Unallocated balance (inputs plus any landed amount) goes to the POINTER output, else the first non-OP_RETURN output. A pointer to an OP_RETURN output or out of range → cenotaph.

A transaction with no envelope but with orchid balance on its inputs is a valid transfer: all balance sweeps to the first non-OP_RETURN output.

## Failure tiers

- **Cenotaph** (decode-level flaw, unrecognized even tag, LIFT present): all input balances burn. If the envelope also carries LAND, the inputs' latent value is landed and burned (`land.md` §Cenotaph interaction).
- **Void** (semantic failure: unknown asset ID in an edict, land of inputs with zero latent value): the instruction is skipped; the rest of the transaction executes normally.

## Test vectors

Generated with the codec, never by hand; the list is in `indexer.md`.
