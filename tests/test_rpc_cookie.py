"""Rpc heals a rotated cookie instead of dying (walker.py).

Core rotates .cookie on every start, so a daemon that outlives a node
restart sees HTTP 401 with credentials that were right when it started.
The client must re-read the cookie file and retry; only a 401 that the
file cannot explain is a real refusal. This is what let cs_indexer die
on 2026-11-06 when the node restarted under it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from claimstones.lineage.walker import Rpc, basic_auth_header

expected = {"cred": None}      # what the fake node currently accepts


class Node(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Authorization") != basic_auth_header(expected["cred"]):
            self.send_response(401)
            self.end_headers()
            return
        calls = json.loads(body)
        if isinstance(calls, list):                      # batch
            reply = [{"result": c["params"], "error": None, "id": c["id"]}
                     for c in calls]
        else:
            reply = {"result": calls["params"], "error": None, "id": 0}
        raw = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):                           # keep pytest quiet
        pass


@pytest.fixture()
def node(tmp_path):
    cookie = tmp_path / "rpc.cookie"
    cookie.write_text("__cookie__:one")
    expected["cred"] = "__cookie__:one"
    srv = HTTPServer(("127.0.0.1", 0), Node)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    rpc = Rpc(url, basic_auth_header(cookie.read_text()),
              cookie_path=str(cookie))
    yield rpc, cookie
    srv.shutdown()


def rotate(cookie, cred):
    """The node restarts: new cookie on disk, new credential required."""
    cookie.write_text(cred)
    expected["cred"] = cred


def test_call_survives_rotation(node):
    rpc, cookie = node
    assert rpc("echo", 1) == [1]
    rotate(cookie, "__cookie__:two")
    assert rpc("echo", 2) == [2]          # 401 -> re-read cookie -> retry
    assert rpc("echo", 3) == [3]          # and the new header sticks


def test_batch_survives_rotation(node):
    rpc, cookie = node
    rotate(cookie, "__cookie__:two")
    assert rpc.batch([("echo", 4), ("echo", 5)]) == [[4], [5]]


def test_unexplained_401_still_fatal(node):
    rpc, cookie = node
    # The node wants credentials the cookie file does not hold: re-reading
    # changes nothing, so this is a real refusal and must still raise.
    expected["cred"] = "__cookie__:other"
    with pytest.raises(RuntimeError, match="authentication failed"):
        rpc("echo", 6)


def test_no_cookie_path_still_fatal(node):
    rpc, cookie = node
    bare = Rpc(rpc.url, basic_auth_header("__cookie__:stale"))
    with pytest.raises(RuntimeError, match="authentication failed"):
        bare("echo", 7)
