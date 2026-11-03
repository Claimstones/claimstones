"""Bech32 and bech32m (BIP-173, BIP-350): segwit addresses and the
orchid wallet code (a 32-byte secret under the human-readable part
"orchid", docs/spec/wallet-policy.md).

Standard library only.
"""

from __future__ import annotations

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_CONST = 1
BECH32M_CONST = 0x2BC830A3

# network -> human-readable part of its segwit addresses
HRP = {"mainnet": "bc", "main": "bc", "testnet": "tb", "test": "tb",
       "signet": "tb", "regtest": "bcrt"}


class Bech32Error(ValueError):
    pass


def _polymod(values) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= gen[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _create_checksum(hrp: str, data: list[int], const: int) -> list[int]:
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0] * 6) ^ const
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def convertbits(data, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or value >> frombits:
            raise Bech32Error("value out of range")
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise Bech32Error("invalid padding")
    return ret


def encode(hrp: str, data: list[int], const: int = BECH32M_CONST) -> str:
    combined = data + _create_checksum(hrp, data, const)
    return hrp + "1" + "".join(CHARSET[d] for d in combined)


def decode(s: str) -> tuple[str, list[int], int]:
    """Returns (hrp, 5-bit data without checksum, constant used)."""
    if any(ord(c) < 33 or ord(c) > 126 for c in s):
        raise Bech32Error("invalid character")
    if s.lower() != s and s.upper() != s:
        raise Bech32Error("mixed case")
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s) or len(s) > 90:
        raise Bech32Error("invalid separator position or length")
    hrp, body = s[:pos], s[pos + 1:]
    if any(c not in CHARSET for c in body):
        raise Bech32Error("invalid character")
    data = [CHARSET.find(c) for c in body]
    const = _polymod(_hrp_expand(hrp) + data)
    if const not in (BECH32_CONST, BECH32M_CONST):
        raise Bech32Error("checksum mismatch")
    return hrp, data[:-6], const


# ---------------------------------------------------------------------------
# Segwit addresses
# ---------------------------------------------------------------------------

def decode_segwit(hrp: str, addr: str) -> tuple[int, bytes]:
    """(witness version, program) of a segwit address for the given hrp."""
    got_hrp, data, const = decode(addr)
    if got_hrp != hrp:
        raise Bech32Error(f"address is for another network (expected {hrp}1...)")
    if not data:
        raise Bech32Error("empty address")
    version = data[0]
    program = bytes(convertbits(data[1:], 5, 8, False))
    if version > 16 or not 2 <= len(program) <= 40:
        raise Bech32Error("invalid witness program")
    if version == 0 and len(program) not in (20, 32):
        raise Bech32Error("invalid v0 program length")
    if (version == 0) != (const == BECH32_CONST):
        raise Bech32Error("wrong checksum variant for the witness version")
    return version, program


def encode_segwit(hrp: str, version: int, program: bytes) -> str:
    const = BECH32_CONST if version == 0 else BECH32M_CONST
    return encode(hrp, [version] + convertbits(program, 8, 5), const)


def address_to_script(hrp: str, addr: str) -> bytes:
    version, program = decode_segwit(hrp, addr)
    op = 0 if version == 0 else 0x50 + version
    return bytes([op, len(program)]) + program


def script_to_address(hrp: str, script: bytes) -> str | None:
    """Segwit script -> address, or None for a non-segwit script."""
    if len(script) < 4 or len(script) != script[1] + 2:
        return None
    op = script[0]
    if op == 0:
        version = 0
    elif 0x51 <= op <= 0x60:
        version = op - 0x50
    else:
        return None
    return encode_segwit(hrp, version, script[2:])


def p2tr_script(output_key_xonly: bytes) -> bytes:
    if len(output_key_xonly) != 32:
        raise ValueError("x-only key must be 32 bytes")
    return bytes([0x51, 0x20]) + output_key_xonly


def is_p2tr(script: bytes) -> bool:
    return len(script) == 34 and script[0] == 0x51 and script[1] == 0x20


# ---------------------------------------------------------------------------
# Wallet code (docs/spec/wallet-policy.md, Keys and address)
# ---------------------------------------------------------------------------

WALLET_CODE_HRP = "orchid"


def encode_wallet_code(secret: bytes) -> str:
    if len(secret) != 32:
        raise ValueError("wallet secret must be 32 bytes")
    return encode(WALLET_CODE_HRP, convertbits(secret, 8, 5), BECH32M_CONST)


def decode_wallet_code(code: str) -> bytes:
    hrp, data, const = decode(code.strip())
    if hrp != WALLET_CODE_HRP or const != BECH32M_CONST:
        raise Bech32Error("not a wallet code (expected orchid1...)")
    secret = bytes(convertbits(data, 5, 8, False))
    if len(secret) != 32:
        raise Bech32Error("wallet code has the wrong length")
    return secret
