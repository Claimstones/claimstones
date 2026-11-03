"""claimstones-cli — bitcoin-cli for the indexer's RPC.

One call per invocation, the result on stdout, errors on stderr with
exit code 1. Parameters are JSON where they parse as JSON (numbers,
objects, arrays, true/false/null) and strings otherwise, which is
bitcoin-cli's rule too:

    claimstones-cli -rpccookie D:/claimstones/rpc.cookie getindexinfo
    claimstones-cli ... getbirthruns <txid> 0
    claimstones-cli ... getaddressutxos bc1p...
    claimstones-cli ... getlatent '{"txid":"...","vout":0}'

The server writes its cookie beside the database (rpc.cookie); pass it
with -rpccookie, or -rpcuser/-rpcpassword where credentials are fixed,
or nothing against a --no-auth server.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:7332"


def parse_param(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def read_credential(args) -> str | None:
    if args.rpccookie:
        with open(args.rpccookie, encoding="ascii") as f:
            return f.read().strip()
    if args.rpcuser is not None:
        return f"{args.rpcuser}:{args.rpcpassword or ''}"
    return None


def call(url: str, credential: str | None, method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": os.getpid(),
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json"})
    if credential is not None:
        req.add_header("Authorization", "Basic "
                       + base64.b64encode(credential.encode()).decode())
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="claimstones-cli",
        description="call the claimstones indexer RPC "
                    "(docs/spec/indexer-rpc.md)")
    ap.add_argument("-rpcurl", default=DEFAULT_URL)
    ap.add_argument("-rpccookie", help="the server's rpc.cookie file")
    ap.add_argument("-rpcuser")
    ap.add_argument("-rpcpassword")
    ap.add_argument("method")
    ap.add_argument("params", nargs="*")
    args = ap.parse_args(argv)

    try:
        credential = read_credential(args)
    except OSError as e:
        print(f"error: cannot read cookie: {e}", file=sys.stderr)
        return 1

    try:
        reply = call(args.rpcurl, credential, args.method,
                     [parse_param(p) for p in args.params])
    except urllib.error.HTTPError as e:
        try:
            reply = json.loads(e.read())
        except ValueError:
            print(f"error: HTTP {e.code} from {args.rpcurl}", file=sys.stderr)
            return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"error: cannot reach {args.rpcurl}: {e}", file=sys.stderr)
        return 1

    if reply.get("error"):
        err = reply["error"]
        print(f"error code: {err.get('code')}\nerror message:\n"
              f"{err.get('message')}", file=sys.stderr)
        return 1
    result = reply.get("result")
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
