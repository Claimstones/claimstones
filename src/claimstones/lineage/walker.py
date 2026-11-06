"""Lineage helpers for scan/discover.py (docs/archive/discovery-prototype-spec.md).

Split out per spec §2 (file grew past ~800 lines). Contains everything
below the command-line surface of the claimstone discovery preview:

  - consensus helpers: subsidy schedule, nBits -> expected work,
    exact BTC-Decimal -> sats conversion;
  - the thin JSON-RPC client (Bitcoin Core or Pynode, spec §3);
  - the SQLite disk cache (spec §6.5);
  - the work table, one nBits per retarget period: w(h) plus the
    running max of w(h)/s(h) that drives the walk cutoff
    (docs/spec/land.md), and the claim-time valuation helpers
    (docs/spec/constants.md);
  - the FIFO lineage walker (spec §6): backward walk from a UTXO
    (unspent transaction output) to coinbase birth blocks under
    ordinals first-in-first-out semantics, with a per-UTXO fetch
    budget, a dust early-cutoff, and coinbase fee-tail resolution.

Read-only against the node; exact integer arithmetic everywhere that
produces a published number (project rule). Stdlib only.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

COIN = 100_000_000                  # sats per BTC
HALVING_INTERVAL = 210_000          # blocks per subsidy halving
RETARGET_PERIOD = 2016              # blocks per difficulty period
A_0 = 30_000_000_000_000            # seeds (3e13); docs/spec/constants.md,
                                    # decided 2026-10-09 (was 5.6e13 of
                                    # 2026-09-16). Seeds are the ledger unit.
SEEDS_PER_ORCHID = 100              # display convention only
DUST_SEEDS = 1                      # default walk cutoff; NOT consensus
                                    # since 2026-10-25. The protocol values
                                    # every run (docs/spec/land.md
                                    # §Valuation sequence), which is dust=0;
                                    # a positive value under-mints to save
                                    # walk time and is the verifier's choice
SUBSIDY_SCALE = 883_562_550_452_072_936_642_907_668_473_515_000_000_000
                                    # L: the lcm of all 33 nonzero
                                    # subsidies, so L // s(h) is exact
                                    # for every one of them. Bitcoin
                                    # halves by a truncating shift, so
                                    # from halving 10 a subsidy is not
                                    # 5^10 times a power of two and no
                                    # scaling of 5e9 divides it: the
                                    # old 5e9 << 33 was exact for 13 of
                                    # 33 (adopted 2026-10-25).
                                    # docs/spec/constants.md; rederived
                                    # by tests/test_valuation_scale.py
CACHE_SCHEMA = "2"                  # bumped when a cache table changes shape
PREFETCH_WINDOW = 8                 # sibling getrawtransaction calls in flight
PREFETCH_WORKERS = 8

VERBOSE = False


def set_verbose(on: bool) -> None:
    global VERBOSE
    VERBOSE = on


def log(msg: str) -> None:
    """Progress logging to stderr (-v only)."""
    if VERBOSE:
        print(msg, file=sys.stderr)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Consensus helpers: subsidy schedule and nBits -> expected work
# ---------------------------------------------------------------------------

def subsidy_sats(height: int) -> int:
    """Block subsidy in sats: 50 BTC halved every 210,000 blocks."""
    halvings = height // HALVING_INTERVAL
    if halvings >= 33:
        return 0            # 5e9 >> 33 == 0: nothing is minted from
                            # height 6 930 000 on, so nothing born
                            # there needs valuing
    return 5_000_000_000 >> halvings


def nbits_to_target(nbits: int) -> int:
    """Decode compact nBits (mantissa/exponent form) to the target int.

    Direct port of bitcoin's arith_uint256::SetCompact; does not parse
    the float `difficulty` field (spec §5).
    """
    exponent = nbits >> 24
    mantissa = nbits & 0x7FFFFF
    if nbits & 0x800000:
        raise ValueError(f"negative nBits target: {nbits:#x}")
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def work_from_nbits(nbits: int) -> int:
    """Expected hashes of a block, exactly as bitcoin computes chainwork:
    w = 2^256 div (target + 1)."""
    return (1 << 256) // (nbits_to_target(nbits) + 1)


def dec_sats(amount: Decimal) -> int:
    """Exact BTC(Decimal from JSON) -> sats. Raises if not a sat multiple."""
    scaled = amount.scaleb(8)
    result = int(scaled)
    if result != scaled:
        raise ValueError(f"non-integer sat amount: {amount}")
    return result


# ---------------------------------------------------------------------------
# RPC layer (spec §3)
# ---------------------------------------------------------------------------

class RpcError(RuntimeError):
    def __init__(self, method: str, code, message: str):
        super().__init__(f"RPC {method}: {message} (code {code})")
        self.method = method
        self.code = code


class RpcMethodMissing(RpcError):
    """The backend does not implement this RPC method at all."""


class Rpc:
    """Thin JSON-RPC client for Bitcoin Core or Pynode.

    Floats in responses are parsed as Decimal so BTC amounts convert to
    sats exactly. Wallet calls (listunspent) go to /wallet/NAME when a
    wallet name is configured.
    """

    def __init__(self, url: str, auth_header: str | None,
                 wallet: str | None = None, cookie_path: str | None = None):
        self.url = url.rstrip("/")
        self.wallet_url = (self.url + "/wallet/" + wallet) if wallet else None
        self.headers = {"Content-Type": "application/json"}
        if auth_header:
            self.headers["Authorization"] = auth_header
        # Where the auth header came from, when it came from a cookie file.
        # Core rotates .cookie on every start, so a long-lived client's header
        # goes stale across a node restart; a 401 is then a cue to re-read the
        # file, not a misconfiguration. Daemons pass this; one-shot tools
        # need not bother.
        self.cookie_path = cookie_path
        self.calls = 0
        self._calls_lock = threading.Lock()  # calls may be shared across
                                             # the walker's prefetch pool

    def _refresh_cookie(self) -> bool:
        """Re-read the cookie file after a 401. True means the credentials
        changed (node restarted; retry the call), False means they did not
        or there is no cookie file to consult (a real refusal)."""
        if not self.cookie_path:
            return False
        try:
            with open(self.cookie_path, encoding="utf-8") as f:
                header = basic_auth_header(f.read().strip())
        except OSError:
            return False
        if self.headers.get("Authorization") == header:
            return False
        self.headers["Authorization"] = header
        return True

    def __call__(self, method: str, *params, timeout: int = 120,
                 wallet: bool = False):
        url = self.wallet_url if (wallet and self.wallet_url) else self.url
        body = json.dumps({"jsonrpc": "2.0", "id": 0,
                           "method": method, "params": list(params)})
        for attempt in range(3):
            req = urllib.request.Request(url, body.encode(), self.headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    if attempt < 2 and self._refresh_cookie():
                        continue
                    raise RuntimeError(
                        "RPC authentication failed (HTTP 401) — supply "
                        "--rpc-user/--rpc-pass or --rpc-cookie PATH") \
                        from None
                raw = e.read()  # Core returns RPC errors as HTTP 500 + JSON
                if e.code == 403:
                    raise RuntimeError(
                        "RPC refused (HTTP 403): this machine's address is "
                        "not in the node's rpcallowip list") from None
                if not raw.strip():
                    raise RuntimeError(
                        f"RPC {method}: HTTP {e.code} with an empty body "
                        f"(node starting up, or rpcallowip/rpcbind mismatch)") from None
                break
            except (OSError, http.client.HTTPException) as e:
                # URLError, resets, timeouts, and a reply cut off mid-body
                # (IncompleteRead on a large getblock over a flaky link)
                # Every call this tool makes is read-only, so a retry is
                # always safe; Core can drop connections under load.
                if attempt == 2:
                    raise RuntimeError(
                        f"cannot reach RPC at {url}: {e}") from None
                warn(f"RPC {method}: {e} — retrying")
                time.sleep(1 + 2 * attempt)
        try:
            out = json.loads(raw, parse_float=Decimal)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"RPC {method}: backend returned non-JSON response: "
                f"{raw[:120]!r}") from None
        err = out.get("error")
        if err:
            if err.get("code") == -32601:
                raise RpcMethodMissing(method, err.get("code"),
                                       err.get("message", "method not found"))
            raise RpcError(method, err.get("code"), err.get("message", "?"))
        with self._calls_lock:
            self.calls += 1
        return out["result"]

    def batch(self, calls: list[tuple], timeout: int = 600) -> list:
        """Send several calls in one HTTP request (a JSON-RPC batch);
        returns the results in order. calls: [(method, params...), ...].
        Raises on the first failed call."""
        if not calls:
            return []
        body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": c[0],
                            "params": list(c[1:])} for i, c in enumerate(calls)])
        for attempt in range(3):
            req = urllib.request.Request(self.url, body.encode(), self.headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    if attempt < 2 and self._refresh_cookie():
                        continue
                    raise RuntimeError("RPC authentication failed (HTTP 401)") from None
                raw = e.read()
                break
            except (OSError, http.client.HTTPException) as e:
                if attempt == 2:
                    raise RuntimeError(f"cannot reach RPC at {self.url}: {e}") from None
                warn(f"RPC batch: {e} — retrying")
                time.sleep(1 + 2 * attempt)
        try:
            out = json.loads(raw, parse_float=Decimal)
        except json.JSONDecodeError:
            raise RuntimeError("RPC batch: backend returned non-JSON response") from None
        if isinstance(out, dict):          # a single error object, not a list
            err = out.get("error") or {}
            raise RpcError("batch", err.get("code"), err.get("message", "?"))
        results = [None] * len(calls)
        for item in out:
            err = item.get("error")
            if err:
                method = calls[item["id"]][0]
                raise RpcError(method, err.get("code"), err.get("message", "?"))
            results[item["id"]] = item["result"]
        with self._calls_lock:
            self.calls += len(calls)
        return results


def basic_auth_header(credentials: str) -> str:
    return "Basic " + base64.b64encode(credentials.encode()).decode()


def default_cookie_path() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "Bitcoin",
                            ".cookie")
    return os.path.expanduser("~/.bitcoin/.cookie")


def resolve_rpc_auth(no_auth: bool, rpc_user: str | None,
                     rpc_pass: str | None, rpc_cookie: str | None
                     ) -> str | None:
    """Auth precedence: --no-auth > --rpc-user/--rpc-pass > cookie file.
    Shared by every scan/*.py entry point that talks to Core or Pynode."""
    if no_auth:
        return None
    if rpc_user:
        return basic_auth_header(f"{rpc_user}:{rpc_pass or ''}")
    cookie = rpc_cookie or default_cookie_path()
    if os.path.exists(cookie):
        with open(cookie, encoding="utf-8") as f:
            return basic_auth_header(f.read().strip())
    if rpc_cookie:
        sys.exit(f"error: cookie file not found: {rpc_cookie}")
    # No credentials found: try unauthenticated (Pynode case); a Core
    # backend will answer 401 with a clear message.
    return None


def resolve_rpc_cookie_path(no_auth: bool, rpc_user: str | None,
                            rpc_cookie: str | None) -> str | None:
    """The cookie file resolve_rpc_auth() read, when it read one — the path a
    long-lived client gives Rpc(cookie_path=...) so a 401 after a node restart
    re-reads the rotated cookie instead of ending the run. None when auth does
    not come from a cookie file."""
    if no_auth or rpc_user:
        return None
    cookie = rpc_cookie or default_cookie_path()
    return cookie if os.path.exists(cookie) else None


# Coinbase transaction of block 100,000 — the txindex availability probe.
# (The spec originally listed ...b1ff16be3e35abee2d0e, which is not a real
# txid; verified against the chain 2026-08-18.)
TXINDEX_PROBE = "8c14f0db3df150123e6f3dbbf30f8b955a8249b62ac1d1ff16284aefa3d06d87"


def require_txindex(rpc: Rpc) -> None:
    """Exit with a clear message if the backend can't serve arbitrary
    historical transactions — every lineage walk depends on it.

    Bitcoin Core answers getindexinfo (any chain, so regtest works);
    a backend without it is probed with a known mainnet transaction."""
    try:
        indexes = rpc("getindexinfo")
    except RpcMethodMissing:
        indexes = None
    if indexes is not None:
        tx = indexes.get("txindex")
        if tx is None:
            sys.exit("error: this tool needs a transaction index (txindex=1 on Bitcoin Core)")
        if not tx.get("synced"):
            sys.exit(f"error: the node's txindex is still syncing (at block {tx.get('best_block_height')})")
        return
    try:
        rpc("getrawtransaction", TXINDEX_PROBE, True)
    except RpcMethodMissing:
        sys.exit("error: backend does not implement getrawtransaction — "
                 "the lineage walk is impossible on this backend")
    except RpcError as e:
        sys.exit("error: this tool needs a transaction index "
                 f"(txindex=1 on Bitcoin Core) — probe said: {e}")


# ---------------------------------------------------------------------------
# Disk cache (spec §6.5): single SQLite file
# ---------------------------------------------------------------------------

class Cache:
    """meta / nbits_period / txheight / lineage / dust_cutoff tables.

    lineage memoizes only *fully resolved* exact ranges (never
    budget-truncated ones); lineage, txheight and nbits_period are chain
    facts and never depend on the valuation. There is no cached-
    valuation table: work is stored in hashes and divided by W(now) at
    display time (manual §Decisions).

    dust_cutoff memoizes ranges that were abandoned purely because the
    walk cutoff (docs/spec/land.md) proved them negligible — never
    ranges that hit the --max-walk-nodes budget or any other anomaly,
    since only the cutoff decision is a deterministic function of chain
    data. A cutoff decision depends on (dust, W): a range below dust at
    chainwork W1 stays below dust at every W2 >= W1 because unclaimed
    value only shrinks as W grows, so rows are keyed by dust, carry the
    W they were decided under, and are reused at any later W. The
    stored upper bound is work (scaled hashes), independent of W.
    """

    def __init__(self, path: str, fresh: bool):
        if fresh and os.path.exists(path):
            os.remove(path)
        # the wallet server touches the cache from request threads, one
        # at a time under CheckService.lock
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS meta("
                        "key TEXT PRIMARY KEY, value TEXT)")
        if self.meta_get("schema") != CACHE_SCHEMA:
            # Schema 1 keyed dust rows by a preview launch height and
            # stored A in base units; chain-fact tables carry over.
            self.db.executescript("""
                DROP TABLE IF EXISTS dust_cutoff;
                DELETE FROM meta WHERE key IN
                    ('launch_height', 'w0_hex', 'a_base');
            """)
            self.meta_set("schema", CACHE_SCHEMA)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS nbits_period(
                period INTEGER PRIMARY KEY, nbits INTEGER);
            CREATE TABLE IF NOT EXISTS txheight(
                txid TEXT PRIMARY KEY, height INTEGER);
            CREATE TABLE IF NOT EXISTS lineage(
                txid TEXT, vout INTEGER, lo INTEGER, hi INTEGER,
                result TEXT, PRIMARY KEY(txid, vout, lo, hi));
            CREATE TABLE IF NOT EXISTS dust_cutoff(
                txid TEXT, vout INTEGER, lo INTEGER, hi INTEGER,
                dust INTEGER, chainwork TEXT,
                result TEXT, unresolved INTEGER, unresolved_ub TEXT,
                PRIMARY KEY(txid, vout, lo, hi, dust));
        """)
        self.db.commit()

    def meta_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?",
                              (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
                        (key, value))

    def load_nbits(self) -> dict[int, int]:
        return dict(self.db.execute(
            "SELECT period, nbits FROM nbits_period"))

    def put_nbits(self, period: int, nbits: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO nbits_period VALUES(?,?)",
                        (period, nbits))

    def txheight_get(self, txid: str) -> int | None:
        row = self.db.execute("SELECT height FROM txheight WHERE txid=?",
                              (txid,)).fetchone()
        return row[0] if row else None

    def txheight_put(self, txid: str, height: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO txheight VALUES(?,?)",
                        (txid, height))

    def lineage_get(self, txid: str, vout: int, lo: int,
                    hi: int) -> list[tuple[int, int]] | None:
        row = self.db.execute(
            "SELECT result FROM lineage WHERE txid=? AND vout=? AND lo=? "
            "AND hi=?", (txid, vout, lo, hi)).fetchone()
        if row is None:
            return None
        return [(int(h), int(n)) for h, n in json.loads(row[0])]

    def lineage_put(self, txid: str, vout: int, lo: int, hi: int,
                    result: Counter) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO lineage VALUES(?,?,?,?,?)",
            (txid, vout, lo, hi,
             json.dumps(sorted(result.items()), separators=(",", ":"))))

    def dust_get(self, txid: str, vout: int, lo: int, hi: int,
                 dust: int, chainwork: int
                 ) -> tuple[list[tuple[int, int]], int, int] | None:
        """A row decided under a chainwork <= the current one is valid
        (see class docstring); a row from a larger W is left unused."""
        row = self.db.execute(
            "SELECT chainwork, result, unresolved, unresolved_ub "
            "FROM dust_cutoff WHERE txid=? AND vout=? AND lo=? AND hi=? "
            "AND dust=?", (txid, vout, lo, hi, dust)).fetchone()
        if row is None or int(row[0], 16) > chainwork:
            return None
        _, result_json, unresolved, unresolved_ub = row
        birth = [(int(h), int(n)) for h, n in json.loads(result_json)]
        return birth, unresolved, int(unresolved_ub)

    def dust_put(self, txid: str, vout: int, lo: int, hi: int,
                 dust: int, chainwork: int, result: Counter,
                 unresolved: int, unresolved_ub: int) -> None:
        # unresolved_ub is scaled work and exceeds SQLite's 64-bit
        # INTEGER, so it travels as a decimal string.
        self.db.execute(
            "INSERT OR REPLACE INTO dust_cutoff VALUES(?,?,?,?,?,?,?,?,?)",
            (txid, vout, lo, hi, dust, f"{chainwork:064x}",
             json.dumps(sorted(result.items()), separators=(",", ":")),
             unresolved, str(unresolved_ub)))

    def commit(self) -> None:
        self.db.commit()


# ---------------------------------------------------------------------------
# Work table: one nBits per 2016-block retarget period (docs/spec/land.md)
# ---------------------------------------------------------------------------

class WorkTable:
    """Per-period expected work w(h) plus maxrate(H) = max over h <= H
    of w(h)/s(h), the per-sat work bound behind the walk cutoff and the
    unresolved upper bound.

    Work is kept "scaled": multiplied by SUBSIDY_SCALE (L) so that
    w(h) * (L // s(h)) carries the fraction w(h)/s(h), which is below
    one for early blocks, as an integer. L is the lcm of the subsidy
    schedule, so L // s(h) is exact and the scaling loses nothing. The
    valuation itself (seeds_from_work) divides by W(c) and floors once
    per land, so this table never floors.
    """

    def __init__(self, rpc: Rpc, cache: Cache, tip: int):
        self.tip = tip
        periods = tip // RETARGET_PERIOD + 1
        known = cache.load_nbits()
        missing = [p for p in range(periods) if p not in known]
        if missing:
            # One header per retarget period, fetched concurrently: a
            # remote node at ~1 s per call would otherwise take ~10 min
            # for mainnet. Done once; the cache keeps every period.
            print(f"work table: fetching {len(missing)} block headers "
                  f"(once; cached afterwards) ...", file=sys.stderr, flush=True)

            def fetch(p):
                block_hash = rpc("getblockhash", p * RETARGET_PERIOD)
                return p, int(rpc("getblockheader", block_hash)["bits"], 16)

            done = 0
            with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as pool:
                for p, nbits in pool.map(fetch, missing):
                    cache.put_nbits(p, nbits)
                    done += 1
                    if done % 100 == 0:
                        print(f"work table: {done}/{len(missing)}",
                              file=sys.stderr, flush=True)
            cache.commit()
            print(f"work table: {len(missing)} periods fetched, "
                  f"{periods - len(missing)} cached", file=sys.stderr, flush=True)
        known = cache.load_nbits()
        self.work_per_period = [work_from_nbits(known[p])
                                for p in range(periods)]
        # Running max of scaled w(h)/s(h) over all periods <= p. Subsidy
        # can halve once inside a period (210,000 is not a multiple of
        # 2016), so use the smallest nonzero subsidy in the period — an
        # upper bound is all the cutoff and the unresolved report need.
        self.maxrate_per_period: list[int] = []
        best = 0
        for p in range(periods):
            first_h = p * RETARGET_PERIOD
            last_h = min(tip, first_h + RETARGET_PERIOD - 1)
            s_values = [s for s in (subsidy_sats(first_h),
                                    subsidy_sats(last_h)) if s > 0]
            if s_values:
                best = max(best, self.work_per_period[p]
                           * (SUBSIDY_SCALE // min(s_values)))
            self.maxrate_per_period.append(best)

    def work(self, height: int) -> int:
        return self.work_per_period[height // RETARGET_PERIOD]

    def maxrate(self, height: int) -> int:
        """Scaled hashes per sat: max of w(h)/s(h) over h <= height."""
        p = min(height // RETARGET_PERIOD, len(self.maxrate_per_period) - 1)
        return self.maxrate_per_period[p]

    def below_dust(self, size: int, height: int, dust: int,
                   chainwork: int) -> bool:
        """Walk cutoff (docs/spec/land.md): `size` sats born at or below
        `height` cannot carry `dust` seeds at chainwork W even at the
        maximum rate:  A_0 * n * maxrate(H) < dust * W."""
        return (A_0 * size * self.maxrate(height)
                < dust * chainwork * SUBSIDY_SCALE)

    def ub_work(self, size: int, height: int) -> int:
        """Exact upper bound, in scaled work, on what `size` sats born
        <= height can carry."""
        return size * self.maxrate(height)


def work_scaled(birth: Counter, table: WorkTable) -> int:
    """N of docs/spec/constants.md: sum over birth blocks of
    n_i * w(h_i) * (L // s(h_i)) — the sats' work in hashes times
    SUBSIDY_SCALE. Every term is exact, the sum is an integer sum, and
    no range is rounded on its own; the one floor is in seeds_from_work.
    Blocks after the last halving contribute 0."""
    total = 0
    for height, count in birth.items():
        s = subsidy_sats(height)
        if s == 0:
            # A block from halving 33 on (height 6 930 000, about 2140)
            # mints no sats, so its work has no carrier and no land can
            # claim it. Deliberate, not an oversight: constants.md
            # §After the last halving, decided 2026-10-25. Unreachable
            # for another century; kept so the domain of N is total.
            continue
        total += count * table.work(height) * (SUBSIDY_SCALE // s)
    return total


def hashes_from_work(scaled: int) -> int:
    """Scaled work -> whole hashes (floor)."""
    return scaled // SUBSIDY_SCALE


def seeds_from_work(scaled: int, chainwork: int, ceil: bool = False) -> int:
    """Claim-time valuation: seeds = floor(A_0 * N / (W(c) * L)). One
    floor per land, and the only one. ceil=True gives the matching upper bound, used for
    unresolved ranges."""
    num = A_0 * scaled
    den = chainwork * SUBSIDY_SCALE
    return -(-num // den) if ceil else num // den


# ---------------------------------------------------------------------------
# The lineage walker (spec §6)
# ---------------------------------------------------------------------------

class Frame:
    """One open range on the walk stack.

    kind "out":     sats [lo,hi) of output (txid, vout).
    kind "feetail": sats [lo,hi) of the fee tail of transaction txjson
                    (its input stream past the sum of its outputs).
    bound is an upper bound on the birth height of every sat in the
    range: the containing-block height of any tx that carried them
    (monotone non-increasing down the walk).
    """
    __slots__ = ("kind", "txid", "vout", "lo", "hi", "bound", "txjson",
                 "children", "next_child", "result", "complete",
                 "from_memo", "dust_pure", "local_unresolved",
                 "local_unresolved_ub")

    def __init__(self, kind, txid, vout, lo, hi, bound, txjson=None):
        self.kind = kind
        self.txid = txid
        self.vout = vout
        self.lo = lo
        self.hi = hi
        self.bound = bound
        self.txjson = txjson
        self.children = None
        self.next_child = 0
        self.result = Counter()     # birth_height -> sat_count
        self.complete = True
        self.from_memo = False
        # True iff every bit of incompleteness under this frame comes
        # from a deterministic dust-cutoff decision, never from budget
        # exhaustion or an anomaly — see LineageWalker._abandon.
        self.dust_pure = True
        self.local_unresolved = 0
        self.local_unresolved_ub = 0


class WalkResult:
    __slots__ = ("birth", "resolved", "unresolved", "unresolved_ub",
                 "visited", "cached", "fetched", "deterministic")

    def __init__(self, birth, resolved, unresolved, unresolved_ub,
                 visited, cached, fetched, deterministic=True):
        self.birth = birth                  # Counter: height -> sats
        self.resolved = resolved
        self.unresolved = unresolved
        self.unresolved_ub = unresolved_ub  # scaled work, upper bound
        self.visited = visited
        self.cached = cached
        self.fetched = fetched
        # True iff every unresolved sat was dropped by the consensus
        # walk cutoff, never by the node budget or an anomaly. Only a
        # deterministic result may enter indexer state.
        self.deterministic = deterministic


class LineageWalker:
    """Iterative (explicit-stack) FIFO backward walk — mainnet lineages
    exceed Python's recursion limit."""

    def __init__(self, rpc: Rpc, cache: Cache, table: WorkTable,
                 max_nodes: int, dust: int, tip: int, chainwork: int,
                 landed: Callable[[str], bool] | None = None):
        """dust is in seeds; chainwork is W(c), the claim-time divisor
        the cutoff is decided against (W(tip) for a preview).

        landed(txid_hex) tells the walk that a transaction is an earlier
        land: every output sat of it is plain and the range resolves to
        nothing (docs/spec/land.md, the double-claim check). The indexer
        supplies it; a discovery preview without one over-estimates
        already-landed sats."""
        self.rpc = rpc
        self.cache = cache
        self.table = table
        self.max_nodes = max_nodes
        self.dust = dust
        self.tip = tip
        self.chainwork = chainwork
        self.landed = landed
        self.dust_memomem: dict[tuple, tuple] = {}  # in-session dust memo
        self.txmem: dict[str, dict] = {}        # txid -> verbose tx json
        self.blockmem: dict[str, dict] = {}     # blockhash -> verbose-2 block
        self.heightmem: dict[str, int] = {}     # blockhash -> height
        self.memomem: dict[tuple, list] = {}    # in-session lineage memo

    # -- per-UTXO entry point ------------------------------------------------

    def walk_utxo(self, txid: str, vout: int, value_sats: int) -> WalkResult:
        self.fetches = 0        # getrawtransaction fetches (the budget)
        self.visited = 0
        self.cached_hits = 0
        self.unresolved = 0
        self.unresolved_ub = 0
        self.landed_sats = 0    # sats of ranges that stop at an earlier land
        root = Frame("out", txid, vout, 0, value_sats, None)
        stack = [root]
        while stack:
            f = stack[-1]
            if f.children is None:
                self.visited += 1
                self._expand(f)
                if VERBOSE and self.visited % 1000 == 0:
                    log(f"  walk: {self.visited} nodes, "
                        f"{self.fetches} fetched")
            if f.next_child < len(f.children):
                stack.append(f.children[f.next_child])
                f.next_child += 1
                continue
            stack.pop()
            if f.kind == "out" and not f.from_memo:
                key = (f.txid, f.vout, f.lo, f.hi)
                if f.complete:
                    self.memomem[key] = sorted(f.result.items())
                    self.cache.lineage_put(f.txid, f.vout, f.lo, f.hi,
                                           f.result)
                elif f.dust_pure:
                    entry = (sorted(f.result.items()), f.local_unresolved,
                             f.local_unresolved_ub)
                    self.dust_memomem[key] = entry
                    self.cache.dust_put(f.txid, f.vout, f.lo, f.hi,
                                        self.dust, self.chainwork,
                                        f.result, f.local_unresolved,
                                        f.local_unresolved_ub)
            if stack:
                parent = stack[-1]
                parent.result.update(f.result)
                parent.complete = parent.complete and f.complete
                parent.dust_pure = parent.dust_pure and f.dust_pure
                parent.local_unresolved += f.local_unresolved
                parent.local_unresolved_ub += f.local_unresolved_ub
        # landed sats are resolved (plain) and contribute no work
        resolved = sum(root.result.values()) + self.landed_sats
        if resolved + self.unresolved != value_sats:
            warn(f"sat accounting mismatch on {txid}:{vout}: "
                 f"{resolved} resolved + {self.unresolved} unresolved "
                 f"!= {value_sats}")
        self.cache.commit()
        return WalkResult(root.result, resolved, self.unresolved,
                          self.unresolved_ub, self.visited,
                          self.cached_hits, self.fetches, root.dust_pure)

    # -- expansion -----------------------------------------------------------

    def _expand(self, f: Frame) -> None:
        f.children = []
        if f.kind == "feetail":
            tx = f.txjson
            out_total = sum(dec_sats(o["value"]) for o in tx["vout"])
            self._map_inputs(f, tx, out_total + f.lo, out_total + f.hi)
            return
        # kind == "out"
        if self.landed is not None and self.landed(f.txid):
            self.landed_sats += f.hi - f.lo
            return          # plain sats: resolved, contributing nothing
        memo = self._memo_get(f.txid, f.vout, f.lo, f.hi)
        if memo is not None:
            f.result.update(dict(memo))
            f.from_memo = True
            self.cached_hits += 1
            return
        dust_hit = self._dust_memo_get(f.txid, f.vout, f.lo, f.hi)
        if dust_hit is not None:
            birth, unresolved, unresolved_ub = dust_hit
            f.result.update(dict(birth))
            self.unresolved += unresolved
            self.unresolved_ub += unresolved_ub
            f.local_unresolved += unresolved
            f.local_unresolved_ub += unresolved_ub
            f.complete = False
            f.from_memo = True
            self.cached_hits += 1
            return
        size = f.hi - f.lo
        if f.bound is not None and self.table.below_dust(
                size, f.bound, self.dust, self.chainwork):
            self._abandon(f, size)
            return
        tx = self._get_tx(f.txid)
        if tx is None:
            self._abandon(f, size, dust=False)
            return
        height = self._height_of(f.txid, tx)
        if height is not None:
            f.bound = height
            if self.table.below_dust(size, height, self.dust,
                                     self.chainwork):
                self._abandon(f, size)
                return
        if "coinbase" in tx["vin"][0]:
            self._expand_coinbase(f, tx, height)
        else:
            shift = sum(dec_sats(o["value"]) for o in tx["vout"][:f.vout])
            self._map_inputs(f, tx, f.lo + shift, f.hi + shift)

    def _map_inputs(self, f: Frame, tx: dict, slo: int, shi: int) -> None:
        """Map input-stream range [slo, shi) onto tx's inputs (spec §6.1):
        the stream is the inputs' sat ranges concatenated in input order.

        Which prefix of inputs is actually needed depends on their
        values, which are only known after fetching them — so inputs
        are prefetched in bounded PREFETCH_WINDOW-sized chunks (not all
        at once) to overlap network latency without burning budget on
        inputs the range never reaches, beyond a small, fixed worst-case
        over-fetch of at most PREFETCH_WINDOW-1 per call.
        """
        vins = tx["vin"]
        cum = 0
        i = 0
        while i < len(vins) and cum < shi:
            chunk = vins[i:i + PREFETCH_WINDOW]
            self._prefetch([v["txid"] for v in chunk])
            for vin in chunk:
                if cum >= shi:
                    break
                prev = self._get_tx(vin["txid"])
                if prev is None:                # budget exhausted
                    remaining = shi - max(slo, cum)
                    if remaining > 0:
                        self._abandon(f, remaining, dust=False)
                    return
                a = cum
                cum += dec_sats(prev["vout"][vin["vout"]]["value"])
                ol, oh = max(slo, a), min(shi, cum)
                if ol < oh:
                    f.children.append(Frame("out", vin["txid"], vin["vout"],
                                            ol - a, oh - a, f.bound))
                i += 1
        if cum < shi:
            remaining = shi - max(slo, cum)
            if remaining > 0:
                warn(f"input stream of {tx.get('txid', '?')} shorter than "
                     f"requested range — treating {remaining} sats as "
                     f"unresolved")
                self._abandon(f, remaining, dust=False)

    def _expand_coinbase(self, f: Frame, tx: dict, height: int | None):
        """Spec §6.4: a coinbase's sat stream at height h is the s(h)
        subsidy sats born at h, then the block's fee sats in tx order."""
        if height is None:
            self._abandon(f, f.hi - f.lo, dust=False)  # can't place coinbase
            return
        s = subsidy_sats(height)
        # §6.1 slicing applied to the coinbase's own outputs first.
        shift = sum(dec_sats(o["value"]) for o in tx["vout"][:f.vout])
        slo, shi = f.lo + shift, f.hi + shift
        if slo < s:
            f.result[height] += min(shi, s) - slo
        if shi <= s:
            return                          # entirely subsidy: resolved
        flo, fhi = max(slo, s) - s, shi - s     # range within the fee tail
        block = self._get_block(tx["blockhash"])
        if block is None:
            self._abandon(f, fhi - flo, dust=False)
            return
        cum = 0
        for t in block["tx"][1:]:           # fee tails, in block tx order
            if cum >= fhi:
                break
            fee = self._fee_of(t)
            if fee is None:                 # budget exhausted mid-block
                remaining = fhi - max(flo, cum)
                if remaining > 0:
                    self._abandon(f, remaining, dust=False)
                return
            a = cum
            cum += fee
            ol, oh = max(flo, a), min(fhi, cum)
            if ol < oh:
                f.children.append(Frame("feetail", t.get("txid"), None,
                                        ol - a, oh - a, height, txjson=t))
        if cum < fhi:
            remaining = fhi - max(flo, cum)
            if remaining > 0:
                warn(f"fee tail of block {height} shorter than requested "
                     f"range — treating {remaining} sats as unresolved")
                self._abandon(f, remaining, dust=False)

    # -- primitives ----------------------------------------------------------

    def _abandon(self, f: Frame, size: int, dust: bool = True) -> None:
        """Give up on `size` sats of f's range: count them unresolved and
        accumulate the upper bound (scaled work) on what they could
        carry (docs/spec/land.md).

        dust=True means this abandonment is a pure, deterministic dust
        decision (the sole reason to cache it — see dust_pure on Frame);
        dust=False covers budget exhaustion and anomalies, which are
        order- and run-dependent and must never be cached.
        """
        bound = f.bound if f.bound is not None else self.tip
        ub = self.table.ub_work(size, bound)
        self.unresolved += size
        self.unresolved_ub += ub
        f.local_unresolved += size
        f.local_unresolved_ub += ub
        f.complete = False
        if not dust:
            f.dust_pure = False

    def _memo_get(self, txid, vout, lo, hi):
        key = (txid, vout, lo, hi)
        hit = self.memomem.get(key)
        if hit is None:
            hit = self.cache.lineage_get(txid, vout, lo, hi)
            if hit is not None:
                self.memomem[key] = hit
        return hit

    def _dust_memo_get(self, txid, vout, lo, hi):
        key = (txid, vout, lo, hi)
        hit = self.dust_memomem.get(key)
        if hit is None:
            hit = self.cache.dust_get(txid, vout, lo, hi,
                                      self.dust, self.chainwork)
            if hit is not None:
                self.dust_memomem[key] = hit
        return hit

    def _prefetch(self, txids: list[str]) -> None:
        """Fetch several distinct, not-yet-cached txids concurrently and
        drop them into self.txmem, so the sequential caller's _get_tx
        calls become cache hits. Threads only perform the RPC call
        itself (self.rpc(...) is thread-safe); every write to shared
        state (self.txmem, self.fetches) happens back on this thread
        after collecting results, so no lock is needed for those.
        Respects the remaining --max-walk-nodes budget exactly as
        _get_tx would, one call at a time.
        """
        remaining = self.max_nodes - self.fetches
        if remaining <= 0:
            return
        seen = set()
        to_fetch = []
        for txid in txids:
            if txid in seen or txid in self.txmem:
                continue
            seen.add(txid)
            to_fetch.append(txid)
            if len(to_fetch) >= remaining:
                break
        if len(to_fetch) <= 1:
            return    # nothing to overlap; let _get_tx handle it plainly
        with ThreadPoolExecutor(max_workers=min(len(to_fetch),
                                               PREFETCH_WORKERS)) as pool:
            futures = {pool.submit(self.rpc, "getrawtransaction", txid,
                                   True): txid for txid in to_fetch}
            for future in futures:
                txid = futures[future]
                try:
                    tx = future.result()
                except RpcError as e:
                    warn(str(e))
                    continue
                self.txmem[txid] = tx
                self.fetches += 1

    def _get_tx(self, txid: str) -> dict | None:
        """Fetch a verbose transaction; None when the budget is exhausted
        (only actual RPC fetches count against --max-walk-nodes)."""
        tx = self.txmem.get(txid)
        if tx is not None:
            return tx
        if self.fetches >= self.max_nodes:
            return None
        try:
            tx = self.rpc("getrawtransaction", txid, True)
        except RpcError as e:
            warn(str(e))
            return None
        self.fetches += 1
        if len(self.txmem) > 100_000:       # crude memory cap
            self.txmem.clear()
        self.txmem[txid] = tx
        return tx

    def _get_block(self, block_hash: str) -> dict | None:
        block = self.blockmem.get(block_hash)
        if block is not None:
            return block
        try:
            block = self.rpc("getblock", block_hash, 2, timeout=600)
        except RpcError as e:
            warn(str(e))
            return None
        if len(self.blockmem) > 4:          # blocks are big; keep few
            self.blockmem.clear()
        self.blockmem[block_hash] = block
        return block

    def _height_of(self, txid: str, tx: dict) -> int | None:
        """Containing-block height of a confirmed tx, cached two ways."""
        height = self.cache.txheight_get(txid)
        if height is not None:
            return height
        block_hash = tx.get("blockhash")
        if block_hash is None:
            return None                     # unconfirmed
        height = self.heightmem.get(block_hash)
        if height is None:
            height = self.rpc("getblockheader", block_hash)["height"]
            self.heightmem[block_hash] = height
        self.cache.txheight_put(txid, height)
        return height

    def _fee_of(self, tx: dict) -> int | None:
        """Fee of a block tx in sats. getblock verbosity 2 includes a
        `fee` field on Core; otherwise compute it from prevouts (each
        prevout fetch counts against the budget)."""
        if "fee" in tx:
            return dec_sats(tx["fee"])
        in_total = 0
        for vin in tx["vin"]:
            prev = self._get_tx(vin["txid"])
            if prev is None:
                return None
            in_total += dec_sats(prev["vout"][vin["vout"]]["value"])
        out_total = sum(dec_sats(o["value"]) for o in tx["vout"])
        return in_total - out_total
