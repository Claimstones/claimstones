"""claimstones-wallet — the reference wallet, a menu-loop CLI.

The release-1 feature set of wallet-policy.md §What a wallet can do,
grown one command at a time. Built: Check (watch-only — any address,
xpub or descriptor) and Claim (land the latent value on the wallet's
own address). Coming: Send, Receive, Withdraw, the external
claim-and-collect form.

It speaks the indexer RPC (spec/indexer-rpc.md) and trusts it only for
what it shows; everything the wallet ever signs is built and verified
locally (wallet/land.py, wallet-policy.md §Envelope scan).

    claimstones-wallet                                  # the menu
    claimstones-wallet check bc1q... zpub6r...          # one check, exit
    claimstones-wallet claim 2                          # claim at 2 sat/vB
    ... -rpcurl http://... -rpccookie <path>/rpc.cookie -wallet <file>
"""

from __future__ import annotations

import argparse
import os
import sys

from claimstones.bitcoin.descriptors import CheckInputError
from claimstones.wallet.check import CheckReport, orchids, run_check
from claimstones.wallet.claim import ClaimPlan, plan_claim, sign_claim
from claimstones.wallet.keys import (
    WalletKey, WalletKeyError, load_wallet, save_wallet,
)
from claimstones.wallet.land import TemplateError
from claimstones.wallet.rpc import (
    DEFAULT_URL, IndexerRpc, RpcUnreachable, WalletRpcError,
)

DEFAULT_WALLET = os.path.join(os.path.expanduser("~"),
                              ".claimstones", "wallet.code")

_SHRINKS = "unclaimed value does not expire, but shrinks about 3 % a month"


def _short(outpoint: str) -> str:
    txid, _, vout = outpoint.rpartition(":")
    return f"{txid[:10]}..{txid[-6:]}:{vout}"


def print_report(report: CheckReport, out=sys.stdout) -> None:
    w = out.write
    w(f"\n{len(report.utxos)} coin{'s' if len(report.utxos) != 1 else ''} "
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
        cost = (f"est. production cost {orchids(row.seeds)} orchids"
                if row.seeds is not None else "")
        w(f"    {_short(row.outpoint):<32} {row.sats:>12,} sats"
          f" {row.latent_sats:>12,} unclaimed   {cost}\n")
    w(f"\n  total {report.total_sats:>29,} sats"
      f" {report.total_latent_sats:>12,} unclaimed")
    if report.total_seeds is not None:
        w(f"   est. production cost {orchids(report.total_seeds)} orchids")
    w("\n")
    if report.valued_at_height is not None:
        w(f"  (valued at height {report.valued_at_height:,}; {_SHRINKS})\n")
    if report.no_node:
        w("  (the server has no host node, so production cost is not shown;"
          " the coins and their runs are exact)\n")
    w("\n")


def do_check(rpc: IndexerRpc, network: str, lines: list[str], out=sys.stdout) -> int:
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


def print_claim_plan(plan: ClaimPlan, out=sys.stdout) -> None:
    w = out.write
    s = plan.summary
    n = len(s.inputs)
    w(f"\nclaim: {n} coin{'s' if n != 1 else ''}, {s.input_sats:,} sats,"
      f" {plan.latent_sats:,} of them unclaimed\n")
    if plan.skipped:
        w(f"  ({plan.skipped} already-claimed"
          f" coin{'s' if plan.skipped != 1 else ''} stay untouched)\n")
    if plan.seeds is not None:
        w(f"  est. production cost {orchids(plan.seeds)} orchids"
          f" (valued at height {plan.valued_at_height:,})\n")
    else:
        w("  (the server has no host node, so production cost is not shown)\n")
    w(f"  fee {s.fee_sats:,} sats; {s.change_sats:,} sats return to the"
      " wallet address\n")
    w("  claiming does not spend your bitcoin; you pay a network fee\n")


def do_claim(rpc: IndexerRpc, key: WalletKey, fee_rate: float,
             out=sys.stdout, ask=input) -> int:
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
    print_claim_plan(plan, out)
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


def _create_wallet(path: str, network: str) -> WalletKey | None:
    key = WalletKey.new()
    try:
        save_wallet(path, key)
    except WalletKeyError as e:
        print(f"cannot save the wallet: {e}")
        return None
    print(f"wallet saved to {path}")
    print("your wallet code — write it down somewhere safe; the code"
          " alone restores the wallet:")
    print(f"  {key.code}")
    print(f"wallet address: {key.address(network)}")
    return key


def _restore_wallet(path: str, network: str) -> WalletKey | None:
    try:
        code = input("wallet code (orchid1...)> ").strip()
    except EOFError:
        return None
    if not code:
        return None
    try:
        key = WalletKey.from_code(code)
        save_wallet(path, key)
    except WalletKeyError as e:
        print(f"cannot restore: {e}")
        return None
    print(f"wallet saved to {path}")
    print(f"wallet address: {key.address(network)}")
    return key


def _prompt_lines() -> list[str]:
    print("Paste addresses, xpubs or descriptors, one per line;"
          " an empty line finishes.")
    lines = []
    while True:
        try:
            line = input("  ").strip()
        except EOFError:
            break
        if not line:
            break
        lines.append(line)
    return lines


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
        key = load_wallet(wallet_path)
    except FileNotFoundError:
        pass
    except WalletKeyError as e:
        print(f"cannot read the wallet file {wallet_path}: {e}")
    print(f"claimstones wallet — {rpc.url}, {state}")
    if key is not None:
        print(f"wallet address: {key.address(network)}")
    while True:
        items = ["  [c] check any bitcoin — address, xpub or descriptor,"
                 " watch-only"]
        if key is not None:
            items.append("  [k] claim — land the latent value on the"
                         " wallet address")
        else:
            items.append("  [n] new wallet    [r] restore from wallet code")
        items.append("  [q] quit")
        print("\n" + "\n".join(items))
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
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
        if choice in ("n", "new") and key is None:
            key = _create_wallet(wallet_path, network)
            continue
        if choice in ("r", "restore") and key is None:
            key = _restore_wallet(wallet_path, network)
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
    ap.add_argument("command", nargs="?", choices=["check", "claim"])
    ap.add_argument("lines", nargs="*", metavar="line",
                    help="check: addresses, xpubs or descriptors;"
                         " claim: the fee rate in sat/vB")
    args = ap.parse_args(argv)

    credential = (f"{args.rpcuser}:{args.rpcpassword or ''}"
                  if args.rpcuser is not None else None)
    rpc = IndexerRpc(args.rpcurl, cookie_path=args.rpccookie,
                     credential=credential)

    if args.command == "check":
        lines = args.lines or [l.strip() for l in sys.stdin if l.strip()]
        return do_check(rpc, args.network, lines)
    if args.command == "claim":
        if len(args.lines) != 1:
            print("usage: claimstones-wallet claim <fee rate in sat/vB>",
                  file=sys.stderr)
            return 2
        try:
            fee_rate = float(args.lines[0])
        except ValueError:
            print(f"not a fee rate: {args.lines[0]}", file=sys.stderr)
            return 2
        try:
            key = load_wallet(args.wallet)
        except FileNotFoundError:
            print(f"no wallet at {args.wallet}; the menu's [n] creates one",
                  file=sys.stderr)
            return 1
        except WalletKeyError as e:
            print(f"cannot read the wallet file {args.wallet}: {e}",
                  file=sys.stderr)
            return 1
        return do_claim(rpc, key, fee_rate)
    return menu(rpc, args.network, args.wallet)


if __name__ == "__main__":
    sys.exit(main())
