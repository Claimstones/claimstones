"""The wallet's -log option (wallet/cli.py): the session as the screen
showed it -- prompts and answers in order -- with the wallet code and
the password never written, whether shown, typed or pasted."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from unittest import mock

from claimstones.wallet import cli
from claimstones.wallet.cli import _Tee, _link, main
from claimstones.wallet.keys import WalletKey, load_wallet, save_wallet

CODE = "orchid1qqqsyqcyq5rqwzqfpg9scrgwpugpzysnzs23v9ccrydpk8qarc0sa7d8kr"
KEY = WalletKey.from_code(CODE)
NO_SERVER = "http://127.0.0.1:9"        # refused at once: the menu says so


def run(argv: list[str], typed: str) -> tuple[int, str]:
    """main() with `typed` as the keyboard; returns (rc, screen)."""
    screen = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(typed)), \
         mock.patch.object(sys, "stdout", screen), \
         mock.patch.object(sys, "stderr", screen):
        rc = main(argv)
    return rc, screen.getvalue()


class TestWalletLog(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.wallet = os.path.join(self.folder.name, "wallet.code")
        self.log = os.path.join(self.folder.name, "session.log")
        self.argv = ["-network", "regtest", "-rpcurl", NO_SERVER,
                     "-wallet", self.wallet, "-log", self.log]

    def tearDown(self):
        self.folder.cleanup()

    def read_log(self) -> str:
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    def test_answers_logged_code_shown_but_not_logged(self):
        save_wallet(self.wallet, KEY, "hunter2")
        rc, screen = run(self.argv, "hunter2\na\no\nhunter2\nfoo\nq\n")
        self.assertEqual(rc, 0)
        log = self.read_log()
        address = KEY.address("regtest")
        # the screen has everything, the code included
        self.assertIn(CODE, screen)
        self.assertIn(address, screen)
        # the log: header, prompts with their answers, output, exit
        self.assertIn("claimstones-wallet -network regtest", log)
        self.assertIn("  menu\n", log)
        self.assertIn("> a\n", log)
        self.assertIn(f"wallet address: {address}", log)
        self.assertIn("> o\n", log)
        self.assertIn("  <wallet code not logged>\n", log)
        self.assertIn("> foo\nno such command: foo", log)
        self.assertIn("--- exit 0\n", log)
        self.assertNotIn(CODE, log)
        self.assertNotIn(CODE[8:30], log)
        self.assertNotIn("hunter2", log)

    def test_backup_needs_the_password(self):
        save_wallet(self.wallet, KEY, "hunter2")
        rc, screen = run(self.argv, "hunter2\no\nnope\no\n\nq\n")
        self.assertEqual(rc, 0)
        self.assertNotIn(CODE, screen)
        self.assertIn("wrong password", screen)
        self.assertEqual(screen.count("the wallet code stays hidden"), 2)

    def test_a_new_wallet_needs_a_password(self):
        rc, screen = run(self.argv, "n\n\nn\nfive5\nq\n")
        self.assertEqual(rc, 0)
        self.assertEqual(screen.count("a password needs at least 6 characters"), 2)
        self.assertFalse(os.path.exists(self.wallet))

    def test_plain_file_enter_then_p_locks_it(self):
        # A file from before passwords were required: it opens without a
        # prompt, Enter is its password at backup, [p] locks it.
        with open(self.wallet, "w", encoding="ascii") as f:
            f.write(f"# claimstones wallet code\n{CODE}\n")
        rc, screen = run(self.argv, "o\n\np\n\nhunter2\nhunter2\no\n\nq\n")
        self.assertEqual(rc, 0)
        self.assertIn("none set, press Enter", screen)
        self.assertEqual(screen.count(CODE), 1)      # the first backup only
        self.assertIn("password changed", screen)
        self.assertIn("wrong password", screen)      # Enter no longer opens it
        self.assertEqual(load_wallet(self.wallet, "hunter2").secret, KEY.secret)
        self.assertNotIn("hunter2", self.read_log())

    def test_restore_and_password_never_logged(self):
        rc, _ = run(self.argv, f"r\n{CODE}\nhunter2\nhunter2\nq\n")
        self.assertEqual(rc, 0)
        self.assertEqual(load_wallet(self.wallet, "hunter2").secret, KEY.secret)
        # a second session unlocks the file
        rc, _ = run(self.argv, "hunter2\nq\n")
        self.assertEqual(rc, 0)
        log = self.read_log()
        self.assertEqual(log.count("  claimstones-wallet -network"), 2)   # appended
        self.assertIn("wallet code (orchid1...)> <not logged>\n", log)
        self.assertIn("the same password again> <not logged>\n", log)
        self.assertIn("wallet password> <not logged>\n", log)
        self.assertIn("(locked)", log)
        self.assertNotIn(CODE, log)
        self.assertNotIn("hunter2", log)

    def test_no_log_without_the_flag(self):
        save_wallet(self.wallet, KEY, "hunter2")
        rc, screen = run(self.argv[:-2], "hunter2\no\nhunter2\nq\n")
        self.assertEqual(rc, 0)
        self.assertIn(CODE, screen)
        self.assertFalse(os.path.exists(self.log))

    def test_streams_restored_after_a_logged_run(self):
        save_wallet(self.wallet, KEY, "hunter2")
        stdin, stdout = sys.stdin, sys.stdout
        run(self.argv + ["address"], "hunter2\n")
        self.assertIs(sys.stdin, stdin)
        self.assertIs(sys.stdout, stdout)
        self.assertIn("  address\n", self.read_log())


TXID = "2b29727795ced1d5fdc272e6ce728c6c87a9b10c789e8fab676f22ced17546ca"


class Terminal(io.StringIO):
    def isatty(self):
        return True


class TestTxidLinks(unittest.TestCase):

    def test_a_link_on_a_terminal_plain_elsewhere(self):
        with mock.patch.object(cli, "_vt_on", lambda: True):
            self.assertEqual(
                _link(TXID, "signet", Terminal()),
                f"\x1b]8;;https://mempool.space/signet/tx/{TXID}\x1b\\{TXID}"
                "\x1b]8;;\x1b\\")
            self.assertIn("https://mempool.space/tx/",
                          _link(TXID, "main", Terminal()))
            self.assertEqual(_link(TXID, "signet", io.StringIO()), TXID)  # piped
            self.assertEqual(_link(TXID, "regtest", Terminal()), TXID)   # no explorer
            self.assertEqual(_link(TXID, "signet", Terminal(), "2b29..ca:1")[-17:],
                             "2b29..ca:1\x1b]8;;\x1b\\")
        with mock.patch.object(cli, "_vt_on", lambda: False):
            self.assertEqual(_link(TXID, "signet", Terminal()), TXID)    # old console

    def test_the_log_gets_the_text_only(self):
        screen, log = Terminal(), io.StringIO()
        with mock.patch.object(cli, "_vt_on", lambda: True):
            tee = _Tee(screen, log)
            tee.write(f"sent: txid {_link(TXID, 'signet', tee)}\n")
        self.assertIn("\x1b]8;;", screen.getvalue())
        self.assertEqual(log.getvalue(), f"sent: txid {TXID}\n")


if __name__ == "__main__":
    unittest.main()
