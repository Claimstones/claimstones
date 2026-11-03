"""runcore must encode to the same bytes as birth.py's tuple machinery,
for all inputs. The replay verifier proves it over the real chain; this
fuzzes the edges fast, both the scalar and the numpy path."""

import random

import pytest

from claimstones.indexer import runcore
from claimstones.indexer.birth import (cut_runs, decode_runs, encode_runs,
                                       extend_merged)


def old_concat(parts):
    stream = []
    for p in parts:
        extend_merged(stream, decode_runs(p))
    return encode_runs(stream)


def old_cut(stream, sizes):
    pieces, leftover = cut_runs(decode_runs(stream), sizes)
    return [encode_runs(p) for p in pieces], encode_runs(leftover)


def synth(rng, n_runs, merged=True):
    runs, h = [], 0
    for _ in range(n_runs):
        h += rng.randrange(1, 9) if merged else rng.randrange(0, 9)
        runs.append((h, rng.randrange(1, 10_000)))
    return encode_runs(runs)


@pytest.mark.parametrize("seed", range(20))
def test_concat_fuzz(seed):
    rng = random.Random(seed)
    for _ in range(200):
        parts = [synth(rng, rng.randrange(0, 12)) for _ in range(rng.randrange(0, 6))]
        # force boundary-height collisions often
        if len(parts) >= 2 and parts[0] and parts[1] and rng.random() < 0.5:
            h = decode_runs(parts[0])[-1][0]
            parts[1] = encode_runs([(h, 7)] + decode_runs(parts[1]))
        assert runcore.concat(parts) == old_concat(parts)


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("big", [False, True])
def test_cut_fuzz(seed, big):
    rng = random.Random(seed * 2 + big)
    for _ in range(60):
        n_runs = rng.randrange(1, 400 if big else 10)
        stream = synth(rng, n_runs)
        total = runcore.total(stream)
        sizes = []
        for _ in range(rng.randrange(1, 8)):
            sizes.append(rng.choice([0, 1, rng.randrange(0, max(total, 1) + 3)]))
        got_p, got_l = runcore.cut(stream, sizes)
        want_p, want_l = old_cut(stream, sizes)
        assert got_p == want_p, (stream.hex(), sizes)
        assert got_l == want_l, (stream.hex(), sizes)


def test_edges():
    one = runcore.make_run(500, 50)
    # empty stream, empty sizes
    assert runcore.cut(b"", [3, 0]) == ([b"", b""], b"")
    assert runcore.concat([]) == b""
    assert runcore.concat([b"", b""]) == b""
    # exact boundary: leftover empty, not a zero run
    p, l = runcore.cut(one, [50])
    assert p == [one] and l == b""
    # cut through one run three ways
    p, l = runcore.cut(one, [10, 10])
    assert p == [runcore.make_run(500, 10), runcore.make_run(500, 10)]
    assert l == runcore.make_run(500, 30)
    # stream runs short: pieces cut short, leftover empty
    p, l = runcore.cut(one, [40, 40])
    assert p == [runcore.make_run(500, 40), runcore.make_run(500, 10)] and l == b""
    # boundary merge
    assert runcore.concat([one, one]) == runcore.make_run(500, 100)
    assert runcore.total(runcore.concat([one, one])) == 100


def test_numpy_threshold_consistency():
    # the same cut through both paths, straddling NUMPY_MIN_RUNS
    rng = random.Random(7)
    stream = synth(rng, runcore.NUMPY_MIN_RUNS + 10)
    sizes = [runcore.total(stream) // 3] * 2
    want = old_cut(stream, sizes)
    got_np = runcore.cut(stream, sizes)
    got_sc = runcore._cut_scalar(stream, sizes)
    assert got_np == (list(want[0]), want[1]) == got_sc
