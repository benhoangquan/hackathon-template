# Hackathon Harness Rules

This repo is coordinated through GitHub Issues and `git worktree`.

## Hard rules
1. Never commit or push to `main`. Work only in a worktree made by `./scripts/claim.sh <issue-id>`.
2. Edit only files matching your issue's `Touches` globs, plus the shared files in `harness/config.json`.
   A hook blocks other edits. Do not work around it with shell commands.
3. Never change `contracts/` or any other protected path in a feature branch.
   Use `./scripts/propose-contract.sh` instead.
4. Never edit issue labels, assignees, branches or worktrees by hand. Use the scripts in `scripts/`.
5. Commit and push to your `issue/<id>` branch after each meaningful step.
6. If you need a file owned by another slice, stop and run `./scripts/escalate.sh "<what you need>"`.

## Workflow
Use the `hackathon-harness` skill for the full Architect, Builder, contract-change and hotfix flows.
