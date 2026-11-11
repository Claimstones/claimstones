"""Transactions and PSBTs for release-1 wallets (BIP-141, BIP-174, BIP-341).

Just enough for the release-1 templates (docs/spec/wallet-policy.md
§Transaction templates). The wallet's own transactions are taproot
key-path spends: `witness_utxo` and `tap_internal_key` per input,
`tap_key_sig` back, finalisation is the witness [signature]. The
claim-and-collect PSBT (spec/land-psbt.md) is signed by someone else's
wallet, so an input may also carry what that wallet's signer wants:
`non_witness_utxo`, `redeem_script` for sh(wpkh), and the key origins
(`bip32_derivation`, `tap_bip32_derivation`) a hardware signer finds
its key by.

Standard library only; deterministic.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

from claimstones.bitcoin.secp import (
    schnorr_sign, schnorr_verify, tagged_hash, taproot_tweak_seckey,
)
from claimstones.bitcoin.txparse import TxIn, TxOut, dsha256, read_compact_size

SIGHASH_DEFAULT = 0x00
SIGHASH_ALL = 0x01

PSBT_MAGIC = b"psbt\xff"
PSBT_GLOBAL_UNSIGNED_TX = 0x00
PSBT_IN_NON_WITNESS_UTXO = 0x00
PSBT_IN_WITNESS_UTXO = 0x01
PSBT_IN_REDEEM_SCRIPT = 0x04
PSBT_IN_BIP32_DERIVATION = 0x06
PSBT_IN_FINAL_SCRIPTSIG = 0x07
PSBT_IN_FINAL_SCRIPTWITNESS = 0x08
PSBT_IN_TAP_KEY_SIG = 0x13
PSBT_IN_TAP_BIP32_DERIVATION = 0x16
PSBT_IN_TAP_INTERNAL_KEY = 0x17


class PsbtError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def compact_size(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def _var_bytes(b: bytes) -> bytes:
    return compact_size(len(b)) + b


def serialize_txout(o: TxOut) -> bytes:
    return o.value.to_bytes(8, "little") + _var_bytes(o.script_pubkey)


class _Reader:
    def __init__(self, raw: bytes, pos: int = 0):
        self.raw = raw
        self.pos = pos

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.raw):
            raise PsbtError("truncated data")
        out = self.raw[self.pos:self.pos + n]
        self.pos += n
        return out

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def varint(self) -> int:
        try:
            n, self.pos = read_compact_size(self.raw, self.pos)
        except IndexError:
            raise PsbtError("truncated varint")
        return n

    def var_bytes(self) -> bytes:
        return self.take(self.varint())

    @property
    def done(self) -> bool:
        return self.pos >= len(self.raw)


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    version: int = 2
    inputs: list[TxIn] = field(default_factory=list)
    outputs: list[TxOut] = field(default_factory=list)
    locktime: int = 0

    def serialize(self, with_witness: bool = True) -> bytes:
        has_witness = with_witness and any(i.witness for i in self.inputs)
        out = bytearray(self.version.to_bytes(4, "little"))
        if has_witness:
            out += b"\x00\x01"
        out += compact_size(len(self.inputs))
        for i in self.inputs:
            out += i.prev_txid + i.prev_vout.to_bytes(4, "little")
            out += _var_bytes(i.script_sig) + i.sequence.to_bytes(4, "little")
        out += compact_size(len(self.outputs))
        for o in self.outputs:
            out += serialize_txout(o)
        if has_witness:
            for i in self.inputs:
                items = i.witness or []
                out += compact_size(len(items))
                for item in items:
                    out += _var_bytes(item)
        out += self.locktime.to_bytes(4, "little")
        return bytes(out)

    @classmethod
    def parse(cls, raw: bytes) -> "Transaction":
        r = _Reader(raw)
        tx = cls(version=r.u32())
        n_in = r.varint()
        segwit = False
        if n_in == 0 and not r.done and r.raw[r.pos] == 1:
            r.take(1)
            segwit = True
            n_in = r.varint()
        for _ in range(n_in):
            prev = r.take(32)
            vout = r.u32()
            script_sig = r.var_bytes()
            seq = r.u32()
            tx.inputs.append(TxIn(prev, vout, script_sig, seq))
        for _ in range(r.varint()):
            value = r.u64()
            tx.outputs.append(TxOut(value, r.var_bytes()))
        if segwit:
            for i in tx.inputs:
                i.witness = [r.var_bytes() for _ in range(r.varint())]
        tx.locktime = r.u32()
        if not r.done:
            raise PsbtError("trailing bytes after the transaction")
        return tx

    def txid(self) -> bytes:
        """Internal byte order (as in TxIn.prev_txid)."""
        return dsha256(self.serialize(with_witness=False))

    @property
    def txid_hex(self) -> str:
        return self.txid()[::-1].hex()

    def weight(self) -> int:
        base = len(self.serialize(with_witness=False))
        total = len(self.serialize(with_witness=True))
        return base * 3 + total

    def vsize(self) -> int:
        return (self.weight() + 3) // 4


# ---------------------------------------------------------------------------
# BIP-341 key-path sighash
# ---------------------------------------------------------------------------

def taproot_sighash(tx: Transaction, prevouts: list[TxOut], index: int,
                    hash_type: int = SIGHASH_DEFAULT) -> bytes:
    """Signature message hash for a key-path spend of input `index`.
    Only SIGHASH_DEFAULT and SIGHASH_ALL are supported (no annex)."""
    if hash_type not in (SIGHASH_DEFAULT, SIGHASH_ALL):
        raise PsbtError("unsupported sighash type")
    if len(prevouts) != len(tx.inputs):
        raise PsbtError("one prevout per input is required")
    sha = hashlib.sha256
    sha_prevouts = sha(b"".join(i.prev_txid + i.prev_vout.to_bytes(4, "little") for i in tx.inputs)).digest()
    sha_amounts = sha(b"".join(p.value.to_bytes(8, "little") for p in prevouts)).digest()
    sha_scripts = sha(b"".join(_var_bytes(p.script_pubkey) for p in prevouts)).digest()
    sha_sequences = sha(b"".join(i.sequence.to_bytes(4, "little") for i in tx.inputs)).digest()
    sha_outputs = sha(b"".join(serialize_txout(o) for o in tx.outputs)).digest()
    msg = (bytes([0x00, hash_type]) + tx.version.to_bytes(4, "little") + tx.locktime.to_bytes(4, "little")
           + sha_prevouts + sha_amounts + sha_scripts + sha_sequences + sha_outputs
           + b"\x00" + index.to_bytes(4, "little"))
    return tagged_hash("TapSighash", msg)


def sign_input(tx: Transaction, prevouts: list[TxOut], index: int, seckey: int,
               aux_rand: bytes = b"\x00" * 32, hash_type: int = SIGHASH_DEFAULT) -> bytes:
    """Key-path signature with the taproot-tweaked secret (tests and tools)."""
    digest = taproot_sighash(tx, prevouts, index, hash_type)
    sig = schnorr_sign(digest, taproot_tweak_seckey(seckey), aux_rand)
    return sig if hash_type == SIGHASH_DEFAULT else sig + bytes([hash_type])


def verify_input(tx: Transaction, prevouts: list[TxOut], index: int, sig: bytes) -> bool:
    """Verify a tap_key_sig against the input's P2TR output key."""
    if len(sig) == 65 and sig[64] == SIGHASH_ALL:
        hash_type, sig = SIGHASH_ALL, sig[:64]
    elif len(sig) == 64:
        hash_type = SIGHASH_DEFAULT
    else:
        return False
    spk = prevouts[index].script_pubkey
    if len(spk) != 34 or spk[0] != 0x51 or spk[1] != 0x20:
        return False
    return schnorr_verify(taproot_sighash(tx, prevouts, index, hash_type), spk[2:], sig)


# ---------------------------------------------------------------------------
# PSBT v0
# ---------------------------------------------------------------------------

@dataclass
class PsbtInput:
    witness_utxo: TxOut | None = None
    non_witness_utxo: bytes | None = None    # the raw previous transaction
    redeem_script: bytes | None = None
    tap_internal_key: bytes | None = None
    tap_key_sig: bytes | None = None
    final_script_sig: bytes | None = None    # a finalizing signer's 0x07
    final_script_witness: bytes | None = None   # ... and 0x08, serialized
    # pubkey (33 bytes) -> fingerprint + uint32-LE path (BIP-174 0x06)
    bip32_derivation: dict[bytes, bytes] = field(default_factory=dict)
    # x-only key (32 bytes) -> leaf-hash count + fingerprint + path (0x16)
    tap_bip32_derivation: dict[bytes, bytes] = field(default_factory=dict)
    unknown: dict[bytes, bytes] = field(default_factory=dict)   # full key -> value


@dataclass
class Psbt:
    tx: Transaction
    inputs: list[PsbtInput] = field(default_factory=list)
    global_unknown: dict[bytes, bytes] = field(default_factory=dict)
    output_unknown: list[dict[bytes, bytes]] = field(default_factory=list)

    @classmethod
    def from_tx(cls, tx: Transaction) -> "Psbt":
        return cls(tx, [PsbtInput() for _ in tx.inputs], {}, [{} for _ in tx.outputs])

    # --- encoding --------------------------------------------------------

    def serialize(self) -> bytes:
        out = bytearray(PSBT_MAGIC)
        out += _kv(bytes([PSBT_GLOBAL_UNSIGNED_TX]), self.tx.serialize(with_witness=False))
        for k, v in sorted(self.global_unknown.items()):
            out += _kv(k, v)
        out += b"\x00"
        for inp in self.inputs:
            if inp.non_witness_utxo is not None:
                out += _kv(bytes([PSBT_IN_NON_WITNESS_UTXO]), inp.non_witness_utxo)
            if inp.witness_utxo is not None:
                out += _kv(bytes([PSBT_IN_WITNESS_UTXO]), serialize_txout(inp.witness_utxo))
            if inp.redeem_script is not None:
                out += _kv(bytes([PSBT_IN_REDEEM_SCRIPT]), inp.redeem_script)
            for pub, origin in sorted(inp.bip32_derivation.items()):
                out += _kv(bytes([PSBT_IN_BIP32_DERIVATION]) + pub, origin)
            if inp.final_script_sig is not None:
                out += _kv(bytes([PSBT_IN_FINAL_SCRIPTSIG]), inp.final_script_sig)
            if inp.final_script_witness is not None:
                out += _kv(bytes([PSBT_IN_FINAL_SCRIPTWITNESS]),
                           inp.final_script_witness)
            if inp.tap_key_sig is not None:
                out += _kv(bytes([PSBT_IN_TAP_KEY_SIG]), inp.tap_key_sig)
            for pub, origin in sorted(inp.tap_bip32_derivation.items()):
                out += _kv(bytes([PSBT_IN_TAP_BIP32_DERIVATION]) + pub, origin)
            if inp.tap_internal_key is not None:
                out += _kv(bytes([PSBT_IN_TAP_INTERNAL_KEY]), inp.tap_internal_key)
            for k, v in sorted(inp.unknown.items()):
                out += _kv(k, v)
            out += b"\x00"
        for unknown in self.output_unknown:
            for k, v in sorted(unknown.items()):
                out += _kv(k, v)
            out += b"\x00"
        return bytes(out)

    def to_base64(self) -> str:
        return base64.b64encode(self.serialize()).decode("ascii")

    # --- decoding --------------------------------------------------------

    @classmethod
    def parse(cls, raw: bytes) -> "Psbt":
        r = _Reader(raw)
        if r.take(5) != PSBT_MAGIC:
            raise PsbtError("not a PSBT (bad magic)")
        globals_ = _read_map(r)
        tx_raw = globals_.pop(bytes([PSBT_GLOBAL_UNSIGNED_TX]), None)
        if tx_raw is None:
            raise PsbtError("PSBT has no unsigned transaction")
        tx = Transaction.parse(tx_raw)
        if any(i.witness for i in tx.inputs) or any(i.script_sig for i in tx.inputs):
            raise PsbtError("the unsigned transaction must have empty scriptSigs and no witness")
        psbt = cls(tx, [], globals_, [])
        for _ in tx.inputs:
            m = _read_map(r)
            inp = PsbtInput()
            wu = m.pop(bytes([PSBT_IN_WITNESS_UTXO]), None)
            if wu is not None:
                rr = _Reader(wu)
                inp.witness_utxo = TxOut(rr.u64(), rr.var_bytes())
                if not rr.done:
                    raise PsbtError("malformed witness_utxo")
            inp.non_witness_utxo = m.pop(bytes([PSBT_IN_NON_WITNESS_UTXO]), None)
            inp.redeem_script = m.pop(bytes([PSBT_IN_REDEEM_SCRIPT]), None)
            inp.final_script_sig = m.pop(bytes([PSBT_IN_FINAL_SCRIPTSIG]), None)
            inp.final_script_witness = m.pop(
                bytes([PSBT_IN_FINAL_SCRIPTWITNESS]), None)
            inp.tap_key_sig = m.pop(bytes([PSBT_IN_TAP_KEY_SIG]), None)
            if inp.tap_key_sig is not None and len(inp.tap_key_sig) not in (64, 65):
                raise PsbtError("tap_key_sig must be 64 or 65 bytes")
            inp.tap_internal_key = m.pop(bytes([PSBT_IN_TAP_INTERNAL_KEY]), None)
            if inp.tap_internal_key is not None and len(inp.tap_internal_key) != 32:
                raise PsbtError("tap_internal_key must be 32 bytes")
            for key in list(m):
                if key[0] == PSBT_IN_BIP32_DERIVATION and len(key) == 34:
                    inp.bip32_derivation[key[1:]] = m.pop(key)
                elif key[0] == PSBT_IN_TAP_BIP32_DERIVATION and len(key) == 33:
                    inp.tap_bip32_derivation[key[1:]] = m.pop(key)
            inp.unknown = m
            psbt.inputs.append(inp)
        for _ in tx.outputs:
            psbt.output_unknown.append(_read_map(r))
        if not r.done:
            raise PsbtError("trailing bytes after the PSBT")
        return psbt

    @classmethod
    def from_base64(cls, s: str) -> "Psbt":
        try:
            raw = base64.b64decode(s.strip(), validate=True)
        except (ValueError, TypeError):
            raise PsbtError("PSBT is not valid base64")
        return cls.parse(raw)

    # --- finalisation ----------------------------------------------------

    def prevouts(self) -> list[TxOut]:
        outs = []
        for n, inp in enumerate(self.inputs):
            if inp.witness_utxo is None:
                raise PsbtError(f"input {n} has no witness_utxo")
            outs.append(inp.witness_utxo)
        return outs

    def finalize(self) -> Transaction:
        """Key-path witnesses from the tap_key_sigs; returns the signed tx.
        Every signature is verified first."""
        prevouts = self.prevouts()
        signed = Transaction.parse(self.tx.serialize(with_witness=False))
        for n, (inp, txin) in enumerate(zip(self.inputs, signed.inputs)):
            if inp.tap_key_sig is None:
                raise PsbtError(f"input {n} is not signed")
            if not verify_input(self.tx, prevouts, n, inp.tap_key_sig):
                raise PsbtError(f"the signature on input {n} does not verify")
            txin.witness = [inp.tap_key_sig]
        return signed

    def extract(self) -> Transaction:
        """The signed transaction from a PSBT another wallet finalized:
        final_scriptSig / final_scriptwitness where present, taproot
        key-path tap_key_sig (verified) as the fallback. ECDSA material
        in the final fields is assembled, not verified — signature
        validity is the network's check; the template checks are the
        caller's (wallet-policy.md §Envelope scan)."""
        signed = Transaction.parse(self.tx.serialize(with_witness=False))
        for n, (inp, txin) in enumerate(zip(self.inputs, signed.inputs)):
            if (inp.final_script_sig is not None
                    or inp.final_script_witness is not None):
                txin.script_sig = inp.final_script_sig or b""
                if inp.final_script_witness:
                    r = _Reader(inp.final_script_witness)
                    txin.witness = [r.var_bytes() for _ in range(r.varint())]
                    if not r.done:
                        raise PsbtError(
                            f"malformed final_scriptwitness on input {n}")
            elif inp.tap_key_sig is not None:
                if not verify_input(self.tx, self.prevouts(), n,
                                    inp.tap_key_sig):
                    raise PsbtError(
                        f"the signature on input {n} does not verify")
                txin.witness = [inp.tap_key_sig]
            else:
                raise PsbtError(f"input {n} is not signed")
        return signed


def _kv(key: bytes, value: bytes) -> bytes:
    return _var_bytes(key) + _var_bytes(value)


def _read_map(r: _Reader) -> dict[bytes, bytes]:
    m: dict[bytes, bytes] = {}
    while True:
        key = r.var_bytes()
        if not key:
            return m
        if key in m:
            raise PsbtError("duplicate key in PSBT map")
        m[key] = r.var_bytes()
