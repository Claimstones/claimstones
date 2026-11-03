"""secp256k1, BIP-340 Schnorr signatures and the BIP-341 key tweak.

Pure Python, standard library only. Slow (a few milliseconds per point
multiplication) and constant-time nowhere: the server uses it only to
*verify* signatures before broadcasting, and the tests use it to sign.
User keys never touch the server (docs/spec/wallet-policy.md).

Test vectors: BIP-340 (tests/test_secp.py).
"""

from __future__ import annotations

import hashlib
from typing import Optional

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)

Point = Optional[tuple[int, int]]   # None is the point at infinity


# ---------------------------------------------------------------------------
# Field and group arithmetic (Jacobian coordinates for the multiplication)
# ---------------------------------------------------------------------------

def _inv(a: int) -> int:
    return pow(a, P - 2, P)


def _jac_double(X1, Y1, Z1):
    if Y1 == 0:
        return (0, 0, 0)
    S = (4 * X1 * Y1 * Y1) % P
    M = (3 * X1 * X1) % P
    X3 = (M * M - 2 * S) % P
    Y3 = (M * (S - X3) - 8 * pow(Y1, 4, P)) % P
    Z3 = (2 * Y1 * Z1) % P
    return (X3, Y3, Z3)


def _jac_add(X1, Y1, Z1, X2, Y2, Z2):
    if Z1 == 0:
        return (X2, Y2, Z2)
    if Z2 == 0:
        return (X1, Y1, Z1)
    Z1Z1 = Z1 * Z1 % P
    Z2Z2 = Z2 * Z2 % P
    U1 = X1 * Z2Z2 % P
    U2 = X2 * Z1Z1 % P
    S1 = Y1 * Z2 * Z2Z2 % P
    S2 = Y2 * Z1 * Z1Z1 % P
    if U1 == U2:
        if S1 != S2:
            return (0, 0, 0)
        return _jac_double(X1, Y1, Z1)
    H = (U2 - U1) % P
    R = (S2 - S1) % P
    HH = H * H % P
    HHH = HH * H % P
    V = U1 * HH % P
    X3 = (R * R - HHH - 2 * V) % P
    Y3 = (R * (V - X3) - S1 * HHH) % P
    Z3 = H * Z1 * Z2 % P
    return (X3, Y3, Z3)


def _from_jac(X, Y, Z) -> Point:
    if Z == 0:
        return None
    zi = _inv(Z)
    zi2 = zi * zi % P
    return (X * zi2 % P, Y * zi2 * zi % P)


def point_add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    return _from_jac(*_jac_add(a[0], a[1], 1, b[0], b[1], 1))


def point_mul(k: int, p: Point) -> Point:
    if p is None or k % N == 0:
        return None
    k %= N
    R = (0, 0, 0)
    Q = (p[0], p[1], 1)
    while k:
        if k & 1:
            R = _jac_add(*R, *Q)
        Q = _jac_double(*Q)
        k >>= 1
    return _from_jac(*R)


def lift_x(x: int) -> Point:
    """The point with x-coordinate x and even y (BIP-340), or None."""
    if not 0 <= x < P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if y * y % P != y_sq:
        return None
    return (x, y if y % 2 == 0 else P - y)


def has_even_y(p: Point) -> bool:
    return p is not None and p[1] % 2 == 0


def xonly(p: Point) -> bytes:
    return p[0].to_bytes(32, "big")


# ---------------------------------------------------------------------------
# Hashes and keys
# ---------------------------------------------------------------------------

def tagged_hash(tag: str, data: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + data).digest()


def pubkey(seckey: int) -> bytes:
    """x-only public key of a secret scalar (BIP-340)."""
    if not 1 <= seckey < N:
        raise ValueError("secret key out of range")
    return xonly(point_mul(seckey, G))


def taproot_tweak(internal_xonly: bytes) -> int:
    """BIP-341/86 tweak for a key with no script tree."""
    return int.from_bytes(tagged_hash("TapTweak", internal_xonly), "big") % N


def taproot_output_key(internal_xonly: bytes) -> bytes:
    """Q = P + t*G as x-only bytes; the witness program of a P2TR output."""
    p = lift_x(int.from_bytes(internal_xonly, "big"))
    if p is None:
        raise ValueError("internal key is not a valid x-only point")
    q = point_add(p, point_mul(taproot_tweak(internal_xonly), G))
    return xonly(q)


def taproot_tweak_seckey(seckey: int) -> int:
    """The scalar that signs for taproot_output_key(pubkey(seckey))."""
    p = point_mul(seckey, G)
    d = seckey if has_even_y(p) else N - seckey
    return (d + taproot_tweak(xonly(p))) % N


# ---------------------------------------------------------------------------
# BIP-340
# ---------------------------------------------------------------------------

def schnorr_sign(msg: bytes, seckey: int, aux_rand: bytes = b"\x00" * 32) -> bytes:
    if len(msg) != 32 or len(aux_rand) != 32:
        raise ValueError("msg and aux_rand must be 32 bytes")
    if not 1 <= seckey < N:
        raise ValueError("secret key out of range")
    p = point_mul(seckey, G)
    d = seckey if has_even_y(p) else N - seckey
    t = (d ^ int.from_bytes(tagged_hash("BIP0340/aux", aux_rand), "big")).to_bytes(32, "big")
    px = xonly(p)
    k0 = int.from_bytes(tagged_hash("BIP0340/nonce", t + px + msg), "big") % N
    if k0 == 0:
        raise RuntimeError("nonce is zero; try another aux_rand")
    r = point_mul(k0, G)
    k = k0 if has_even_y(r) else N - k0
    rx = xonly(r)
    e = int.from_bytes(tagged_hash("BIP0340/challenge", rx + px + msg), "big") % N
    sig = rx + ((k + e * d) % N).to_bytes(32, "big")
    if not schnorr_verify(msg, px, sig):
        raise RuntimeError("signature self-check failed")
    return sig


def schnorr_verify(msg: bytes, pubkey_xonly: bytes, sig: bytes) -> bool:
    if len(msg) != 32 or len(pubkey_xonly) != 32 or len(sig) != 64:
        return False
    p = lift_x(int.from_bytes(pubkey_xonly, "big"))
    if p is None:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(tagged_hash("BIP0340/challenge", sig[:32] + pubkey_xonly + msg), "big") % N
    R = point_add(point_mul(s, G), point_mul(N - e, p))
    return R is not None and has_even_y(R) and R[0] == r
