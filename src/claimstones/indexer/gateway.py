"""claimstones gateway: the public face of the indexer RPC.

    python -I -m claimstones.indexer.gateway --rpc-cookie PATH [options]

The indexer RPC (rpc.py, docs/spec/indexer-rpc.md) is never exposed to
the internet: it is a JSON-RPC surface with cookie auth, and its answers
are unbounded (an exchange address can hold a hundred thousand UTXOs).
This gateway is the piece that *is* exposed — through an edge proxy that
owns TLS, rate limiting and caching — and it is shaped for that job:

- GET only, two paths, JSON out. Nothing here mutates anything.
- It accepts whatever a visitor pastes: bech32/bech32m, legacy base58
  (1.../3..., m.../n.../2... on the test networks), or a raw
  scriptPubKey in hex. An address of another network than the index's
  is refused, never answered "empty". The indexer RPC itself stays
  bech32/hex only; being friendly is this layer's job.
- Responses are bounded: totals are always exact, the UTXO list is
  capped at --max-utxos rows and says so with "truncated".
- Every answer names the height and time it was computed at ("height",
  "as_of"), so the edge can serve a cached copy during an outage marked
  as of block X instead of erroring.
- The backend client rides the same cookie self-heal as the daemon
  (Rpc(cookie_path=...)): the indexer RPC restarting under the gateway
  degrades answers to 502 for a moment, it never kills the process.

Paths:

    /v1/info
        {"height", "utxos", "runs", "activation_height", "as_of"}

    /v1/address/<address-or-script-hex>
        {"target", "script", "height", "chainwork", "seeds",
         "settled_seeds", "sats", "latent_sats", "utxo_count",
         "utxos": [...], "truncated", "as_of"}

        "seeds" is the latent valuation seeds = floor(A_0*N/(W*L)) of
        spec getlatent, the orchids still to be claimed; "settled_seeds"
        the orchids already on the script (getorchidbalance), per row
        too, null on an index without a ledger half. Presentation ("est.
        orchids to be landed", never "value") is the frontend's
        contract, wording in wallet-policy.md.

Errors are JSON too: {"error": msg} with 400 (bad input), 404 (no such
path), 405 (not GET), or 502 (backend away). The edge treats 502 as
"serve the cache".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
from claimstones.bitcoin import base58
from claimstones.bitcoin.bech32 import HRP, Bech32Error, address_to_script
from claimstones.lineage.walker import Rpc, RpcError

DEFAULT_PORT = 7333


class BadTarget(ValueError):
    pass


_HRP_NETWORK = {"bc": "mainnet", "tb": "testnet or signet", "bcrt": "regtest"}
_HEX = set("0123456789abcdefABCDEF")


def parse_target(target: str, chain: str = "main") -> bytes:
    """What a visitor pasted -> scriptPubKey bytes, for the index's chain.

    An address of another network is an error, never a lookup: its
    script would be looked up here and answered "empty". Hex goes before
    base58 because a script can start with any digit; a base58 address
    made of hex characters only, at even length, is astronomically
    unlikely."""
    if not target or len(target) > 20_000:
        raise BadTarget("expected an address or script hex")
    low = target.lower()
    hrp = low.rsplit("1", 1)[0] if "1" in low else ""
    if hrp in _HRP_NETWORK:
        expected = HRP.get(chain, "tb")
        if hrp != expected:
            raise BadTarget(f"wrong network: {hrp}1... is an address for "
                            f"{_HRP_NETWORK[hrp]}, this index is {chain}")
        try:
            return address_to_script(hrp, target)
        except Bech32Error as e:
            raise BadTarget(f"bad address: {e}")
    if len(target) % 2 == 0 and set(target) <= _HEX:
        return bytes.fromhex(target)
    if target[0] in "13mn2":
        try:
            return base58.address_to_script(target, chain)
        except base58.Base58Error as e:
            msg = str(e)
            raise BadTarget(msg if msg.startswith("wrong network")
                            else f"bad address: {msg}")
    raise BadTarget("expected a bech32/bech32m or legacy address, "
                    "or script hex")


def make_handler(rpc: Rpc, max_utxos: int):
    # The index's chain, asked once: it never changes under a running
    # index (the daemon refuses a node on another chain).
    chain: list[str] = []

    def index_chain() -> str:
        if not chain:
            chain.append(rpc("getindexinfo").get("chain") or "main")
        return chain[0]

    def settled(script_hex: str) -> dict | None:
        """The ledger's balances by outpoint, None on a birth-only index
        (-5 there: no ledger half, which is not a zero)."""
        try:
            out = rpc("getorchidbalance", script_hex)
        except RpcError as e:
            if e.code == -5:
                return None
            raise
        return {(u["txid"], u["vout"]): u["seeds"] for u in out["utxos"]}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "claimstones-gateway"
        sys_version = ""

        def _send(self, code: int, obj: dict) -> None:
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):          # noqa: N802  (BaseHTTPRequestHandler API)
            try:
                if self.path == "/v1/info":
                    return self._info()
                if self.path.startswith("/v1/address/"):
                    return self._address(self.path[len("/v1/address/"):])
                self._send(404, {"error": "no such path"})
            except BadTarget as e:
                self._send(400, {"error": str(e)})
            except RpcError as e:
                # the indexer RPC refused the call; its message is safe
                # and useful (e.g. a script it will not index)
                self._send(400, {"error": str(e)})
            except Exception as e:
                # backend away (connection refused, cookie waiting, ...).
                # The details (internal URL, OS error) go to our log only.
                print(f"502 {self.path}: {e!r}", flush=True)
                self._send(502, {"error": "index backend unavailable"})

        def _info(self):
            info = rpc("getindexinfo")
            info["as_of"] = int(time.time())
            self._send(200, info)

        def _address(self, target: str):
            script = parse_target(target, index_chain())
            out = rpc("getlatent", script.hex())
            utxos = out.get("utxos", [])
            balances = settled(script.hex())
            if balances is not None:
                for u in utxos:
                    u["settled_seeds"] = balances.get((u["txid"], u["vout"]), 0)
            self._send(200, {
                "target": target,
                "script": script.hex(),
                "height": out.get("valued_at_height"),
                "chainwork": out.get("chainwork"),
                "seeds": out.get("seeds"),
                "settled_seeds": (None if balances is None
                                  else sum(balances.values())),
                "sats": sum(u["sats"] for u in utxos),
                "latent_sats": sum(u["latent_sats"] for u in utxos),
                "utxo_count": len(utxos),
                "utxos": utxos[:max_utxos],
                "truncated": len(utxos) > max_utxos,
                "as_of": int(time.time()),
            })

        def do_POST(self):         # noqa: N802
            self._send(405, {"error": "GET only"})

        def log_message(self, fmt, *a):
            print("%s %s" % (self.address_string(), fmt % a), flush=True)

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(
        description="claimstones gateway: bounded read-only HTTP in front "
                    "of the indexer RPC (never expose rpc.py directly)")
    ap.add_argument("--rpc-url", default="http://127.0.0.1:7332",
                    help="the indexer RPC to front (default %(default)s)")
    ap.add_argument("--rpc-cookie", required=True,
                    help="the indexer RPC's cookie file")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to serve on (default %(default)s; the "
                         "edge proxy's network on deployment)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--max-utxos", type=int, default=100,
                    help="UTXO rows per answer; totals stay exact "
                         "(default %(default)s)")
    args = ap.parse_args()

    # Start even if the cookie is not there yet (backend may be booting
    # beside us); the first 401 re-reads it.
    auth = None
    if os.path.exists(args.rpc_cookie):
        with open(args.rpc_cookie, encoding="utf-8") as f:
            from claimstones.lineage.walker import basic_auth_header
            auth = basic_auth_header(f.read().strip())
    rpc = Rpc(args.rpc_url, auth, cookie_path=args.rpc_cookie)

    srv = ThreadingHTTPServer((args.bind, args.port),
                              make_handler(rpc, args.max_utxos))
    print(f"gateway: serving {args.rpc_url} on "
          f"http://{args.bind}:{args.port} (max {args.max_utxos} "
          f"utxo rows per answer)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
