# Hackathon Template

A GitHub Issues + `git worktree` coordination harness for hackathon teams working with Claude Code. See `SPEC.md` for the full design and `CLAUDE.md` for the rules agents follow.

## Start a new hackathon

1. Click **Use this template** (or run `gh repo create <org>/<name> --template <owner>/hackathon-template --private --clone`).
2. Clone the new repo and edit `harness/team.json`: set each member's GitHub login and role, and name at least two `contract_owners`.
3. Run `./scripts/doctor.sh` to check your tools (`git`, `gh`, `python3`, `node`, `pnpm`, `uv`).
4. Run `./scripts/init-hackathon.sh` from `main`. It invites members, generates `.github/CODEOWNERS`, pushes the setup commit, creates labels and sets up the `main` ruleset.
5. The Architect writes `MASTER_PLAN.md` and `issues.json`, then runs `./scripts/bootstrap-issues.sh`.
6. Builders claim work with `./scripts/claim.sh <issue-id>`.

`.github/CODEOWNERS` is intentionally absent from the template. `init-hackathon.sh` generates it.
