"""Claimstones envelope codec — release 1 (spec: docs/spec/envelope.md).

Decodes and encodes the orchid envelope: the first transaction output
whose script begins `OP_RETURN OP_PUSHNUM_15` (6a 5f). The parsing
machinery (LEB128 varints, tag/value pairs, delta-coded edicts, the
cenotaph rules) is ported from ord's runestone codec so that edicts keep
the runes shape verbatim. Runestones (6a 5d) are a different protocol
and are never matched here.

Tag set v1: BODY, POINTER, LAND, NOP; LIFT reserved (present -> cenotaph).
Values taken from the spec's [decide] markers: LIFT = 26, LAND = 28,
LAND carries one value that must be 0, orchid asset ID = (0, 0).

This module has no dependencies beyond the standard library and is
deterministic. The only interface it needs from a transaction is
`tx.outputs`, a list of objects with a `script_pubkey: bytes` attribute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

U128_MAX = (1 << 128) - 1
U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1

# Script opcodes used here
OP_RETURN = 0x6A
OP_PUSHNUM_15 = 0x5F  # the claimstones envelope identifier ("magic number")
OP_PUSHDATA1 = 0x4C
OP_PUSHDATA2 = 0x4D
OP_PUSHDATA4 = 0x4E

MAGIC = bytes([OP_RETURN, OP_PUSHNUM_15])


# ---------------------------------------------------------------------------
# ACTIVATION_HEIGHT (docs/spec/constants.md)
# ---------------------------------------------------------------------------
# The height below which an envelope does not count. It is consensus, and it
# exists only so that every indexer starts from the same state -- which a
# per-run command-line option quietly defeats, since two operators could then
# build two different states from the same chain and neither would know. So it
# is a constant, read by both indexers (indexer/birth.py, indexer/replay.py)
# and advertised to wallets by the server (/hello).
#
# None is this code's spelling of the spec's [decide]: undecided, so no
# envelope counts at any height. Consumers must test for None rather than
# compare against it. A 0 default would have meant the opposite -- active
# since genesis -- which is exactly why the undecided case is not spelled 0.
#
#     Before this is given a number, take a land-free snapshot of the
#     birth index: docs/birth-index-check.md, "The land-free snapshot".
#
# The birth index marks landed sats with the PLAIN run height, and PLAIN is not
# an annotation on the landed output: BirthIndexer._place cuts the input run
# stream positionally across the outputs, so a PLAIN run enters the stream of
# every later spend and travels forward with the sats. It cannot be subtracted
# out afterwards, nor recovered by overlaying a list of lands without replaying
# the whole forward walk. So once the first land is applied there is no
# land-free index except a rebuild from genesis -- days, even as a fastbuild --
# whereas a copy stopped cleanly just below activation turns that into a replay
# of only the blocks since. The window for taking it closes the moment the
# chain reaches the height chosen here.
ACTIVATION_HEIGHT: int | None = None


def activation_height(chain: str = "main") -> int | None:
    """The activation height for a chain, as the node names it
    (getblockchaininfo "chain": main/test/signet/regtest).

    Mainnet is ACTIVATION_HEIGHT above, the one [decide]. Test chains
    are active from height 1 (decided 2026-11-11): a test network
    exists to exercise the protocol, and a land that cannot count tests
    nothing. Height 1, not 0 -- a genesis coinbase is unspendable on
    every chain, and 0 is reserved as the explicit "active since
    genesis" spelling should mainnet ever want it."""
    return ACTIVATION_HEIGHT if chain in ("main", "mainnet") else 1


# ---------------------------------------------------------------------------
# Tags (envelope.md, Tags)
# ---------------------------------------------------------------------------

class Tag:
    BODY = 0        # even: edicts follow
    POINTER = 22    # even: one output index for the unallocated balance
    LIFT = 26       # even: reserved; present -> cenotaph
    LAND = 28       # even: one value, must be 0
    NOP = 127       # odd: ignored

    # RULE (6), PARAMS (8) and CLAIM (24) from the older draft are not
    # part of v1; they fall under the unrecognized-even-tag rule.


# ---------------------------------------------------------------------------
# Flaws (cenotaph reasons)
# ---------------------------------------------------------------------------

class Flaw:
    EDICT_OUTPUT = "edict-output"
    EDICT_ASSET_ID = "edict-asset-id"
    INVALID_SCRIPT = "invalid-script"
    LIFT = "lift-reserved"
    OPCODE = "opcode"
    TRAILING_INTEGERS = "trailing-integers"
    TRUNCATED_FIELD = "truncated-field"
    UNRECOGNIZED_EVEN_TAG = "unrecognized-even-tag"
    VARINT = "varint"


# ---------------------------------------------------------------------------
# Varints: LEB128, at most 19 bytes, must fit in u128
# ---------------------------------------------------------------------------

class VarintError(ValueError):
    pass


def varint_decode(buffer: bytes, start: int = 0) -> tuple[int, int]:
    """Decode one LEB128 varint from buffer[start:].

    Returns (value, length_consumed). Raises VarintError on an
    overlong (>19 byte), overflowing (>u128), or unterminated varint.
    """
    n = 0
    i = 0
    while start + i < len(buffer):
        byte = buffer[start + i]
        if i > 18:
            raise VarintError("overlong")
        value = byte & 0b0111_1111
        if i == 18 and value & 0b0111_1100 != 0:
            raise VarintError("overflow")
        n |= value << (7 * i)
        if byte & 0b1000_0000 == 0:
            return n, i + 1
        i += 1
    raise VarintError("unterminated")


def varint_encode(n: int) -> bytes:
    if not 0 <= n <= U128_MAX:
        raise VarintError("value out of u128 range")
    out = bytearray()
    while n >> 7 > 0:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class AssetId:
    """Runes-shaped asset ID (block, tx). Release 1 has one asset, ORCHID."""

    block: int = 0
    tx: int = 0

    @staticmethod
    def new(block: int, tx: int) -> Optional["AssetId"]:
        if not (0 <= block <= U64_MAX and 0 <= tx <= U32_MAX):
            return None
        if block == 0 and tx > 0:
            return None
        return AssetId(block, tx)

    def next(self, block_delta: int, tx_delta: int) -> Optional["AssetId"]:
        """Apply an edict delta (used while decoding)."""
        block = self.block + block_delta
        tx = self.tx + tx_delta if block_delta == 0 else tx_delta
        return AssetId.new(block, tx)

    def delta(self, next_id: "AssetId") -> Optional[tuple[int, int]]:
        """Delta from self to next_id (used while encoding)."""
        block = next_id.block - self.block
        if block < 0:
            return None
        if block == 0:
            tx = next_id.tx - self.tx
            if tx < 0:
                return None
        else:
            tx = next_id.tx
        return block, tx

    def __str__(self) -> str:
        return f"{self.block}:{self.tx}"


ORCHID = AssetId(0, 0)


@dataclass(frozen=True)
class Edict:
    id: AssetId
    amount: int  # seeds; 0 = all remaining
    output: int  # == len(tx.outputs) means split across non-OP_RETURN outputs

    @staticmethod
    def from_integers(tx, id: AssetId, amount: int, output: int) -> Optional["Edict"]:
        if output > U32_MAX:
            return None
        if output > len(tx.outputs):
            return None
        return Edict(id, amount, output)


@dataclass
class Envelope:
    """A valid decoded envelope."""

    edicts: list[Edict] = field(default_factory=list)
    pointer: Optional[int] = None
    land: bool = False


@dataclass
class Cenotaph:
    """A flawed envelope: all input balances burn.

    `land` is True when a well-formed LAND tag was present, in which
    case the inputs' latent value is landed and burned as well
    (land.md, Cenotaph interaction).
    """

    flaw: Optional[str] = None
    land: bool = False


Artifact = Union[Envelope, Cenotaph]


# ---------------------------------------------------------------------------
# Script instruction iterator (mirrors rust-bitcoin's Script::instructions)
# ---------------------------------------------------------------------------

_PUSH = "push"
_OP = "op"
_ERR = "err"


def _instructions(script: bytes):
    """Yield ('push', bytes) | ('op', opcode) | ('err', None).

    Opcodes 0x00-0x4b are direct pushes (0x00 pushes the empty string),
    0x4c/0x4d/0x4e are OP_PUSHDATA1/2/4, everything else is a plain
    opcode. A push that runs off the end of the script is an error
    ('err'), after which iteration stops.
    """
    i = 0
    n = len(script)
    while i < n:
        op = script[i]
        i += 1
        if op <= 0x4B:
            length = op
        elif op == OP_PUSHDATA1:
            if i + 1 > n:
                yield (_ERR, None)
                return
            length = script[i]
            i += 1
        elif op == OP_PUSHDATA2:
            if i + 2 > n:
                yield (_ERR, None)
                return
            length = int.from_bytes(script[i : i + 2], "little")
            i += 2
        elif op == OP_PUSHDATA4:
            if i + 4 > n:
                yield (_ERR, None)
                return
            length = int.from_bytes(script[i : i + 4], "little")
            i += 4
        else:
            yield (_OP, op)
            continue
        if i + length > n:
            yield (_ERR, None)
            return
        yield (_PUSH, script[i : i + length])
        i += length


def is_op_return(script_pubkey: bytes) -> bool:
    return len(script_pubkey) > 0 and script_pubkey[0] == OP_RETURN


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def _payload(tx) -> Union[None, bytes, str]:
    """Find the envelope payload in a transaction.

    Returns the concatenated payload bytes of the first output whose
    script begins OP_RETURN OP_PUSHNUM_15, a Flaw string if that output
    is malformed, or None if no output matches.
    """
    for output in tx.outputs:
        instructions = _instructions(output.script_pubkey)

        if next(instructions, None) != (_OP, OP_RETURN):
            continue
        if next(instructions, None) != (_OP, OP_PUSHNUM_15):
            continue

        payload = bytearray()
        for kind, value in instructions:
            if kind == _PUSH:
                payload.extend(value)
            elif kind == _OP:
                return Flaw.OPCODE
            else:
                return Flaw.INVALID_SCRIPT
        return bytes(payload)

    return None


def _integers(payload: bytes) -> list[int]:
    integers = []
    i = 0
    while i < len(payload):
        value, length = varint_decode(payload, i)
        integers.append(value)
        i += length
    return integers


def _message(tx, payload: list[int]):
    """Split the integer stream into (flaw, edicts, fields).

    fields maps tag -> list of values in encounter order. Everything
    after TAG_BODY is edicts in groups of four.
    """
    edicts: list[Edict] = []
    fields: dict[int, list[int]] = {}
    flaw: Optional[str] = None

    i = 0
    while i < len(payload):
        tag = payload[i]

        if tag == Tag.BODY:
            id = AssetId(0, 0)
            body = payload[i + 1 :]
            for j in range(0, len(body), 4):
                chunk = body[j : j + 4]
                if len(chunk) != 4:
                    flaw = flaw or Flaw.TRAILING_INTEGERS
                    break
                next_id = id.next(chunk[0], chunk[1])
                if next_id is None:
                    flaw = flaw or Flaw.EDICT_ASSET_ID
                    break
                edict = Edict.from_integers(tx, next_id, chunk[2], chunk[3])
                if edict is None:
                    flaw = flaw or Flaw.EDICT_OUTPUT
                    break
                id = next_id
                edicts.append(edict)
            break

        if i + 1 >= len(payload):
            flaw = flaw or Flaw.TRUNCATED_FIELD
            break

        fields.setdefault(tag, []).append(payload[i + 1])
        i += 2

    return flaw, edicts, fields


def _take(fields: dict[int, list[int]], tag: int, n: int, convert):
    """Consume the first n values of a tag.

    convert receives a list of n ints and returns the converted value
    or None. On None (or too few values) nothing is consumed and the
    tag's values stay in fields, which later triggers the
    unrecognized-even-tag cenotaph rule for even tags. Leftover values
    of a consumed even tag (a duplicate) trigger the same rule.
    """
    values = fields.get(tag)
    if values is None or len(values) < n:
        return None
    result = convert(values[:n])
    if result is None:
        return None
    del values[:n]
    if not values:
        del fields[tag]
    return result


def decipher(tx) -> Optional[Artifact]:
    """Decode the envelope in a transaction, if any.

    Returns an Envelope, a Cenotaph, or None (no envelope output).
    """
    payload = _payload(tx)
    if payload is None:
        return None
    if isinstance(payload, str):  # a Flaw
        return Cenotaph(flaw=payload)

    try:
        integers = _integers(payload)
    except VarintError:
        return Cenotaph(flaw=Flaw.VARINT)

    flaw, edicts, fields = _message(tx, integers)

    def convert_pointer(values):
        pointer = values[0]
        if pointer > U32_MAX or pointer >= len(tx.outputs):
            return None
        if is_op_return(tx.outputs[pointer].script_pubkey):
            return None
        return pointer

    pointer = _take(fields, Tag.POINTER, 1, convert_pointer)

    land = bool(_take(fields, Tag.LAND, 1, lambda v: True if v[0] == 0 else None))

    if Tag.LIFT in fields:
        flaw = flaw or Flaw.LIFT

    if any(tag % 2 == 0 for tag in fields):
        flaw = flaw or Flaw.UNRECOGNIZED_EVEN_TAG

    if flaw is not None:
        return Cenotaph(flaw=flaw, land=land)

    return Envelope(edicts=edicts, pointer=pointer, land=land)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def _encode_tag(payload: bytearray, tag: int, value: int) -> None:
    payload.extend(varint_encode(tag))
    payload.extend(varint_encode(value))


def encipher(envelope: Envelope) -> bytes:
    """Build the OP_RETURN script_pubkey bytes for an envelope.

    Tag order: LAND, POINTER, BODY. Edicts are sorted by asset ID so the
    delta coding never goes negative.
    """
    payload = bytearray()

    if envelope.land:
        _encode_tag(payload, Tag.LAND, 0)

    if envelope.pointer is not None:
        _encode_tag(payload, Tag.POINTER, envelope.pointer)

    if envelope.edicts:
        payload.extend(varint_encode(Tag.BODY))
        previous = AssetId(0, 0)
        for edict in sorted(envelope.edicts, key=lambda e: e.id):
            block, tx = previous.delta(edict.id)
            payload.extend(varint_encode(block))
            payload.extend(varint_encode(tx))
            payload.extend(varint_encode(edict.amount))
            payload.extend(varint_encode(edict.output))
            previous = edict.id

    script = bytearray(MAGIC)
    data = bytes(payload)
    if data:
        script.extend(_push_bytes(data))
    return bytes(script)


def _push_bytes(data: bytes) -> bytes:
    n = len(data)
    if n <= 75:
        return bytes([n]) + data
    if n <= 0xFF:
        return bytes([OP_PUSHDATA1, n]) + data
    if n <= 0xFFFF:
        return bytes([OP_PUSHDATA2]) + n.to_bytes(2, "little") + data
    return bytes([OP_PUSHDATA4]) + n.to_bytes(4, "little") + data


# ---------------------------------------------------------------------------
# Builders for the transactions release 1 produces
# ---------------------------------------------------------------------------

def land_script(pointer: Optional[int] = None) -> bytes:
    """Envelope for a land: LAND, optional POINTER, no edicts.

    Landed seeds go to the pointer output, else the first non-OP_RETURN
    output (the default rule).
    """
    return encipher(Envelope(pointer=pointer, land=True))


def send_script(
    transfers: list[tuple[int, int]],
    change: Optional[int] = None,
    land: bool = False,
) -> bytes:
    """Envelope for a transfer of orchids.

    transfers: (amount_in_seeds, output_index) pairs. change: pointer for
    the unallocated remainder. land: also land the inputs' latent value
    (auto-land in a sponsored send).
    """
    edicts = [Edict(ORCHID, amount, output) for amount, output in transfers]
    return encipher(Envelope(edicts=edicts, pointer=change, land=land))
