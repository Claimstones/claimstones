"""Send orchids and withdraw bitcoin: what leaves the wallet.

The two outbound templates of wallet-policy.md §Transaction templates,
built, verified twice and signed like the claim (wallet/claim.py).

Send: three outputs — the envelope with exactly one edict (asset 0:0,
the amount, to output 2), the wallet's change, the recipient's 330-sat
carrier. Inputs are the wallet's own: settled orchid coins, largest
first, until the amount is covered, then plain coins as the fee needs
(coin control: a token-bearing coin is never pulled in as mere fee
material). Settled balances come from `getorchidbalance`, which the
index serves once the ledger half exists (indexer-rpc.md §Planned);
until then Send refuses with the reason rather than guessing.

Withdraw: three outputs — the envelope with a POINTER at the wallet's
own change (output 1), the change, the destination's sats (output 2).
The envelope is mandatory: without one, the default rule would sweep
every settled orchid to the destination. What the envelope cannot
cover: latent value rides the sats themselves, so when the selected
coins still carry unclaimed value the wallet must ask "claim first?"
before anything is signed — the caller gets `latent_sats` for exactly
that question.

Pure logic, the RPC client injected.
"""

from __future__ import annotations

from dataclasses import dataclass

from claimstones.bitcoin.descriptors import CheckInputError, parse_line
from claimstones.bitcoin.psbt import Psbt, Transaction
from claimstones.bitcoin.txparse import TxOut
from claimstones.protocol.codec import (
    ORCHID, Envelope, decipher, encipher, is_op_return, send_script,
)
from claimstones.wallet.keys import WalletKey
from claimstones.wallet.land import (
    CARRIER_SATS, FundingInput, TemplateError, _DUST, _check_fee_bound,
    _estimate_fee, _fill_input, _unsigned_tx, script_type,
)
from claimstones.wallet.rpc import IndexerRpc, WalletRpcError


@dataclass(frozen=True)
class SendSummary:
    inputs: tuple[str, ...]
    input_sats: int
    input_seeds: int            # settled orchids the inputs carry
    amount_seeds: int
    change_sats: int
    carrier_sats: int
    fee_sats: int


@dataclass(frozen=True)
class SendPlan:
    psbt: Psbt
    summary: SendSummary
    recipient: str
    recipient_script: bytes


@dataclass(frozen=True)
class WithdrawSummary:
    inputs: tuple[str, ...]
    input_sats: int
    amount_sats: int
    change_sats: int
    fee_sats: int


@dataclass(frozen=True)
class WithdrawPlan:
    psbt: Psbt
    summary: WithdrawSummary
    destination: str
    destination_script: bytes
    latent_sats: int            # unclaimed value riding the selected coins


def _address_script(address: str, network: str | None) -> bytes:
    try:
        branches = parse_line(address, network)
    except CheckInputError as e:
        raise TemplateError(str(e)) from None
    if len(branches) != 1 or branches[0].ranged:
        raise TemplateError("one address, not an xpub or ranged descriptor")
    return branches[0].derive(0).script


def _wallet_coins(key: WalletKey, rpc: IndexerRpc) -> list[dict]:
    utxos = rpc.getaddressutxos(key.script.hex())
    if not utxos:
        raise TemplateError("the wallet address holds no coins")
    return utxos


def _funding(u: dict, script: bytes) -> FundingInput:
    return FundingInput(bytes.fromhex(u["txid"])[::-1], u["vout"],
                        TxOut(u["sats"], script))


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def plan_send(key: WalletKey, rpc: IndexerRpc, recipient: str,
              amount_seeds: int, fee_rate: float,
              network: str | None = "main") -> SendPlan:
    if amount_seeds <= 0:
        raise TemplateError("nothing to send")
    recipient_script = _address_script(recipient, network)
    dust = _DUST[script_type(recipient_script)]
    if CARRIER_SATS < dust:
        raise TemplateError(
            f"orchids travel on {CARRIER_SATS} sats, below that address "
            f"type's {dust}-sat dust bound; give a taproot or native "
            "segwit address")
    if recipient_script == key.script:
        raise TemplateError("that is this wallet's own address")

    try:
        balance = rpc.getorchidbalance(key.script.hex())
    except WalletRpcError as e:
        raise TemplateError(
            "the server cannot serve settled orchid balances yet "
            f"(indexer-rpc.md §Planned); it said: {e.message}") from None
    seeds_by_outpoint = {f"{u['txid']}:{u['vout']}": u["seeds"]
                         for u in balance.get("utxos", [])
                         if u.get("seeds", 0) > 0}
    coins = {f"{u['txid']}:{u['vout']}": u
             for u in _wallet_coins(key, rpc)}

    # Settled orchid coins, largest first, until the amount is covered.
    orchid_coins = sorted(seeds_by_outpoint, key=seeds_by_outpoint.get,
                          reverse=True)
    picked: list[str] = []
    covered = 0
    for op in orchid_coins:
        if covered >= amount_seeds:
            break
        if op not in coins:
            continue            # balance and UTXO views disagree; skip
        picked.append(op)
        covered += seeds_by_outpoint[op]
    if covered < amount_seeds:
        raise TemplateError(
            f"not enough orchids: the wallet holds {covered} seeds "
            f"settled, sending {amount_seeds}")

    # Plain coins (no orchids, nothing latent) as the fee needs.
    plain = sorted((op for op, u in coins.items()
                    if op not in seeds_by_outpoint and u["latent_sats"] == 0),
                   key=lambda op: coins[op]["sats"], reverse=True)
    outputs = [TxOut(0, send_script([(amount_seeds, 2)])),
               TxOut(0, key.script),
               TxOut(CARRIER_SATS, recipient_script)]
    while True:
        input_sats = sum(coins[op]["sats"] for op in picked)
        fee = _estimate_fee(["tr"] * len(picked), outputs, fee_rate)
        change = input_sats - CARRIER_SATS - fee
        if change >= _DUST["tr"]:
            break
        if not plain:
            raise TemplateError(
                f"not enough plain sats for the fee: {input_sats} sats in, "
                f"{CARRIER_SATS} ride to the recipient, the fee is {fee}, "
                f"and the change must keep {_DUST['tr']}")
        picked.append(plain.pop(0))
    _check_fee_bound(fee, input_sats)

    outputs[1] = TxOut(change, key.script)
    inputs = [_funding(coins[op], key.script) for op in picked]
    psbt = Psbt.from_tx(_unsigned_tx(inputs, outputs))
    for n in range(len(inputs)):
        _fill_input(psbt, n, inputs[n], "tr")
        psbt.inputs[n].tap_internal_key = key.internal_key
    summary = SendSummary(tuple(picked), input_sats, covered, amount_seeds,
                          change, CARRIER_SATS, fee)
    verify_send(psbt.tx, psbt.prevouts(), key.script, recipient_script,
                summary)
    return SendPlan(psbt, summary, recipient, recipient_script)


def verify_send(tx: Transaction, prevouts: list[TxOut], wallet_script: bytes,
                recipient_script: bytes, summary: SendSummary) -> None:
    """verifySend (wallet-policy.md §Envelope scan): refuse unless the
    transaction is exactly the send the summary describes."""
    if len(tx.outputs) != 3:
        raise TemplateError("a send has exactly three outputs")
    _check_spend_envelope(tx, expect_pointer=None)
    artifact = decipher(tx)
    if len(artifact.edicts) != 1:
        raise TemplateError("a send carries exactly one edict")
    edict = artifact.edicts[0]
    if edict.id != ORCHID or edict.output != 2:
        raise TemplateError("the edict does not pay orchids to output 2")
    if edict.amount != summary.amount_seeds:
        raise TemplateError("the edict's amount is not the typed amount")
    if summary.input_seeds < summary.amount_seeds:
        raise TemplateError("the inputs' settled orchids do not cover the amount")
    if tx.outputs[1].script_pubkey != wallet_script:
        raise TemplateError("output 1 does not pay the wallet's own address")
    if tx.outputs[2].script_pubkey != recipient_script:
        raise TemplateError("output 2 does not pay the typed recipient")
    if tx.outputs[2].value != CARRIER_SATS:
        raise TemplateError(f"output 2 must carry exactly {CARRIER_SATS} sats")
    input_sats = _check_own_inputs(tx, prevouts, wallet_script, summary.inputs)
    if (input_sats != summary.input_sats
            or tx.outputs[1].value != summary.change_sats
            or input_sats - CARRIER_SATS - tx.outputs[1].value
            != summary.fee_sats):
        raise TemplateError("the sats do not match the summary")
    _check_fee_bound(summary.fee_sats, input_sats)


# ---------------------------------------------------------------------------
# Withdraw
# ---------------------------------------------------------------------------

def plan_withdraw(key: WalletKey, rpc: IndexerRpc, destination: str,
                  amount_sats: int, fee_rate: float,
                  network: str | None = "main") -> WithdrawPlan:
    if amount_sats <= 0:
        raise TemplateError("nothing to withdraw")
    dest_script = _address_script(destination, network)
    if dest_script == key.script:
        raise TemplateError("that is this wallet's own address")
    if amount_sats < _DUST[script_type(dest_script)]:
        raise TemplateError(
            f"{amount_sats} sats is below the destination's "
            f"{_DUST[script_type(dest_script)]}-sat dust bound")
    coins = {f"{u['txid']}:{u['vout']}": u for u in _wallet_coins(key, rpc)}

    # Already-claimed coins first, largest first: latent value rides the
    # sats, so coins still carrying it are the last resort (and trigger
    # the "claim first?" question).
    order = sorted(coins, key=lambda op: (coins[op]["latent_sats"] > 0,
                                          -coins[op]["sats"]))
    outputs = [TxOut(0, encipher(Envelope(pointer=1))),
               TxOut(0, key.script),
               TxOut(amount_sats, dest_script)]
    picked: list[str] = []
    input_sats = 0
    while True:
        fee = _estimate_fee(["tr"] * max(len(picked), 1), outputs, fee_rate)
        if picked and input_sats >= amount_sats + fee + _DUST["tr"]:
            break
        if not order:
            raise TemplateError(
                f"not enough sats: the wallet holds {input_sats}, the "
                f"withdrawal is {amount_sats} plus a {fee}-sat fee, and "
                f"the change must keep {_DUST['tr']}")
        op = order.pop(0)
        picked.append(op)
        input_sats += coins[op]["sats"]
    _check_fee_bound(fee, input_sats)

    outputs[1] = TxOut(input_sats - amount_sats - fee, key.script)
    inputs = [_funding(coins[op], key.script) for op in picked]
    psbt = Psbt.from_tx(_unsigned_tx(inputs, outputs))
    for n in range(len(inputs)):
        _fill_input(psbt, n, inputs[n], "tr")
        psbt.inputs[n].tap_internal_key = key.internal_key
    summary = WithdrawSummary(tuple(picked), input_sats, amount_sats,
                              input_sats - amount_sats - fee, fee)
    verify_withdraw(psbt.tx, psbt.prevouts(), key.script, dest_script,
                    summary)
    return WithdrawPlan(psbt, summary, destination, dest_script,
                        sum(coins[op]["latent_sats"] for op in picked))


def verify_withdraw(tx: Transaction, prevouts: list[TxOut],
                    wallet_script: bytes, dest_script: bytes,
                    summary: WithdrawSummary) -> None:
    """verifyWithdraw: the mandatory POINTER at the wallet's own change,
    no edicts, no LAND; only plain bitcoin leaves."""
    if len(tx.outputs) != 3:
        raise TemplateError("a withdrawal has exactly three outputs")
    _check_spend_envelope(tx, expect_pointer=1)
    if tx.outputs[1].script_pubkey != wallet_script:
        raise TemplateError("output 1 does not pay the wallet's own address")
    if tx.outputs[2].script_pubkey != dest_script:
        raise TemplateError("output 2 does not pay the typed destination")
    if tx.outputs[2].value != summary.amount_sats:
        raise TemplateError("output 2 is not the typed amount")
    input_sats = _check_own_inputs(tx, prevouts, wallet_script, summary.inputs)
    if (input_sats != summary.input_sats
            or tx.outputs[1].value != summary.change_sats
            or input_sats - summary.amount_sats - tx.outputs[1].value
            != summary.fee_sats):
        raise TemplateError("the sats do not match the summary")
    _check_fee_bound(summary.fee_sats, input_sats)


# ---------------------------------------------------------------------------
# Shared checks and signing
# ---------------------------------------------------------------------------

def _check_spend_envelope(tx: Transaction, expect_pointer: int | None) -> None:
    """Output 0 the only OP_RETURN, a clean envelope, no LAND; the
    pointer absent or at the wallet's own output 1 — for a withdrawal,
    exactly 1, mandatorily."""
    if not is_op_return(tx.outputs[0].script_pubkey):
        raise TemplateError("output 0 is not the envelope")
    if tx.outputs[0].value != 0:
        raise TemplateError("the envelope output carries sats")
    for n, out in enumerate(tx.outputs[1:], start=1):
        if is_op_return(out.script_pubkey):
            raise TemplateError(f"output {n} is a second OP_RETURN")
    artifact = decipher(tx)
    if not isinstance(artifact, Envelope):
        flaw = getattr(artifact, "flaw", "no envelope")
        raise TemplateError(f"the envelope does not decode cleanly ({flaw})")
    if artifact.land:
        raise TemplateError("the envelope carries LAND; this is not a claim")
    if expect_pointer is not None:
        if artifact.pointer != expect_pointer:
            raise TemplateError(
                "the envelope's pointer must sit at the wallet's own change")
        if artifact.edicts:
            raise TemplateError("a withdrawal carries no edicts")
    elif artifact.pointer not in (None, 1):
        raise TemplateError("the envelope's pointer leaves output 1")


def _check_own_inputs(tx: Transaction, prevouts: list[TxOut],
                      wallet_script: bytes, named: tuple[str, ...]) -> int:
    if len(prevouts) != len(tx.inputs):
        raise TemplateError("one prevout per input is required")
    outpoints = {f"{i.prev_txid[::-1].hex()}:{i.prev_vout}"
                 for i in tx.inputs}
    if len(outpoints) != len(tx.inputs):
        raise TemplateError("an input is spent twice")
    if outpoints != set(named):
        raise TemplateError("the transaction's inputs are not the summary's")
    for n, p in enumerate(prevouts):
        if p.script_pubkey != wallet_script:
            raise TemplateError(
                f"input {n} does not pay the wallet's own address")
    return sum(p.value for p in prevouts)


def _sign(psbt: Psbt, key: WalletKey, verify) -> Transaction:
    prevouts = psbt.prevouts()
    verify(psbt.tx, prevouts)
    for n in range(len(psbt.inputs)):
        psbt.inputs[n].tap_key_sig = key.sign(psbt.tx, prevouts, n)
    tx = psbt.finalize()
    verify(tx, prevouts)
    return tx


def sign_send(plan: SendPlan, key: WalletKey) -> Transaction:
    """Verify, sign, finalize, verify — §Envelope scan, both moments."""
    return _sign(plan.psbt, key,
                 lambda tx, prevouts: verify_send(
                     tx, prevouts, key.script, plan.recipient_script,
                     plan.summary))


def sign_withdraw(plan: WithdrawPlan, key: WalletKey) -> Transaction:
    return _sign(plan.psbt, key,
                 lambda tx, prevouts: verify_withdraw(
                     tx, prevouts, key.script, plan.destination_script,
                     plan.summary))
