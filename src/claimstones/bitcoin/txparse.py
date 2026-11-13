"""Minimal deterministic bitcoin block/transaction parser.

Parses raw block bytes (as stored by Pynode in blocks.dat) into
transactions with the fields the runes scanner needs: outputs
(value + script_pubkey), txid (non-witness serialization), coinbase
detection, and weight/vsize for fee-rate work.

Standard library only. Little-endian on the wire; txids are displayed
big-endian (reversed), matching every block explorer.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass


def dsha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def read_compact_size(raw: bytes, pos: int) -> tuple[int, int]:
    """Bitcoin CompactSize ("varint") — returns (value, new_pos)."""
    first = raw[pos]
    if first < 0xFD:
        return first, pos + 1
    if first == 0xFD:
        return int.from_bytes(raw[pos + 1 : pos + 3], "little"), pos + 3
    if first == 0xFE:
        return int.from_bytes(raw[pos + 1 : pos + 5], "little"), pos + 5
    return int.from_bytes(raw[pos + 1 : pos + 9], "little"), pos + 9


@dataclass
class TxIn:
    prev_txid: bytes  # 32 bytes, internal (little-endian) order
    prev_vout: int
    script_sig: bytes
    sequence: int
    witness: list[bytes] | None = None  # kept only when requested


@dataclass
class TxOut:
    value: int  # satoshis
    script_pubkey: bytes


@dataclass
class Tx:
    txid: bytes  # 32 bytes, internal order; hex display = txid[::-1].hex()
    inputs: list[TxIn]
    outputs: list[TxOut]
    is_coinbase: bool
    weight: int
    wtxid: bytes = b""  # only computed when parsing with keep_witness=True

    @property
    def txid_hex(self) -> str:
        return self.txid[::-1].hex()

    @property
    def vsize(self) -> int:
        return (self.weight + 3) // 4


REGTEST_BITS = 0x207FFFFF   # regtest's constant nBits: w(h) = 2 hashes


@dataclass
class Block:
    height: int  # filled in by the caller (not part of the wire format)
    hash: bytes  # 32 bytes, internal order
    time: int  # header timestamp (unix)
    txs: list[Tx]
    # the header's compact target; the indexer computes W(h) from it
    # (docs/spec/indexer.md §Work). The parsers always fill the real
    # value; the default is for hand-built test blocks only
    bits: int = REGTEST_BITS

    @property
    def hash_hex(self) -> str:
        return self.hash[::-1].hex()


def parse_tx(raw: bytes, pos: int, keep_witness: bool = False) -> tuple[Tx, int]:
    """Parse one transaction starting at pos. Returns (tx, new_pos)."""
    start = pos
    version = raw[pos : pos + 4]
    pos += 4

    segwit = raw[pos] == 0 and raw[pos + 1] == 1
    if segwit:
        pos += 2

    body_start = pos

    n_in, pos = read_compact_size(raw, pos)
    # A hostile or corrupted CompactSize can declare billions of inputs; the
    # loop below would spin for hours on empty slices. The smallest possible
    # input is 41 bytes (32 txid + 4 vout + 1 script_len + 4 sequence).
    if n_in * 41 > len(raw) - pos:
        raise ValueError(
            f"transaction at {start} declares {n_in} inputs, "
            f"more than the {len(raw) - pos} remaining bytes allow"
        )
    inputs = []
    for _ in range(n_in):
        prev_txid = raw[pos : pos + 32]
        prev_vout = int.from_bytes(raw[pos + 32 : pos + 36], "little")
        pos += 36
        script_len, pos = read_compact_size(raw, pos)
        script_sig = raw[pos : pos + script_len]
        pos += script_len
        sequence = int.from_bytes(raw[pos : pos + 4], "little")
        pos += 4
        inputs.append(TxIn(prev_txid, prev_vout, script_sig, sequence))

    n_out, pos = read_compact_size(raw, pos)
    # Smallest possible output is 9 bytes (8 value + 1 script_len).
    if n_out * 9 > len(raw) - pos:
        raise ValueError(
            f"transaction at {start} declares {n_out} outputs, "
            f"more than the {len(raw) - pos} remaining bytes allow"
        )
    outputs = []
    for _ in range(n_out):
        value = int.from_bytes(raw[pos : pos + 8], "little")
        pos += 8
        script_len, pos = read_compact_size(raw, pos)
        outputs.append(TxOut(value, raw[pos : pos + script_len]))
        pos += script_len

    body_end = pos

    if segwit:
        for inp in inputs:
            n_items, pos = read_compact_size(raw, pos)
            if n_items > len(raw) - pos:  # each item is at least 1 length byte
                raise ValueError(
                    f"transaction at {start} declares {n_items} witness items, "
                    f"more than the {len(raw) - pos} remaining bytes allow"
                )
            items = [] if keep_witness else None
            for _ in range(n_items):
                item_len, pos = read_compact_size(raw, pos)
                if keep_witness:
                    items.append(raw[pos : pos + item_len])
                pos += item_len
            inp.witness = items

    locktime = raw[pos : pos + 4]
    pos += 4

    txid = dsha256(version + raw[body_start:body_end] + locktime)

    total_size = pos - start
    base_size = 4 + (body_end - body_start) + 4
    weight = base_size * 3 + total_size

    is_coinbase = (
        len(inputs) == 1
        and inputs[0].prev_txid == b"\x00" * 32
        and inputs[0].prev_vout == 0xFFFFFFFF
    )

    wtxid = b""
    if keep_witness:
        wtxid = dsha256(raw[start:pos]) if segwit else txid

    return Tx(txid, inputs, outputs, is_coinbase, weight, wtxid), pos


def merkle_root(txids: list[bytes]) -> bytes:
    """Plain merkle root - no mutation detection.

    An odd level duplicates its last hash, which is what the consensus
    rules say. That duplication is also the CVE-2012-2459 malleability
    vector, so anything validating an untrusted block must use
    :func:`merkle_root_checked` instead. This form stays for the witness
    commitment in tools/witness_blocks.py, which rebuilds the tree from
    wtxids parsed out of a block we already trust.
    """
    return merkle_root_checked(txids)[0]


def merkle_root_checked(hashes: list[bytes]) -> tuple[bytes, bool]:
    """Merkle root plus a mutation flag - mirrors Core's ComputeMerkleRoot.

    Because an odd level duplicates its last hash, a block with its last
    two transactions repeated hashes to the *same* merkle root as the
    honest block (CVE-2012-2459). Such a block is invalid - two identical
    txids in one block would spend the same inputs twice - but a validator
    that only compares roots accepts it, and then disagrees with the
    network about which block it is holding.

    Core's defence is to look for an identical adjacent pair at every
    level *before* the duplication is applied; that is the inner loop.
    Returns (root, mutated); a mutated block must be rejected.
    """
    if not hashes:
        return b"\x00" * 32, False

    level = hashes
    mutated = False
    while len(level) > 1:
        for i in range(0, len(level) - 1, 2):
            if level[i] == level[i + 1]:
                mutated = True
        if len(level) % 2:
            level = level + [level[-1]]
        level = [
            dsha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)
        ]
    return level[0], mutated


def parse_block(raw: bytes, height: int = -1, keep_witness: bool = False) -> Block:
    """Parse a full raw block (header + transactions).

    The transaction list is validated against the header's merkle
    root, which proves we parsed exactly the block's transactions
    (witness data excepted — txids do not commit to witnesses).
    Trailing bytes after the last transaction are ignored: some
    stored block entries over-state their size.
    """
    block_hash = dsha256(raw[:80])
    time = int.from_bytes(raw[68:72], "little")

    pos = 80
    n_tx, pos = read_compact_size(raw, pos)
    txs = []
    for _ in range(n_tx):
        tx, pos = parse_tx(raw, pos, keep_witness=keep_witness)
        txs.append(tx)

    root, mutated = merkle_root_checked([tx.txid for tx in txs])
    if root != raw[36:68]:
        raise ValueError(f"merkle root mismatch (height {height})")
    if mutated:
        # Same root as some honest block, but duplicated transactions.
        raise ValueError(f"mutated merkle tree - duplicate txids (height {height})")

    return Block(height, block_hash, time, txs,
                 bits=int.from_bytes(raw[72:76], "little"))


MAX_BLOCK_SERIALIZED_SIZE = 4_000_000  # consensus limit, post-segwit

# BIP141: OP_RETURN 0x24 aa21a9ed <32-byte commitment>
WITNESS_COMMITMENT_PREFIX = bytes.fromhex("6a24aa21a9ed")


def witness_commitment_error(block: Block) -> str | None:
    """Check the BIP141 witness commitment. None means OK.

    The transaction merkle root commits to txids, and a txid does not
    cover witness data - so on a post-segwit block, where most of the
    bytes *are* witness data, a merkle-root check alone proves almost
    nothing about what arrived. The coinbase's commitment output is what
    covers the rest, and this is the only check that notices a flipped
    bit inside a signature.

    Needs a block parsed with ``keep_witness=True``; wtxids are empty
    otherwise and the check would fail for the wrong reason.
    """
    coinbase = block.txs[0]

    segwit_present = any(
        any(inp.witness for inp in tx.inputs) for tx in block.txs
    )
    commitments = [
        o.script_pubkey[6:38]
        for o in coinbase.outputs
        if o.script_pubkey.startswith(WITNESS_COMMITMENT_PREFIX)
        and len(o.script_pubkey) >= 38
    ]
    if not commitments:
        if segwit_present:
            return "witness data present but no commitment in coinbase"
        return None  # fully pre-segwit block: nothing to commit to

    reserved = (coinbase.inputs[0].witness or [None])[0]
    if reserved is None or len(reserved) != 32:
        return "coinbase witness reserved value missing"

    wtxids = [b"\x00" * 32] + [tx.wtxid for tx in block.txs[1:]]
    root, mutated = merkle_root_checked(wtxids)
    if mutated:
        return "mutated witness merkle tree"
    if dsha256(root + reserved) != commitments[-1]:  # BIP141: the last counts
        return "witness commitment mismatch"
    return None


def target_from_bits(bits: int) -> int:
    """Expand the header's compact nBits field into a full 256-bit target."""
    exponent = bits >> 24
    mantissa = bits & 0x007FFFFF
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def verify_block(
    raw: bytes,
    canonical_hash: bytes | None = None,
    height: int = -1,
    keep_witness: bool = False,
    check_pow: bool = True,
    check_witness: bool = True,
) -> Block:
    """Parse a block from an untrusted source, or raise ValueError.

    This is the gate for block bytes that did not come from our own
    validated store: a peer, an explorer API, a file copied off another
    machine. It proves four things, and deliberately not more:

    1. **Framing** - the buffer is a plausible block and the transaction
       list ends exactly where the buffer does. No trailing slack is
       tolerated here (``parse_block`` allows it, because stored index
       entries over-state their size; a block off the wire has no excuse).
    2. **Identity** - ``dsha256(header)`` equals ``canonical_hash``, the
       hash we already hold from our independently synced header chain.
       That is what makes a sender untrusted-but-useful: they cannot hand
       us a different block than the one we asked for.
    3. **Proof of work** - the header hash meets its own nBits target.
       Redundant when ``canonical_hash`` comes from a header chain whose
       work we already checked, which is why it is separable; essential
       when it does not.
    4. **Commitment** - the transactions hash to the header's merkle
       root, with the CVE-2012-2459 duplicate-branch case rejected, and
       the witnesses hash to the coinbase's BIP141 commitment. Both
       halves are needed: txids do not cover witness data, so on a
       post-segwit block the merkle root alone leaves most of the bytes
       unchecked.

    What it does *not* prove: that the transactions are valid, that their
    inputs exist or are unspent, that scripts pass, or that nBits is the
    right difficulty for this height. A block can clear every check here
    and still be consensus-invalid. Use it to trust *transport*, never as
    a substitute for validation by Bitcoin Core.
    """
    if len(raw) < 81:
        raise ValueError(f"block is {len(raw)} bytes, too short for a header + 1 tx")
    if len(raw) > MAX_BLOCK_SERIALIZED_SIZE:
        raise ValueError(
            f"block is {len(raw)} bytes, over the {MAX_BLOCK_SERIALIZED_SIZE} limit"
        )

    block_hash = dsha256(raw[:80])
    if canonical_hash is not None and block_hash != canonical_hash:
        raise ValueError(
            f"block hash mismatch (height {height}): "
            f"got {block_hash[::-1].hex()}, expected {canonical_hash[::-1].hex()}"
        )

    if check_pow:
        bits = int.from_bytes(raw[72:76], "little")
        target = target_from_bits(bits)
        if target == 0 or int.from_bytes(block_hash[::-1], "big") > target:
            raise ValueError(f"header does not meet its own target (height {height})")

    n_tx, pos = read_compact_size(raw, 80)
    if n_tx == 0:
        raise ValueError(f"block declares 0 transactions (height {height})")
    # Smallest possible transaction is 60 bytes serialized.
    if n_tx * 60 > len(raw) - pos:
        raise ValueError(
            f"block declares {n_tx} transactions, more than the "
            f"{len(raw) - pos} remaining bytes allow (height {height})"
        )

    txs = []
    for i in range(n_tx):
        try:
            # The witness check needs wtxids, so it forces witness parsing.
            tx, pos = parse_tx(raw, pos, keep_witness=keep_witness or check_witness)
        except (IndexError, struct.error) as exc:
            raise ValueError(
                f"truncated transaction {i} of {n_tx} (height {height}): {exc}"
            ) from exc
        txs.append(tx)

    if pos != len(raw):
        raise ValueError(
            f"{len(raw) - pos} trailing bytes after the last transaction "
            f"(height {height})"
        )

    root, mutated = merkle_root_checked([tx.txid for tx in txs])
    if root != raw[36:68]:
        raise ValueError(f"merkle root mismatch (height {height})")
    if mutated:
        raise ValueError(f"mutated merkle tree - duplicate txids (height {height})")

    if not txs[0].is_coinbase:
        raise ValueError(f"first transaction is not a coinbase (height {height})")
    if any(tx.is_coinbase for tx in txs[1:]):
        raise ValueError(f"coinbase after the first transaction (height {height})")

    time = int.from_bytes(raw[68:72], "little")
    block = Block(height, block_hash, time, txs,
                  bits=int.from_bytes(raw[72:76], "little"))

    if check_witness:
        err = witness_commitment_error(block)
        if err is not None:
            raise ValueError(f"{err} (height {height})")

    return block


def block_subsidy(height: int) -> int:
    """Coinbase subsidy in satoshis at a given height."""
    return 50 * 100_000_000 >> (height // 210_000)
