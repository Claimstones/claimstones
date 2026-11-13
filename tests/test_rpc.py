"""The indexer RPC server (src/claimstones/indexer/rpc.py) and the
claimstones-cli client, over a real LMDB store and a live HTTP server
on an ephemeral port. The node is a scripted fake: one retarget period
at the genesis difficulty, so the expected seed counts are computed
with the same walker arithmetic the server uses."""

import json
import threading
from collections import Counter

import pytest

from claimstones.bitcoin.bech32 import script_to_address
from claimstones.cli import rpc_cli
from claimstones.indexer.rpc import (IndexQueries, NbitsCache, make_server,
                                     write_cookie)
from claimstones.indexer.store_lmdb import LmdbBirthStore
from claimstones.indexer.birth import PLAIN
from claimstones.lineage.walker import (WorkTable, seeds_from_work,
                                        work_scaled)

TXID_A = bytes(range(32))            # internal (digest) order
TXID_B = bytes(range(32))[::-1]
SCRIPT_A = bytes.fromhex("5120") + bytes(32)          # p2tr, key = zeros
SCRIPT_B = bytes.fromhex("0014") + bytes(20)          # p2wpkh
RUNS_A = [(100, 5_000), (170, 2_500)]
RUNS_B = [(120, 1_000), (PLAIN, 600)]
CHAINWORK_HEX = "%064x" % (1 << 40)
GENESIS_BITS = "1d00ffff"
TIP = 200


def fake_node(method, *params):
    if method == "getblockchaininfo":
        return {"chain": "regtest", "blocks": TIP, "chainwork": CHAINWORK_HEX}
    if method == "getblockhash":
        return "00" * 32
    if method == "getblockheader":
        return {"bits": GENESIS_BITS}
    if method == "sendrawtransaction":
        return "feed" * 16
    raise AssertionError(f"unexpected node call {method}")


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rpcidx")
    db = str(tmp / "birth.lmdb")
    store = LmdbBirthStore(db, map_mb=64)
    store.begin()
    store.put(TXID_A, 0, SCRIPT_A, RUNS_A, height=170)
    store.put(TXID_A, 1, SCRIPT_B, RUNS_B, height=170, seeds=1234)
    store.put(TXID_B, 3, SCRIPT_A, [(150, 42)], height=171, seeds=5)
    store.set_meta("height", str(TIP))
    store.set_meta("ledger_height", "150")
    store.stats["lands"] = 2
    store.stats["minted"] = 1239
    store.commit()
    store.close()

    reader = LmdbBirthStore(db, map_mb=64, readonly=True)
    cookie = str(tmp / "rpc.cookie")
    credential = write_cookie(cookie)
    queries = IndexQueries(reader, node=fake_node,
                           nbits_cache=NbitsCache(str(tmp / "nbits.json")))
    server = make_server(queries, "127.0.0.1", 0, credential)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"url": url, "cookie": cookie, "credential": credential}
    server.shutdown()
    server.server_close()
    reader.close()


def rpc(served, method, *params, credential=...):
    cred = served["credential"] if credential is ... else credential
    return rpc_cli.call(served["url"], cred, method, list(params))


def test_getindexinfo(served):
    reply = rpc(served, "getindexinfo")
    assert reply["error"] is None
    info = reply["result"]
    assert info["height"] == TIP
    assert info["utxos"] == 3
    assert info["runs"] == 5
    assert info["schema"] == "3"
    assert (info["lands"], info["minted"], info["burned"]) == (2, 1239, 0)
    assert info["ledger_height"] == 150


def test_getorchidbalance_by_script_and_outpoint(served):
    addr_a = script_to_address("bcrt", SCRIPT_A)
    r = rpc(served, "getorchidbalance", addr_a)["result"]
    assert r["height"] == TIP
    assert r["seeds"] == 5                          # only TXID_B:3 on A
    assert r["utxos"] == [{"txid": TXID_B[::-1].hex(), "vout": 3, "seeds": 5}]
    by_hex = rpc(served, "getorchidbalance", SCRIPT_B.hex())["result"]
    assert by_hex["seeds"] == 1234
    assert by_hex["utxos"] == [{"txid": TXID_A[::-1].hex(), "vout": 1,
                                "seeds": 1234}]
    one = rpc(served, "getorchidbalance",
              {"txid": TXID_A[::-1].hex(), "vout": 1})["result"]
    assert one["seeds"] == 1234 and len(one["utxos"]) == 1
    none = rpc(served, "getorchidbalance",
               {"txid": TXID_A[::-1].hex(), "vout": 0})["result"]
    assert none == {"height": TIP, "seeds": 0, "utxos": []}
    assert rpc(served, "getorchidbalance",
               {"txid": "ab" * 32, "vout": 0})["error"]["code"] == -5


def test_getorchidbalance_without_a_ledger(tmp_path):
    """A reader on an index the fused daemon never wrote: the method
    exists and says why it cannot answer, rather than answering 0."""
    import lmdb
    from claimstones.indexer.rpc import RpcFault
    env = lmdb.open(str(tmp_path / "old"), max_dbs=4, map_size=64 << 20)
    env.open_db(b"utxo")
    env.open_db(b"undo")
    env.open_db(b"addr", dupsort=True)
    d_meta = env.open_db(b"meta")
    with env.begin(write=True) as txn:
        txn.put(b"schema", b"3", db=d_meta)
    env.close()
    reader = LmdbBirthStore(str(tmp_path / "old"), map_mb=64, readonly=True)
    try:
        q = IndexQueries(reader)
        assert q.getindexinfo()["ledger_height"] is None
        with pytest.raises(RpcFault) as e:
            q.getorchidbalance(SCRIPT_A.hex())
        assert "no ledger half" in str(e.value)
    finally:
        reader.close()


def test_getbirthruns_display_order_and_plain(served):
    txid_display = TXID_A[::-1].hex()
    r = rpc(served, "getbirthruns", txid_display, 1)["result"]
    assert r["runs"] == [[120, 1_000], ["plain", 600]]
    assert r["sats"] == 1_600
    assert r["latent_sats"] == 1_000


def test_getbirthruns_missing(served):
    reply = rpc(served, "getbirthruns", "ab" * 32, 0)
    assert reply["error"]["code"] == -5


def test_getaddressutxos_by_address_and_script(served):
    addr = script_to_address("bcrt", SCRIPT_A)
    by_addr = rpc(served, "getaddressutxos", addr)["result"]
    by_hex = rpc(served, "getaddressutxos", SCRIPT_A.hex())["result"]
    assert by_addr == by_hex
    assert {(u["txid"], u["vout"]) for u in by_addr} == {
        (TXID_A[::-1].hex(), 0), (TXID_B[::-1].hex(), 3)}


def test_getlatent_matches_walker_arithmetic(served):
    addr = script_to_address("bcrt", SCRIPT_A)
    r = rpc(served, "getlatent", addr)["result"]
    table = WorkTable(fake_node, NbitsCache(None), TIP)
    chainwork = int(CHAINWORK_HEX, 16)
    expected = seeds_from_work(
        work_scaled(Counter({100: 5_000, 170: 2_500, 150: 42}), table),
        chainwork)
    assert r["seeds"] == expected
    assert r["valued_at_height"] == TIP
    per_utxo = {(u["txid"], u["vout"]): u["seeds"] for u in r["utxos"]}
    assert per_utxo[(TXID_A[::-1].hex(), 0)] == seeds_from_work(
        work_scaled(Counter({100: 5_000, 170: 2_500}), table), chainwork)


def test_getlatent_single_outpoint(served):
    r = rpc(served, "getlatent",
            {"txid": TXID_B[::-1].hex(), "vout": 3})["result"]
    assert len(r["utxos"]) == 1
    assert r["utxos"][0]["sats"] == 42


def test_sendrawtransaction_passthrough(served):
    assert rpc(served, "sendrawtransaction", "beef")["result"] == "feed" * 16


def test_auth_required(served):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as e:
        rpc(served, "getindexinfo", credential="__cookie__:wrong")
    assert e.value.code == 401


def test_unknown_method(served):
    assert rpc(served, "eatblock")["error"]["code"] == -32601


def test_no_node_error():
    queries = IndexQueries.__new__(IndexQueries)
    queries.node = None
    from claimstones.indexer.rpc import RpcFault
    with pytest.raises(RpcFault) as e:
        queries._require_node("getlatent")
    assert e.value.code == -10


def test_cli_end_to_end(served, capsys):
    code = rpc_cli.main(["-rpcurl", served["url"],
                         "-rpccookie", served["cookie"], "getindexinfo"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["height"] == TIP


def test_cli_error_path(served, capsys):
    code = rpc_cli.main(["-rpcurl", served["url"],
                         "-rpccookie", served["cookie"], "eatblock"])
    assert code == 1
    assert "-32601" in capsys.readouterr().err.replace(" ", "")
