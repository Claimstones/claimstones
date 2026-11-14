"""claimstones-wallet — the reference wallet, a menu-loop CLI.

The release-1 feature set of wallet-policy.md §What a wallet can do,
grown one command at a time and now complete: Check (watch-only — any
address, xpub or descriptor), Receive, Balance, Claim, Send, Withdraw,
the external claim and collect (build the PSBT for UTXOs another
wallet holds; that wallet signs and broadcasts), and backup by wallet
code. The wallet file can be locked under a password; the wallet code
alone restores either way.

It speaks the indexer RPC (spec/indexer-rpc.md) and trusts it only for
what it shows; everything the wallet ever signs is built and verified
locally (wallet/land.py, wallet-policy.md §Envelope scan).

    claimstones-wallet                                  # the menu
    claimstones-wallet check bc1q... zpub6r...          # one check, exit
    claimstones-wallet claim 2                          # claim at 2 sat/vB
    claimstones-wallet collect bc1q... 2                # build the PSBT
    claimstones-wallet send bc1p... 12.34 2
    claimstones-wallet withdraw bc1q... 50000 2
    claimstones-wallet address | balance | backup
    ... -rpcurl http://... -rpccookie <path>/rpc.cookie -wallet <file>
    ... -log <file>          # append the session to a file, for testing
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import sys

import getpass

from claimstones.bitcoin.descriptors import CheckInputError
from claimstones.wallet.check import CheckReport, orchids, parse_orchids, run_check
from claimstones.wallet.claim import ClaimPlan, plan_claim, sign_claim
from claimstones.wallet.collect import CollectPlan, plan_collect
from claimstones.wallet.keys import (
    WalletKey, WalletKeyError, WalletLocked, load_wallet, save_wallet,
)
from claimstones.wallet.land import TemplateError
from claimstones.wallet.rpc import (
    DEFAULT_URL, IndexerRpc, RpcUnreachable, WalletRpcError,
)
from claimstones.wallet.spend import (
    SendPlan, WithdrawPlan, plan_send, plan_withdraw, sign_send,
    sign_withdraw,
)

DEFAULT_WALLET = os.path.join(os.path.expanduser("~"),
                              ".claimstones", "wallet.code")

_SHRINKS = "unclaimed orchids do not expire, but shrink about 3 % a month"
_RULE = "-" * 54


# ---------------------------------------------------------------------------
# -log: the session as the terminal showed it, secrets left out
# ---------------------------------------------------------------------------

class _Stdout:
    """sys.stdout as it is when written to. The `out=` defaults below
    bind this, not the stream itself, so the -log tee installed after
    import still sees them."""

    def write(self, s: str) -> int:
        return sys.stdout.write(s)

    def flush(self) -> None:
        sys.stdout.flush()


STDOUT = _Stdout()


class _Tee:
    """A terminal stream that also appends to the -log file. It has no
    fileno, so input() prompts through sys.stdout and reads
    sys.stdin.readline() instead of the console directly: the prompt and
    the answer both pass through here and the log reads like the
    screen."""

    def __init__(self, stream, log):
        self.stream, self.log, self.paused = stream, log, False

    def write(self, s: str) -> int:
        n = self.stream.write(s)
        self._log(s)
        return n

    def readline(self, *size) -> str:
        line = self.stream.readline(*size)
        self._log(line)
        return line

    def __iter__(self):
        return iter(self.readline, "")

    def _log(self, s: str) -> None:
        if s and not self.paused:
            self.log.write(s)
            self.log.flush()

    def flush(self) -> None:
        self.stream.flush()

    def isatty(self) -> bool:
        return self.stream.isatty()

    @property
    def encoding(self) -> str:
        return self.stream.encoding


@contextlib.contextmanager
def _unlogged(stand_in: str):
    """Nothing written or typed in the block reaches the -log file;
    `stand_in` takes its place. For the wallet code and the password: a
    log is a file people copy around, and the code alone owns the
    orchids. Without -log, a no-op."""
    tees = [s for s in (sys.stdin, sys.stdout, sys.stderr) if isinstance(s, _Tee)]
    for t in tees:
        t.paused = True
    try:
        yield
    finally:
        for t in tees:
            t.paused = False
        if tees:
            tees[0].log.write(stand_in + "\n")
            tees[0].log.flush()


def _utxos(n: int) -> str:
    """UTXO, not coin: the CLI reader knows the word, and "1 coin" reads
    as a quantity (decided 2026-11-13)."""
    return f"{n:,} UTXO{'s' if n != 1 else ''}"


def _abbr(address: str) -> str:
    """An address as a person recognizes it: head, dots, tail."""
    return address if len(address) <= 16 else f"{address[:6]}....{address[-5:]}"


def _short(outpoint: str) -> str:
    txid, _, vout = outpoint.rpartition(":")
    return f"{txid[:10]}..{txid[-6:]}:{vout}"


def print_report(report: CheckReport, out=STDOUT) -> None:
    w = out.write
    w(f"\n{_utxos(len(report.utxos))} "
      f"found ({report.branches} branch{'es' if report.branches != 1 else ''}, "
      f"{report.scripts} script{'s' if report.scripts != 1 else ''} asked)\n")
    if not report.utxos:
        w("\n")
        return
    source = None
    for row in report.utxos:
        if row.source != source:
            source = row.source
            label = source if len(source) <= 64 else source[:61] + "..."
            w(f"\n  {label}\n")
        cost = (f"est. orchids to be landed {orchids(row.seeds)}"
                if row.seeds is not None else "")
        w(f"    {_short(row.outpoint):<32} {row.sats:>12,} sats"
          f" {row.latent_sats:>12,} unclaimed   {cost}\n")
    w(f"\n  total {report.total_sats:>29,} sats"
      f" {report.total_latent_sats:>12,} unclaimed")
    if report.total_seeds is not None:
        w(f"   est. orchids to be landed {orchids(report.total_seeds)}")
    w("\n")
    if report.valued_at_height is not None:
        w(f"  (valued at height {report.valued_at_height:,}; {_SHRINKS})\n")
    if report.no_node:
        w("  (the server has no host node, so the orchids to be landed are"
          " not shown; the UTXOs and their runs are exact)\n")
    w("\n")


def do_check(rpc: IndexerRpc, network: str, lines: list[str], out=STDOUT) -> int:
    try:
        report = run_check(lines, rpc, network)
    except CheckInputError as e:
        print(f"cannot read that: {e}", file=sys.stderr)
        return 1
    except WalletRpcError as e:
        print(f"the server refused: {e.message}", file=sys.stderr)
        return 1
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
        return 1
    print_report(report, out)
    return 0


def print_claim_plan(plan: ClaimPlan, out=STDOUT,
                     fee_rate: float | None = None) -> None:
    """The same shape as the collect plan (wallet-policy.md §Wording):
    sats in and unclaimed, the orchids to be landed, what is paid."""
    w = out.write
    s = plan.summary
    w(f"\n{_RULE}\nOn this wallet's address:\n")
    w(f"{s.input_sats:,} sats in, {plan.latent_sats:,} of them unclaimed\n")
    if plan.skipped:
        w(f"({_utxos(plan.skipped)} on the address with nothing left to"
          " land stay out of this transaction)\n")
    if plan.seeds is not None:
        w(f"est. orchids to be landed: {orchids(plan.seeds)} (valued at"
          f" height {plan.valued_at_height:,}); they land on this wallet's"
          " address\n")
    else:
        w("(the server has no host node, so the orchids to be landed are"
          " not shown)\n")
    rate = f" ({fee_rate:g} sat/vB)" if fee_rate is not None else ""
    w(f"\nThe miner fee{rate}: {s.fee_sats:,} sats; remaining"
      f" {s.change_sats:,} sats return to this wallet's address\n")
    w("claiming does not spend your bitcoin; you pay a network fee\n")


def do_claim(rpc: IndexerRpc, key: WalletKey, fee_rate: float,
             out=STDOUT, ask=input) -> int:
    try:
        plan = plan_claim(key, rpc, fee_rate)
    except TemplateError as e:
        print(f"cannot claim: {e}", file=sys.stderr)
        return 1
    except WalletRpcError as e:
        print(f"the server refused: {e.message}", file=sys.stderr)
        return 1
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
        return 1
    print_claim_plan(plan, out, fee_rate)
    try:
        answer = ask("claim? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        out.write("nothing sent\n")
        return 0
    try:
        tx = sign_claim(plan, key)
        txid = rpc.sendrawtransaction(tx.serialize().hex())
    except TemplateError as e:
        print(f"refusing to sign: {e}", file=sys.stderr)
        return 1
    except WalletRpcError as e:
        print(f"the server refused the transaction: {e.message}",
              file=sys.stderr)
        return 1
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
        return 1
    out.write(f"claim sent: {txid}\n")
    return 0


def print_collect_plan(plan: CollectPlan, out=STDOUT) -> None:
    """What is on the funding address and what landing it yields; what
    the signer pays follows the file names, in do_collect."""
    w = out.write
    s = plan.summary
    w(f"\n{_RULE}\nOn address {plan.funding_address}:\n")
    w(f"{s.input_sats:,} sats in, {plan.latent_sats:,} of them unclaimed\n")
    if plan.seeds is not None:
        w(f"est. orchids to be landed: {orchids(plan.seeds)} (valued at"
          f" height {plan.valued_at_height:,}); they arrive on this"
          " wallet's address\n")
    else:
        w("(the server has no host node, so the orchids to be landed are"
          " not shown)\n")


def do_collect(rpc: IndexerRpc, key: WalletKey, network: str,
               funding_address: str, fee_rate: float,
               out_dir: str = ".", out=STDOUT) -> int:
    """out_dir is where the PSBT lands; the callers pass the wallet
    file's folder, so the file is found where the wallet is, not in
    whatever directory the terminal happened to be in."""
    try:
        plan = plan_collect(key.script, funding_address, rpc, fee_rate,
                            network)
    except TemplateError as e:
        print(f"cannot build: {e}", file=sys.stderr)
        return 1
    except WalletRpcError as e:
        print(f"the server refused: {e.message}", file=sys.stderr)
        return 1
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
        return 1
    print_collect_plan(plan, out)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.abspath(os.path.join(out_dir, f"collect-{stamp}"))
    with open(base + ".psbt", "w", encoding="ascii", newline="\n") as f:
        f.write(plan.psbt.to_base64() + "\n")
    w = out.write
    s = plan.summary
    short = _abbr(funding_address)
    w(f"\nPSBT written to {base}.psbt\n")
    w(f"The signer pays the miner fee ({fee_rate:g} sat/vB) plus"
      f" {s.carrier_sats} sats (to this wallet): {s.fee_sats:,} +"
      f" {s.carrier_sats} = {s.fee_sats + s.carrier_sats:,} sats\n")
    w(f"Remaining {s.return_sats:,} sats will return to {short}\n")
    w(f"\nCheck, sign & broadcast in the wallet that holds address {short}\n")
    w("to raise the fee later, use CPFP in your other wallet: spend the"
      " returned UTXO at a higher rate\n")
    w("do not use Increase fee (RBF): a wallet that did not build this"
      " transaction rebuilds it and drops the envelope — the claim"
      " silently does not happen\n")
    return 0


def do_receive(key: WalletKey, network: str, out=STDOUT) -> int:
    out.write(f"\nwallet address: {key.address(network)}\n")
    out.write("  bitcoin sent here stays yours and becomes claimable;"
              " orchids sent by\n  another wallet arrive as balance —"
              " one address, reused\n\n")
    return 0


def do_balance(rpc: IndexerRpc, key: WalletKey, network: str,
               out=STDOUT) -> int:
    """The wallet's own UTXOs: the check report on the wallet address,
    then the landed orchids the index serves — shown apart, as
    wallet-policy.md §UTXO states asks. Until the ledger half exists
    the server has no landed balances, and this says so."""
    rc = do_check(rpc, network, [key.address(network)], out)
    if rc:
        return rc
    try:
        balance = rpc.getorchidbalance(key.script.hex())
    except WalletRpcError:
        out.write("  landed orchids: not served yet by this index"
                  " (indexer-rpc.md §Planned)\n\n")
        return 0
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
        return 1
    seeds = sum(u.get("seeds", 0) for u in balance.get("utxos", []))
    out.write(f"  landed orchids {orchids(seeds)}\n\n")
    return 0


def do_backup(key: WalletKey, out=STDOUT) -> int:
    out.write("\nyour wallet code — write it down somewhere safe; the"
              " code alone restores\nthe wallet, and anyone who knows"
              " it owns the orchids:\n")
    with _unlogged("  <wallet code not logged>"):
        out.write(f"  {key.code}\n")
    out.write("\n")
    return 0


def print_send_plan(plan: SendPlan, out=STDOUT) -> None:
    w = out.write
    s = plan.summary
    w(f"\nsend {orchids(s.amount_seeds)} orchids to {plan.recipient}\n")
    w(f"  {_utxos(len(s.inputs))} in,"
      f" carrying {orchids(s.input_seeds)} orchids settled\n")
    w(f"  the recipient's UTXO carries {s.carrier_sats} sats; the"
      f" {s.fee_sats:,}-sat miner fee\n  comes from this wallet's own"
      f" sats; {s.change_sats:,} sats and the remaining\n  orchids"
      " return to the wallet address\n")


def do_send(rpc: IndexerRpc, key: WalletKey, network: str, recipient: str,
            amount_text: str, fee_rate: float, out=STDOUT,
            ask=input) -> int:
    try:
        amount_seeds = parse_orchids(amount_text)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    try:
        plan = plan_send(key, rpc, recipient, amount_seeds, fee_rate, network)
    except TemplateError as e:
        print(f"cannot send: {e}", file=sys.stderr)
        return 1
    except WalletRpcError as e:
        print(f"the server refused: {e.message}", file=sys.stderr)
        return 1
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
        return 1
    print_send_plan(plan, out)
    try:
        answer = ask("send? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        out.write("nothing sent\n")
        return 0
    try:
        tx = sign_send(plan, key)
        txid = rpc.sendrawtransaction(tx.serialize().hex())
    except TemplateError as e:
        print(f"refusing to sign: {e}", file=sys.stderr)
        return 1
    except (WalletRpcError, RpcUnreachable) as e:
        print(f"the server refused the transaction: {e}", file=sys.stderr)
        return 1
    out.write(f"sent: {txid}\n")
    return 0


def print_withdraw_plan(plan: WithdrawPlan, out=STDOUT) -> None:
    w = out.write
    s = plan.summary
    w(f"\nwithdraw {s.amount_sats:,} sats to {plan.destination}\n")
    w(f"  {_utxos(len(s.inputs))} in,"
      f" fee {s.fee_sats:,} sats, {s.change_sats:,} sats stay on the"
      " wallet address\n")
    w("  your orchids stay in this wallet; only bitcoin leaves\n")
    if plan.latent_sats:
        w(f"  CAUTION: {plan.latent_sats:,} of the selected sats are still"
          " unclaimed;\n  their orchids would go to the destination"
          " — claim first?\n")


def do_withdraw(rpc: IndexerRpc, key: WalletKey, network: str,
                destination: str, amount_sats: int, fee_rate: float,
                out=STDOUT, ask=input) -> int:
    try:
        plan = plan_withdraw(key, rpc, destination, amount_sats, fee_rate,
                             network)
    except TemplateError as e:
        print(f"cannot withdraw: {e}", file=sys.stderr)
        return 1
    except WalletRpcError as e:
        print(f"the server refused: {e.message}", file=sys.stderr)
        return 1
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
        return 1
    print_withdraw_plan(plan, out)
    prompt = ("withdraw anyway? [y/N] " if plan.latent_sats
              else "withdraw? [y/N] ")
    try:
        answer = ask(prompt)
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        out.write("nothing sent\n")
        return 0
    try:
        tx = sign_withdraw(plan, key)
        txid = rpc.sendrawtransaction(tx.serialize().hex())
    except TemplateError as e:
        print(f"refusing to sign: {e}", file=sys.stderr)
        return 1
    except (WalletRpcError, RpcUnreachable) as e:
        print(f"the server refused the transaction: {e}", file=sys.stderr)
        return 1
    out.write(f"withdrawal sent: {txid}\n")
    return 0


def _ask_secret(prompt: str) -> str:
    """getpass on a terminal; plain stdin when piped (scripts, tests) —
    on Windows getpass reads the console directly and would hang a
    piped run forever. Never in the -log file."""
    with _unlogged(f"{prompt}<not logged>"):
        if sys.stdin.isatty():
            return getpass.getpass(prompt)
        try:
            return input(prompt)
        except EOFError:
            return ""


def _unlock(path: str) -> WalletKey:
    """Load the wallet, prompting for the password when the file is
    locked; an empty password gives up."""
    try:
        return load_wallet(path)
    except WalletLocked:
        pass
    while True:
        pw = _ask_secret("wallet password> ")
        if not pw:
            raise WalletKeyError("the wallet stays locked")
        try:
            return load_wallet(path, pw)
        except WalletKeyError as e:
            print(e)


def _ask_new_password() -> str | None:
    """A password for a new wallet file; empty = none. Returns None
    when the two entries disagree."""
    pw = _ask_secret("password for the wallet file (empty for none)> ")
    if not pw:
        return ""
    again = _ask_secret("the same password again> ")
    if pw != again:
        print("the passwords do not match")
        return None
    return pw


def _create_wallet(path: str, network: str) -> WalletKey | None:
    pw = _ask_new_password()
    if pw is None:
        return None
    key = WalletKey.new()
    try:
        save_wallet(path, key, pw or None)
    except WalletKeyError as e:
        print(f"cannot save the wallet: {e}")
        return None
    print(f"wallet saved to {path}" + (" (locked)" if pw else ""))
    print("your wallet code — write it down somewhere safe; the code"
          " alone restores the wallet" + (", password or not" if pw else "")
          + ":")
    with _unlogged("  <wallet code not logged>"):
        print(f"  {key.code}")
    print(f"wallet address: {key.address(network)}")
    return key


def _restore_wallet(path: str, network: str) -> WalletKey | None:
    prompt = "wallet code (orchid1...)> "
    try:
        with _unlogged(f"{prompt}<not logged>"):
            code = input(prompt).strip()
    except EOFError:
        return None
    if not code:
        return None
    try:
        key = WalletKey.from_code(code)
    except WalletKeyError as e:
        print(f"cannot restore: {e}")
        return None
    pw = _ask_new_password()
    if pw is None:
        return None
    try:
        save_wallet(path, key, pw or None)
    except WalletKeyError as e:
        print(f"cannot restore: {e}")
        return None
    print(f"wallet saved to {path}" + (" (locked)" if pw else ""))
    print(f"wallet address: {key.address(network)}")
    return key


def _prompt_lines() -> list[str]:
    """Addresses, xpubs or descriptors, one per line. The prompt after
    each line says how to finish: a pasted xpub left people waiting on
    a check that had not started (signet, 2026-11-13)."""
    print("Paste an address, xpub or descriptor:")
    lines = []
    while True:
        try:
            line = input("  " if not lines
                         else "  (another, or press Enter to run) ").strip()
        except EOFError:
            break
        if not line:
            break
        lines.append(line)
    return lines


def _beside(wallet_path: str) -> str:
    """The wallet file's folder: where collect writes its PSBT."""
    return os.path.dirname(os.path.abspath(wallet_path))


def menu(rpc: IndexerRpc, network: str, wallet_path: str) -> int:
    try:
        info = rpc.getindexinfo()
        state = f"index at height {info['height']:,}"
        if info.get("activation_height") is None:
            state += ", activation not yet decided"
    except (WalletRpcError, RpcUnreachable, OSError) as e:
        state = f"server not answering ({e})"
    key = None
    try:
        key = _unlock(wallet_path)
    except FileNotFoundError:
        pass
    except WalletKeyError as e:
        print(f"cannot open the wallet {wallet_path}: {e}")
    print(f"claimstones wallet — {rpc.url}, {state}")
    if key is not None:
        print(f"wallet address: {key.address(network)}")
    # The menu prints once, and again on [m] or when its items change
    # (a wallet appears); between commands a one-line prompt keeps the
    # command's output on screen.
    show_menu = True
    while True:
        if show_menu:
            items = ["  [c] check any bitcoin — address, xpub or descriptor,"
                     " watch-only"]
            if key is not None:
                items.append("  [a] receive — the wallet address")
                items.append("  [l] balance — the wallet's own UTXOs")
                items.append("  [k] claim — land the unclaimed orchids on"
                             " the wallet address")
                items.append("  [s] send orchids    [w] withdraw bitcoin")
                items.append("  [e] external claim and collect — harvest"
                             " UTXOs another wallet holds")
                items.append("  [o] backup — show the wallet code")
            else:
                items.append("  [n] new wallet    [r] restore from wallet"
                             " code")
            items.append("  [q] quit")
            print("\n" + "\n".join(items))
            show_menu = False
        else:
            print("\nWaiting for next command, [q] quit   [m] show menu")
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
        if choice in ("m", "menu"):
            show_menu = True
            continue
        if choice in ("c", "check"):
            lines = _prompt_lines()
            if lines:
                do_check(rpc, network, lines)
            continue
        if choice in ("k", "claim") and key is not None:
            try:
                raw = input("fee rate (sat/vB)> ").strip()
            except EOFError:
                continue
            try:
                fee_rate = float(raw)
            except ValueError:
                print(f"not a number: {raw}")
                continue
            do_claim(rpc, key, fee_rate)
            continue
        if choice in ("a", "receive", "address") and key is not None:
            do_receive(key, network)
            continue
        if choice in ("l", "balance") and key is not None:
            do_balance(rpc, key, network)
            continue
        if choice in ("o", "backup") and key is not None:
            do_backup(key)
            continue
        if choice in ("s", "send") and key is not None:
            try:
                recipient = input("recipient address> ").strip()
                amount = input("orchids to send (e.g. 12.34)> ").strip()
                raw = input("fee rate (sat/vB)> ").strip()
            except EOFError:
                continue
            if not (recipient and amount):
                continue
            try:
                fee_rate = float(raw)
            except ValueError:
                print(f"not a number: {raw}")
                continue
            do_send(rpc, key, network, recipient, amount, fee_rate)
            continue
        if choice in ("w", "withdraw") and key is not None:
            try:
                dest = input("destination address> ").strip()
                amount = input("amount in sats> ").strip()
                raw = input("fee rate (sat/vB)> ").strip()
            except EOFError:
                continue
            if not (dest and amount):
                continue
            try:
                sats = int(amount.replace(",", ""))
                fee_rate = float(raw)
            except ValueError:
                print("the amount is whole sats and the fee rate a number")
                continue
            do_withdraw(rpc, key, network, dest, sats, fee_rate)
            continue
        if choice in ("e", "collect") and key is not None:
            try:
                addr = input("funding address (holds the bitcoin)> ").strip()
                raw = input("fee rate (sat/vB)> ").strip()
            except EOFError:
                continue
            if not addr:
                continue
            try:
                fee_rate = float(raw)
            except ValueError:
                print(f"not a number: {raw}")
                continue
            do_collect(rpc, key, network, addr, fee_rate,
                       _beside(wallet_path))
            continue
        if choice in ("n", "new") and key is None:
            key = _create_wallet(wallet_path, network)
            show_menu = key is not None
            continue
        if choice in ("r", "restore") and key is None:
            key = _restore_wallet(wallet_path, network)
            show_menu = key is not None
            continue
        if choice:
            print(f"no such command: {choice}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="claimstones-wallet",
        description="the reference wallet (docs/spec/wallet-policy.md)")
    ap.add_argument("-rpcurl", default=DEFAULT_URL)
    ap.add_argument("-rpccookie", help="the server's rpc.cookie file")
    ap.add_argument("-rpcuser")
    ap.add_argument("-rpcpassword")
    ap.add_argument("-network", default="main",
                    choices=["main", "test", "signet", "regtest"])
    ap.add_argument("-wallet", default=DEFAULT_WALLET,
                    help="the wallet file (default %(default)s)")
    ap.add_argument("command", nargs="?",
                    choices=["check", "claim", "collect", "send",
                             "withdraw", "address", "balance", "backup"])
    ap.add_argument("lines", nargs="*", metavar="line",
                    help="check: addresses, xpubs or descriptors;"
                         " claim: fee rate (sat/vB);"
                         " collect: funding address + fee rate;"
                         " send: address + orchids + fee rate;"
                         " withdraw: address + sats + fee rate")
    ap.add_argument("-log", metavar="FILE",
                    help="append the session to FILE as the terminal shows"
                         " it, answers included; the wallet code and the"
                         " password are never written")
    args = ap.parse_args(argv)
    if args.log is None:
        return _run(args)

    path = os.path.abspath(args.log)
    what = " ".join([args.command] + args.lines) if args.command else "menu"
    saved = sys.stdin, sys.stdout, sys.stderr
    with open(path, "a", encoding="utf-8") as log:
        log.write(f"\n--- {datetime.datetime.now().isoformat(timespec='seconds')}"
                  f"  claimstones-wallet -network {args.network}"
                  f" -rpcurl {args.rpcurl}"
                  f" -wallet {os.path.abspath(args.wallet)}  {what}\n")
        sys.stdin, sys.stdout, sys.stderr = (_Tee(s, log) for s in saved)
        rc = 1
        try:
            print(f"logging to {path}")
            rc = _run(args)
            return rc
        finally:
            sys.stdout.flush()
            sys.stdin, sys.stdout, sys.stderr = saved
            log.write(f"--- exit {rc}\n")


def _run(args) -> int:
    credential = (f"{args.rpcuser}:{args.rpcpassword or ''}"
                  if args.rpcuser is not None else None)
    rpc = IndexerRpc(args.rpcurl, cookie_path=args.rpccookie,
                     credential=credential)

    if args.command == "check":
        lines = args.lines or [l.strip() for l in sys.stdin if l.strip()]
        return do_check(rpc, args.network, lines)
    if args.command is None:
        return menu(rpc, args.network, args.wallet)

    # Everything else needs the wallet.
    usage = {"claim": "claim <fee rate in sat/vB>",
             "collect": "collect <funding address> <fee rate>",
             "send": "send <address> <orchids> <fee rate>",
             "withdraw": "withdraw <address> <sats> <fee rate>",
             "address": "address", "balance": "balance", "backup": "backup"}
    want = {"claim": 1, "collect": 2, "send": 3, "withdraw": 3,
            "address": 0, "balance": 0, "backup": 0}
    if len(args.lines) != want[args.command]:
        print(f"usage: claimstones-wallet {usage[args.command]}",
              file=sys.stderr)
        return 2
    fee_rate = None
    if want[args.command]:
        try:
            fee_rate = float(args.lines[-1])
        except ValueError:
            print(f"not a fee rate: {args.lines[-1]}", file=sys.stderr)
            return 2
    try:
        key = _unlock(args.wallet)
    except FileNotFoundError:
        print(f"no wallet at {args.wallet}; the menu's [n] creates one",
              file=sys.stderr)
        return 1
    except WalletKeyError as e:
        print(f"cannot open the wallet {args.wallet}: {e}", file=sys.stderr)
        return 1
    if args.command == "address":
        return do_receive(key, args.network)
    if args.command == "balance":
        return do_balance(rpc, key, args.network)
    if args.command == "backup":
        return do_backup(key)
    if args.command == "claim":
        return do_claim(rpc, key, fee_rate)
    if args.command == "collect":
        return do_collect(rpc, key, args.network, args.lines[0], fee_rate,
                          _beside(args.wallet))
    if args.command == "send":
        return do_send(rpc, key, args.network, args.lines[0], args.lines[1],
                       fee_rate)
    try:
        sats = int(args.lines[1].replace(",", ""))
    except ValueError:
        print(f"the amount is whole sats: {args.lines[1]}", file=sys.stderr)
        return 2
    return do_withdraw(rpc, key, args.network, args.lines[0], sats, fee_rate)


if __name__ == "__main__":
    sys.exit(main())
