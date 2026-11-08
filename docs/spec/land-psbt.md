# Spec 8 — Land service builder (draft)

A small service that builds the claim-and-collect PSBT of
`wallet-policy.md` §Transaction templates for anyone who can sign with
an ordinary bitcoin wallet. It exists so orchids can be landed with no
claimstones wallet at all: name the address holding the bitcoin and the
address that will keep the orchids, sign what comes back in the wallet
you already trust, broadcast anywhere. Drafted 2026-11-08.

Trust is the whole design: the service never sees a key, never takes
custody, and builds a transaction whose outputs the signer's own wallet
displays — all the bitcoin returns to the address it came from, minus
the miner fee and the 330-sat carrier. Every PSBT is checkable against
the public `verifyCollect` rules (`wallet-policy.md` §Envelope scan),
so anyone can audit the service — or run their own beside their own
node. A PSBT service that cannot be independently verified is
indistinguishable from phishing; this one invites the check.

## Transport

- Its own process (`claimstones.indexer.land_builder`) beside the
  gateway, default bind `127.0.0.1:7334`. It is the one exception to
  the gateway's GET-only rule, kept out of the gateway so Spec 7's
  promise stays whole: the edge forwards exactly `POST /v1/land` here
  and nothing else.
- JSON in and out; errors as the gateway shapes them (`{"error": msg}`,
  400 bad input / 405 not POST / 502 backend unavailable).
- No auth: boundedness and the edge's rate limits are the protection,
  and building a PSBT mutates nothing — the chain only changes if the
  user signs and broadcasts.

## Request

```json
{"address": "bc1q... (holds the bitcoin)",
 "orchids_to": "bc1p... (keeps the orchids)",
 "fee_rate": 4}
```

One funding address per transaction (this fixes, for the service, the
multi-address question `wallet-policy.md` leaves open — the return
output is then always unambiguous). `fee_rate` in sat/vB is optional;
default is the node's estimate. The fee must respect `verifyCollect`'s
bound: at most a fifth of the input sats.

## Answer

```json
{"psbt": "<base64>",
 "summary": {"inputs": 3, "input_sats": 150000, "carrier_sats": 330,
             "return_sats": 148870, "fee_sats": 800,
             "seeds": 101, "height": 981234}}
```

The transaction is exactly the claim-and-collect template: output 0 the
LAND-only envelope, output 1 `orchids_to` with `carrier_sats`, output 2
the funding address with everything else. Per input `witness_utxo`
(plus `non_witness_utxo` when not taproot). The `summary` is what
`verifyCollect` checks the PSBT against, and what the frontend must
show before signing, in the mandated wording: the signer pays the miner
fee plus 330 sats, nothing more. RBF and CPFP guidance is inherited
from `wallet-policy.md`: raise fees via output 2, never edit outputs.

## Refusals

- Tip below `ACTIVATION_HEIGHT`, or `ACTIVATION_HEIGHT` unset: refuse.
  An envelope below activation is ignored by every indexer; building
  one would waste the signer's sats.
- More UTXOs on the address than `--max-inputs` (default 50): build
  from the largest `--max-inputs` and say so in the summary.
- Inputs too small to cover carrier + fee with a non-dust return:
  refuse with the arithmetic in the message.

## Service fee variant ([decide], off at launch)

A hosted service may charge in orchids — never in sats — by adding one
edict to the envelope: asset 0:0, the fee amount, to an output 3 paying
the service's own address `carrier_sats`; the unallocated remainder
still lands on output 1 by the default rule. The fee is thereby visible
in the PSBT itself and bounded by the same verification: a checker
knowing the variant verifies the fee equals what the service quoted.
Whether and when a hosted instance charges is deployment, not protocol;
the shape is specced here so the trust model is public before any fee
exists.

## What it will not do

No keys, no signing, no custody, no sweep of anything but the named
address. No broadcast at launch — the signer's wallet does that;
accepting the signed transaction back and forwarding it to Spec 6
`sendrawtransaction` is **[decide]**. Collecting settled orchids (as
opposed to landing latent value) waits on per-outpoint balances from
the index, exactly as the wallet's external form does.
