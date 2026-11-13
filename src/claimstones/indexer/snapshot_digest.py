"""The canonical-row digest of a birth index (docs/birth-index-check.md
§Procedure, step 4).

sha256 over every unspent output as (txid, vout, script, runs) in key
order -- exactly the `utxo` sub-database's cursor order, and byte for
byte the row form the fastbuild acceptance gate hashes
(fastbuild/compare.py), so a snapshot's digest and a rebuild's digest
are comparable. It is a digest of rows, never of data.mdb: a compacted
copy and its source hold the same rows in different pages.

The environment is opened read-only without a lock file, so a
write-protected archive can be digested without being written to.

    python -m claimstones.indexer.snapshot_digest D:\\claimstones\\birth-landfree-975433
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time

import lmdb

from claimstones.indexer.fastbuild.compare import _digest_row
from claimstones.indexer.store_lmdb import _unrow


def digest(path: str, map_gb: int = 200, report=print) -> tuple[str, int, dict]:
    """(sha256 hex, rows hashed, meta) of the environment at `path`."""
    env = lmdb.open(path, readonly=True, lock=False, max_dbs=4,
                    map_size=map_gb << 30)
    h = hashlib.sha256()
    rows = 0
    t0 = time.time()
    with env.begin() as txn:
        d_meta = env.open_db(b"meta", txn=txn)
        meta = {k.decode(): v.decode() for k, v in txn.cursor(db=d_meta)}
        d_utxo = env.open_db(b"utxo", txn=txn)
        for key, value in txn.cursor(db=d_utxo):
            script, runs = _unrow(value)
            _digest_row(h, (key[:32], int.from_bytes(key[32:], "big"),
                            script, runs))
            rows += 1
            if rows % 50_000_000 == 0:
                report(f"  {rows:,} rows, {time.time() - t0:.0f}s")
    env.close()
    return h.hexdigest(), rows, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="the LMDB environment directory")
    ap.add_argument("--map-gb", type=int, default=200,
                    help="reader map size; must not be below the file")
    args = ap.parse_args()
    hexdigest, rows, meta = digest(args.path, args.map_gb)
    print(f"height {meta.get('height')}  schema {meta.get('schema')}"
          f"  chain {meta.get('chain', 'main')}")
    print(f"rows {rows:,}  meta.utxos {int(meta.get('utxos', 0)):,}"
          f"  {'MATCH' if rows == int(meta.get('utxos', 0)) else 'MISMATCH'}")
    print(f"sha256 {hexdigest}")
    return 0 if rows == int(meta.get("utxos", 0)) else 1


if __name__ == "__main__":
    sys.exit(main())
