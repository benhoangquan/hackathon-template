---
name: hackathon-harness
description: GitHub Issues + git worktree coordination protocol for hackathon teams. Use when claiming, building, finishing or handing over an issue in this repo.
---

# Hackathon Harness

## Where am I?
- `.harness/task.json` exists: this is a worktree session. Follow "Builder session".
- Otherwise this is the control clone. Follow "Control clone". Do not edit files here.

## Control clone
1. List your work: `gh issue list --label "owner:<me>" --label "status:ready"`.
2. Claim one: `./scripts/claim.sh <issue-id>`.
3. Tell the user to run the printed command. It opens a new session in the worktree.

## Builder session
1. Read `.harness/task.json`, then `gh issue view <id>`, then the relevant files in `contracts/`.
2. Implement inside `Touches`. Commit and push as you go.
3. Run `./scripts/finish-pr.sh`. Fix what it reports and re-run:
   - exit 11: you changed a file outside `Touches`. Revert it or escalate.
   - exit 12: `Verify` failed. Fix the code.
   - exit 13: rebase conflict. Resolve it unless it is in a protected path.
   - exit 14: your contract PR has not merged yet. Tell the user who needs to review it.
4. When the PR is open, the session is finished. It merges by itself when CI is green.

## Changing a contract
1. `./scripts/propose-contract.sh "<title>"`, then make the change in that worktree and run `./scripts/finish-pr.sh` there.
2. In your feature worktree: `./scripts/propose-contract.sh --stack <contract-issue-id>`.
3. Keep building. `finish-pr.sh` will wait for the contract PR.

## Architect: Phase 0
1. Claim the Phase 0 issue. Write `MASTER_PLAN.md`, `contracts/`, `.env.example`, the folder skeleton and `issues.json`.
2. Size each issue at 30 to 90 minutes. Give each one non-overlapping `Touches` and a `Verify` command that passes only when the slice works.
3. After the Phase 0 PR merges, run `./scripts/bootstrap-issues.sh` from the control clone.

## Broken main
A `type:hotfix` issue appears with the exact revert. Whoever it is assigned to claims it, runs the revert, and runs `./scripts/finish-pr.sh`. Revert first; re-land the change afterwards.

## Handover
`./scripts/reclaim.sh <issue-id> --to <p-tag>`, then the new owner runs `./scripts/claim.sh <issue-id> --resume`.
