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
wallet code in a file created with owner-only permissions — plain, or
locked under a password the way bitcoind's wallet encryption works:
the password unlocks the file, it is not the backup. The wallet code
alone still restores everything, password forgotten or not.

The lock, standard library only: scrypt stretches the password
(n=2^15, r=8 — tens of milliseconds and 32 MiB, so guessing is slow),
the first 32 derived bytes XOR the secret (a one-time pad: the pad is
used for exactly one 32-byte message per salt), and the last 32 key an
HMAC over salt and ciphertext, so a wrong password or a tampered file
is caught before anything decrypts.
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
# The wallet file: one line, plain or locked, comments ignored
# ---------------------------------------------------------------------------

_LOCKED = "locked/scrypt"
# n=2^15, r=8 needs exactly 32 MiB, which is also OpenSSL's default
# ceiling — maxmem raises it so the parameters are honest, not capped.
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 2 ** 26}


class WalletLocked(Exception):
    """The wallet file needs a password; ask and load again."""


def _lock_keys(password: str, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=64, **_SCRYPT)
    return dk[:32], dk[32:]


def lock_line(key: WalletKey, password: str) -> str:
    salt = secrets.token_bytes(16)
    pad, mac_key = _lock_keys(password, salt)
    ct = bytes(a ^ b for a, b in zip(key.secret, pad))
    tag = hmac.new(mac_key, salt + ct, hashlib.sha256).digest()
    return f"{_LOCKED} {salt.hex()} {ct.hex()} {tag.hex()}"


def unlock_line(line: str, password: str) -> WalletKey:
    try:
        _, salt_hex, ct_hex, tag_hex = line.split()
        salt, ct, tag = (bytes.fromhex(h) for h in (salt_hex, ct_hex, tag_hex))
    except ValueError:
        raise WalletKeyError("the locked wallet line is malformed") from None
    pad, mac_key = _lock_keys(password, salt)
    want = hmac.new(mac_key, salt + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, want):
        raise WalletKeyError("wrong password (or the wallet file was changed)")
    return WalletKey(bytes(a ^ b for a, b in zip(ct, pad)))


def load_wallet(path: str, password: str | None = None) -> WalletKey:
    """The key from the wallet file. A locked file raises WalletLocked
    until a password is given; a wrong password is WalletKeyError."""
    with open(path, encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(_LOCKED):
                if password is None:
                    raise WalletLocked(path)
                return unlock_line(line, password)
            return WalletKey.from_code(line)
    raise WalletKeyError(f"no wallet code in {path}")


def save_wallet(path: str, key: WalletKey, password: str | None = None) -> None:
    """Writes the wallet file; refuses to overwrite one that exists.
    With a password the stored line is locked; the wallet code shown at
    creation stays the only backup either way."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise WalletKeyError(f"a wallet already exists at {path}") from None
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
        if password:
            f.write("# claimstones wallet, locked - the wallet code alone"
                    " restores it, password or not\n")
            f.write(lock_line(key, password) + "\n")
        else:
            f.write("# claimstones wallet code - the code alone restores"
                    " the wallet\n")
            f.write(key.code + "\n")
