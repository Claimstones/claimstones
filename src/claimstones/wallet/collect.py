"""Claim and collect via external PSBT: the harvest, from the wallet.

The wallet-policy.md flow for coins the wallet holds no keys for: name
the one address holding the bitcoin, the wallet builds the
claim-and-collect template (wallet/land.py) with the orchids coming to
its own address, and hands back a PSBT for whatever wallet owns the
coins to sign and broadcast. The signer pays the miner fee plus the
330-sat carrier, nothing more; the rest returns to the address it came
from. There is no deadline: the landed amount is fixed at the
inclusion block, so the PSBT fails only if an input is spent first.

The wallet never sees the signed transaction again (decided
2026-11-13: a wallet that signs a PSBT broadcasts it; a bring-it-back
step was a second file nobody would carry). What protects the user is
the plan shown before signing and the fee guidance with it.

Pure logic, the RPC client injected; file handling is the CLI's.
"""

from __future__ import annotations

from dataclasses import dataclass

from claimstones.bitcoin.descriptors import CheckInputError, parse_line
from claimstones.bitcoin.psbt import Psbt
from claimstones.bitcoin.txparse import TxOut
from claimstones.wallet.land import (
    CollectSummary, FundingInput, TemplateError, build_collect,
)
from claimstones.wallet.rpc import NO_NODE, IndexerRpc, WalletRpcError


@dataclass(frozen=True)
class CollectPlan:
    psbt: Psbt
    summary: CollectSummary
    funding_address: str
    orchids_script: bytes
    latent_sats: int
    seeds: int | None           # est. orchids to be landed; None without a node
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
