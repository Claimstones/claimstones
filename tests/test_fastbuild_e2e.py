"""The whole fastbuild pipeline over a synthetic four-block chain: phase 0
scans hand-serialized blk files, phase 1 resolves, phase 2 replays with
epochs forced small enough that outputs cross epoch boundaries through
spill files. The final stream must equal a brute-force dict replay.

The chain exercises: multi-input consolidation, fee tails into the
coinbase, an OP_RETURN output, a same-block spend, and an output that
lives across an epoch boundary."""

import json
import os

from claimstones.bitcoin.txparse import dsha256, merkle_root
from claimstones.indexer import runcore
from claimstones.indexer.fastbuild import blocks as B
from claimstones.indexer.fastbuild import replay as P
from claimstones.indexer.fastbuild import resolve as R
from claimstones.lineage.walker import subsidy_sats

COIN = 100_000_000
S_A, S_B, S_RET = b"\x51", b"\x52", b"\x6a\x04test"
quiet = lambda *a, **k: None


def ser_tx(ins, outs):
    b = bytearray(b"\x01\x00\x00\x00")
    b.append(len(ins))
    for ptxid, pvout, sig in ins:
        b += ptxid + pvout.to_bytes(4, "little")
        b.append(len(sig))
        b += sig + b"\xff\xff\xff\xff"
    b.append(len(outs))
    for v, spk in outs:
        b += v.to_bytes(8, "little")
        b.append(len(spk))
        b += spk
    b += b"\x00\x00\x00\x00"
    return bytes(b)


def coinbase(height, outs):
    return ser_tx([(b"\x00" * 32, 0xFFFFFFFF, bytes([42, height]))], outs)


def block(prev, txs_raw):
    header = (b"\x01\x00\x00\x00" + prev
              + merkle_root([dsha256(t) for t in txs_raw])
              + (1234).to_bytes(4, "little") + bytes.fromhex("ffff001d")
              + b"\x00\x00\x00\x00")
    return dsha256(header), header + bytes([len(txs_raw)]) + b"".join(txs_raw)


def build_chain():
    """Four blocks; returns (list of raw blocks, list of [tx bytes])."""
    sub = [subsidy_sats(h) for h in range(4)]
    cb0 = coinbase(0, [(sub[0], S_A)])
    # b1: tx1 spends cb0 -> 30, 15, fee 5 (in COIN units of the subsidy)
    tx1 = ser_tx([(dsha256(cb0), 0, b"")],
                 [(30 * COIN, S_A), (15 * COIN, S_B)])
    cb1 = coinbase(1, [(sub[1] + 5 * COIN, S_B)])
    # b2: tx2 consolidates tx1:0 + cb1:0 -> 10, OP_RETURN 0, 74, fee 1;
    #     tx3 spends tx2:2 in the same block, fee 0
    tx2 = ser_tx([(dsha256(tx1), 0, b""), (dsha256(cb1), 0, b"")],
                 [(10 * COIN, S_A), (0, S_RET), (74 * COIN, S_B)])
    tx3 = ser_tx([(dsha256(tx2), 2, b"")], [(74 * COIN, S_A)])
    cb2 = coinbase(2, [(sub[2] + 1 * COIN, S_A)])
    # b3: tx4 spends tx1:1 — created in epoch 0, dies here: spill crossing
    tx4 = ser_tx([(dsha256(tx1), 1, b"")], [(15 * COIN, S_B)])
    cb3 = coinbase(3, [(sub[3], S_B)])

    txsets = [[cb0], [cb1, tx1], [cb2, tx2, tx3], [cb3, tx4]]
    raws, prev = [], b"\x00" * 32
    for txs in txsets:
        prev, raw = block(prev, txs)
        raws.append(raw)
    return raws, txsets


def brute_force(raws):
    """Dict replay with runcore: {(txid, vout): (script, runs)} at the end."""
    from claimstones.bitcoin.txparse import parse_block
    state = {}
    for height, raw in enumerate(raws):
        blk = parse_block(raw, height)
        tails = []
        for tx in blk.txs[1:]:
            parts = [state.pop((inp.prev_txid, inp.prev_vout))[1]
                     for inp in tx.inputs]
            pieces, leftover = runcore.cut(runcore.concat(parts),
                                           [o.value for o in tx.outputs])
            tails.append(leftover)
            for vout, piece in enumerate(pieces):
                state[(tx.txid, vout)] = (tx.outputs[vout].script_pubkey, piece)
        cb, sub = blk.txs[0], subsidy_sats(height)
        pieces, _ = runcore.cut(
            runcore.concat(([runcore.make_run(height, sub)] if sub else [])
                           + tails),
            [o.value for o in cb.outputs])
        for vout, piece in enumerate(pieces):
            state[(cb.txid, vout)] = (cb.outputs[vout].script_pubkey, piece)
    return state


import pytest


@pytest.mark.parametrize("workers", [0, 2])
def test_pipeline_end_to_end(tmp_path, monkeypatch, workers):
    monkeypatch.setattr(R, "HIST_BIN", 1)        # histogram per height
    monkeypatch.setattr(R, "RES_PER_RANGE", 4)   # force the multi-range path
    monkeypatch.setattr(R, "DEATH_PER_RANGE", 4)

    raws, _ = build_chain()
    blocks_dir = str(tmp_path / "blocks")
    os.makedirs(blocks_dir)
    with open(os.path.join(blocks_dir, "blk00000.dat"), "wb") as f:
        for raw in raws:
            f.write(B.MAGIC_MAINNET + len(raw).to_bytes(4, "little") + raw)

    # phase 0
    scanned = B.scan(blocks_dir, report=quiet)
    chain = B.best_chain(scanned)
    assert len(chain) == 4
    build_dir = str(tmp_path / "build")
    idx_path = os.path.join(build_dir, "blocks.idx")
    B.write_index(idx_path, scanned, chain, blocks_dir, 0.0)
    idx = B.BlockIndex(idx_path)

    # phase 1
    n_cre, n_spd = R.emit(idx, 3, build_dir, report=quiet)
    hist, overwritten, joined = R.join(build_dir, 3, report=quiet)
    assert joined == n_spd == 5 and overwritten == []
    assert hist == [0, 1, 3, 1]                  # deaths per height
    R.scatter_resolved(build_dir, n_spd,
                       os.path.join(build_dir, "resolved.ids"), report=quiet)
    alive = R.scatter_death(build_dir, n_cre,
                            os.path.join(build_dir, "death.u32"), report=quiet)
    assert (n_cre, alive) == (11, 6)
    with open(os.path.join(build_dir, "phase-1.manifest.json"), "w") as f:
        json.dump({"end": 3, "creations": n_cre, "spends": n_spd,
                   "alive": alive, "hist_bin": 1, "death_histogram": hist,
                   "overwritten_cids": overwritten}, f)

    # phase 2, with a budget so small every histogram bin is its own epoch:
    # tx1:1 (born height 1, dies height 3) must survive through a spill file
    out = os.path.join(build_dir, "final.stream")
    count, run_count = P.replay(idx, build_dir, out,
                                budget_bytes=40, report=quiet,
                                workers=workers)
    assert count == 6

    from claimstones.indexer.fastbuild.spill import iter_final
    got = {(txid, vout): (script, runs)
           for _, vout, txid, script, runs in iter_final(out)}
    want = brute_force(raws)
    assert got == want
    assert sum(len(r) // 12 for _, r in got.values()) == run_count
    # the spill directory consumed itself
    assert os.listdir(os.path.join(build_dir, "epochs")) == []


def test_checkpoint_resume_is_bit_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HIST_BIN", 1)
    raws, _ = build_chain()
    blocks_dir = str(tmp_path / "blocks")
    os.makedirs(blocks_dir)
    with open(os.path.join(blocks_dir, "blk00000.dat"), "wb") as f:
        for raw in raws:
            f.write(B.MAGIC_MAINNET + len(raw).to_bytes(4, "little") + raw)
    scanned = B.scan(blocks_dir, report=quiet)
    build_dir = str(tmp_path / "build")
    idx_path = os.path.join(build_dir, "blocks.idx")
    B.write_index(idx_path, scanned, B.best_chain(scanned), blocks_dir, 0.0)
    idx = B.BlockIndex(idx_path)
    n_cre, n_spd = R.emit(idx, 3, build_dir, report=quiet)
    hist, ow, _ = R.join(build_dir, 3, report=quiet)
    R.scatter_resolved(build_dir, n_spd,
                       os.path.join(build_dir, "resolved.ids"), report=quiet)
    alive = R.scatter_death(build_dir, n_cre,
                            os.path.join(build_dir, "death.u32"), report=quiet)
    with open(os.path.join(build_dir, "phase-1.manifest.json"), "w") as f:
        json.dump({"end": 3, "creations": n_cre, "spends": n_spd,
                   "alive": alive, "hist_bin": 1, "death_histogram": hist,
                   "overwritten_cids": ow}, f)
    out = os.path.join(build_dir, "final.stream")
    ckpt = os.path.join(build_dir, "phase-2.checkpoint.json")

    # the uninterrupted run is the yardstick
    counts = P.replay(idx, build_dir, out, budget_bytes=40, report=quiet)
    with open(out, "rb") as f:
        want = f.read()
    assert not os.path.exists(ckpt)              # done runs clean it up

    # crash after epoch 0's checkpoint, then scribble past the recorded
    # offsets as a mid-epoch crash would have
    assert P.replay(idx, build_dir, out, budget_bytes=40, report=quiet,
                    _stop_after_epoch=0) is None
    assert os.path.exists(ckpt)
    with open(out + ".tmp", "ab") as f:
        f.write(b"torn mid-epoch write")
    for name in os.listdir(os.path.join(build_dir, "epochs")):
        with open(os.path.join(build_dir, "epochs", name), "ab") as f:
            f.write(b"junk")

    # a different budget must refuse the checkpoint
    with pytest.raises(RuntimeError, match="different parameters"):
        P.replay(idx, build_dir, out, budget_bytes=41, report=quiet)

    # the resume must reproduce the uninterrupted run exactly
    lines = []
    assert P.replay(idx, build_dir, out, budget_bytes=40,
                    report=lines.append) == counts
    with open(out, "rb") as f:
        assert f.read() == want
    assert any("resuming at epoch" in l for l in lines)
    assert not os.path.exists(ckpt)
    assert os.listdir(os.path.join(build_dir, "epochs")) == []
    with open(os.path.join(build_dir, "phase-2.manifest.json")) as f:
        assert json.load(f)["resumed_from_epoch"] >= 1


def test_reference_parses_grouped_numbers(tmp_path):
    # real slowbuild report lines: counts are space-grouped, and more
    # space-separated fields follow (this format killed the first 710k run:
    # a non-greedy match read "runs 1 172 103 540" as runs=1)
    ref = tmp_path / "old.log"
    ref.write_text(
        "  h=620000  1.4 blk/s  3233 tx/s  utxos 108 197 005  "
        "runs 1 172 103 540  runs/utxo 10.83  bytes/utxo 130.0  "
        "script b/utxo 36.2  hot 0  db 38.25 GB  eta 77.8 h\n"
        "  h=2  1.0 blk/s  3 tx/s  utxos 9 runs 99 runs/utxo x\n"
        "  h=710000  0.9 blk/s  12 755 tx/s  utxos 125 397 010  "
        "runs 1 668 243 004  runs/utxo 13.31\n")
    assert P.load_reference(str(ref)) == {
        620000: (108_197_005, 1_172_103_540),
        2: (9, 99),
        710000: (125_397_010, 1_668_243_004)}


def test_reference_mismatch_warns_but_never_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HIST_BIN", 1)
    raws, _ = build_chain()
    blocks_dir = str(tmp_path / "blocks")
    os.makedirs(blocks_dir)
    with open(os.path.join(blocks_dir, "blk00000.dat"), "wb") as f:
        for raw in raws:
            f.write(B.MAGIC_MAINNET + len(raw).to_bytes(4, "little") + raw)
    scanned = B.scan(blocks_dir, report=quiet)
    build_dir = str(tmp_path / "build")
    idx_path = os.path.join(build_dir, "blocks.idx")
    B.write_index(idx_path, scanned, B.best_chain(scanned), blocks_dir, 0.0)
    idx = B.BlockIndex(idx_path)
    n_cre, n_spd = R.emit(idx, 3, build_dir, report=quiet)
    hist, ow, _ = R.join(build_dir, 3, report=quiet)
    R.scatter_resolved(build_dir, n_spd,
                       os.path.join(build_dir, "resolved.ids"), report=quiet)
    alive = R.scatter_death(build_dir, n_cre,
                            os.path.join(build_dir, "death.u32"), report=quiet)
    with open(os.path.join(build_dir, "phase-1.manifest.json"), "w") as f:
        json.dump({"end": 3, "creations": n_cre, "spends": n_spd,
                   "alive": alive, "hist_bin": 1, "death_histogram": hist,
                   "overwritten_cids": ow}, f)
    ref = tmp_path / "old.log"
    ref.write_text("  h=2  1.0 blk/s  3 tx/s  utxos 9 runs 99 runs/utxo x\n")
    lines = []
    count, _ = P.replay(idx, build_dir,
                        os.path.join(build_dir, "final.stream"),
                        budget_bytes=1 << 30, report_every=2,
                        reference=str(ref), report=lines.append)
    assert count == 6                 # the build itself ran to the end
    text = "\n".join(lines)
    assert "REF MISMATCH h=2" in text and "ref MISMATCH" in text
    with open(os.path.join(build_dir, "phase-2.manifest.json")) as f:
        assert json.load(f)["ref_mismatches"] == 1
