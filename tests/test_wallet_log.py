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

from claimstones.wallet.cli import main
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
        save_wallet(self.wallet, KEY)
        rc, screen = run(self.argv, "a\no\nfoo\nq\n")
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
        save_wallet(self.wallet, KEY)
        rc, screen = run(self.argv[:-2], "o\nq\n")
        self.assertEqual(rc, 0)
        self.assertIn(CODE, screen)
        self.assertFalse(os.path.exists(self.log))

    def test_streams_restored_after_a_logged_run(self):
        save_wallet(self.wallet, KEY)
        stdin, stdout = sys.stdin, sys.stdout
        run(self.argv + ["address"], "")
        self.assertIs(sys.stdin, stdin)
        self.assertIs(sys.stdout, stdout)
        self.assertIn("  address\n", self.read_log())


if __name__ == "__main__":
    unittest.main()
