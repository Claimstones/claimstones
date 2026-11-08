"""The gateway: a visitor's paste in, a bounded JSON answer out.

A fake indexer RPC stands behind it; the tests go through real HTTP on
both hops, because the gateway's whole job is HTTP shape — status
codes, caps, and the fields the edge cache keys on.
"""

import hashlib
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest

from claimstones.bitcoin import base58
from claimstones.indexer.gateway import make_handler, parse_target
from claimstones.lineage.walker import Rpc

GENESIS_B58 = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
GENESIS_SCRIPT = "76a91462e907b15cbf27d5425399ebf6f0fb50ebb88f1888ac"


# --------------------------------------------------------------------------
# base58
# --------------------------------------------------------------------------

def b58encode_check(payload: bytes) -> str:
    """Test-side encoder (the module only decodes, on purpose)."""
    raw = payload + hashlib.sha256(
        hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(raw, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = base58.ALPHABET[r] + s
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + s


def test_base58_p2pkh_known_vector():
    assert base58.address_to_script(GENESIS_B58).hex() == GENESIS_SCRIPT


def test_base58_p2sh_round_trip():
    h = bytes(range(20))
    addr = b58encode_check(bytes([base58.P2SH_VERSION]) + h)
    assert addr[0] == "3"
    assert base58.address_to_script(addr) == \
        bytes([0xA9, 0x14]) + h + bytes([0x87])


def test_base58_rejects_damage():
    with pytest.raises(base58.Base58Error, match="checksum"):
        base58.address_to_script(GENESIS_B58[:-1] + "b")
    with pytest.raises(base58.Base58Error, match="version"):  # testnet
        base58.address_to_script(
            b58encode_check(bytes([0x6F]) + bytes(20)))
    with pytest.raises(base58.Base58Error):
        base58.address_to_script("10Ol")          # 0, O, l not in alphabet


# --------------------------------------------------------------------------
# target parsing
# --------------------------------------------------------------------------

def test_parse_target_all_three_forms():
    bech = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert parse_target(bech).hex() == \
        "0014751e76e8199196d454941c45d1b3a323f1433bd6"
    assert parse_target(GENESIS_B58).hex() == GENESIS_SCRIPT
    assert parse_target(GENESIS_SCRIPT).hex() == GENESIS_SCRIPT


def test_parse_target_garbage():
    for bad in ("", "hello world", "zz" * 3, "bc1qqqqq", "x" * 30000):
        with pytest.raises(Exception):
            parse_target(bad)


# --------------------------------------------------------------------------
# the HTTP surface
# --------------------------------------------------------------------------

N_UTXOS = 7          # fake answer size; the fixture caps the gateway at 5
CAP = 5


class FakeIndexerRpc(BaseHTTPRequestHandler):
    def do_POST(self):
        call = json.loads(self.rfile.read(
            int(self.headers["Content-Length"])))
        method = call["method"]
        if method == "getindexinfo":
            result = {"height": 975000, "utxos": 1, "runs": 1,
                      "schema": "3", "activation_height": None}
        elif method == "getlatent":
            result = {"valued_at_height": 975000, "chainwork": "beef",
                      "seeds": 42,
                      "utxos": [{"txid": "00" * 32, "vout": i,
                                 "runs": [[100, 10]], "sats": 10,
                                 "latent_sats": 10, "seeds": 0}
                                for i in range(N_UTXOS)]}
        else:
            raise AssertionError(f"unexpected method {method}")
        raw = json.dumps({"result": result, "error": None,
                          "id": call["id"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture()
def gateway(tmp_path):
    backend = HTTPServer(("127.0.0.1", 0), FakeIndexerRpc)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    cookie = tmp_path / "rpc.cookie"
    cookie.write_text("__cookie__:x")
    rpc = Rpc(f"http://127.0.0.1:{backend.server_port}", None,
              cookie_path=str(cookie))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rpc, CAP))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    backend.shutdown()


def get(url: str):
    # 30s: a dead backend answers 502 only after the Rpc client's three
    # connect attempts and their backoff sleeps (~10s on Windows)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def test_info(gateway):
    status, body, headers = get(gateway + "/v1/info")
    assert status == 200
    assert body["height"] == 975000
    assert body["activation_height"] is None
    assert body["as_of"] > 0
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "max-age=60" in headers["Cache-Control"]


def test_address_totals_exact_rows_capped(gateway):
    status, body, _ = get(gateway + "/v1/address/" + GENESIS_B58)
    assert status == 200
    assert body["script"] == GENESIS_SCRIPT
    assert body["seeds"] == 42
    # totals over ALL utxos, rows capped, and it says so
    assert body["sats"] == N_UTXOS * 10
    assert body["utxo_count"] == N_UTXOS
    assert len(body["utxos"]) == CAP
    assert body["truncated"] is True
    assert body["height"] == 975000


def test_bad_address_is_400(gateway):
    status, body, _ = get(gateway + "/v1/address/" + GENESIS_B58[:-1] + "b")
    assert status == 400
    assert "checksum" in body["error"]


def test_unknown_path_is_404(gateway):
    assert get(gateway + "/nope")[0] == 404


def test_post_is_405(gateway):
    req = urllib.request.Request(gateway + "/v1/info", data=b"{}")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 405


def test_backend_away_is_502(tmp_path):
    cookie = tmp_path / "rpc.cookie"
    cookie.write_text("__cookie__:x")
    # a port that refuses fast: bind, note it, close (port 1 can hang on
    # Windows while a dead ephemeral port answers RST immediately)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    gone = probe.getsockname()[1]
    probe.close()
    rpc = Rpc(f"http://127.0.0.1:{gone}", None, cookie_path=str(cookie))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rpc, CAP))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body, _ = get(f"http://127.0.0.1:{srv.server_port}/v1/info")
        assert status == 502
        assert "unavailable" in body["error"]
    finally:
        srv.shutdown()
