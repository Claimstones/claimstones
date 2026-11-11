"""Claim and collect via external PSBT: the harvest, from the wallet.

The wallet-policy.md flow for coins the wallet holds no keys for: name
the one address holding the bitcoin, the wallet builds the
claim-and-collect template (wallet/land.py) with the orchids coming to
its own address, and hands back a PSBT for whatever wallet owns the
coins to sign. The signer pays the miner fee plus the 330-sat carrier,
nothing more; the rest returns to the address it came from.

The plan is written down as a record — everything verification needs,
independent of this process surviving: the signing happens in another
wallet, on another machine, with no deadline (the landed amount is
fixed at the inclusion block, so the PSBT fails only if an input is
spent first). When the signed transaction comes back, verify_returned
runs the same verify_collect against the record before anything goes
near sendrawtransaction — the second moment of §Envelope scan.

Pure logic, the RPC client injected; file handling is the CLI's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from claimstones.bitcoin.descriptors import CheckInputError, parse_line
from claimstones.bitcoin.psbt import Psbt, PsbtError, Transaction
from claimstones.bitcoin.txparse import TxOut
from claimstones.wallet.land import (
    CollectSummary, FundingInput, TemplateError, build_collect, verify_collect,
)
from claimstones.wallet.rpc import NO_NODE, IndexerRpc, WalletRpcError

RECORD_KIND = "claimstones claim-and-collect v1"


@dataclass(frozen=True)
class CollectPlan:
    psbt: Psbt
    summary: CollectSummary
    funding_address: str
    orchids_script: bytes
    latent_sats: int
    seeds: int | None           # est. production cost; None without a node
    valued_at_height: int | None


def plan_collect(orchids_script: bytes, funding_address: str,
                 rpc: IndexerRpc, fee_rate: float,
                 network: str | None = "main") -> CollectPlan:
    try:
        branches = parse_line(funding_address, network)
    except CheckInputError as e:
        raise TemplateError(str(e)) from None
    if len(branches) != 1 or branches[0].ranged:
        raise TemplateError(
            "one funding address per transaction — an address, not an "
            "xpub or ranged descriptor")
    derived = branches[0].derive(0)
    script_hex = derived.script.hex()

    utxos = rpc.getaddressutxos(script_hex)
    if not utxos:
        raise TemplateError(f"no coins on {funding_address}")
    inputs = [FundingInput(bytes.fromhex(u["txid"])[::-1], u["vout"],
                           TxOut(u["sats"], derived.script))
              for u in utxos]
    psbt, summary = build_collect(inputs, orchids_script, derived.script,
                                  fee_rate)

    seeds = None
    valued_at = None
    try:
        latent = rpc.getlatent(script_hex)
        valued_at = latent["valued_at_height"]
        seeds = sum(u["seeds"] for u in latent["utxos"])
    except WalletRpcError as e:
        if e.code != NO_NODE:
            raise
    return CollectPlan(psbt, summary, funding_address, orchids_script,
                       sum(u["latent_sats"] for u in utxos), seeds, valued_at)


# ---------------------------------------------------------------------------
# The record: what the signed transaction is verified against later
# ---------------------------------------------------------------------------

def plan_record(plan: CollectPlan) -> dict:
    s = plan.summary
    return {
        "kind": RECORD_KIND,
        "funding_address": plan.funding_address,
        "orchids_script": plan.orchids_script.hex(),
        "inputs": [{"outpoint": op, "sats": p.value,
                    "script": p.script_pubkey.hex()}
                   for op, p in zip(s.inputs, plan.psbt.prevouts())],
        "summary": {"input_sats": s.input_sats,
                    "carrier_sats": s.carrier_sats,
                    "return_sats": s.return_sats,
                    "fee_sats": s.fee_sats},
        "psbt": plan.psbt.to_base64(),
    }


def load_record(text: str) -> dict:
    try:
        record = json.loads(text)
    except ValueError:
        raise TemplateError("the record file is not JSON") from None
    if not isinstance(record, dict) or record.get("kind") != RECORD_KIND:
        raise TemplateError("not a claim-and-collect record")
    return record


def verify_returned(raw: str, record: dict) -> Transaction:
    """The signed transaction the user brings back — raw hex, or the
    PSBT their wallet finalized — checked against the record. Raises
    TemplateError; a transaction this returns is exactly the template
    the user confirmed."""
    raw = "".join(raw.split())
    try:
        if raw and all(c in "0123456789abcdefABCDEF" for c in raw):
            tx = Transaction.parse(bytes.fromhex(raw))
        else:
            tx = Psbt.from_base64(raw).extract()
    except (PsbtError, ValueError) as e:
        raise TemplateError(f"cannot read the signed transaction: {e}") from None

    by_outpoint = {i["outpoint"]: TxOut(i["sats"], bytes.fromhex(i["script"]))
                   for i in record["inputs"]}
    prevouts = []
    for txin in tx.inputs:
        outpoint = f"{txin.prev_txid[::-1].hex()}:{txin.prev_vout}"
        if outpoint not in by_outpoint:
            raise TemplateError(
                f"the transaction spends {outpoint}, which is not in the "
                "record")
        prevouts.append(by_outpoint[outpoint])
    s = record["summary"]
    summary = CollectSummary(tuple(by_outpoint), s["input_sats"],
                             s["carrier_sats"], s["return_sats"],
                             s["fee_sats"])
    verify_collect(tx, prevouts, bytes.fromhex(record["orchids_script"]),
                   summary)
    return tx
