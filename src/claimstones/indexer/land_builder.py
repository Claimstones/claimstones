"""claimstones land builder: claim-and-collect PSBTs for ordinary wallets.

    python -I -m claimstones.indexer.land_builder --rpc-cookie PATH [options]

Spec 8 (docs/spec/land-psbt.md). One path, POST /v1/land: name the
address holding the bitcoin and the address that will keep the orchids,
get back an unsigned PSBT of the claim-and-collect template to sign in
the wallet you already trust. The template, its checks and the PSBT are
wallet/land.py and wallet/collect.py — the same code the CLI wallet runs,
imported, never copied, so the public verifyCollect check keeps meaning
something (land.py says why).

    request   {"address": "...", "orchids_to": "...", "fee_rate": 4}
              fee_rate (sat/vB) is optional; the default is the indexer
              RPC's getfeerate, the median of what the last blocks paid
    answer    {"psbt": "<base64>",
               "summary": {"inputs", "input_sats", "carrier_sats",
                           "return_sats", "fee_sats", "fee_rate", "seeds",
                           "height", "utxo_count", "capped",
                           "orchids_to_coins", "orchids_to_latent_sats"},
               "as_of"}

What the builder refuses, beyond what build_collect refuses itself:
below ACTIVATION_HEIGHT or with it unset (an envelope there is ignored
by every indexer), an address of another network, script hex instead of
an address, the same address on both sides, and legacy funding
addresses (1.../m.../n...): their signer needs the raw previous
transactions, which this server cannot fetch from a pruned node without
a transaction index. Taproot and segwit inputs carry witness_utxo only;
the signing wallet adds the previous transactions from its own history
where its signer wants them (land-psbt.md §Answer).

It never sees a key, signs nothing, broadcasts nothing; building a PSBT
mutates nothing. No auth: the edge's rate limit is the protection, and
the edge forwards exactly POST /v1/land here. Errors are JSON,
{"error": msg}: 400 bad input, 404 no such path, 405 not POST, 413 body
too large, 502 index backend unavailable.
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
from claimstones.indexer.gateway import _HEX, BadTarget, parse_target
from claimstones.wallet.collect import plan_collect
from claimstones.wallet.land import TemplateError, script_type
from claimstones.wallet.rpc import (NO_NODE, NODE_ERROR, IndexerRpc,
                                    RpcUnreachable, WalletRpcError)

DEFAULT_PORT = 7334
MAX_BODY = 4096
MAX_FEE_RATE = 1000         # sat/vB; anything above is a typo


class Refused(ValueError):
    """A plain-words 400."""


class Unavailable(RuntimeError):
    """The index backend is away: 502."""


def _address(text, chain: str, what: str) -> bytes:
    if not isinstance(text, str) or not text.strip():
        raise Refused(f"{what}: expected a bitcoin address")
    text = text.strip()
    if len(text) % 2 == 0 and set(text) <= _HEX:
        raise Refused(f"{what}: expected a bitcoin address, not script hex")
    try:
        return parse_target(text, chain)
    except BadTarget as e:
        raise Refused(f"{what}: {e}") from None


def build(rpc: IndexerRpc, request: dict, max_inputs: int = 50,
          fee_blocks: int = 3) -> dict:
    """One POST /v1/land body -> the answer. Raises Refused (400) or
    Unavailable (502)."""
    if not isinstance(request, dict):
        raise Refused("expected a JSON object")
    try:
        info = rpc.getindexinfo()
        chain = info.get("chain") or "main"
        activation = info.get("activation_height")
        if activation is None:
            raise Refused("claims are not open: the activation height is "
                          "not set yet")
        if info.get("height", -1) < activation:
            raise Refused(f"claims open at block {activation:,}; the index "
                          f"is at {info.get('height', 0):,}")

        address = request.get("address")
        funding = _address(address, chain, "address holding the orchids")
        orchids = _address(request.get("orchids_to"), chain,
                           "address to keep the orchids")
        if funding == orchids:
            raise Refused("use a different address to keep the orchids "
                          "than the one holding the bitcoin")
        try:
            kind = script_type(funding)
        except TemplateError as e:
            raise Refused(str(e)) from None
        if kind == "pkh":
            raise Refused("legacy addresses (1..., m..., n...) cannot be "
                          "claimed here yet: their signer needs the previous "
                          "transactions, which this server cannot supply. "
                          "Move the bitcoin to a segwit or taproot address "
                          "first, or use a Claimstones wallet")
        try:
            script_type(orchids)
        except TemplateError as e:
            raise Refused(f"address to keep the orchids: {e}") from None

        fee_rate = request.get("fee_rate")
        if fee_rate is None:
            fee_rate = rpc.getfeerate(fee_blocks)["fee_rate"]
        elif (isinstance(fee_rate, bool)
              or not isinstance(fee_rate, (int, float))
              or not 1 <= fee_rate <= MAX_FEE_RATE):
            raise Refused(f"fee_rate must be a number of sat/vB, "
                          f"1..{MAX_FEE_RATE}")

        try:
            plan = plan_collect(orchids, address.strip(), rpc, fee_rate,
                                chain, max_inputs)
        except TemplateError as e:
            raise Refused(str(e)) from None
        # What the orchid address already holds, for the page's warning:
        # unclaimed bitcoin there means a wallet that spends from it, or
        # an address that is not the claimer's at all (a faucet's, in
        # the signet trial). A warning, never a refusal.
        held = rpc.getaddressutxos(orchids.hex())
    except WalletRpcError as e:
        if e.code in (NO_NODE, NODE_ERROR):
            raise Unavailable(e.message) from None
        raise Refused(e.message) from None
    except (RpcUnreachable, OSError) as e:
        # OSError: the cookie file is missing while the RPC restarts
        raise Unavailable(str(e)) from None

    s = plan.summary
    return {
        "psbt": plan.psbt.to_base64(),
        "summary": {
            "inputs": len(s.inputs),
            "input_sats": s.input_sats,
            "carrier_sats": s.carrier_sats,
            "return_sats": s.return_sats,
            "fee_sats": s.fee_sats,
            "fee_rate": fee_rate,
            "seeds": plan.seeds,
            "height": plan.valued_at_height,
            "utxo_count": plan.utxo_count,
            "capped": plan.utxo_count > len(s.inputs),
            "orchids_to_coins": len(held),
            "orchids_to_latent_sats": sum(u.get("latent_sats", 0)
                                          for u in held),
        },
        "as_of": int(time.time()),
    }


def make_handler(rpc: IndexerRpc, max_inputs: int, fee_blocks: int):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "claimstones-land-builder"
        sys_version = ""

        def _send(self, code: int, obj: dict) -> None:
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):         # noqa: N802  (BaseHTTPRequestHandler API)
            if self.path != "/v1/land":
                return self._send(404, {"error": "no such path"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self._send(413, {"error": "request body too large"})
            try:
                request = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                return self._send(400, {"error": "expected a JSON object"})
            try:
                self._send(200, build(rpc, request, max_inputs, fee_blocks))
            except Refused as e:
                self._send(400, {"error": str(e)})
            except Unavailable as e:
                self._send(502, {"error": f"index backend unavailable: {e}"})
            except Exception as e:      # a bug: say so, keep serving
                self._send(500, {"error": f"internal error: {e}"})

        def do_GET(self):          # noqa: N802
            self._send(405, {"error": "POST only"})

        def log_message(self, fmt, *a):
            print("%s %s" % (self.address_string(), fmt % a), flush=True)

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(
        description="claimstones land builder (Spec 8): unsigned "
                    "claim-and-collect PSBTs over POST /v1/land")
    ap.add_argument("--rpc-url", default="http://127.0.0.1:7332",
                    help="the indexer RPC (default %(default)s)")
    ap.add_argument("--rpc-cookie", required=True,
                    help="the indexer RPC's cookie file (re-read per call)")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to serve on (default %(default)s)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--max-inputs", type=int, default=50,
                    help="coins spent per PSBT, the largest first "
                         "(default %(default)s)")
    ap.add_argument("--fee-blocks", type=int, default=3,
                    help="blocks the default fee rate is the median of "
                         "(default %(default)s)")
    args = ap.parse_args()

    rpc = IndexerRpc(args.rpc_url, cookie_path=args.rpc_cookie)
    srv = ThreadingHTTPServer((args.bind, args.port),
                              make_handler(rpc, args.max_inputs,
                                           args.fee_blocks))
    print(f"land builder: {args.rpc_url} on http://{args.bind}:{args.port}"
          f" (max {args.max_inputs} inputs, fee = median of "
          f"{args.fee_blocks} blocks)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
