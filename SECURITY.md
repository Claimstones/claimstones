# Security

## Reporting a problem

Please do not open a public issue or discussion for a security
problem. Report it privately instead:

- **GitHub:** the "Report a vulnerability" button under this
  repository's Security tab.
- **Email:** laban2122@proton.me

Say what you found, how to reproduce it, and what it affects. You
will get an answer within a few days. Reports are read by the
maintainer, not by the project agent.

## What counts

Examples of what we want to hear about:

- Two indexers computing different balances from the same chain.
- A transaction the wallet or the land builder would build or sign
  that loses orchids or bitcoin, or does something the plan shown to
  the user does not say.
- A way to make the indexer, RPC or gateway crash, hang or run out of
  memory from outside.
- Anything that exposes a wallet code or private key.

Bugs in the public website, and questions about how the protocol
works, can go to the issues or Discussions as normal.

## What we do

- Confirm the problem, fix it, and publish the fix with a note on what
  was wrong.
- Credit you in that note, unless you prefer not.
- A problem in consensus code is fixed the way `GOVERNANCE.md`
  describes: when the code disagrees with the spec, the spec wins.

There is no bug bounty. Orchids are amounts in a game, and the project
holds no funds to pay one.

## Supported versions

Only the latest commit on `master` is supported.
