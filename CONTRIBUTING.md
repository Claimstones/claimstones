# Contributing

Thank you for looking. Questions, ideas and bug reports are all
welcome; this file says how to make them land.

Most first answers come from the project agent, an AI that works in
public under `AGENTS.md`. Its posts are marked as such. The maintainer
makes every final decision (`GOVERNANCE.md`).

## Where things go

| You have | Put it in |
|---|---|
| A question | Discussions → Q&A |
| An idea or a protocol change | Discussions → Ideas, before any code |
| A bug | An issue, with the steps to see it |
| A security problem | Not in public: see `SECURITY.md` |
| A fix or feature | A pull request, after an issue or discussion for anything larger than a typo |

## Before you write code

1. **Find the class of your change** (`GOVERNANCE.md`): consensus,
   behaviour or housekeeping. Consensus is frozen from block 980 000
   and needs a proposal first; a pull request that changes it without
   one will be closed.
2. **Spec first.** If the specs in `docs/spec/` describe the behaviour,
   change the spec in the same pull request, or before it. Code that
   disagrees with the spec is a bug in one of them, and the pull
   request should say which.
3. **One topic per pull request.** A fix and a refactor are two pull
   requests.

## What a pull request needs

- A description that says what changes, why, and which class it is.
- Tests that fail without the change and pass with it.
- All tests passing: `pip install -e .` then `pytest`.
- No new dependencies without discussion. The project uses `numpy`
  and `lmdb` only, and its own Bitcoin code instead of third-party
  crypto, on purpose.
- Consensus code stays deterministic: no floats, no clocks, no
  randomness, no network answers that could differ between machines.

## Style

- Follow the code around your change.
- Comments say why, not what.
- Docs use plain words and say amounts, never money: no prices, worth
  or investment framing (`docs/spec/wallet-policy.md` §Wording).

## What happens next

The agent labels the issue or pull request, checks it against this
file, runs the tests and writes a review, usually within a day. The
maintainer reads it and merges or declines. A declined pull request
gets a reason.

## Licence

By contributing you agree that your work is published under the MIT
licence in `LICENSE`.
