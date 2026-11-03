"""Phase 3 and the acceptance gate: the external sort, the row-for-row
compare against a schema-3 SQLite index, and the LMDB load."""

import os
import random
import sqlite3
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from claimstones.indexer.fastbuild import spill
from claimstones.indexer.fastbuild.compare import compare
from claimstones.indexer.fastbuild.load import iter_sorted

RUN = struct.Struct("<IQ")


def make_rows(n, seed=7):
    """n unique (txid, vout, script, runs) rows, unordered. One row gets a
    script past 64 KiB: mainnet has them since the 2025 uncapped OP_RETURN
    (a u16 script_len killed the first tip-height build at h~870k)."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        txid = rng.randbytes(32)
        for vout in range(rng.randint(1, 3)):
            script = rng.randbytes(rng.randint(0, 40))
            runs = b"".join(RUN.pack(rng.randint(0, 800_000),
                                     rng.randint(1, 10**12))
                            for _ in range(rng.randint(0, 5)))
            rows.append((txid, vout, script, runs))
    rows[len(rows) // 2] = (rng.randbytes(32), 0,
                            b"\x6a" + rng.randbytes(70_000), RUN.pack(1, 1))
    return rows


def write_stream(path, rows):
    w = spill.FinalWriter(str(path))
    for cid, (txid, vout, script, runs) in enumerate(rows):
        w.write(cid, vout, txid, script, runs)
    w.close()


def write_sqlite(path, rows):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE utxo (txid BLOB NOT NULL, vout INTEGER NOT "
               "NULL, script BLOB NOT NULL, runs BLOB NOT NULL, "
               "PRIMARY KEY (txid, vout)) WITHOUT ROWID")
    db.executemany("INSERT INTO utxo VALUES (?,?,?,?)", rows)
    db.commit()
    db.close()


def test_external_sort_orders_and_preserves(tmp_path):
    rows = make_rows(400)
    write_stream(tmp_path / "f.stream", rows)
    # tiny chunks force many files through the merge
    out = list(iter_sorted(str(tmp_path / "f.stream"),
                           str(tmp_path / "scratch"), chunk_bytes=2048,
                           report=lambda *a: None))
    assert len(out) == len(rows)
    keys = [(t, v) for t, v, _, _ in out]
    assert keys == sorted(keys)
    assert sorted(out) == sorted(rows)
    assert not os.listdir(tmp_path / "scratch")      # chunks consumed


def quiet_compare(tmp_path, rows_new, rows_old, **kw):
    write_stream(tmp_path / "f.stream", rows_new)
    write_sqlite(tmp_path / "old.sqlite", rows_old)
    lines = []
    bad = compare(str(tmp_path / "f.stream"), str(tmp_path / "old.sqlite"),
                  str(tmp_path / "scratch"), report=lines.append, **kw)
    return bad, "\n".join(lines)


def test_gate_passes_on_identical_rows(tmp_path):
    rows = make_rows(300)
    bad, text = quiet_compare(tmp_path, rows, rows)
    assert bad == 0 and "GATE PASSED" in text
    # both digests in the output and equal
    digests = [l.split("sha256 ")[1] for l in text.splitlines()
               if "sha256" in l]
    assert len(digests) == 2 and digests[0] == digests[1]


def test_gate_reports_each_kind(tmp_path):
    rows = make_rows(300)
    old = [list(r) for r in rows]
    old[40][3] = RUN.pack(1, 1)                      # runs differ
    old[140][2] = b"\x51"                            # script differs
    old = [tuple(r) for r in old]
    dropped = old[200]                               # only in the fast build
    old = old[:200] + old[201:]
    bad, text = quiet_compare(tmp_path, rows, old)
    assert bad == 3
    assert "runs differ" in text and "script differs" in text
    assert "only in the fast build" in text and "GATE FAILED" in text
    assert dropped[0][::-1].hex() in text            # key named, display order


def test_gate_counts_past_max_mismatches(tmp_path):
    rows = make_rows(300)
    old = [(t, v, s, RUN.pack(2, 2)) for t, v, s, _ in rows]
    bad, text = quiet_compare(tmp_path, rows, old, max_mismatches=5)
    assert bad == len(rows)                          # counted beyond the shown
    assert text.count("MISMATCH") == 5


def test_lmdb_load_roundtrip(tmp_path):
    lmdb = pytest.importorskip("lmdb")
    from claimstones.indexer.fastbuild.load import load
    rows = make_rows(200)
    write_stream(tmp_path / "f.stream", rows)
    utxos, runs = load(str(tmp_path / "f.stream"), str(tmp_path / "db"),
                       str(tmp_path / "scratch"), height=123,
                       map_gb=1, report=lambda *a: None)
    assert utxos == len(rows)
    env = lmdb.open(str(tmp_path / "db"), max_dbs=4, readonly=True)
    d_utxo = env.open_db(b"utxo")
    d_addr = env.open_db(b"addr", dupsort=True)
    d_meta = env.open_db(b"meta")
    with env.begin() as txn:
        assert txn.get(b"height", db=d_meta) == b"123"
        got = []
        for key, val in txn.cursor(db=d_utxo):
            txid, vout = key[:32], int.from_bytes(key[32:], "big")
            slen, = struct.unpack_from("<I", val)
            got.append((txid, vout, val[4:4 + slen], val[4 + slen:]))
        assert sorted(got) == sorted(rows)
        # every script resolves back to its outpoints through addr
        import hashlib
        txid, vout, script, _ = rows[17]
        cur = txn.cursor(db=d_addr)
        assert cur.set_key(hashlib.sha256(script).digest())
        outs = {bytes(v) for v in cur.iternext_dup()}
        assert txid + vout.to_bytes(4, "big") in outs
    env.close()
