"""The land transactions: claim and claim-and-collect, built and checked.

The templates of docs/spec/wallet-policy.md §Transaction templates, as
code. The claim lands the wallet's own coins: two outputs, the LAND-only
envelope and the wallet's change. The claim-and-collect (the harvest
model, spec/land-psbt.md) lands anyone's coins: three outputs — the
envelope, `carrier_sats` to the orchid wallet, everything else back to
the address it came from — signed by whatever wallet holds the keys.

This one module serves both callers on purpose: the CLI wallet builds
its own claims and its external form here, and the hosted land builder
(spec/land-psbt.md, `[decide]` deployment) imports the same functions —
the service must never grow a private variant of the template, or the
public `verifyCollect` check stops meaning anything.

Verification is the other half (`wallet-policy.md` §Envelope scan): the
checks run on the PSBT when it is built, and again on the signed
transaction a user brings back, and a failed check means refusal —
never a signed transaction after a failed decode. The verifiers take a
transaction plus its prevouts so both moments use the same code.

Pure functions, no RPC: the caller brings the UTXOs (and raw previous
transactions where a legacy signer will want them) from wherever it
talks to — the indexer RPC or its own node.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from claimstones.bitcoin.descriptors import KeyOrigin
from claimstones.bitcoin.psbt import Psbt, Transaction, compact_size
from claimstones.bitcoin.ripemd160 import hash160
from claimstones.bitcoin.secp import taproot_output_key
from claimstones.bitcoin.txparse import TxIn, TxOut
from claimstones.protocol.codec import (
    MAGIC, Envelope, decipher, is_op_return, land_script,
)

# The sats that ride under a token output: taproot dust plus headroom
# (wallet-policy.md §Transaction templates).
CARRIER_SATS = 330

# Every template input signals RBF (wallet-policy.md).
SEQUENCE = 0xFFFFFFFD

# Core's dust thresholds at the default 3 sat/vB relay rate, per the
# script type of the output that would receive the sats.
_DUST = {"tr": 330, "wpkh": 294, "sh(wpkh)": 540, "pkh": 546}

# Worst-case signature material per input type, for fee estimation:
# (scriptSig bytes, witness stack items). 72 covers a high-S-free DER
# signature with sighash byte; taproot key-path is one 64-byte signature.
_DUMMY = {
    "tr": (b"", [bytes(64)]),
    "wpkh": (b"", [bytes(72), bytes(33)]),
    "sh(wpkh)": (bytes(23), [bytes(72), bytes(33)]),
    "pkh": (bytes(107), []),
}


class TemplateError(ValueError):
    """Plain-words reason the wallet can show."""


@dataclass(frozen=True)
class FundingInput:
    """One coin the transaction spends. `prev_tx` is the raw previous
    transaction, wanted by legacy signers; `pubkey` and `origin` come
    from descriptors.py when the coin was found by derivation, and let
    the PSBT carry what a hardware signer needs."""
    prev_txid: bytes            # 32 bytes, internal order (as TxIn)
    prev_vout: int
    utxo: TxOut
    prev_tx: bytes | None = None
    pubkey: bytes | None = None             # compressed (33) or x-only (32)
    origin: KeyOrigin | None = None

    @property
    def outpoint(self) -> str:
        return f"{self.prev_txid[::-1].hex()}:{self.prev_vout}"


@dataclass(frozen=True)
class ClaimSummary:
    """What verifyLand holds the transaction against, and what the
    wallet shows before signing."""
    inputs: tuple[str, ...]     # outpoints, txid:vout
    input_sats: int
    change_sats: int
    fee_sats: int


@dataclass(frozen=True)
class CollectSummary:
    """What verifyCollect holds the transaction against, and what the
    frontend must show before signing: the signer pays the miner fee
    plus carrier_sats, nothing more."""
    inputs: tuple[str, ...]
    input_sats: int
    carrier_sats: int
    return_sats: int
    fee_sats: int


# ---------------------------------------------------------------------------
# Script types
# ---------------------------------------------------------------------------

def script_type(script: bytes) -> str:
    if len(script) == 34 and script[:2] == bytes([0x51, 0x20]):
        return "tr"
    if len(script) == 22 and script[:2] == bytes([0x00, 0x14]):
        return "wpkh"
    if (len(script) == 23 and script[:2] == bytes([0xA9, 0x14])
            and script[-1] == 0x87):
        return "sh(wpkh)"
    if (len(script) == 25 and script[:3] == bytes([0x76, 0xA9, 0x14])
            and script[-2:] == bytes([0x88, 0xAC])):
        return "pkh"
    raise TemplateError(
        "an input pays a script this builder cannot spend; only single-key "
        "coins (taproot, segwit, legacy) can fund a land")


def _check_key_matches(kind: str, pubkey: bytes, script: bytes) -> None:
    if kind == "tr":
        internal = pubkey[1:] if len(pubkey) == 33 else pubkey
        ok = taproot_output_key(internal) == script[2:]
    elif kind == "wpkh":
        ok = len(pubkey) == 33 and hash160(pubkey) == script[2:]
    elif kind == "sh(wpkh)":
        ok = (len(pubkey) == 33
              and hash160(bytes([0x00, 0x14]) + hash160(pubkey)) == script[2:22])
    else:   # pkh
        ok = len(pubkey) == 33 and hash160(pubkey) == script[3:23]
    if not ok:
        raise TemplateError("the given key does not match the coin's script")


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _estimate_fee(kinds: list[str], outputs: list[TxOut], fee_rate: float) -> int:
    """Fee for the signed transaction at fee_rate sat/vB, from a build
    with worst-case signature material in place."""
    if fee_rate < 1:
        raise TemplateError("fee rate below 1 sat/vB would not relay")
    tx = Transaction()
    for kind in kinds:
        script_sig, witness = _DUMMY[kind]
        tx.inputs.append(TxIn(bytes(32), 0, script_sig, SEQUENCE,
                              witness or None))
    tx.outputs = list(outputs)
    return math.ceil(tx.vsize() * fee_rate)


def _unsigned_tx(inputs: list[FundingInput], outputs: list[TxOut]) -> Transaction:
    tx = Transaction()
    for f in inputs:
        tx.inputs.append(TxIn(f.prev_txid, f.prev_vout, b"", SEQUENCE))
    tx.outputs = outputs
    return tx


def _origin_value(origin: KeyOrigin) -> bytes:
    return origin.fingerprint + b"".join(
        i.to_bytes(4, "little") for i in origin.path)


def _check_prev_tx(f: FundingInput) -> None:
    prev = Transaction.parse(f.prev_tx)
    if prev.txid() != f.prev_txid:
        raise TemplateError(
            f"the previous transaction given for {f.outpoint} is a "
            "different transaction")
    if f.prev_vout >= len(prev.outputs) or prev.outputs[f.prev_vout] != f.utxo:
        raise TemplateError(
            f"the previous transaction given for {f.outpoint} does not "
            "pay that coin")


def _fill_input(psbt: Psbt, n: int, f: FundingInput, kind: str) -> None:
    inp = psbt.inputs[n]
    inp.witness_utxo = f.utxo
    if f.prev_tx is not None:
        _check_prev_tx(f)
        inp.non_witness_utxo = f.prev_tx
    elif kind == "pkh":
        raise TemplateError(
            f"{f.outpoint} is a legacy coin; its signer needs the raw "
            "previous transaction and none was given")
    if f.pubkey is None:
        return
    _check_key_matches(kind, f.pubkey, f.utxo.script_pubkey)
    if kind == "tr":
        xonly = f.pubkey[1:] if len(f.pubkey) == 33 else f.pubkey
        inp.tap_internal_key = xonly
        if f.origin is not None:
            inp.tap_bip32_derivation[xonly] = (
                compact_size(0) + _origin_value(f.origin))
        return
    if len(f.pubkey) != 33:
        raise TemplateError("an x-only key fits only a taproot coin")
    if kind == "sh(wpkh)":
        inp.redeem_script = bytes([0x00, 0x14]) + hash160(f.pubkey)
    if f.origin is not None:
        inp.bip32_derivation[f.pubkey] = _origin_value(f.origin)


def build_claim(inputs: list[FundingInput], wallet_script: bytes,
                internal_key: bytes, fee_rate: float) -> tuple[Psbt, ClaimSummary]:
    """The claim template: the wallet lands its own coins. Exactly two
    outputs — the LAND-only envelope, then the wallet's own address with
    all input sats minus the miner fee."""
    if not inputs:
        raise TemplateError("nothing to claim: no coins given")
    if script_type(wallet_script) != "tr":
        raise TemplateError("the wallet address must be taproot")
    if taproot_output_key(internal_key) != wallet_script[2:]:
        raise TemplateError("the internal key does not match the wallet address")
    for f in inputs:
        if f.utxo.script_pubkey != wallet_script:
            raise TemplateError(
                f"{f.outpoint} does not pay the wallet's own address; "
                "a claim spends only the wallet's coins")

    input_sats = sum(f.utxo.value for f in inputs)
    outputs = [TxOut(0, land_script()), TxOut(0, wallet_script)]
    fee = _estimate_fee(["tr"] * len(inputs), outputs, fee_rate)
    _check_fee_bound(fee, input_sats)
    change = input_sats - fee
    if change < _DUST["tr"]:
        raise TemplateError(
            f"the coins are too small: {input_sats} sats minus the "
            f"{fee}-sat fee leaves {change}, below the {_DUST['tr']}-sat dust bound")
    outputs[1] = TxOut(change, wallet_script)

    psbt = Psbt.from_tx(_unsigned_tx(inputs, outputs))
    for n, f in enumerate(inputs):
        _fill_input(psbt, n, f, "tr")
        psbt.inputs[n].tap_internal_key = internal_key
    summary = ClaimSummary(tuple(f.outpoint for f in inputs),
                           input_sats, change, fee)
    verify_land(psbt.tx, psbt.prevouts(), wallet_script, summary)
    return psbt, summary


def build_collect(inputs: list[FundingInput], orchids_script: bytes,
                  return_script: bytes, fee_rate: float) -> tuple[Psbt, CollectSummary]:
    """The claim-and-collect template (the harvest model): land external
    coins, orchids to the wallet on the carrier, the bitcoin back where
    it came from. One funding script per transaction — the return output
    is then always unambiguous (spec/land-psbt.md)."""
    if not inputs:
        raise TemplateError("nothing to land: no coins given")
    kinds = [script_type(f.utxo.script_pubkey) for f in inputs]
    for f in inputs:
        if f.utxo.script_pubkey != return_script:
            raise TemplateError(
                f"{f.outpoint} does not pay the funding address; one "
                "funding address per transaction")
        if f.utxo.script_pubkey == orchids_script:
            raise TemplateError(
                "the funding address and the orchid address are the same; "
                "that is a plain claim, not a collect")
    script_type(orchids_script)   # must be a known single-key script

    input_sats = sum(f.utxo.value for f in inputs)
    outputs = [TxOut(0, land_script()),
               TxOut(CARRIER_SATS, orchids_script),
               TxOut(0, return_script)]
    fee = _estimate_fee(kinds, outputs, fee_rate)
    _check_fee_bound(fee, input_sats)
    returned = input_sats - CARRIER_SATS - fee
    dust = _DUST[kinds[0]]
    if returned < dust:
        raise TemplateError(
            f"the coins are too small: {input_sats} sats minus the "
            f"{CARRIER_SATS}-sat carrier and the {fee}-sat fee leaves "
            f"{returned}, below the {dust}-sat dust bound")
    outputs[2] = TxOut(returned, return_script)

    psbt = Psbt.from_tx(_unsigned_tx(inputs, outputs))
    for n, (f, kind) in enumerate(zip(inputs, kinds)):
        _fill_input(psbt, n, f, kind)
    summary = CollectSummary(tuple(f.outpoint for f in inputs),
                             input_sats, CARRIER_SATS, returned, fee)
    verify_collect(psbt.tx, psbt.prevouts(), orchids_script, summary)
    return psbt, summary


# ---------------------------------------------------------------------------
# Verification (wallet-policy.md §Envelope scan before signing)
# ---------------------------------------------------------------------------

def _check_fee_bound(fee: int, input_sats: int) -> None:
    if fee * 5 > input_sats:
        raise TemplateError(
            f"the fee ({fee} sats) is more than a fifth of the input "
            f"sats ({input_sats}); use a lower fee rate or bigger coins")


def _check_envelope(tx: Transaction) -> None:
    """Output 0 the only OP_RETURN, deciphering to LAND alone."""
    if tx.outputs[0].script_pubkey[:2] != MAGIC:
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
    if not artifact.land:
        raise TemplateError("the envelope carries no LAND")
    if artifact.edicts:
        raise TemplateError("a land envelope must carry no edicts")
    if artifact.pointer not in (None, 1):
        raise TemplateError("the envelope's pointer leaves output 1")


def _check_inputs_named(tx: Transaction, prevouts: list[TxOut],
                        named: tuple[str, ...]) -> int:
    if len(prevouts) != len(tx.inputs):
        raise TemplateError("one prevout per input is required")
    outpoints = {f"{i.prev_txid[::-1].hex()}:{i.prev_vout}" for i in tx.inputs}
    if len(outpoints) != len(tx.inputs):
        raise TemplateError("an input is spent twice")
    if outpoints != set(named):
        raise TemplateError("the transaction's inputs are not the summary's")
    return sum(p.value for p in prevouts)


def verify_land(tx: Transaction, prevouts: list[TxOut],
                wallet_script: bytes, summary: ClaimSummary) -> None:
    """verifyLand: refuse unless the transaction is exactly the claim
    template the summary describes. For a PSBT, pass psbt.tx and
    psbt.prevouts(); for a signed transaction, the same prevouts."""
    if len(tx.outputs) != 2:
        raise TemplateError("a claim has exactly two outputs")
    _check_envelope(tx)
    if tx.outputs[1].script_pubkey != wallet_script:
        raise TemplateError("output 1 does not pay the wallet's own address")
    input_sats = _check_inputs_named(tx, prevouts, summary.inputs)
    for n, p in enumerate(prevouts):
        if p.script_pubkey != wallet_script:
            raise TemplateError(
                f"input {n} does not pay the wallet's own address")
    if (input_sats != summary.input_sats
            or tx.outputs[1].value != summary.change_sats
            or input_sats - tx.outputs[1].value != summary.fee_sats):
        raise TemplateError("the sats do not match the summary")
    _check_fee_bound(summary.fee_sats, input_sats)


def verify_collect(tx: Transaction, prevouts: list[TxOut],
                   orchids_script: bytes, summary: CollectSummary) -> None:
    """verifyCollect: refuse unless the transaction is exactly the
    claim-and-collect template the summary describes. Runs on the built
    PSBT and again on the signed transaction a user brings back, before
    it goes anywhere near sendrawtransaction."""
    if len(tx.outputs) != 3:
        raise TemplateError("a claim-and-collect has exactly three outputs")
    _check_envelope(tx)
    if tx.outputs[1].script_pubkey != orchids_script:
        raise TemplateError("output 1 does not pay the orchid wallet")
    if tx.outputs[1].value != summary.carrier_sats or summary.carrier_sats != CARRIER_SATS:
        raise TemplateError(f"output 1 must carry exactly {CARRIER_SATS} sats")
    input_scripts = {p.script_pubkey for p in prevouts}
    if tx.outputs[2].script_pubkey not in input_scripts:
        raise TemplateError(
            "output 2 does not return to the address the coins came from")
    if orchids_script in input_scripts:
        raise TemplateError(
            "an input pays the orchid wallet; a collect spends only "
            "external coins")
    input_sats = _check_inputs_named(tx, prevouts, summary.inputs)
    returned = tx.outputs[2].value
    if (input_sats != summary.input_sats
            or returned != summary.return_sats
            or input_sats - CARRIER_SATS - returned != summary.fee_sats):
        raise TemplateError("the sats do not match the summary")
    _check_fee_bound(summary.fee_sats, input_sats)
