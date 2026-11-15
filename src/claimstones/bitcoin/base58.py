"""Base58Check decoding, for the two legacy address forms.

The indexer RPC (docs/spec/indexer-rpc.md) deliberately speaks only
bech32/bech32m and raw script hex. The public gateway cannot be that
strict: a visitor pastes whatever their wallet shows them, and a lot of
coins still sit on 1... and 3... addresses (m.../n.../2... on the test
networks). Nothing in this codebase
ever needs to *produce* a legacy address; `encode_check` exists for
extended public keys (descriptors.py re-serializes a SLIP-132 key in
its canonical xpub/tpub form), not for addresses.
"""

from __future__ import annotations

import hashlib

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}

# Version bytes per chain (the chain names of getblockchaininfo). Another
# chain's address is rejected on purpose: answered with zeros it would
# read as "this address is empty" when the truth is "wrong network".
P2PKH_VERSION = 0x00
P2SH_VERSION = 0x05
TEST_P2PKH_VERSION = 0x6F                              # m... / n...
TEST_P2SH_VERSION = 0xC4                               # 2...
VERSIONS = {"main": (P2PKH_VERSION, P2SH_VERSION),
            "mainnet": (P2PKH_VERSION, P2SH_VERSION)}
for _chain in ("test", "testnet", "testnet4", "signet", "regtest"):
    VERSIONS[_chain] = (TEST_P2PKH_VERSION, TEST_P2SH_VERSION)


class Base58Error(ValueError):
    pass


def decode_check(s: str) -> bytes:
    """Base58Check string -> payload (version byte included)."""
    # 112 covers every address and extended public key; the cap exists so
    # the gateway never chews on an arbitrarily long pasted line.
    if not s or len(s) > 112:
        raise Base58Error("not a base58 string")
    n = 0
    for c in s:
        if c not in _INDEX:
            raise Base58Error(f"invalid base58 character {c!r}")
        n = n * 58 + _INDEX[c]
    # each leading '1' is a literal zero byte, lost in the integer form
    zeros = len(s) - len(s.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    raw = b"\x00" * zeros + body
    if len(raw) < 5:
        raise Base58Error("too short for a checksum")
    payload, check = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != check:
        raise Base58Error("bad checksum")
    return payload


def encode_check(payload: bytes) -> str:
    """Payload -> Base58Check string (version byte included in payload)."""
    check = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    raw = payload + check
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = ALPHABET[r] + out
    zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * zeros + out


def address_to_script(addr: str, chain: str = "main") -> bytes:
    """Legacy address of `chain` -> scriptPubKey."""
    payload = decode_check(addr)
    version, h = payload[0], payload[1:]
    if len(h) != 20:
        raise Base58Error("payload is not a 20-byte hash")
    p2pkh, p2sh = VERSIONS[chain]
    if version == p2pkh:                               # 1... / m... n...
        return bytes([0x76, 0xA9, 0x14]) + h + bytes([0x88, 0xAC])
    if version == p2sh:                                # 3... / 2...
        return bytes([0xA9, 0x14]) + h + bytes([0x87])
    if any(version in v for v in VERSIONS.values()):
        other = "mainnet" if version in VERSIONS["main"] else "a test network"
        raise Base58Error(f"wrong network: this is an address for {other}, "
                          f"not {chain}")
    raise Base58Error(f"unsupported address version {version}")
