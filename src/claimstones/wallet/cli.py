"""claimstones-wallet — the reference wallet, a menu-loop CLI.

The release-1 feature set of wallet-policy.md §What a wallet can do,
grown one command at a time. Built: Check (watch-only — any address,
xpub or descriptor). Coming: Claim, Send, Receive, Withdraw, the
external claim-and-collect form.

It speaks the indexer RPC (spec/indexer-rpc.md) and trusts it only for
what it shows; everything the wallet ever signs is built and verified
locally (wallet/land.py, wallet-policy.md §Envelope scan).

    claimstones-wallet                                  # the menu
    claimstones-wallet check bc1q... zpub6r...          # one check, exit
    ... -rpcurl http://... -rpccookie <path>/rpc.cookie
"""

from __future__ import annotations

import argparse
import sys

from claimstones.bitcoin.descriptors import CheckInputError
from claimstones.wallet.check import CheckReport, orchids, run_check
from claimstones.wallet.rpc import (
    DEFAULT_URL, IndexerRpc, RpcUnreachable, WalletRpcError,
)

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


def menu(rpc: IndexerRpc, network: str) -> int:
    try:
        info = rpc.getindexinfo()
        state = f"index at height {info['height']:,}"
        if info.get("activation_height") is None:
            state += ", activation not yet decided"
    except (WalletRpcError, RpcUnreachable, OSError) as e:
        state = f"server not answering ({e})"
    print(f"claimstones wallet — {rpc.url}, {state}")
    while True:
        print("\n  [c] check any bitcoin — address, xpub or descriptor,"
              " watch-only\n  [q] quit")
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
    ap.add_argument("command", nargs="?", choices=["check"])
    ap.add_argument("lines", nargs="*", metavar="line",
                    help="addresses, xpubs or descriptors (check)")
    args = ap.parse_args(argv)

    credential = (f"{args.rpcuser}:{args.rpcpassword or ''}"
                  if args.rpcuser is not None else None)
    rpc = IndexerRpc(args.rpcurl, cookie_path=args.rpccookie,
                     credential=credential)

    if args.command == "check":
        lines = args.lines or [l.strip() for l in sys.stdin if l.strip()]
        return do_check(rpc, args.network, lines)
    return menu(rpc, args.network)


if __name__ == "__main__":
    sys.exit(main())
