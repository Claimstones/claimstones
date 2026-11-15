"""Send and Withdraw (wallet/spend.py, wallet/cli.py) and the wallet
file lock (wallet/keys.py): coin selection under coin control, the
envelope shapes, both verify moments, tamper refusals, the mandated
wording, and the scrypt lock round trip."""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from claimstones.bitcoin.psbt import Transaction, verify_input
from claimstones.bitcoin.txparse import TxOut
from claimstones.protocol.codec import Envelope, decipher
from claimstones.wallet.check import parse_orchids
from claimstones.wallet.cli import do_send, do_withdraw
from claimstones.wallet.keys import (
    WalletKey, WalletKeyError, WalletLocked, change_password, is_locked,
    load_wallet, lock_line, save_wallet, unlock_line,
)
from claimstones.wallet.land import CARRIER_SATS, TemplateError
from claimstones.wallet.rpc import NOT_FOUND, WalletRpcError
from claimstones.wallet.spend import (
    plan_send, plan_withdraw, sign_send, sign_withdraw, verify_send,
)

CODE = "orchid1qqqsyqcyq5rqwzqfpg9scrgwpugpzysnzs23v9ccrydpk8qarc0sa7d8kr"
KEY = WalletKey.from_code(CODE)

# An external recipient (taproot, so the 330-sat carrier clears dust).
RECIPIENT = "bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297"
DEST_WPKH = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
DEST_PKH = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def utxo(salt: int, sats: int, latent: int = 0, seeds: int = 0) -> dict:
    return {"txid": f"{salt:064x}", "vout": salt, "sats": sats,
            "latent_sats": latent, "runs": [], "seeds": seeds}


class FakeRpc:
    """One wallet script; getorchidbalance serves the coins' own seeds
    unless the method is "not built yet"."""

    def __init__(self, coins: list[dict], balances: bool = True):
        self.coins = coins
        self.balances = balances
        self.sent: list[str] = []

    def getaddressutxos(self, script_hex: str) -> list[dict]:
        if script_hex != KEY.script.hex():
            return []
        return [dict(u) for u in self.coins]

    def getorchidbalance(self, target) -> dict:
        if not self.balances:
            raise WalletRpcError(NOT_FOUND, "getorchidbalance is not served")
        utxos = [{"txid": u["txid"], "vout": u["vout"], "seeds": u["seeds"]}
                 for u in self.coins if u["seeds"]]
        return {"seeds": sum(u["seeds"] for u in utxos), "utxos": utxos}

    def sendrawtransaction(self, hexstring: str) -> str:
        tx = Transaction.parse(bytes.fromhex(hexstring))
        self.sent.append(hexstring)
        return tx.txid_hex


class ParseOrchidsTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(parse_orchids("12.34"), 1234)
        self.assertEqual(parse_orchids("12"), 1200)
        self.assertEqual(parse_orchids("0.05"), 5)
        self.assertEqual(parse_orchids(".5"), 50)
        self.assertEqual(parse_orchids("1,234.5"), 123450)
        for bad in ("", ".", "12.345", "1.2.3", "x", "-1"):
            with self.assertRaises(ValueError):
                parse_orchids(bad)


class SendTests(unittest.TestCase):
    def test_send_selects_orchid_coins_largest_first(self):
        rpc = FakeRpc([utxo(1, 50_000, seeds=200),
                       utxo(2, 40_000, seeds=1_000),
                       utxo(3, 30_000)])              # plain, stays out
        plan = plan_send(KEY, rpc, RECIPIENT, 900, 1.0)
        s = plan.summary
        self.assertEqual(s.inputs, (f"{2:064x}:2",))  # 1000 seeds cover 900
        self.assertEqual(s.input_seeds, 1_000)
        self.assertEqual(s.amount_seeds, 900)
        self.assertEqual(s.input_sats,
                         s.change_sats + s.carrier_sats + s.fee_sats)
        env = decipher(plan.psbt.tx)
        self.assertIsInstance(env, Envelope)
        self.assertFalse(env.land)
        self.assertEqual(len(env.edicts), 1)
        self.assertEqual(env.edicts[0].amount, 900)
        self.assertEqual(env.edicts[0].output, 2)

    def test_send_adds_plain_coins_for_the_fee(self):
        # The orchid coin's sats cannot cover carrier + fee + change dust.
        rpc = FakeRpc([utxo(1, 500, seeds=1_000), utxo(2, 30_000)])
        plan = plan_send(KEY, rpc, RECIPIENT, 900, 1.0)
        self.assertEqual(len(plan.summary.inputs), 2)

    def test_fee_from_other_orchid_coins_when_plain_runs_out(self):
        # The signet trial, 2026-11-14: 6 orchids from a 330-sat carrier,
        # one 330-sat plain coin, the sats on a coin holding 123.72
        # orchids. The plain coin goes first, then the orchid coin; its
        # orchids are counted and ride back to the change. A coin with
        # unclaimed sats never joins.
        rpc = FakeRpc([utxo(1, 330, seeds=200_055),
                       utxo(2, 8_394, seeds=12_372),
                       utxo(3, 330),
                       utxo(4, 50_000, latent=50_000)])
        s = plan_send(KEY, rpc, RECIPIENT, 600, 2.0).summary
        self.assertEqual(s.inputs, (f"{1:064x}:1", f"{3:064x}:3", f"{2:064x}:2"))
        self.assertEqual(s.input_seeds, 200_055 + 12_372)
        self.assertGreaterEqual(s.change_sats, 330)
        # enough plain sats: the orchid coin stays out
        rpc = FakeRpc([utxo(1, 330, seeds=200_055),
                       utxo(2, 8_394, seeds=12_372), utxo(3, 5_000)])
        s = plan_send(KEY, rpc, RECIPIENT, 600, 2.0).summary
        self.assertEqual(s.inputs, (f"{1:064x}:1", f"{3:064x}:3"))
        # only the latent coin left: refused, not used
        rpc = FakeRpc([utxo(1, 330, seeds=200_055),
                       utxo(4, 50_000, latent=50_000)])
        with self.assertRaisesRegex(TemplateError, "not enough sats for the fee"):
            plan_send(KEY, rpc, RECIPIENT, 600, 2.0)

    def test_send_refusals(self):
        with self.assertRaisesRegex(TemplateError, "not enough orchids"):
            plan_send(KEY, FakeRpc([utxo(1, 50_000, seeds=100)]),
                      RECIPIENT, 900, 1.0)
        with self.assertRaisesRegex(TemplateError, "cannot serve settled"):
            plan_send(KEY, FakeRpc([utxo(1, 50_000, seeds=1_000)],
                                   balances=False), RECIPIENT, 900, 1.0)
        with self.assertRaisesRegex(TemplateError, "dust bound"):
            plan_send(KEY, FakeRpc([utxo(1, 50_000, seeds=1_000)]),
                      DEST_PKH, 900, 1.0)   # 330 < legacy dust 546
        with self.assertRaisesRegex(TemplateError, "own address"):
            plan_send(KEY, FakeRpc([utxo(1, 50_000, seeds=1_000)]),
                      KEY.address("main"), 900, 1.0)

    def test_send_to_wpkh_is_fine(self):
        # 330 sats clears the 294-sat wpkh bound.
        plan = plan_send(KEY, FakeRpc([utxo(1, 50_000, seeds=1_000)]),
                         DEST_WPKH, 900, 1.0)
        self.assertEqual(plan.psbt.tx.outputs[2].value, CARRIER_SATS)

    def test_sign_and_tamper(self):
        rpc = FakeRpc([utxo(1, 50_000, seeds=1_000)])
        plan = plan_send(KEY, rpc, RECIPIENT, 900, 1.0)
        tx = sign_send(plan, KEY)
        prevouts = plan.psbt.prevouts()
        for n in range(len(tx.inputs)):
            self.assertTrue(verify_input(tx, prevouts, n,
                                         tx.inputs[n].witness[0]))
        plan2 = plan_send(KEY, rpc, RECIPIENT, 900, 1.0)
        out2 = plan2.psbt.tx.outputs[2]
        plan2.psbt.tx.outputs[2] = TxOut(out2.value + 1, out2.script_pubkey)
        with self.assertRaisesRegex(TemplateError, "exactly 330"):
            sign_send(plan2, KEY)

    def test_verify_refuses_claimed_coverage_lie(self):
        rpc = FakeRpc([utxo(1, 50_000, seeds=1_000)])
        plan = plan_send(KEY, rpc, RECIPIENT, 900, 1.0)
        lying = plan.summary.__class__(
            plan.summary.inputs, plan.summary.input_sats, 100,   # < amount
            plan.summary.amount_seeds, plan.summary.change_sats,
            plan.summary.carrier_sats, plan.summary.fee_sats)
        with self.assertRaisesRegex(TemplateError, "do not cover"):
            verify_send(plan.psbt.tx, plan.psbt.prevouts(), KEY.script,
                        plan.recipient_script, lying)


class WithdrawTests(unittest.TestCase):
    def test_withdraw_prefers_claimed_coins(self):
        rpc = FakeRpc([utxo(1, 60_000, latent=60_000),
                       utxo(2, 50_000)])
        plan = plan_withdraw(KEY, rpc, DEST_WPKH, 30_000, 1.0)
        self.assertEqual(plan.summary.inputs, (f"{2:064x}:2",))
        self.assertEqual(plan.latent_sats, 0)
        env = decipher(plan.psbt.tx)
        self.assertIsInstance(env, Envelope)
        self.assertEqual(env.pointer, 1)
        self.assertFalse(env.land)
        self.assertEqual(env.edicts, [])

    def test_withdraw_warns_when_latent_coins_are_pulled_in(self):
        rpc = FakeRpc([utxo(1, 60_000, latent=60_000), utxo(2, 5_000)])
        plan = plan_withdraw(KEY, rpc, DEST_WPKH, 30_000, 1.0)
        self.assertEqual(plan.latent_sats, 60_000)

    def test_withdraw_sign_and_tamper(self):
        rpc = FakeRpc([utxo(1, 60_000)])
        plan = plan_withdraw(KEY, rpc, DEST_WPKH, 30_000, 1.0)
        tx = sign_withdraw(plan, KEY)
        self.assertEqual(tx.outputs[2].value, 30_000)
        plan2 = plan_withdraw(KEY, rpc, DEST_WPKH, 30_000, 1.0)
        # The attack the envelope exists for: drop the pointer.
        from claimstones.protocol.codec import encipher
        plan2.psbt.tx.outputs[0] = TxOut(0, encipher(Envelope()))
        with self.assertRaisesRegex(TemplateError, "pointer"):
            sign_withdraw(plan2, KEY)

    def test_withdraw_refusals(self):
        rpc = FakeRpc([utxo(1, 10_000)])
        with self.assertRaisesRegex(TemplateError, "not enough sats"):
            plan_withdraw(KEY, rpc, DEST_WPKH, 50_000, 1.0)
        with self.assertRaisesRegex(TemplateError, "dust bound"):
            plan_withdraw(KEY, rpc, DEST_WPKH, 100, 1.0)


class CliSpendTests(unittest.TestCase):
    def test_send_wording_and_flow(self):
        rpc = FakeRpc([utxo(1, 50_000, seeds=1_000)])
        out = io.StringIO()
        rc = do_send(rpc, KEY, "main", RECIPIENT, "9.00", 1.0, out,
                     ask=lambda _: "y")
        self.assertEqual(rc, 0)
        self.assertEqual(len(rpc.sent), 1)
        text = out.getvalue()
        self.assertIn("send 9.00 orchids", text)
        self.assertIn("miner fee", text)
        self.assertIn("sent: ", text)
        self.assertNotIn("worth", text)

    def test_withdraw_wording(self):
        rpc = FakeRpc([utxo(1, 60_000, latent=60_000)])
        out = io.StringIO()
        rc = do_withdraw(rpc, KEY, "main", DEST_WPKH, 30_000, 1.0, out,
                         ask=lambda _: "n")
        self.assertEqual(rc, 0)
        self.assertEqual(rpc.sent, [])
        text = out.getvalue()
        self.assertIn("your orchids stay in this wallet;"
                      " only bitcoin leaves", text)
        self.assertIn("claim first?", text)
        self.assertIn("nothing sent", text)

    def test_password_is_the_last_step(self):
        # After the yes, a failed password check sends nothing; asked
        # only after the yes, never for a no.
        for do, args in ((do_send, (RECIPIENT, "9.00", 1.0)),
                         (do_withdraw, (DEST_WPKH, 30_000, 1.0))):
            rpc = FakeRpc([utxo(1, 60_000, seeds=1_000)])
            asked = []
            out = io.StringIO()
            rc = do(rpc, KEY, "main", *args, out, ask=lambda _: "y",
                    confirm=lambda: asked.append(1) or False)
            self.assertEqual((rc, rpc.sent, asked), (0, [], [1]))
            self.assertIn("nothing sent", out.getvalue())
            do(rpc, KEY, "main", *args, io.StringIO(), ask=lambda _: "n",
               confirm=lambda: asked.append(1) or False)
            self.assertEqual(asked, [1])
            rc = do(rpc, KEY, "main", *args, io.StringIO(), ask=lambda _: "y",
                    confirm=lambda: True)
            self.assertEqual((rc, len(rpc.sent)), (0, 1))


class WalletLockTests(unittest.TestCase):
    def test_lock_round_trip(self):
        line = lock_line(KEY, "hunter2")
        self.assertEqual(unlock_line(line, "hunter2").secret, KEY.secret)
        with self.assertRaisesRegex(WalletKeyError, "wrong password"):
            unlock_line(line, "hunter3")

    def test_tampered_line_is_refused(self):
        salt_hex = lock_line(KEY, "pw").split()[1]
        line = lock_line(KEY, "pw").split()
        line[2] = ("00" * 32)
        with self.assertRaisesRegex(WalletKeyError, "wrong password"):
            unlock_line(" ".join(line), "pw")
        self.assertTrue(salt_hex)   # two locks, two salts
        self.assertNotEqual(lock_line(KEY, "pw"), lock_line(KEY, "pw"))

    def test_locked_wallet_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "wallet.code")
            save_wallet(path, KEY, "hunter2")
            with self.assertRaises(WalletLocked):
                load_wallet(path)
            self.assertEqual(load_wallet(path, "hunter2").secret, KEY.secret)
            with self.assertRaisesRegex(WalletKeyError, "wrong password"):
                load_wallet(path, "nope")
            # A file written plain before passwords were required
            # (2026-11-14): its password is empty, nothing else opens it.
            plain = os.path.join(folder, "plain.code")
            with open(plain, "w", encoding="ascii") as f:
                f.write(f"# claimstones wallet code\n{CODE}\n")
            self.assertFalse(is_locked(plain))
            for pw in (None, ""):
                self.assertEqual(load_wallet(plain, pw).secret, KEY.secret)
            with self.assertRaisesRegex(WalletKeyError, "press Enter"):
                load_wallet(plain, "hunter2")

    def test_change_password(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "wallet.code")
            with open(path, "w", encoding="ascii") as f:
                f.write(f"{CODE}\n")
            with self.assertRaisesRegex(WalletKeyError, "at least 6"):
                change_password(path, "", "short")
            with self.assertRaises(WalletKeyError):
                change_password(path, "wrong1", "hunter2")
            self.assertFalse(is_locked(path))            # both left it plain
            change_password(path, "", "hunter2")         # the plain file locked
            self.assertTrue(is_locked(path))
            with open(path, encoding="ascii") as f:
                self.assertNotIn(CODE, f.read())
            with self.assertRaisesRegex(WalletKeyError, "wrong password"):
                change_password(path, "", "secret1")
            change_password(path, "hunter2", "secret1")
            self.assertEqual(load_wallet(path, "secret1").secret, KEY.secret)
            self.assertEqual(os.listdir(folder), ["wallet.code"])   # no leftover


if __name__ == "__main__":
    unittest.main()
