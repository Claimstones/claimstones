"""The wallet's line to the indexer RPC (docs/spec/indexer-rpc.md).

A thin JSON-RPC client: one HTTP POST per call, cookie or fixed
credentials, errors surfaced as WalletRpcError with the server's own
plain-words message. The cookie file is re-read on every call, so a
server restart (which rotates the cookie) never strands a wallet
session that is still open.

The wallet trusts this line exactly as far as an Electrum user trusts
an Electrum server: for what it shows, never for what it signs
(wallet-policy.md §Envelope scan).
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_URL = "http://127.0.0.1:7332"

# Error codes the wallet acts on (spec/indexer-rpc.md).
NOT_FOUND = -5
NO_NODE = -10
NODE_ERROR = -11


class WalletRpcError(Exception):
    """The server's error, code and message as served."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RpcUnreachable(Exception):
    """No server answered at all."""


def _http_transport(url: str, body: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            raise RpcUnreachable(f"HTTP {e.code} from {url}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RpcUnreachable(f"cannot reach {url}: {e}") from None


class IndexerRpc:
    def __init__(self, url: str = DEFAULT_URL, cookie_path: str | None = None,
                 credential: str | None = None,
                 transport: Callable[[str, bytes, dict], dict] = _http_transport):
        self.url = url
        self.cookie_path = cookie_path
        self.credential = credential
        self.transport = transport
        self._id = 0

    def _auth(self) -> str | None:
        if self.cookie_path:
            with open(self.cookie_path, encoding="ascii") as f:
                return f.read().strip()
        return self.credential

    def call(self, method: str, *params) -> Any:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": list(params)}).encode()
        headers = {"Content-Type": "application/json"}
        auth = self._auth()
        if auth is not None:
            headers["Authorization"] = ("Basic "
                                        + base64.b64encode(auth.encode()).decode())
        reply = self.transport(self.url, body, headers)
        if reply.get("error"):
            err = reply["error"]
            raise WalletRpcError(err.get("code", 0), err.get("message", ""))
        return reply.get("result")

    # --- the calls the wallet makes -----------------------------------

    def getindexinfo(self) -> dict:
        return self.call("getindexinfo")

    def getaddressutxos(self, script_or_address: str) -> list[dict]:
        return self.call("getaddressutxos", script_or_address)

    def getbirthruns(self, txid: str, vout: int) -> dict:
        return self.call("getbirthruns", txid, vout)

    def getlatent(self, target) -> dict:
        return self.call("getlatent", target)

    def getorchidbalance(self, target) -> dict:
        # Settled balances; the index serves this once the ledger half
        # exists (indexer-rpc.md §Planned). Until then the server
        # answers with an error the caller shows.
        return self.call("getorchidbalance", target)

    def sendrawtransaction(self, hexstring: str) -> str:
        return self.call("sendrawtransaction", hexstring)
