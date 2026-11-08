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

from claimstones.indexer.birth import transient_rpc
from claimstones.lineage.walker import (Rpc, RpcCookieWait, basic_auth_header,
                                        resolve_rpc_auth,
                                        resolve_rpc_cookie_path)

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


def test_unexplained_401_waits_not_dies(node):
    # The node wants credentials the cookie file does not hold yet. After
    # a crash the stale cookie sits on disk while the new node starts, so
    # this is indistinguishable from a restart in progress: the client
    # must raise the *waitable* error, which transient_rpc lets
    # wait_for_rpc retry until the new cookie lands (2026-11-08, cs-index:
    # systemd restarts bitcoind fast enough to hit this window every time).
    rpc, cookie = node
    expected["cred"] = "__cookie__:other"
    with pytest.raises(RpcCookieWait) as e:
        rpc("echo", 6)
    assert transient_rpc(e.value)
    # the node finally writes its cookie: the next attempt heals in place,
    # which is exactly the loop wait_for_rpc drives
    rotate(cookie, "__cookie__:other")
    assert rpc("echo", 6) == [6]


def test_batch_unexplained_401_waits_not_dies(node):
    rpc, cookie = node
    expected["cred"] = "__cookie__:other"
    with pytest.raises(RpcCookieWait) as e:
        rpc.batch([("echo", 6)])
    assert transient_rpc(e.value)


def test_missing_cookie_file_waits_not_dies(node):
    # Clean shutdown: Core deletes .cookie, the port may answer 401 before
    # the new file exists. Same verdict: wait.
    rpc, cookie = node
    expected["cred"] = "__cookie__:other"
    cookie.unlink()
    with pytest.raises(RpcCookieWait):
        rpc("echo", 6)


def test_daemon_starting_before_the_cookie_exists(node, tmp_path):
    # Boot race: systemd starts the daemon and the node together, and the
    # daemon can resolve auth before the node has written .cookie. The
    # resolvers must neither exit nor drop the path (both happened on
    # cs-index's first reboot, 2026-11-08); the client starts without
    # credentials, waits through the 401, and heals once the file lands.
    rpc, cookie = node
    missing = tmp_path / "not-written-yet" / "rpc.cookie"
    assert resolve_rpc_auth(False, None, None, str(missing)) is None
    assert resolve_rpc_cookie_path(False, None, str(missing)) == str(missing)

    young = Rpc(rpc.url, None, cookie_path=str(cookie))
    expected["cred"] = "__cookie__:boot"
    cookie.unlink()                       # node has not written it yet
    with pytest.raises(RpcCookieWait) as e:
        young("echo", 8)
    assert transient_rpc(e.value)
    rotate(cookie, "__cookie__:boot")     # node finishes starting
    assert young("echo", 9) == [9]


def test_no_cookie_path_still_fatal(node):
    # Fixed credentials (--rpc-user/--rpc-pass): a 401 is a real refusal,
    # and must not be waited out.
    rpc, cookie = node
    bare = Rpc(rpc.url, basic_auth_header("__cookie__:stale"))
    with pytest.raises(RuntimeError, match="authentication failed") as e:
        bare("echo", 7)
    assert not transient_rpc(e.value)
