"""Claim: the wallet lands the latent value on its own address.

The in-wallet half of wallet-policy.md §What a wallet can do — Check
found the coins, Claim lands them. The transaction is the claim
template of wallet/land.py: two outputs, the LAND-only envelope and
the wallet's own change; the minted orchids ride to output 1 by the
default rule, and the only cost is the miner fee.

Coin selection is the simple, safe rule: every coin on the wallet
address with latent value goes in; coins with nothing left to land
stay untouched (coin control — a settled orchid coin is never pulled
into a transaction it has no business in).

Pure logic, the RPC client injected: plan_claim builds and verifies
the PSBT, sign_claim verifies again right before signing (the two
moments of wallet-policy.md §Envelope scan), and the caller sends the
result wherever it talks to.
"""

from __future__ import annotations

from dataclasses import dataclass

from claimstones.bitcoin.psbt import Psbt, Transaction
from claimstones.bitcoin.txparse import TxOut
from claimstones.wallet.keys import WalletKey
from claimstones.wallet.land import (
    ClaimSummary, FundingInput, TemplateError, build_claim, verify_land,
)
from claimstones.wallet.rpc import NO_NODE, IndexerRpc, WalletRpcError


@dataclass(frozen=True)
class ClaimPlan:
    psbt: Psbt
    summary: ClaimSummary
    latent_sats: int            # what the claim lands
    seeds: int | None           # est. production cost; None without a node
    valued_at_height: int | None
    skipped: int                # coins with nothing left to land, left out


def plan_claim(key: WalletKey, rpc: IndexerRpc, fee_rate: float) -> ClaimPlan:
    script_hex = key.script.hex()
    utxos = rpc.getaddressutxos(script_hex)
    coins = [u for u in utxos if u["latent_sats"] > 0]
    if not coins:
        raise TemplateError(
            "every coin on the wallet address is already claimed" if utxos
            else "the wallet address holds no coins")

    inputs = [FundingInput(bytes.fromhex(u["txid"])[::-1], u["vout"],
                           TxOut(u["sats"], key.script))
              for u in coins]
    psbt, summary = build_claim(inputs, key.script, key.internal_key, fee_rate)

    seeds = None
    valued_at = None
    try:
        latent = rpc.getlatent(script_hex)
        valued_at = latent["valued_at_height"]
        by_outpoint = {f"{u['txid']}:{u['vout']}": u["seeds"]
                       for u in latent["utxos"]}
        seeds = sum(by_outpoint.get(op, 0) for op in summary.inputs)
    except WalletRpcError as e:
        if e.code != NO_NODE:
            raise
    return ClaimPlan(psbt, summary, sum(u["latent_sats"] for u in coins),
                     seeds, valued_at, len(utxos) - len(coins))


def sign_claim(plan: ClaimPlan, key: WalletKey) -> Transaction:
    """Verify, sign every input, finalize, verify the signed result."""
    prevouts = plan.psbt.prevouts()
    verify_land(plan.psbt.tx, prevouts, key.script, plan.summary)
    for n in range(len(plan.psbt.inputs)):
        plan.psbt.inputs[n].tap_key_sig = key.sign(plan.psbt.tx, prevouts, n)
    tx = plan.psbt.finalize()
    verify_land(tx, prevouts, key.script, plan.summary)
    return tx
