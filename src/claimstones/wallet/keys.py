"""The wallet's own key (wallet-policy.md §Keys and address).

One taproot key per garden, one address, reused. Backup = the wallet
code: the 32-byte secret as bech32m under the human-readable part
"orchid" — the code alone restores the wallet, no seed phrase.

Derivation, pinned byte for byte by tests/test_wallet_vectors.py:
master = HMAC-SHA512(key "orchids/1", secret); garden key =
HMAC-SHA512(master, "<garden>/<index>")[0:32] mod n — garden "bitcoin",
index 0. Rotation later = index 1, same code.

How the secret rests on disk is each front end's business (the policy
asks only that the code alone restores). This reference CLI keeps the
wallet code in a plain file created with owner-only permissions: the
code IS the wallet, so the file deserves the same care as a key file
in a bitcoind wallet directory.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from claimstones.bitcoin import secp
from claimstones.bitcoin.bech32 import (
    HRP, Bech32Error, decode_wallet_code, encode_segwit, encode_wallet_code,
    p2tr_script,
)
from claimstones.bitcoin.psbt import Transaction, sign_input
from claimstones.bitcoin.txparse import TxOut

GARDEN = "bitcoin"


class WalletKeyError(ValueError):
    """Plain-words reason the wallet can show."""


def garden_key(secret: bytes, garden: str = GARDEN, index: int = 0) -> int:
    master = hmac.new(b"orchids/1", secret, hashlib.sha512).digest()
    child = hmac.new(master, f"{garden}/{index}".encode(),
                     hashlib.sha512).digest()
    return int.from_bytes(child[:32], "big") % secp.N


# repr=False: a dataclass repr would print the secret into logs and
# tracebacks.
@dataclass(frozen=True, repr=False)
class WalletKey:
    secret: bytes

    def __post_init__(self):
        if len(self.secret) != 32:
            raise WalletKeyError("wallet secret must be 32 bytes")

    @classmethod
    def new(cls) -> "WalletKey":
        return cls(secrets.token_bytes(32))

    @classmethod
    def from_code(cls, code: str) -> "WalletKey":
        try:
            return cls(decode_wallet_code(code))
        except Bech32Error as e:
            raise WalletKeyError(f"not a wallet code: {e}") from None

    @property
    def code(self) -> str:
        return encode_wallet_code(self.secret)

    @property
    def seckey(self) -> int:
        return garden_key(self.secret)

    @property
    def internal_key(self) -> bytes:
        return secp.pubkey(self.seckey)

    @property
    def script(self) -> bytes:
        return p2tr_script(secp.taproot_output_key(self.internal_key))

    def address(self, network: str = "main") -> str:
        return encode_segwit(HRP[network], 1,
                             secp.taproot_output_key(self.internal_key))

    def sign(self, tx: Transaction, prevouts: list[TxOut], index: int) -> bytes:
        """BIP-341 key-path signature, SIGHASH_DEFAULT, random aux."""
        return sign_input(tx, prevouts, index, self.seckey,
                          secrets.token_bytes(32))


# ---------------------------------------------------------------------------
# The wallet file: the code on one line, comments ignored
# ---------------------------------------------------------------------------

def load_wallet(path: str) -> WalletKey:
    with open(path, encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                return WalletKey.from_code(line)
    raise WalletKeyError(f"no wallet code in {path}")


def save_wallet(path: str, key: WalletKey) -> None:
    """Writes the wallet file; refuses to overwrite one that exists."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise WalletKeyError(f"a wallet already exists at {path}") from None
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
        f.write("# claimstones wallet code - the code alone restores"
                " the wallet\n")
        f.write(key.code + "\n")
