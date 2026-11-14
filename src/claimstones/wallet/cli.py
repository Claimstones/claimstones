"""claimstones-wallet — the reference wallet, a menu-loop CLI.

The release-1 feature set of wallet-policy.md §What a wallet can do,
grown one command at a time and now complete: Check (watch-only — any
address, xpub or descriptor), Receive, Balance, Claim, Send, Withdraw,
the external claim and collect (build the PSBT for UTXOs another
wallet holds; that wallet signs and broadcasts), and backup by wallet
code. The wallet file is locked under a password, asked when the
wallet opens and again before the code is shown and as the last step
of Send and Withdraw; the wallet code alone restores, password or not.

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
import functools
import os
import re
import sys

import getpass

from claimstones.bitcoin.descriptors import CheckInputError
from claimstones.wallet.check import CheckReport, orchids, parse_orchids, run_check
from claimstones.wallet.claim import ClaimPlan, plan_claim, sign_claim
from claimstones.wallet.collect import CollectPlan, plan_collect
from claimstones.wallet.keys import (
    WalletKey, WalletKeyError, WalletLocked, change_password,
    check_new_password, is_locked, load_wallet, save_wallet,
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

    def isatty(self) -> bool:
        return sys.stdout.isatty()


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
            self.log.write(_OSC8.sub("", s))     # links: the text only
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


# ---------------------------------------------------------------------------
# txids as terminal hyperlinks to an explorer
# ---------------------------------------------------------------------------

_EXPLORER = {"main": "https://mempool.space/tx/",
             "test": "https://mempool.space/testnet/tx/",
             "signet": "https://mempool.space/signet/tx/"}     # regtest: none
_OSC8 = re.compile(r"\x1b\]8;;[^\x1b]*\x1b\\")


@functools.cache
def _vt_on() -> bool:
    """Whether the console takes escape sequences; on Windows, switch it
    on (ENABLE_VIRTUAL_TERMINAL_PROCESSING), as Python does not."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(mode.value & 4) or bool(k.SetConsoleMode(h, mode.value | 4))
    except (AttributeError, OSError):
        return False


def _link(txid: str, network: str, out, text: str | None = None) -> str:
    """`text` (the txid itself by default) as an OSC 8 hyperlink to the
    transaction on mempool.space -- on a terminal only, where it shows
    as the text and opens on a click; piped, in the -log file, or on
    regtest it is plain text."""
    text = text or txid
    base = _EXPLORER.get(network)
    try:
        tty = out.isatty()
    except (AttributeError, ValueError):
        tty = False
    if base is None or not tty or not _vt_on():
        return text
    return f"\x1b]8;;{base}{txid}\x1b\\{text}\x1b]8;;\x1b\\"


def print_report(report: CheckReport, out=STDOUT, network: str = "main",
                 settled: int | str | None = None) -> None:
    """The UTXO report of Check and Balance (layout of 2026-11-14).

    Columns as wide as their widest value. Under an xpub or descriptor
    each row ends with the address that locks it, in full: the one to
    paste into [e] collect. Then the estimate's footnote and the totals.
    `settled` is Balance's: the wallet's orchids (landed and received),
    or the reason the server cannot say; Check passes None and shows no
    such line. Balance also leaves out the search line, always 1 and 1
    for one address."""
    w = out.write
    head = f"{_utxos(len(report.utxos))} found"
    if settled is None:
        head += (f" ({report.branches} branch"
                 f"{'es' if report.branches != 1 else ''},"
                 f" {report.scripts} script"
                 f"{'s' if report.scripts != 1 else ''} asked)")
    w(f"\n{head}\n")

    rows = report.utxos
    shorts = [_short(r.outpoint) for r in rows]
    sats = [f"{r.sats:,}" for r in rows]
    latent = [f"{r.latent_sats:,}" for r in rows]
    est = [f"est. orchid amount {orchids(r.seeds)}*" if r.seeds is not None
           else "" for r in rows]
    width = lambda col: max(map(len, col), default=0)
    w_short, w_sats, w_latent, w_est = map(width, (shorts, sats, latent, est))
    source = None
    for row, short, s, lat, e in zip(rows, shorts, sats, latent, est):
        if row.source != source:
            source = row.source
            label = source if len(source) <= 64 else source[:61] + "..."
            w(f"\n  {label}\n")
        link = _link(row.outpoint.rpartition(":")[0], network, out, short)
        line = (f"    {link}{' ' * (w_short - len(short))}  {s:>{w_sats}} sats"
                f"  {lat:>{w_latent}} unclaimed")
        if e:
            line += f"   {e}"
        if row.address and row.address != source:
            line += f"{' ' * (w_est - len(e))}   adr {row.address}"
        w(line + "\n")

    w("\n")
    if report.valued_at_height is not None and rows:
        w(f"* estimate based on claim height {report.valued_at_height:,}"
          " - claim amount decreases ~3%/month\n\n")
    if report.no_node:
        w("(the server has no host node, so the orchid amounts are not"
          " shown; the UTXOs and their runs are exact)\n\n")
    totals = [("total satoshis", f"{report.total_sats:,}")]
    if settled is not None:
        totals.append(("total orchids", settled if isinstance(settled, str)
                       else orchids(settled)))
    if report.total_seeds is not None and (settled is None
                                           or report.total_seeds):
        totals.append(("total est. orchid amount",
                       f"{orchids(report.total_seeds)}*"))
    # whole-number parts right-aligned, the decimals hanging
    whole = lambda v: len(v.split(".")[0]) if v[:1].isdigit() else 0
    w_label = max(len(k) for k, _ in totals) + 3
    w_whole = max(whole(v) for _, v in totals)
    for label, value in totals:
        w(f"{label:<{w_label}}{' ' * (w_whole - whole(value))}{value}\n")
    w("\n")


def _report(rpc: IndexerRpc, network: str, lines: list[str]) -> CheckReport | None:
    """run_check, its failures said in plain words (None)."""
    try:
        return run_check(lines, rpc, network)
    except CheckInputError as e:
        print(f"cannot read that: {e}", file=sys.stderr)
    except WalletRpcError as e:
        print(f"the server refused: {e.message}", file=sys.stderr)
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
    return None


def do_check(rpc: IndexerRpc, network: str, lines: list[str], out=STDOUT) -> int:
    report = _report(rpc, network, lines)
    if report is None:
        return 1
    print_report(report, out, network)
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
             out=STDOUT, ask=input, network: str = "main") -> int:
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
    out.write(f"claim sent: txid {_link(txid, network, out)}\n")
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
    with the settled orchids the index serves (landed and received) as
    "total orchids", apart from the estimate, as wallet-policy.md §UTXO
    states asks. A server without the ledger half says so there."""
    report = _report(rpc, network, [key.address(network)])
    if report is None:
        return 1
    try:
        balance = rpc.getorchidbalance(key.script.hex())
        settled = sum(u.get("seeds", 0) for u in balance.get("utxos", []))
    except WalletRpcError:
        settled = "not served by this index"
    except RpcUnreachable as e:
        print(f"no server: {e}", file=sys.stderr)
        return 1
    print_report(report, out, network, settled)
    return 0


def do_backup(key: WalletKey, out=STDOUT, confirm=None) -> int:
    """`confirm` (the CLI passes the password check) runs before the
    code is shown; False shows nothing."""
    if confirm is not None and not confirm():
        out.write("the wallet code stays hidden\n")
        return 1
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
            ask=input, confirm=None) -> int:
    """`confirm` (the CLI passes the password check) is the last step
    after the yes, before anything is signed; False sends nothing."""
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
    if answer.strip().lower() not in ("y", "yes") or (
            confirm is not None and not confirm()):
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
    out.write(f"sent: txid {_link(txid, network, out)}\n")
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
                out=STDOUT, ask=input, confirm=None) -> int:
    """`confirm` as in do_send: the last step after the yes."""
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
    if answer.strip().lower() not in ("y", "yes") or (
            confirm is not None and not confirm()):
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
    out.write(f"withdrawal sent: txid {_link(txid, network, out)}\n")
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


def _password_ok(path: str) -> bool:
    """The password again, checked against the file, where the wallet
    gives itself away (backup) or its funds (send, withdraw): a wallet
    left open on a terminal is not enough. One try. A file from before
    passwords were required has an empty one: Enter, and only Enter."""
    pw = _ask_secret(_password_prompt(path))
    try:
        load_wallet(path, pw)
    except (WalletKeyError, OSError) as e:
        print(e)
        return False
    return True


def _password_prompt(path: str) -> str:
    try:
        locked = is_locked(path)
    except (WalletKeyError, OSError):
        locked = True
    return ("wallet password> " if locked
            else "wallet password (none set, press Enter; [p] sets one)> ")


def _ask_new_password() -> str | None:
    """A new password for the wallet file, required. Returns None when
    it is too short or the two entries disagree."""
    pw = _ask_secret("password for the wallet file> ")
    try:
        check_new_password(pw)
    except WalletKeyError as e:
        print(e)
        return None
    again = _ask_secret("the same password again> ")
    if pw != again:
        print("the passwords do not match")
        return None
    return pw


def _change_password(path: str) -> None:
    """[p]: the current password (Enter for a file that has none), then
    the new one twice."""
    old = _ask_secret(_password_prompt(path).replace("wallet password",
                                                     "current password"))
    try:
        load_wallet(path, old)
    except (WalletKeyError, OSError) as e:
        print(e)
        return
    new = _ask_new_password()
    if new is None:
        return
    try:
        change_password(path, old, new)
    except (WalletKeyError, OSError) as e:
        print(f"the password stays as it was: {e}")
        return
    print("password changed; the wallet code is the same and still"
          " restores the wallet")


def _create_wallet(path: str, network: str) -> WalletKey | None:
    pw = _ask_new_password()
    if pw is None:
        return None
    key = WalletKey.new()
    try:
        save_wallet(path, key, pw)
    except WalletKeyError as e:
        print(f"cannot save the wallet: {e}")
        return None
    print(f"wallet saved to {path} (locked)")
    print("your wallet code — write it down somewhere safe; the code"
          " alone restores the wallet, password or not:")
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
        save_wallet(path, key, pw)
    except WalletKeyError as e:
        print(f"cannot restore: {e}")
        return None
    print(f"wallet saved to {path} (locked)")
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
    confirm = lambda: _password_ok(wallet_path)
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
                items.append("  [p] change the wallet password")
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
            do_claim(rpc, key, fee_rate, network=network)
            continue
        if choice in ("a", "receive", "address") and key is not None:
            do_receive(key, network)
            continue
        if choice in ("l", "balance") and key is not None:
            do_balance(rpc, key, network)
            continue
        if choice in ("o", "backup") and key is not None:
            do_backup(key, confirm=confirm)
            continue
        if choice in ("p", "password") and key is not None:
            _change_password(wallet_path)
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
            do_send(rpc, key, network, recipient, amount, fee_rate,
                    confirm=confirm)
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
            do_withdraw(rpc, key, network, dest, sats, fee_rate,
                        confirm=confirm)
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
    confirm = lambda: _password_ok(args.wallet)
    if args.command == "backup":
        return do_backup(key, confirm=confirm)
    if args.command == "claim":
        return do_claim(rpc, key, fee_rate, network=args.network)
    if args.command == "collect":
        return do_collect(rpc, key, args.network, args.lines[0], fee_rate,
                          _beside(args.wallet))
    if args.command == "send":
        return do_send(rpc, key, args.network, args.lines[0], args.lines[1],
                       fee_rate, confirm=confirm)
    try:
        sats = int(args.lines[1].replace(",", ""))
    except ValueError:
        print(f"the amount is whole sats: {args.lines[1]}", file=sys.stderr)
        return 2
    return do_withdraw(rpc, key, args.network, args.lines[0], sats, fee_rate,
                       confirm=confirm)


if __name__ == "__main__":
    sys.exit(main())
