"""Tests for the untrusted-block gate: txparse.verify_block.

The case that matters here is the merkle mutation (CVE-2012-2459). A forged
block whose last transaction is repeated carries the *same* header - so the
same block hash - and hashes to the *same* merkle root, so a validator that
compares only those two values accepts a block the network considers
invalid. These tests build that exact forgery and require it to be rejected.

Blocks are assembled byte by byte rather than fetched: the forgery needs an
odd transaction count >= 3 and a known-good witness commitment, and building
them locally keeps the suite offline and deterministic. Real chain data from
the local node is used too, where it is available, as a check that the gate
accepts blocks that actually happened.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from claimstones.bitcoin.txparse import (
    MAX_BLOCK_SERIALIZED_SIZE,
    dsha256,
    merkle_root,
    merkle_root_checked,
    parse_block,
    parse_tx,
    read_compact_size,
    target_from_bits,
    verify_block,
    witness_commitment_error,
)

CLI = os.environ.get("BITCOIN_CLI", r"D:\bitcoin\bin\bitcoin-cli.exe")
DATADIR = os.environ.get("BITCOIN_DATADIR", r"D:\bitcoin-data")

NO_POW = dict(check_pow=False)  # synthetic headers carry no work


# --------------------------------------------------------------------------
# block construction
# --------------------------------------------------------------------------


def compact_size(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def make_tx(
    tag: int,
    coinbase: bool = False,
    extra_outputs: list[bytes] | None = None,
    witness: list[bytes] | None = None,
) -> bytes:
    """A syntactically valid transaction. Not consensus-valid, and not
    meant to be: these tests exercise framing and the merkle trees."""
    out = bytearray()
    out += (1).to_bytes(4, "little")  # version
    if witness is not None:
        out += b"\x00\x01"  # segwit marker + flag

    out += compact_size(1)  # one input
    if coinbase:
        out += b"\x00" * 32 + (0xFFFFFFFF).to_bytes(4, "little")
    else:
        out += bytes([tag]) * 32 + (0).to_bytes(4, "little")
    out += compact_size(4) + tag.to_bytes(4, "little")  # script_sig
    out += (0xFFFFFFFF).to_bytes(4, "little")  # sequence

    scripts = [b"\x51"] + list(extra_outputs or [])  # OP_TRUE, then any extras
    out += compact_size(len(scripts))
    for script in scripts:
        out += (5000).to_bytes(8, "little")
        out += compact_size(len(script)) + script

    if witness is not None:
        out += compact_size(len(witness))
        for item in witness:
            out += compact_size(len(item)) + item

    out += (0).to_bytes(4, "little")  # locktime
    return bytes(out)


def make_header(merkle: bytes, bits: int = 0x1D00FFFF, nonce: int = 0) -> bytes:
    header = bytearray()
    header += (1).to_bytes(4, "little")  # version
    header += b"\x00" * 32  # prev block
    header += merkle  # bytes 36:68
    header += (1231006505).to_bytes(4, "little")  # time
    header += bits.to_bytes(4, "little")
    header += nonce.to_bytes(4, "little")
    assert len(header) == 80
    return bytes(header)


def assemble(txs: list[bytes], merkle: bytes | None = None) -> bytes:
    """Serialize a block. `merkle` overrides the computed root, which is
    how the forgeries below keep the honest header."""
    ids = [txid_of(tx) for tx in txs]
    root = merkle if merkle is not None else merkle_root(ids)
    return make_header(root) + compact_size(len(txs)) + b"".join(txs)


def txid_of(raw_tx: bytes) -> bytes:
    tx, pos = parse_tx(raw_tx, 0, keep_witness=True)
    assert pos == len(raw_tx), "test tx did not round-trip through the parser"
    return tx.txid


def wtxid_of(raw_tx: bytes) -> bytes:
    tx, _ = parse_tx(raw_tx, 0, keep_witness=True)
    return tx.wtxid


def duplicate_last_tx(raw: bytes) -> bytes:
    """The CVE-2012-2459 forgery: append a verbatim copy of the final
    transaction and bump the count. The header is untouched, so both the
    block hash and the merkle root still match the honest block."""
    n_tx, body_start = read_compact_size(raw, 80)
    assert n_tx % 2 == 1 and n_tx >= 3, f"need an odd tx count >= 3, got {n_tx}"

    pos = body_start
    for _ in range(n_tx):
        last_start = pos
        _, pos = parse_tx(raw, pos, keep_witness=True)
    assert pos == len(raw), "block has trailing bytes"

    return raw[:80] + compact_size(n_tx + 1) + raw[body_start:] + raw[last_start:pos]


@pytest.fixture(scope="module")
def three_tx_block() -> bytes:
    txs = [make_tx(1, coinbase=True), make_tx(2), make_tx(3)]
    raw = assemble(txs)
    assert len(raw) < MAX_BLOCK_SERIALIZED_SIZE
    return raw


@pytest.fixture(scope="module")
def segwit_block() -> bytes:
    """A block with a correct BIP141 witness commitment.

    The commitment lives in the coinbase and covers the *other*
    transactions' wtxids, with the coinbase's own wtxid taken as zero -
    so there is no circularity: build the spend first, then the coinbase
    around its commitment.
    """
    spend = make_tx(7, witness=[b"\xab" * 72])
    reserved = b"\x00" * 32
    wroot = merkle_root([b"\x00" * 32, wtxid_of(spend)])
    commitment = bytes.fromhex("6a24aa21a9ed") + dsha256(wroot + reserved)
    coinbase = make_tx(
        1, coinbase=True, extra_outputs=[commitment], witness=[reserved]
    )
    return assemble([coinbase, spend])


# --------------------------------------------------------------------------
# merkle mutation detection
# --------------------------------------------------------------------------


def _h(i: int) -> bytes:
    return bytes([i]) * 32


def test_merkle_single_hash_is_itself():
    assert merkle_root_checked([_h(1)]) == (_h(1), False)


def test_merkle_root_matches_plain_helper():
    hashes = [_h(i) for i in range(1, 6)]
    assert merkle_root(hashes) == merkle_root_checked(hashes)[0]


def test_merkle_odd_level_duplicates_last():
    a, b, c = _h(1), _h(2), _h(3)
    expected = dsha256(dsha256(a + b) + dsha256(c + c))
    assert merkle_root_checked([a, b, c]) == (expected, False)


def test_merkle_three_and_forged_four_share_a_root():
    """The vulnerability itself: two different trees, one root."""
    a, b, c = _h(1), _h(2), _h(3)
    honest, honest_mut = merkle_root_checked([a, b, c])
    forged, forged_mut = merkle_root_checked([a, b, c, c])
    assert honest == forged
    assert honest_mut is False
    assert forged_mut is True  # only the flag separates them


def test_merkle_mutation_detected_at_a_deeper_level():
    a, b, c, d, e = (_h(i) for i in range(1, 6))
    honest, _ = merkle_root_checked([a, b, c, d, e])
    forged, mutated = merkle_root_checked([a, b, c, d, e, e])
    assert honest == forged
    assert mutated is True


def test_merkle_empty_list():
    assert merkle_root_checked([]) == (b"\x00" * 32, False)


# --------------------------------------------------------------------------
# nBits expansion
# --------------------------------------------------------------------------


def test_target_from_bits_genesis():
    # 0x1d00ffff expands to the difficulty-1 target, 0x00000000FFFF0000...0000
    assert target_from_bits(0x1D00FFFF) == 0xFFFF << (8 * 26)


def test_target_from_bits_small_exponents():
    assert target_from_bits(0x01003456) == 0x00
    assert target_from_bits(0x02008000) == 0x80
    assert target_from_bits(0x04923456) == 0x12345600  # sign bit masked off


# --------------------------------------------------------------------------
# the forgery
# --------------------------------------------------------------------------


def test_synthetic_block_verifies(three_tx_block):
    block = verify_block(three_tx_block, dsha256(three_tx_block[:80]), 1, **NO_POW)
    assert len(block.txs) == 3
    assert block.txs[0].is_coinbase
    assert not any(tx.is_coinbase for tx in block.txs[1:])


def test_forged_duplicate_tx_is_rejected(three_tx_block):
    forged = duplicate_last_tx(three_tx_block)
    canonical = dsha256(three_tx_block[:80])

    # The forgery is genuinely indistinguishable by hash and by root.
    assert dsha256(forged[:80]) == canonical
    assert forged[36:68] == three_tx_block[36:68]
    assert len(forged) > len(three_tx_block)
    ids = _txids(forged)
    assert merkle_root(ids) == forged[36:68]

    with pytest.raises(ValueError, match="mutated merkle tree"):
        verify_block(forged, canonical, 1, **NO_POW)

    # parse_block is the lenient path and must refuse it too.
    with pytest.raises(ValueError, match="mutated merkle tree"):
        parse_block(forged, 1)


def _txids(raw: bytes) -> list[bytes]:
    n_tx, pos = read_compact_size(raw, 80)
    ids = []
    for _ in range(n_tx):
        tx, pos = parse_tx(raw, pos, keep_witness=True)
        ids.append(tx.txid)
    return ids


# --------------------------------------------------------------------------
# witness commitment
# --------------------------------------------------------------------------


def test_segwit_block_verifies(segwit_block):
    block = verify_block(segwit_block, dsha256(segwit_block[:80]), 2, **NO_POW)
    assert witness_commitment_error(block) is None


def test_witness_bitflip_is_caught_by_the_commitment(segwit_block):
    """A flipped bit inside a signature leaves every txid unchanged.

    The transaction merkle root commits to txids, and txids exclude
    witness data - so this corruption is invisible to the merkle check
    and only the BIP141 commitment notices it. On a post-segwit block
    most of the bytes are witness data, which is why verify_block checks
    both.
    """
    pos = segwit_block.index(b"\xab" * 72)
    tampered = bytearray(segwit_block)
    tampered[pos + 10] ^= 0x80  # the XOR pattern the faulty remote produces

    # Invisible to the merkle root: the txids are identical.
    assert _txids(bytes(tampered)) == _txids(segwit_block)
    assert merkle_root(_txids(bytes(tampered))) == segwit_block[36:68]

    with pytest.raises(ValueError, match="witness commitment mismatch"):
        verify_block(bytes(tampered), dsha256(segwit_block[:80]), 2, **NO_POW)

    # ...and it passes when the witness check is switched off, which is
    # exactly why it defaults to on.
    verify_block(
        bytes(tampered), dsha256(segwit_block[:80]), 2, check_witness=False, **NO_POW
    )


def test_missing_commitment_with_witness_data_is_rejected():
    spend = make_tx(7, witness=[b"\xab" * 72])
    coinbase = make_tx(1, coinbase=True, witness=[b"\x00" * 32])
    raw = assemble([coinbase, spend])
    with pytest.raises(ValueError, match="no commitment in coinbase"):
        verify_block(raw, dsha256(raw[:80]), 2, **NO_POW)


def test_pre_segwit_block_needs_no_commitment(three_tx_block):
    block = parse_block(three_tx_block, 1)
    assert witness_commitment_error(block) is None


# --------------------------------------------------------------------------
# identity, framing and corruption
# --------------------------------------------------------------------------


def test_wrong_canonical_hash_is_rejected(three_tx_block):
    with pytest.raises(ValueError, match="block hash mismatch"):
        verify_block(three_tx_block, b"\x11" * 32, 1, **NO_POW)


def test_truncated_block_is_rejected(three_tx_block):
    canonical = dsha256(three_tx_block[:80])
    with pytest.raises(ValueError):
        verify_block(three_tx_block[:-20], canonical, 1, **NO_POW)


def test_trailing_bytes_are_rejected(three_tx_block):
    canonical = dsha256(three_tx_block[:80])
    with pytest.raises(ValueError, match="trailing bytes"):
        verify_block(three_tx_block + b"\x00" * 4, canonical, 1, **NO_POW)


def test_tampered_output_breaks_the_root(three_tx_block):
    """One flipped bit in transaction data - the single-bit DRAM flip case."""
    canonical = dsha256(three_tx_block[:80])
    tampered = bytearray(three_tx_block)
    tampered[len(tampered) - 30] ^= 0x80
    with pytest.raises(ValueError, match="merkle root mismatch"):
        verify_block(bytes(tampered), canonical, 1, **NO_POW)


def test_header_bitflip_is_caught_by_the_canonical_hash(three_tx_block):
    canonical = dsha256(three_tx_block[:80])
    tampered = bytearray(three_tx_block)
    tampered[40] ^= 0x80  # inside the merkle root field
    with pytest.raises(ValueError, match="block hash mismatch"):
        verify_block(bytes(tampered), canonical, 1, **NO_POW)


def test_coinbase_must_come_first():
    txs = [make_tx(2), make_tx(1, coinbase=True), make_tx(3)]
    raw = assemble(txs)
    with pytest.raises(ValueError, match="not a coinbase"):
        verify_block(raw, dsha256(raw[:80]), 1, **NO_POW)


def test_second_coinbase_is_rejected():
    txs = [make_tx(1, coinbase=True), make_tx(2, coinbase=True), make_tx(3)]
    raw = assemble(txs)
    with pytest.raises(ValueError, match="coinbase after the first"):
        verify_block(raw, dsha256(raw[:80]), 1, **NO_POW)


def test_too_short():
    with pytest.raises(ValueError, match="too short"):
        verify_block(b"\x00" * 80)


def test_over_size_limit():
    with pytest.raises(ValueError, match="over the"):
        verify_block(b"\x00" * (MAX_BLOCK_SERIALIZED_SIZE + 1), **NO_POW)


def test_absurd_tx_count_does_not_hang():
    """A forged CompactSize must be rejected before any loop runs."""
    raw = make_header(b"\x00" * 32) + b"\xff" + (2**64 - 1).to_bytes(8, "little")
    raw += b"\x00" * 64
    with pytest.raises(ValueError, match="more than the"):
        verify_block(raw, **NO_POW)


def test_absurd_input_count_does_not_hang():
    """Same guard one level down, inside parse_tx."""
    tx = (1).to_bytes(4, "little") + b"\xff" + (2**64 - 1).to_bytes(8, "little")
    raw = make_header(b"\x00" * 32) + compact_size(1) + tx + b"\x00" * 60
    with pytest.raises(ValueError, match="declares .* inputs"):
        verify_block(raw, **NO_POW)


def test_zero_tx_count():
    raw = make_header(b"\x00" * 32) + b"\x00" + b"\x00" * 64
    with pytest.raises(ValueError, match="0 transactions"):
        verify_block(raw, **NO_POW)


def test_pow_check_rejects_an_unworked_header(three_tx_block):
    """Default check_pow=True: a real target, a hash nowhere near it."""
    with pytest.raises(ValueError, match="does not meet its own target"):
        verify_block(three_tx_block, dsha256(three_tx_block[:80]), 1)


# --------------------------------------------------------------------------
# real chain data from the local node, when it is running
# --------------------------------------------------------------------------


def _cli(*args: str) -> str:
    out = subprocess.run(
        [CLI, f"-datadir={DATADIR}", *args], capture_output=True, text=True, timeout=60
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"bitcoin-cli {args} failed")
    return out.stdout.strip()


@pytest.fixture(scope="module")
def node_blocks() -> dict[int, bytes]:
    """A few blocks straight out of the local node, with the hashes it
    reports - so verify_block is tested against its real caller."""
    try:
        tip = int(_cli("getblockcount"))
    except Exception as exc:  # node not running: these tests are a bonus
        pytest.skip(f"local node not reachable: {exc}")
    if tip < 170:
        pytest.skip(f"node has only {tip} blocks")

    out = {}
    for height in (1, 170, tip):
        h = _cli("getblockhash", str(height))
        out[height] = bytes.fromhex(_cli("getblock", h, "0"))
    return out


def test_node_blocks_verify(node_blocks):
    """Full gate, proof of work included, on blocks that really happened."""
    for height, raw in node_blocks.items():
        canonical = dsha256(raw[:80])
        block = verify_block(raw, canonical, height)
        assert block.txs[0].is_coinbase
        assert block.hash == canonical


def test_node_block_170_has_the_first_payment(node_blocks):
    """Block 170 holds Satoshi's 10 BTC to Hal Finney - a 2-tx block, the
    smallest real block that exercises a merkle tree with a pair in it."""
    block = verify_block(node_blocks[170], dsha256(node_blocks[170][:80]), 170)
    assert len(block.txs) == 2
    assert merkle_root([tx.txid for tx in block.txs]) == node_blocks[170][36:68]


def test_node_block_bitflip_is_rejected(node_blocks):
    """Every byte of a real block, flipped one at a time, must be caught.

    Walks the whole buffer rather than sampling, because the point of the
    gate is that there is no window where a corrupted byte slips through.
    """
    raw = node_blocks[170]
    canonical = dsha256(raw[:80])
    for pos in range(len(raw)):
        tampered = bytearray(raw)
        tampered[pos] ^= 0x80
        with pytest.raises(ValueError):
            verify_block(bytes(tampered), canonical, 170)
