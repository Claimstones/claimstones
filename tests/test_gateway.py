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
from claimstones.bitcoin.bech32 import encode_segwit
from claimstones.indexer.gateway import BadTarget, make_handler, parse_target
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
    with pytest.raises(base58.Base58Error, match="wrong network"):  # testnet
        base58.address_to_script(
            b58encode_check(bytes([0x6F]) + bytes(20)))
    with pytest.raises(base58.Base58Error, match="version"):
        base58.address_to_script(
            b58encode_check(bytes([0x30]) + bytes(20)))   # litecoin L...
    with pytest.raises(base58.Base58Error):
        base58.address_to_script("10Ol")          # 0, O, l not in alphabet


def test_base58_test_networks():
    h = bytes(range(20))
    p2pkh = b58encode_check(bytes([base58.TEST_P2PKH_VERSION]) + h)
    p2sh = b58encode_check(bytes([base58.TEST_P2SH_VERSION]) + h)
    assert p2pkh[0] in "mn" and p2sh[0] == "2"
    assert base58.address_to_script(p2pkh, "signet") == \
        bytes([0x76, 0xA9, 0x14]) + h + bytes([0x88, 0xAC])
    assert base58.address_to_script(p2sh, "signet") == \
        bytes([0xA9, 0x14]) + h + bytes([0x87])
    with pytest.raises(base58.Base58Error, match="for mainnet, not signet"):
        base58.address_to_script(GENESIS_B58, "signet")


# --------------------------------------------------------------------------
# target parsing
# --------------------------------------------------------------------------

def test_parse_target_all_three_forms():
    bech = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert parse_target(bech).hex() == \
        "0014751e76e8199196d454941c45d1b3a323f1433bd6"
    assert parse_target(GENESIS_B58).hex() == GENESIS_SCRIPT
    assert parse_target(GENESIS_SCRIPT).hex() == GENESIS_SCRIPT


def test_parse_target_refuses_another_network():
    signet_tr = encode_segwit("tb", 1, bytes(32))
    with pytest.raises(BadTarget, match="wrong network: tb1... is an "
                                        "address for testnet or signet"):
        parse_target(signet_tr)
    assert parse_target(signet_tr, "signet")[:2] == bytes([0x51, 0x20])
    with pytest.raises(BadTarget, match="this index is signet"):
        parse_target("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "signet")
    with pytest.raises(BadTarget, match="wrong network"):
        parse_target(GENESIS_B58, "signet")
    testnet = b58encode_check(bytes([base58.TEST_P2PKH_VERSION]) + bytes(20))
    assert parse_target(testnet, "signet").hex() == "76a914" + "00" * 20 + "88ac"
    # hex has no network, and a script may start with any digit
    assert parse_target("2102" + "11" * 32 + "ac").hex()[:4] == "2102"


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
    ledger = True        # False: a birth-only index, getorchidbalance -5

    def do_POST(self):
        call = json.loads(self.rfile.read(
            int(self.headers["Content-Length"])))
        method = call["method"]
        result, error = None, None
        if method == "getindexinfo":
            result = {"height": 975000, "utxos": 1, "runs": 1,
                      "schema": "3", "chain": "main",
                      "activation_height": None}
        elif method == "getlatent":
            result = {"valued_at_height": 975000, "chainwork": "beef",
                      "seeds": 42,
                      "utxos": [{"txid": "00" * 32, "vout": i,
                                 "runs": [[100, 10]], "sats": 10,
                                 "latent_sats": 10, "seeds": 0}
                                for i in range(N_UTXOS)]}
        elif method == "getorchidbalance" and self.ledger:
            result = {"height": 975000, "seeds": 700,
                      "utxos": [{"txid": "00" * 32, "vout": 1, "seeds": 500},
                                {"txid": "00" * 32, "vout": 6, "seeds": 200}]}
        elif method == "getorchidbalance":
            error = {"code": -5, "message": "no ledger half"}
        else:
            raise AssertionError(f"unexpected method {method}")
        raw = json.dumps({"result": result, "error": error,
                          "id": call["id"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class BirthOnlyRpc(FakeIndexerRpc):
    ledger = False


@pytest.fixture(params=[FakeIndexerRpc])
def gateway(tmp_path, request):
    backend = HTTPServer(("127.0.0.1", 0), request.param)
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


def test_address_settled_orchids(gateway):
    status, body, _ = get(gateway + "/v1/address/" + GENESIS_B58)
    assert status == 200
    assert body["settled_seeds"] == 700
    rows = {u["vout"]: u["settled_seeds"] for u in body["utxos"]}
    assert rows == {0: 0, 1: 500, 2: 0, 3: 0, 4: 0}     # capped rows


@pytest.mark.parametrize("gateway", [BirthOnlyRpc], indirect=True)
def test_address_on_a_birth_only_index(gateway):
    status, body, _ = get(gateway + "/v1/address/" + GENESIS_B58)
    assert status == 200
    assert body["settled_seeds"] is None                # not 0
    assert "settled_seeds" not in body["utxos"][0]


def test_wrong_network_is_400(gateway):
    status, body, _ = get(gateway + "/v1/address/"
                          + encode_segwit("tb", 1, bytes(32)))
    assert status == 400
    assert "wrong network" in body["error"]


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
        assert body["error"] == "index backend unavailable"   # no internals
    finally:
        srv.shutdown()
