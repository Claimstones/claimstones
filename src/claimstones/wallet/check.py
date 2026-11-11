"""Check any bitcoin: addresses, xpubs and descriptors, watch-only.

The wallet-policy.md feature — per UTXO: sats, latent value, and the
"est. production cost" at W(tip). The lines are parsed and derived by
bitcoin/descriptors.py, each ranged branch walked under the gap limit;
the index answers per script (`getaddressutxos`), and valuation comes
from `getlatent` when the server has a node to read W from. Without
one, the coins and their runs still show — only the orchid figure is
missing, and the report says so instead of pretending.

Pure logic: the RPC client is injected, nothing here prints.
"""

from __future__ import annotations

from dataclasses import dataclass

from claimstones.bitcoin.descriptors import Branch, DerivedScript, parse_inputs, scan_branch
from claimstones.wallet.rpc import NO_NODE, IndexerRpc, WalletRpcError


@dataclass(frozen=True)
class CheckUtxo:
    outpoint: str               # txid:vout, display order
    sats: int
    latent_sats: int
    seeds: int | None           # est. production cost; None without a node
    source: str                 # the pasted line this coin was found under
    address: str | None         # the derived address, where one exists


@dataclass(frozen=True)
class CheckReport:
    utxos: tuple[CheckUtxo, ...]
    total_sats: int
    total_latent_sats: int
    total_seeds: int | None     # None when nothing could be valued
    valued_at_height: int | None
    branches: int               # derivation branches walked
    scripts: int                # scripts the index was asked about
    no_node: bool               # the server could not value (no host node)


def run_check(lines: list[str], rpc: IndexerRpc,
              network: str | None = "main") -> CheckReport:
    branches = parse_inputs(lines, network)
    hits: list[tuple[DerivedScript, Branch, list[dict]]] = []
    scripts = 0

    for branch in branches:
        def probe(index: int, derived: DerivedScript,
                  branch: Branch = branch) -> bool:
            nonlocal scripts
            scripts += 1
            utxos = rpc.getaddressutxos(derived.script.hex())
            if utxos:
                hits.append((derived, branch, utxos))
            return bool(utxos)
        scan_branch(branch, probe)

    rows: list[CheckUtxo] = []
    seen: set[str] = set()
    valued_at = None
    no_node = False
    for derived, branch, utxos in hits:
        seeds_by_outpoint: dict[str, int] = {}
        if not no_node:
            try:
                latent = rpc.getlatent(derived.script.hex())
                valued_at = latent["valued_at_height"]
                seeds_by_outpoint = {f"{u['txid']}:{u['vout']}": u["seeds"]
                                     for u in latent["utxos"]}
            except WalletRpcError as e:
                if e.code != NO_NODE:
                    raise
                no_node = True
        for u in utxos:
            outpoint = f"{u['txid']}:{u['vout']}"
            if outpoint in seen:    # the same coin pasted twice over
                continue
            seen.add(outpoint)
            rows.append(CheckUtxo(outpoint, u["sats"], u["latent_sats"],
                                  seeds_by_outpoint.get(outpoint),
                                  branch.source, derived.address))

    valued = [r.seeds for r in rows if r.seeds is not None]
    return CheckReport(
        utxos=tuple(rows),
        total_sats=sum(r.sats for r in rows),
        total_latent_sats=sum(r.latent_sats for r in rows),
        total_seeds=sum(valued) if valued else None,
        valued_at_height=valued_at,
        branches=len(branches),
        scripts=scripts,
        no_node=no_node,
    )


def orchids(seeds: int) -> str:
    """Seeds as orchids (100 seeds each), exact, no floats."""
    whole, part = divmod(seeds, 100)
    return f"{whole:,}.{part:02d}"


def parse_orchids(text: str) -> int:
    """An orchid amount as typed ("12.34") to seeds, exact, no floats."""
    t = text.strip().replace(",", "").replace(" ", "")
    whole, dot, part = t.partition(".")
    if dot:
        if not part or len(part) > 2 or not part.isdigit():
            raise ValueError(f"not an orchid amount: {text}"
                             " (orchids have two decimals)")
        part = part.ljust(2, "0")
    else:
        part = "00"
    if not (whole or dot) or (whole and not whole.isdigit()):
        raise ValueError(f"not an orchid amount: {text}")
    return int(whole or 0) * 100 + int(part)
