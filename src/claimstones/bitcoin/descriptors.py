"""What a user pastes into Check — an address, an extended public key,
or an output descriptor — turned into the scripts the index is asked
about. Pure functions, no RPC.

The indexer RPC looks up by script (`getaddressutxos`,
docs/spec/indexer-rpc.md: key derivation is the wallet's business). The
retired server handed that problem to Bitcoin Core's scanner; a wallet
with only a node has no scanner, so the derivation lives here: BIP-32
public child derivation, the four single-key script types, the
descriptor checksum, and the gap-limit walk.

Accepted lines:

- a bitcoin address                -> one script
- an extended public key           -> receive and change branches per
  script type. A SLIP-132 version (ypub, zpub, upub, vpub) pins the
  type; a plain xpub/tpub does not say which the wallet used, so all
  four single-key types are walked: pkh, sh(wpkh), wpkh, tr.
- a single-key output descriptor   -> pkh(), wpkh(), sh(wpkh()), tr(),
  addr(). A `[fingerprint/path]` origin is kept so a claim-and-collect
  PSBT can carry the key origin a hardware signer needs
  (wallet-policy.md §Transaction templates). One `<a;b>` multipath
  group is allowed (how modern wallets export receive and change in one
  line); a final `*` makes the branch ranged.

Refused, with a reason the wallet can show: private keys (xprv and kin
— never paste those anywhere), multisig SLIP-132 versions (Ypub, Zpub:
Check reads single-key wallets), and hardened steps after a public key
(no private key, no hardened child — export the account-level xpub).

Gap limit: BIP-44 style, decided 2026-09-21. A ranged branch is walked
until a trailing window of GAP_LIMIT indices shows no history,
MAX_SCAN_RANGE the safety cap (a cap, not a policy choice).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Callable

from claimstones.bitcoin import base58, bech32, secp
from claimstones.bitcoin.ripemd160 import hash160

GAP_LIMIT = 20          # addresses per branch scanned before extending
MAX_SCAN_RANGE = 2000   # hard cap on a branch's range regardless of hits

HARDENED = 0x80000000

SINGLE_KEY_TYPES = ("pkh", "sh(wpkh)", "wpkh", "tr")

# Extended-key version bytes -> (network, script type pinned or None).
_XPUB_VERSIONS: dict[bytes, tuple[str, str | None]] = {
    bytes.fromhex("0488b21e"): ("main", None),          # xpub
    bytes.fromhex("049d7cb2"): ("main", "sh(wpkh)"),    # ypub
    bytes.fromhex("04b24746"): ("main", "wpkh"),        # zpub
    bytes.fromhex("043587cf"): ("test", None),          # tpub
    bytes.fromhex("044a5262"): ("test", "sh(wpkh)"),    # upub
    bytes.fromhex("045f1cf6"): ("test", "wpkh"),        # vpub
}
_MULTISIG_VERSIONS = {
    bytes.fromhex("0295b43f"), bytes.fromhex("02aa7ed3"),   # Ypub, Zpub
    bytes.fromhex("024289ef"), bytes.fromhex("02575483"),   # Upub, Vpub
}
_PRIVATE_VERSIONS = {
    bytes.fromhex("0488ade4"), bytes.fromhex("049d7878"),   # xprv, yprv
    bytes.fromhex("04b2430c"), bytes.fromhex("04358394"),   # zprv, tprv
    bytes.fromhex("044a4e28"), bytes.fromhex("045f18bc"),   # uprv, vprv
}
_CANONICAL_VERSION = {"main": bytes.fromhex("0488b21e"),    # xpub
                      "test": bytes.fromhex("043587cf")}    # tpub

# Which networks each extended-key family serves. tpub is testnet,
# signet and regtest alike; xpub is mainnet alone.
_KEY_NETWORKS = {"main": {"main", "mainnet"},
                 "test": {"test", "testnet", "signet", "regtest"}}


class CheckInputError(ValueError):
    """Plain-words reason the wallet can show."""


# ---------------------------------------------------------------------------
# Compressed public keys
# ---------------------------------------------------------------------------

def _point(pub: bytes) -> secp.Point:
    if len(pub) != 33 or pub[0] not in (2, 3):
        raise CheckInputError("not a compressed public key")
    p = secp.lift_x(int.from_bytes(pub[1:], "big"))
    if p is None:
        raise CheckInputError("public key is not on the curve")
    return p if pub[0] == 2 else (p[0], secp.P - p[1])


def _compressed(p: secp.Point) -> bytes:
    return bytes([2 + (p[1] & 1)]) + p[0].to_bytes(32, "big")


# ---------------------------------------------------------------------------
# BIP-32 extended public keys
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class XPub:
    network: str                # "main" or "test"
    depth: int
    parent_fingerprint: bytes
    child_number: int
    chain_code: bytes
    pubkey: bytes               # 33 bytes, compressed

    def fingerprint(self) -> bytes:
        return hash160(self.pubkey)[:4]

    def child(self, i: int) -> "XPub":
        """CKDpub: the i-th non-hardened child."""
        if not 0 <= i < HARDENED:
            raise CheckInputError(
                "hardened derivation needs the private key; "
                "export the account-level xpub instead")
        digest = hmac.new(self.chain_code,
                          self.pubkey + i.to_bytes(4, "big"),
                          hashlib.sha512).digest()
        il = int.from_bytes(digest[:32], "big")
        child = (secp.point_add(secp.point_mul(il, secp.G), _point(self.pubkey))
                 if il < secp.N else None)
        if child is None:
            # Probability ~2^-127; BIP-32 says skip, a wallet never hits it.
            raise CheckInputError(f"child {i} does not exist for this key")
        return XPub(self.network, self.depth + 1, self.fingerprint(), i,
                    digest[32:], _compressed(child))

    def derive(self, path: tuple[int, ...]) -> "XPub":
        key = self
        for i in path:
            key = key.child(i)
        return key

    def serialize(self) -> str:
        """The canonical xpub/tpub string, whatever version came in."""
        payload = (_CANONICAL_VERSION[self.network] + bytes([self.depth])
                   + self.parent_fingerprint
                   + self.child_number.to_bytes(4, "big")
                   + self.chain_code + self.pubkey)
        return base58.encode_check(payload)


def looks_like_xpub(s: str) -> bool:
    return len(s) in (111, 112) and s[1:4].lower() == "pub" and s[0] in "xyztuvXYZTUV"


def parse_xpub(s: str) -> tuple[XPub, str | None]:
    """Extended public key string -> (XPub, pinned script type or None)."""
    try:
        payload = base58.decode_check(s)
    except base58.Base58Error as e:
        raise CheckInputError(f"not an extended key: {e}") from None
    if len(payload) != 78:
        raise CheckInputError("extended key has the wrong length")
    version = payload[:4]
    if version in _PRIVATE_VERSIONS:
        raise CheckInputError(
            "this is a PRIVATE key (xprv) — never paste it anywhere; "
            "Check wants the public xpub")
    if version in _MULTISIG_VERSIONS:
        raise CheckInputError(
            "this is a multisig extended key (Ypub/Zpub); "
            "Check reads single-key wallets only")
    if version not in _XPUB_VERSIONS:
        raise CheckInputError(
            "unknown extended key version; expected xpub, ypub, zpub, "
            "tpub, upub or vpub")
    network, script_type = _XPUB_VERSIONS[version]
    pubkey = payload[45:78]
    if pubkey[0] not in (2, 3):
        raise CheckInputError("extended key does not hold a public key")
    xpub = XPub(network, payload[4], payload[5:9],
                int.from_bytes(payload[9:13], "big"), payload[13:45], pubkey)
    _point(pubkey)   # on-curve check, raises CheckInputError
    return xpub, script_type


# ---------------------------------------------------------------------------
# Scripts per single-key type
# ---------------------------------------------------------------------------

def _script_for(kind: str, pubkey: bytes) -> bytes:
    if kind == "tr":
        internal = pubkey[1:] if len(pubkey) == 33 else pubkey
        return bech32.p2tr_script(secp.taproot_output_key(internal))
    h = hash160(pubkey)
    if kind == "pkh":
        return bytes([0x76, 0xA9, 0x14]) + h + bytes([0x88, 0xAC])
    if kind == "wpkh":
        return bytes([0x00, 0x14]) + h
    if kind == "sh(wpkh)":
        return bytes([0xA9, 0x14]) + hash160(bytes([0x00, 0x14]) + h) + bytes([0x87])
    raise CheckInputError(f"unsupported script type {kind}")


def _address_for(kind: str, script: bytes, hrp: str) -> str | None:
    """bech32/bech32m address where one exists; legacy types have none
    here (base58.py produces no legacy addresses on purpose)."""
    if kind == "wpkh":
        return bech32.encode_segwit(hrp, 0, script[2:])
    if kind == "tr":
        return bech32.encode_segwit(hrp, 1, script[2:])
    return None


# ---------------------------------------------------------------------------
# Descriptor checksum (Bitcoin Core's scheme, the #8-character tail)
# ---------------------------------------------------------------------------

_DESC_INPUT = ("0123456789()[],'/*abcdefgh@:$%{}"
               "IJKLMNOPQRSTUVWXYZ&+-.;<=>?!^_|~"
               "ijklmnopqrstuvwxyzABCDEFGH`#\"\\ ")
_DESC_GEN = (0xF5DEE51989, 0xA9FDCA3312, 0x1BAB10E32D, 0x3706B1677A, 0x644D626FFD)


def _desc_polymod(symbols: list[int]) -> int:
    chk = 1
    for value in symbols:
        top = chk >> 35
        chk = (chk & 0x7FFFFFFFF) << 5 ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _DESC_GEN[i]
    return chk


def _desc_expand(s: str) -> list[int] | None:
    groups: list[int] = []
    symbols: list[int] = []
    for c in s:
        v = _DESC_INPUT.find(c)
        if v < 0:
            return None
        symbols.append(v & 31)
        groups.append(v >> 5)
        if len(groups) == 3:
            symbols.append(groups[0] * 9 + groups[1] * 3 + groups[2])
            groups = []
    if len(groups) == 1:
        symbols.append(groups[0])
    elif len(groups) == 2:
        symbols.append(groups[0] * 3 + groups[1])
    return symbols


def descriptor_checksum(body: str) -> str:
    symbols = _desc_expand(body)
    if symbols is None:
        raise CheckInputError("descriptor holds a character no descriptor uses")
    chk = _desc_polymod(symbols + [0] * 8) ^ 1
    return "".join(bech32.CHARSET[(chk >> (5 * (7 - i))) & 31] for i in range(8))


def _split_checksum(s: str) -> str:
    """Strip and verify a #checksum if one is present."""
    if "#" not in s:
        return s
    body, _, check = s.partition("#")
    if descriptor_checksum(body) != check:
        raise CheckInputError(
            "descriptor checksum does not match; the line was mistyped "
            "or cut short")
    return body


# ---------------------------------------------------------------------------
# Branches: what a line turns into
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyOrigin:
    """[fingerprint/path] — what a PSBT's bip32_derivation carries so a
    hardware signer can find its key. The path runs from the fingerprint's
    key all the way to the derived pubkey."""
    fingerprint: bytes          # 4 bytes
    path: tuple[int, ...]       # HARDENED-offset ints for hardened steps


@dataclass(frozen=True)
class DerivedScript:
    script: bytes               # scriptPubKey — what getaddressutxos takes
    address: str | None         # bech32/bech32m where one exists
    pubkey: bytes | None        # compressed (33) or x-only (32, bare tr key)
    origin: KeyOrigin | None    # only when the source line gave one


@dataclass(frozen=True)
class Branch:
    """One derivation branch of one pasted line. Ranged branches derive a
    script per index and are walked under the gap limit; a fixed branch
    (an address, a bare key, an unranged path) has exactly index 0."""
    source: str                     # the user's line
    descriptor: str                 # normalized, this branch's path, no checksum
    ranged: bool
    network: str | None             # "main"/"test" when the key pins one
    _kind: str                      # script type, or "fixed"
    _hrp: str = "bc"
    _xpub: XPub | None = None
    _path: tuple[int, ...] = ()
    _pubkey: bytes | None = None    # bare-key descriptor
    _script: bytes | None = None    # fixed script (addr/pasted address)
    _address: str | None = None     # the address as pasted, for reporting
    _origin: KeyOrigin | None = None  # as given, path up to the xpub

    def derive(self, index: int = 0) -> DerivedScript:
        if not self.ranged and index != 0:
            raise CheckInputError("not a ranged branch")
        if self._script is not None:
            return DerivedScript(self._script, self._address, None, None)
        if self._xpub is None:
            script = _script_for(self._kind, self._pubkey)
            return DerivedScript(script, _address_for(self._kind, script, self._hrp),
                                 self._pubkey, self._origin)
        path = self._path + (index,) if self.ranged else self._path
        key = self._xpub.derive(path)
        origin = None
        if self._origin is not None:
            origin = KeyOrigin(self._origin.fingerprint, self._origin.path + path)
        script = _script_for(self._kind, key.pubkey)
        return DerivedScript(script, _address_for(self._kind, script, self._hrp),
                             key.pubkey, origin)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_path(steps: list[str], where: str, hardened_ok: bool) -> tuple[int, ...]:
    path = []
    for step in steps:
        hard = step[-1:] in ("h", "H", "'")
        if hard:
            if not hardened_ok:
                raise CheckInputError(
                    "hardened derivation needs the private key; "
                    "export the account-level xpub instead")
            step = step[:-1]
        if not step.isdigit() or int(step) >= HARDENED:
            raise CheckInputError(f"bad derivation step {step!r} in {where}")
        path.append(int(step) + (HARDENED if hard else 0))
    return tuple(path)


def _parse_key_expr(expr: str, kind: str, network: str | None, hrp: str,
                    source: str) -> list[Branch]:
    origin = None
    if expr.startswith("["):
        end = expr.find("]")
        if end < 0:
            raise CheckInputError("key origin [ ... ] is not closed")
        inside = expr[1:end].split("/")
        try:
            fingerprint = bytes.fromhex(inside[0])
        except ValueError:
            fingerprint = b""
        if len(fingerprint) != 4:
            raise CheckInputError(
                "key origin must start with an 8-hex-digit fingerprint")
        origin = KeyOrigin(fingerprint,
                           _parse_path(inside[1:], "the key origin", True))
        expr = expr[end + 1:]

    key_str, _, path_str = expr.partition("/")

    # A bare key in hex: compressed everywhere, x-only also fine for tr.
    stripped = key_str.lower()
    if stripped and all(c in "0123456789abcdef" for c in stripped):
        if path_str:
            raise CheckInputError("a bare public key has no derivation path")
        pub = bytes.fromhex(stripped) if len(stripped) % 2 == 0 else None
        if pub is None or len(pub) not in (32, 33):
            raise CheckInputError("a key in hex must be 33 bytes (or 32 for tr)")
        if len(pub) == 32:
            if kind != "tr":
                raise CheckInputError("an x-only key belongs in tr() only")
            if secp.lift_x(int.from_bytes(pub, "big")) is None:
                raise CheckInputError("public key is not on the curve")
        else:
            _point(pub)
        return [Branch(source, f"{_wrap(kind, expr)}", False, network,
                       _kind=kind, _hrp=hrp, _pubkey=pub, _origin=origin)]

    if not looks_like_xpub(key_str):
        raise CheckInputError(
            f"not a public key or extended key: {key_str[:24]}...")
    xpub, pinned = parse_xpub(key_str)
    if pinned is not None and pinned != kind:
        raise CheckInputError(
            f"the key's version says {pinned} but the descriptor says {kind}")
    _check_network(xpub.network, network)

    # Expand one <a;b;...> multipath group into one branch per variant.
    variants = [path_str]
    if "<" in path_str:
        head, _, rest = path_str.partition("<")
        group, closed, tail = rest.partition(">")
        if not closed or "<" in group or "<" in tail:
            raise CheckInputError("multipath must be one <a;b> group")
        variants = [head + alt + tail for alt in group.split(";")]

    branches = []
    for variant in variants:
        steps = [s for s in variant.split("/") if s != ""]
        ranged = bool(steps) and steps[-1] == "*"
        if ranged:
            steps = steps[:-1]
        if "*" in steps:
            raise CheckInputError("only the last derivation step may be *")
        path = _parse_path(steps, "the key's path", False)
        shown = "/".join([xpub.serialize(), *map(str, path)] + (["*"] if ranged else []))
        if origin is not None:
            shown = "[" + "/".join([origin.fingerprint.hex(),
                                    *(_step_str(i) for i in origin.path)]) + "]" + shown
        branches.append(Branch(source, _wrap(kind, shown), ranged,
                               xpub.network, _kind=kind, _hrp=hrp,
                               _xpub=xpub, _path=path, _origin=origin))
    return branches


def _step_str(i: int) -> str:
    return f"{i - HARDENED}h" if i >= HARDENED else str(i)


def _wrap(kind: str, inner: str) -> str:
    return f"sh(wpkh({inner}))" if kind == "sh(wpkh)" else f"{kind}({inner})"


def _check_network(key_network: str, wanted: str | None) -> None:
    if wanted is not None and wanted not in _KEY_NETWORKS[key_network]:
        raise CheckInputError(
            f"this key is for {'mainnet' if key_network == 'main' else 'testnet'}, "
            f"not {wanted}")


def _parse_descriptor(line: str, network: str | None, hrp: str) -> list[Branch]:
    body = _split_checksum(line)
    for kind in ("sh(wpkh(", "wpkh(", "pkh(", "tr(", "addr("):
        if body.startswith(kind):
            closing = ")" * kind.count("(")
            if not body.endswith(closing):
                raise CheckInputError(
                    f"descriptor does not look complete: {line[:24]}...")
            inner = body[len(kind):-len(closing)]
            if kind == "addr(":
                script = _address_script(inner, network, hrp)
                return [Branch(line, body, False, network, _kind="fixed",
                               _hrp=hrp, _script=script, _address=inner)]
            name = "sh(wpkh)" if kind == "sh(wpkh(" else kind.rstrip("(")
            return _parse_key_expr(inner, name, network, hrp, line)
    raise CheckInputError(
        "only single-key descriptors are read: "
        "pkh(), wpkh(), sh(wpkh()), tr(), addr()")


def _address_script(addr: str, network: str | None, hrp: str) -> bytes:
    low = addr.lower()
    if low.startswith(("bc1", "tb1", "bcrt1")):
        addr_hrp = low.rsplit("1", 1)[0]
        if network is not None and addr_hrp != hrp:
            raise CheckInputError(
                f"address is for another network ({addr_hrp}1..., expected {hrp}1...)")
        try:
            return bech32.address_to_script(addr_hrp, low)
        except bech32.Bech32Error as e:
            raise CheckInputError(f"bad address: {e}") from None
    try:
        return base58.address_to_script(addr)
    except base58.Base58Error as e:
        raise CheckInputError(f"bad address: {e}") from None


def parse_line(line: str, network: str | None = "main") -> list[Branch]:
    """One pasted line -> its branches. network pins the HRP for derived
    addresses and refuses keys and addresses of another network; None
    accepts anything and shows each key under its own network's HRP."""
    s = line.strip()
    if not s:
        return []
    if "(" in s:
        return _parse_descriptor(s, network, _hrp_for(network))
    if looks_like_xpub(s):
        xpub, pinned = parse_xpub(s)
        _check_network(xpub.network, network)
        hrp = _hrp_for(network) if network else _hrp_for(xpub.network)
        kinds = (pinned,) if pinned else SINGLE_KEY_TYPES
        return [
            branch
            for kind in kinds
            for b in (0, 1)
            for branch in _parse_key_expr(f"{xpub.serialize()}/{b}/*",
                                          kind, network, hrp, s)
        ]
    if s[1:4].lower() == "prv" and len(s) in (111, 112):
        raise CheckInputError(
            "this is a PRIVATE key (xprv) — never paste it anywhere; "
            "Check wants the public xpub")
    if any(c in s for c in " \t/"):
        raise CheckInputError(f"not an address, xpub or descriptor: {s[:24]}...")
    script = _address_script(s, network, _hrp_for(network))
    return [Branch(s, f"addr({s})", False, network, _kind="fixed",
                   _hrp=_hrp_for(network), _script=script, _address=s)]


def _hrp_for(network: str | None) -> str:
    if network is None:
        return "bc"
    try:
        return bech32.HRP[network]
    except KeyError:
        raise CheckInputError(f"unknown network {network!r}") from None


def parse_inputs(lines: list[str], network: str | None = "main") -> list[Branch]:
    branches: list[Branch] = []
    seen = set()
    for line in lines:
        for branch in parse_line(line, network):
            if branch.descriptor not in seen:
                seen.add(branch.descriptor)
                branches.append(branch)
    if not branches:
        raise CheckInputError(
            "nothing to check: paste an address, an xpub or a descriptor")
    return branches


# ---------------------------------------------------------------------------
# The gap-limit walk
# ---------------------------------------------------------------------------

def scan_branch(branch: Branch,
                probe: Callable[[int, DerivedScript], bool],
                gap_limit: int = GAP_LIMIT,
                max_range: int = MAX_SCAN_RANGE) -> int:
    """Walk a branch. probe(index, derived) answers whether that script
    has any history (the caller collects what it finds); the walk stops
    once the trailing gap_limit indices were all empty, or at max_range.
    Returns how many indices were scanned."""
    if not branch.ranged:
        probe(0, branch.derive(0))
        return 1
    last_hit = -1
    i = 0
    while i < max_range and (i < gap_limit or i <= last_hit + gap_limit):
        if probe(i, branch.derive(i)):
            last_hit = i
        i += 1
    return i
