"""The join and the scatters of fastbuild/resolve.py, on hand-built bucket
files: the full-chain behavior is proved by the phase-2 replay against the
reference build; this covers the mechanics and the named edge cases fast."""

import os

import numpy as np
import pytest

from claimstones.indexer.fastbuild import resolve as R


def bucket(bdir, kind, b, recs, st):
    os.makedirs(os.path.join(bdir, "buckets"), exist_ok=True)
    with open(R.bucket_path(bdir, kind, b), "ab") as f:
        for r in recs:
            f.write(st.pack(*r))


def txid(first, fill=0x55):
    return bytes([first]) + bytes([fill] * 31)


def run_all(bdir, end, n_cre, n_spd):
    hist, over, joined = R.join(bdir, end, keep=True)
    R.scatter_resolved(bdir, n_spd, os.path.join(bdir, "resolved.ids"))
    alive = R.scatter_death(bdir, n_cre, os.path.join(bdir, "death.u32"))
    res = np.fromfile(os.path.join(bdir, "resolved.ids"), "<u8")
    dth = np.fromfile(os.path.join(bdir, "death.u32"), "<u4")
    return hist, over, joined, alive, list(res), list(dth)


def test_join_scatter_roundtrip(tmp_path, monkeypatch):
    # tiny ranges so the multi-range path is the one under test
    monkeypatch.setattr(R, "RES_PER_RANGE", 2)
    monkeypatch.setattr(R, "DEATH_PER_RANGE", 2)
    bdir = str(tmp_path)
    t1, t2 = txid(1), txid(2)
    bucket(bdir, "cre", 1, [(t1, 0, 0), (t1, 1, 1)], R.CREATION)
    bucket(bdir, "cre", 2, [(t2, 0, 2), (t2, 1, 3)], R.CREATION)
    # spend seqs cross the outputs: 0 -> t2:0, 1 -> t1:1, 2 -> t1:0
    bucket(bdir, "spd", 1, [(t1, 1, 1, 7), (t1, 0, 2, 9)], R.SPEND)
    bucket(bdir, "spd", 2, [(t2, 0, 0, 5)], R.SPEND)

    hist, over, joined, alive, res, dth = run_all(bdir, 10_000, 4, 3)
    assert (over, joined) == ([], 3)
    assert res == [2, 1, 0]                      # creation id per spend seq
    assert dth == [9, 7, 5, R.UNSPENT]           # death height per creation
    assert alive == 1                            # t2:1 never spent
    assert hist[0] == 3 and sum(hist) == 3


def test_bip30_overwrite(tmp_path):
    bdir = str(tmp_path)
    dup, dup_h = next(iter(R.BIP30_DUP.items()))
    b = dup[0]
    # the same outpoint created twice (cid 0 then 5), spent once after
    bucket(bdir, "cre", b, [(dup, 0, 0), (dup, 0, 5)], R.CREATION)
    bucket(bdir, "spd", b, [(dup, 0, 0, 100_000)], R.SPEND)

    hist, over, joined, alive, res, dth = run_all(bdir, 101_000, 6, 1)
    assert (over, joined, alive) == ([[0, dup_h]], 1, 4)
    assert res == [5]                            # the spend joins the latest
    assert dth[0] == dup_h                       # overwritten: dead, no spend
    assert dth[5] == 100_000
    assert hist[dup_h // R.HIST_BIN] == 1 and hist[100] == 1


def test_duplicate_outside_bip30_refused(tmp_path):
    bdir = str(tmp_path)
    t = txid(9)
    bucket(bdir, "cre", 9, [(t, 0, 0), (t, 0, 1)], R.CREATION)
    with pytest.raises(RuntimeError, match="outside the BIP30 pair"):
        R.join(bdir, 1000, keep=True)


def test_unknown_spend_refused(tmp_path):
    bdir = str(tmp_path)
    bucket(bdir, "spd", 3, [(txid(3), 0, 0, 10)], R.SPEND)
    with pytest.raises(RuntimeError, match="unknown"):
        R.join(bdir, 1000, keep=True)


def test_missing_spend_caught_by_scatter(tmp_path):
    bdir = str(tmp_path)
    t = txid(4)
    bucket(bdir, "cre", 4, [(t, 0, 0)], R.CREATION)
    bucket(bdir, "spd", 4, [(t, 0, 0, 10)], R.SPEND)
    R.join(bdir, 1000, keep=True)
    with pytest.raises(RuntimeError, match="missing or doubled"):
        R.scatter_resolved(bdir, 2, os.path.join(bdir, "resolved.ids"))
