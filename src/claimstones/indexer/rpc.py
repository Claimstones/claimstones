"""rpc — the indexer's reading face: JSON-RPC over the birth index.

The analogy is bitcoind's own RPC, deliberately: cs_indexer stands
beside bitcoind the way bitcoind stands beside the network, and a
wallet asks it questions in the same idiom — HTTP POST, one JSON-RPC
call per request, cookie authentication from a file beside the data.

This process never writes the index. It opens the LMDB environment
read-only (the daemon is the single writer; LMDB readers see each
committed block and never block the writer) and answers lookups:

    getindexinfo                     the index's height and counters
    getbirthruns <txid> <vout>       one outpoint's (height, sats) runs
    getaddressutxos <addr|script>    every unspent output on a script
    getlatent <addr|script|outpoint> runs valued in seeds at the node tip
    sendrawtransaction <hex>         relay through the node
    help

Everything here is a view. The server holds no keys, builds no
transactions, quotes nothing, and nothing a client receives requires
trust for funds: a wallet verifies what it signs locally
(docs/spec/wallet-policy.md), and any answer can be reproduced from
chain data alone — the backward walker is the independent check, and
docs/birth-index-check.md settles whether a built index is honest.

Valuation needs the host node (W(c) is the chain's cumulative work and
the work table is one header per retarget period), so --node-url and
its auth are optional: without them getlatent and sendrawtransaction
answer with an error naming the missing piece, and the pure index
lookups still serve. Run beside the daemon:

    python -m claimstones.indexer.rpc --db D:/claimstones/birth.lmdb \
        --node-url http://127.0.0.1:8332 --node-cookie D:/bitcoin/.cookie

On start the server writes `rpc.cookie` beside the database —
`__cookie__:<64 hex>`, bitcoind's convention — and requires it as HTTP
Basic credentials on every call (--no-auth drops this for localhost
experiments). Port 7332 by default: one digit off the node it serves
beside, the same mnemonic bitcoind's 8332 uses.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
from claimstones.bitcoin.bech32 import Bech32Error, address_to_script
from claimstones.indexer.birth import PLAIN, Run
from claimstones.indexer.store_lmdb import LmdbBirthStore
from claimstones.lineage.walker import (RETARGET_PERIOD, Rpc, RpcError,
                                        WorkTable, resolve_rpc_auth,
                                        seeds_from_work, work_scaled)
from claimstones.protocol.codec import ACTIVATION_HEIGHT

DEFAULT_PORT = 7332


class RpcFault(Exception):
    """An error the client caused or must act on; becomes the JSON-RPC
    error object. Codes follow JSON-RPC 2.0 where one fits, negative
    application codes elsewhere."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
NO_NODE = -10
NODE_ERROR = -11
NOT_FOUND = -5           # bitcoind's RPC_INVALID_ADDRESS_OR_KEY habit


class NbitsCache:
    """The walker Cache's three nbits methods over a JSON file beside
    the database: one header per retarget period, immutable once
    fetched, so a flat file is the whole persistence story."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self.nbits: dict[int, int] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.nbits = {int(k): v for k, v in json.load(f).items()}
            except (OSError, ValueError):
                self.nbits = {}

    def load_nbits(self) -> dict[int, int]:
        return dict(self.nbits)

    def put_nbits(self, period: int, nbits: int) -> None:
        self.nbits[period] = nbits

    def commit(self) -> None:
        if self.path:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.nbits, f)
            os.replace(tmp, self.path)


def _parse_outpoint(txid: str, vout) -> tuple[bytes, int]:
    try:
        raw = bytes.fromhex(txid)
    except ValueError:
        raise RpcFault(INVALID_PARAMS, "txid must be 64 hex characters")
    if len(raw) != 32:
        raise RpcFault(INVALID_PARAMS, "txid must be 64 hex characters")
    if not isinstance(vout, int) or vout < 0:
        raise RpcFault(INVALID_PARAMS, "vout must be a non-negative integer")
    return raw[::-1], vout          # display order -> internal digest order


def _parse_script(target: str) -> bytes:
    """An address (any bech32/bech32m HRP) or a scriptPubKey in hex."""
    if not isinstance(target, str) or not target:
        raise RpcFault(INVALID_PARAMS, "expected an address or script hex")
    if "1" in target and target[:target.rfind("1")].isalpha():
        hrp = target[:target.rfind("1")].lower()
        try:
            return address_to_script(hrp, target)
        except Bech32Error as e:
            raise RpcFault(INVALID_PARAMS, f"bad address: {e}")
    try:
        script = bytes.fromhex(target)
    except ValueError:
        raise RpcFault(INVALID_PARAMS,
                       "expected a bech32/bech32m address or script hex")
    if not script:
        raise RpcFault(INVALID_PARAMS, "empty script")
    return script


def _runs_out(runs: list[Run]) -> dict:
    return {
        "runs": [[("plain" if h == PLAIN else h), n] for h, n in runs],
        "sats": sum(n for _, n in runs),
        "latent_sats": sum(n for h, n in runs if h != PLAIN),
    }


class IndexQueries:
    """The method table. One instance serves every request thread: the
    store's readers open their own LMDB read snapshot per call, and the
    work table is replaced, never mutated, under a lock."""

    def __init__(self, store: LmdbBirthStore, node=None,
                 nbits_cache: Optional[NbitsCache] = None):
        self.store = store
        self.node = node                 # callable(method, *params) or None
        self.nbits = nbits_cache or NbitsCache(None)
        self._table: Optional[WorkTable] = None
        self._table_lock = threading.Lock()

    # --- index lookups (no node) ---

    def getindexinfo(self) -> dict:
        s = self.store.stats
        return {
            "height": self.store.last_height(),
            "utxos": s["utxos"],
            "runs": s["runs"],
            "schema": self.store.get_meta("schema"),
            "activation_height": ACTIVATION_HEIGHT,
            "db_bytes": self.store.file_size(),
        }

    def getbirthruns(self, txid: str, vout: int) -> dict:
        key_txid, key_vout = _parse_outpoint(txid, vout)
        runs = self.store.get(key_txid, key_vout)
        if runs is None:
            raise RpcFault(NOT_FOUND, f"no unspent output {txid}:{vout}")
        return {"txid": txid, "vout": vout, **_runs_out(runs)}

    def getaddressutxos(self, target: str) -> list[dict]:
        script = _parse_script(target)
        return [{"txid": txid[::-1].hex(), "vout": vout, **_runs_out(runs)}
                for txid, vout, runs in self.store.by_script(script)]

    # --- valuation (needs the node for W) ---

    def _require_node(self, what: str):
        if self.node is None:
            raise RpcFault(NO_NODE, f"{what} needs the host node; this "
                           "server was started without --node-url. A wallet "
                           "with its own node can value getbirthruns output "
                           "locally (docs/spec/constants.md).")
        return self.node

    def _work_table(self, tip: int) -> WorkTable:
        with self._table_lock:
            if self._table is None or tip // RETARGET_PERIOD > \
                    self._table.tip // RETARGET_PERIOD:
                self._table = WorkTable(self._node_call_raw, self.nbits, tip)
            return self._table

    def _node_call_raw(self, method, *params):
        try:
            return self.node(method, *params)
        except RpcError as e:
            raise RpcFault(NODE_ERROR, f"node refused {method}: {e}")

    def getlatent(self, target) -> dict:
        """target: an address/script string, or {"txid":..., "vout":...}."""
        node = self._require_node("getlatent")
        if isinstance(target, dict):
            txid, vout = target.get("txid", ""), target.get("vout", -1)
            utxos = [self.getbirthruns(txid, vout)]
        else:
            utxos = self.getaddressutxos(target)
        info = self._node_call_raw("getblockchaininfo")
        chainwork = int(info["chainwork"], 16)
        table = self._work_table(info["blocks"])
        total_scaled = 0
        for u in utxos:
            birth = Counter()
            for h, n in u["runs"]:
                if h != "plain":
                    birth[h] += n
            scaled = work_scaled(birth, table)
            u["seeds"] = seeds_from_work(scaled, chainwork)
            total_scaled += scaled
        return {
            "valued_at_height": info["blocks"],
            "chainwork": info["chainwork"],
            "seeds": seeds_from_work(total_scaled, chainwork),
            "utxos": utxos,
        }

    # --- relay ---

    def sendrawtransaction(self, hexstring: str) -> str:
        node = self._require_node("sendrawtransaction")
        if not isinstance(hexstring, str):
            raise RpcFault(INVALID_PARAMS, "expected raw transaction hex")
        return self._node_call_raw("sendrawtransaction", hexstring)

    # --- discovery of the surface itself ---

    def help(self) -> list[str]:
        return [
            "getindexinfo",
            "getbirthruns <txid> <vout>",
            "getaddressutxos <address|script_hex>",
            "getlatent <address|script_hex|{txid,vout}>",
            "sendrawtransaction <hex>",
            "help",
        ]

    METHODS = ("getindexinfo", "getbirthruns", "getaddressutxos",
               "getlatent", "sendrawtransaction", "help")

    def dispatch(self, method: str, params: list):
        if method not in self.METHODS:
            raise RpcFault(METHOD_NOT_FOUND, f"unknown method {method!r}; "
                           "try 'help'")
        try:
            return getattr(self, method)(*params)
        except TypeError as e:
            raise RpcFault(INVALID_PARAMS, str(e))


def write_cookie(path: str) -> str:
    cred = "__cookie__:" + secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(cred)
    return cred


def make_server(queries: IndexQueries, bind: str, port: int,
                credential: Optional[str]) -> ThreadingHTTPServer:
    """credential None means no auth (accept everything)."""
    expect = None
    if credential is not None:
        expect = "Basic " + base64.b64encode(credential.encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):   # quiet; errors go via JSON
            pass

        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if expect is not None and \
                    self.headers.get("Authorization") != expect:
                self._reply(401, {"result": None, "id": None, "error": {
                    "code": -32600, "message": "unauthorized"}})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                rid = req.get("id")
                method = req["method"]
                params = req.get("params", [])
                if not isinstance(params, list):
                    raise RpcFault(INVALID_PARAMS, "params must be an array")
            except (ValueError, KeyError):
                self._reply(400, {"result": None, "id": None, "error": {
                    "code": -32700, "message": "parse error"}})
                return
            try:
                result = queries.dispatch(method, params)
                self._reply(200, {"result": result, "error": None, "id": rid})
            except RpcFault as e:
                self._reply(200, {"result": None, "id": rid, "error": {
                    "code": e.code, "message": str(e)}})
            except Exception as e:     # noqa: BLE001 — a bug, said plainly
                self._reply(200, {"result": None, "id": rid, "error": {
                    "code": -32603, "message": f"internal error: {e}"}})

    return ThreadingHTTPServer((bind, port), Handler)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="claimstones indexer RPC: serve the birth index "
                    "read-only over JSON-RPC (docs/spec/indexer-rpc.md)")
    ap.add_argument("--db", required=True, help="the LMDB birth index")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--cookie-file", default=None,
                    help="where to write the auth cookie "
                         "(default: rpc.cookie beside the db)")
    ap.add_argument("--no-auth", action="store_true",
                    help="serve without authentication (localhost tests)")
    ap.add_argument("--node-url", default=None,
                    help="host node RPC; enables getlatent and "
                         "sendrawtransaction")
    ap.add_argument("--node-user")
    ap.add_argument("--node-pass")
    ap.add_argument("--node-cookie")
    ap.add_argument("--map-gb", type=int, default=200,
                    help="LMDB map size; match the daemon's")
    args = ap.parse_args()

    store = LmdbBirthStore(args.db, map_mb=args.map_gb * 1024, readonly=True)
    beside = os.path.dirname(os.path.abspath(args.db))

    node = None
    if args.node_url:
        auth = resolve_rpc_auth(False, args.node_user, args.node_pass,
                                args.node_cookie)
        node = Rpc(args.node_url, auth)
    queries = IndexQueries(store, node,
                           NbitsCache(os.path.join(beside, "nbits.json")))

    credential = None
    if not args.no_auth:
        cookie_path = args.cookie_file or os.path.join(beside, "rpc.cookie")
        credential = write_cookie(cookie_path)
        print(f"auth cookie written to {cookie_path}", flush=True)

    server = make_server(queries, args.bind, args.port, credential)
    print(f"indexer rpc: serving {args.db} (h={store.last_height()}) "
          f"on http://{args.bind}:{args.port} "
          f"(node: {args.node_url or 'none — index lookups only'})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
