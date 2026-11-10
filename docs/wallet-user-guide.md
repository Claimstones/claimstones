# The wallet: a user guide

`claimstones-wallet` is the reference wallet of this repository: a small
command-line program for holding, checking and claiming orchids. It is
self-custodial — the key never leaves your machine, and everything the
wallet signs is built and checked locally. It is written against
`docs/spec/wallet-policy.md`; this guide is the same material in plain
words, for someone who wants to use the wallet rather than build one.

**Nothing is live yet.** `ACTIVATION_HEIGHT` is not set, so no land
counts on any network and everything is testing. Do not move bitcoin
anywhere expecting tokens until the activation height is a public
commit and has passed.

## Starting it

Installing the repository gives you the command:

```
pip install -e .
claimstones-wallet
```

With no arguments it opens a menu. The wallet talks to a claimstones
server (`docs/spec/indexer-rpc.md`) — by default one on your own
machine at `http://127.0.0.1:7332`.

A worked example, everything on one machine, from the repository
folder. The server serves the birth index and writes its auth cookie
`rpc.cookie` beside the index by default; the wallet reads that same
file, and the default URL already matches, so only the cookie needs
pointing:

```
cd claimstones
python -m claimstones.indexer.rpc --db <path>/birth.lmdb ^
    --node-url http://127.0.0.1:8332 --node-cookie <bitcoin datadir>/.cookie
claimstones-wallet -rpccookie <path>/rpc.cookie
```

(`^` continues a line on Windows; use `\` on Linux. Without the
`--node-*` flags the server still serves coins and runs exactly, but
cannot value them at the tip or relay transactions.)

| flag | meaning |
|---|---|
| `-rpcurl` | the server to ask (default `http://127.0.0.1:7332`) |
| `-rpccookie` | the server's `rpc.cookie` file, for a server on this machine |
| `-rpcuser` / `-rpcpassword` | fixed credentials, for a remote server |
| `-network` | `main`, `test`, `signet` or `regtest` (default `main`) |
| `-wallet` | the wallet file (default `~/.claimstones/wallet.code`) |

Trust the server the way an Electrum user trusts an Electrum server:
for what it *shows*, never for what you *sign*. The wallet builds and
verifies every transaction itself; a wrong or malicious server can
show you wrong numbers, but it cannot make the wallet sign something
other than the exact transaction it describes to you. A server cannot
spend, redirect or lose your orchids.

## Your wallet and the wallet code

The first time, choose `[n] new wallet`. The wallet prints two things:

- **your address** — one taproot address (`bc1p...`), reused for
  everything; and
- **your wallet code** — one line starting `orchid1...`.

The wallet code is the entire backup. There is no seed phrase: the
code is the key. Write it down somewhere safe, on paper if the wallet
will ever hold something that matters. On any machine, `[r] restore
from wallet code` recovers the whole wallet from the code alone.

Two warnings, both absolute:

- **Anyone who knows the code owns the orchids.** Treat it like cash.
- **If the code is lost and the wallet file is gone, nothing can
  recover the wallet.** Nobody operates claimstones; there is no one
  to ask.

The wallet file (default `~/.claimstones/wallet.code`) holds the code
in plain text, created readable by your user only. It deserves the
same care as a Bitcoin Core wallet file. The wallet never overwrites
an existing wallet file — restoring over one is refused, so a mistake
cannot destroy a wallet silently.

## Receiving

Give out your address. Two different things arrive on it:

- **Bitcoin** sent to the address stays your bitcoin, and carries
  unclaimed value you can claim (below).
- **Orchids** sent by another wallet arrive as settled balance,
  riding on a small coin (330 sats) created by the sender.

The menu shows the address whenever a wallet is loaded.

## Check: look at any bitcoin, yours or not

`[c]` in the menu, or directly:

```
claimstones-wallet check bc1q...
claimstones-wallet check zpub6r... "wpkh([fingerprint/84h/0h/0h]xpub.../<0;1>/*)"
```

Paste any address, any xpub (`xpub`/`ypub`/`zpub` and testnet kin) or
any single-key descriptor, one per line. Check is watch-only: no keys
are involved and nothing can be spent. For an xpub the wallet derives
the addresses itself and scans with the standard gap limit of 20, so
it finds what any ordinary wallet would have used.

Each coin shows three figures:

- **sats** — the coin's bitcoin, untouched by anything here;
- **unclaimed** — the sats whose latent value has not been landed yet;
- **est. production cost** — what the unclaimed value would mint if
  claimed now, in orchids.

The orchid figure is an estimate of production cost — the mining
electricity behind the coins — and never a price; nothing here has a
market. Unclaimed value does not expire, but shrinks about 3 % a
month as the chain's work grows: claiming sooner mints more, and
there is no deadline.

If the server runs without a host node it cannot value at the tip; the
coins and their unclaimed sats still show exactly, and the report says
why the orchid figure is missing.

## Claim: land the value on your own address

`[k]` in the menu, or directly (the number is the fee rate in sat/vB):

```
claimstones-wallet claim 2
```

A claim takes every coin on your address that still has unclaimed
value, and lands that value as orchid balance — on the same address,
in one transaction. Coins already claimed stay untouched. You choose
the fee rate; the wallet shows the whole plan before anything is
signed:

- which coins go in, their sats and unclaimed sats;
- the est. production cost that would be minted;
- the network fee, and the sats that return to your own address.

**Claiming does not spend your bitcoin; you pay a network fee.** The
sats go in and come back to the same address, minus only the fee.
Confirm with `y`, and the wallet signs and hands the transaction to
the server to relay. Once the transaction confirms, Check shows the
coins as claimed and the orchids as balance.

The wallet refuses a claim where the fee would eat more than a fifth
of the coins — small coins at a high fee rate are better left until
fees are low, since unclaimed value keeps.

## What the wallet will never do

These are rules from `docs/spec/wallet-policy.md`, enforced in code:

- It never signs a transaction it has not fully decoded, including
  its own: every build is verified against the plan you confirmed,
  when it is built and again at the moment of signing.
- It only ever builds the fixed transaction templates of the spec —
  never a freehand transaction.
- It never selects a coin carrying orchid balance as fee material for
  something else; settled coins move only when you send orchids.

## Coming next

In build order: claim-and-collect for coins held in another wallet
(the wallet prepares a PSBT, you sign it in your own wallet, nothing
moves to anyone); Send and Withdraw; then the end-to-end trial on
regtest and signet before any of this touches mainnet.
