# Governance

Claimstones is a small open-source project with one maintainer (Laban)
and a project agent, an AI that does most of the day-to-day work in
public. This file says who decides what, so that nobody has to guess.

## Who decides

| Who | Decides |
|---|---|
| The maintainer | The protocol, releases, merges, the roadmap, and anything not listed below |
| The project agent | Nothing final. It triages, reviews, answers and drafts, within `AGENTS.md` |
| Contributors | Their own proposals, until they are merged or declined |

The maintainer can delegate a decision to the agent by changing
`AGENTS.md` in a public commit. Nothing is delegated in any other way.

## Three classes of change

Every issue and pull request belongs to one class. The class decides
how far it can go and who must agree.

### 1. Consensus

What every indexer must compute identically: the envelope, the
constants, the lineage walk, valuation, and how balances move. In code,
`src/claimstones/protocol/`, `src/claimstones/lineage/`, the ledger and
land steps of the indexer, and `docs/spec/constants.md`,
`envelope.md`, `land.md` and `indexer.md`.

- Frozen from bitcoin block 980 000, the activation height. A change
  is a new protocol version, not a fix.
- A change needs a written proposal first (a Discussion, then a spec
  change), test vectors, and its own activation height far enough
  ahead for every indexer to upgrade.
- Only the maintainer merges, and never on the same day it is proposed.
- One exception: a bug where the code disagrees with the written spec.
  The spec wins and the code is fixed, with a test that shows the
  disagreement.

### 2. Behaviour

What users and operators see but other indexers do not have to agree
on: the wallet, the RPC and gateway, the land builder, performance,
storage and tools.

- Needs tests, and a spec update where the spec describes it.
- The agent reviews and runs the tests; the maintainer merges.

### 3. Housekeeping

Documentation, typos, test additions, CI, comments.

- The agent reviews; the maintainer merges.
- The maintainer may later let the agent merge this class on its own,
  recorded in `AGENTS.md`.

When the class is unclear, it is the higher one.

## How decisions are recorded

- Open points in the specifications are marked `[decide]`. Closing one
  is a public commit that removes the marker and states the decision
  and its date.
- Larger questions start in GitHub Discussions (Ideas). A decision
  there is linked from the commit that carries it out.
- Nothing is decided in private messages or on other forums.

## What the project never does

- Sell, premine, or promise orchids, or say what they are worth.
- Present orchids as money or an investment. They are amounts in a
  game built on public chain data.
- Accept a change that makes balances depend on anything but public
  chain data.
