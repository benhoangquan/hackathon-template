# Multi-Agent Hackathon Harness — System Specification (v1.1)

Supersedes v1.0. Section 14 lists what changed. Section 13 lists what is still unverified and which details were filled in while writing this version.

## 1. Problem, solution and scope

### 1.1 Problem
On a hackathon team where each teammate has Claude Code, uncoordinated prompting creates a single-person bottleneck: one teammate drives most of the progress while the others sit idle or collide on shared files, schema definitions and the state of `main`.

### 1.2 Solution
Turn GitHub Issues and GitHub Actions into a deterministic, asynchronous task bus backed by isolated `git worktree` environments. It ships as a GitHub template repository with a Claude Code skill inside it.

### 1.3 Scope of v1
- **Plan A only**: one interactive Claude Code session per teammate laptop, each on their own Claude subscription.
- **Done** means the dry run in section 11 passes on at least two real laptops.
- **Out of scope**: headless worker code, the 2-strike counter, enforcing the integration freeze, GitHub's native issue dependencies, merge queues, and syncing an instantiated repo from the template. Section 12 lists the seams left for Plan B.

### 1.4 Invariants
1. **No direct commits to `main`.** Everything merges by pull request after CI passes. A ruleset on `main` enforces this on the server, and a local pre-push hook is a second layer. The single exception is the setup commit `init-hackathon.sh` makes before protection is switched on.
2. **Clean control clone.** Each teammate's primary clone stays on clean `main` and is used for triage and running scripts. All edits happen in a worktree, with one fresh Claude session per issue launched inside that worktree.
3. **Contracts first.** Builder issues cannot exist until Phase 0 has merged: `bootstrap-issues.sh` refuses to run before then.
4. **Scoped file ownership.** Every issue declares `Touches` globs, and no role bypasses the check. A feature PR never changes a protected path (section 8.2).
5. **Scripts over prose.** Claude never improvises `gh api` mutations, label changes or worktree commands. Every state transition runs through a script in `scripts/`.
6. **One lock per issue.** The remote branch `issue/<id>` is the claim. Labels and assignees are display state written after the lock is won.

## 2. Repository layout

```text
hackathon-harness-template/
├── CLAUDE.md                        # under 60 lines: invariants + skill pointer
├── MASTER_PLAN.md                   # architecture, slices, milestones (Phase 0)
├── .env.example                     # environment variable contract (protected)
├── issues.json                      # bootstrap manifest (written in Phase 0)
├── contracts/                       # shared interfaces (protected)
│   └── README.md
├── harness/
│   ├── team.json                    # p-tags, GitHub logins, contract owners
│   ├── config.json                  # shared paths, protected paths, tools, thresholds
│   └── VERSION                      # template version this repo was created from
├── .claude/
│   ├── settings.json                # pre-edit Touches hook
│   └── skills/hackathon-harness/SKILL.md
├── .github/
│   ├── CODEOWNERS                   # generated from team.json by init-hackathon.sh
│   ├── ISSUE_TEMPLATE/agent-task.md
│   └── workflows/
│       ├── ci.yml                   # required check on every PR
│       ├── main-health.yml          # checks main after each merge
│       ├── unblock-issues.yml       # blocked to ready when blockers close
│       └── stale-claim.yml          # flags claims with no recent push
└── scripts/
    ├── harness.py                   # all logic; Python 3 stdlib only
    ├── check.sh                     # the stack seam (pnpm + uv preset)
    ├── doctor.sh
    ├── init-hackathon.sh
    ├── bootstrap-issues.sh
    ├── claim.sh
    ├── finish-pr.sh
    ├── propose-contract.sh
    ├── escalate.sh
    ├── reclaim.sh
    ├── sync.sh
    ├── reset-demo.sh
    ├── git-hooks/pre-push
    └── tests/                       # stdlib unittest for the pure logic
```

Every `scripts/*.sh` file is a thin entrypoint that calls a subcommand of `harness.py`. macOS ships bash 3.2, so no logic lives in bash. The module must run on Python 3.9 with no third-party packages.

Per-worktree files, all gitignored: `.env`, `.env.local`, `.harness/task.json`.

## 3. Configuration

### 3.1 `harness/team.json`
Team size is whatever this file lists; nothing hard-codes four.

```json
{
  "members": {
    "p1": { "login": "github-login-1", "role": "architect" },
    "p2": { "login": "github-login-2", "role": "builder" },
    "p3": { "login": "github-login-3", "role": "builder" },
    "p4": { "login": "github-login-4", "role": "builder" }
  },
  "contract_owners": ["p1", "p3"]
}
```

`contract_owners` must name at least two members, because nobody can approve their own PR.

Scripts work out who is running them by matching the `gh` login against `members`. `--as <p-tag>` overrides this on any script.

### 3.2 `harness/config.json`

```json
{
  "shared_paths": ["package.json", "**/package.json", "pnpm-lock.yaml",
                   "pnpm-workspace.yaml", "pyproject.toml", "**/pyproject.toml", "uv.lock"],
  "lockfiles": ["pnpm-lock.yaml", "uv.lock"],
  "protected_paths": ["contracts/**", ".env.example", "scripts/**", ".github/**",
                      ".claude/**", "harness/**", "CLAUDE.md", "dryrun/contract/**"],
  "required_tools": ["git", "gh", "python3", "node", "pnpm", "uv"],
  "trusted_bots": ["github-actions[bot]"],
  "stale_minutes": 60
}
```

- `shared_paths`: files any issue may modify without listing them, because nearly every slice adds a dependency.
- `protected_paths`: files that need contract-owner review and may not be listed by feature or integration issues. `dryrun/contract/**` is there so the dry run exercises that review; it matches nothing once the dry run is cleaned up.
- The values above are the pnpm + uv preset. A different stack changes this file and `scripts/check.sh`, nothing else.

## 4. Roles

**Architect (p1 by default)**
- Writes `MASTER_PLAN.md`, the Phase 0 contracts and `issues.json`.
- Reviews contract PRs. Builders author them (section 9).
- Takes builder slices only off the critical path: nothing that appears in another issue's `Blocked-By` chain. Also owns `type:integration` issues and the final demo build.

**Contract owners (p1 plus one deputy)**
- Either can approve a PR that touches a protected path. The deputy covers when p1 is busy, asleep, or the PR's author.

**Builders**
- Own vertical slices. Work only on issues labelled `owner:<me>` and `status:ready`.
- Stay inside `Touches`. Propose contract changes by PR instead of waiting for the Architect.

**Merging**
- Nobody clicks merge. `finish-pr.sh` enables squash auto-merge, and GitHub merges when CI is green.
- Zero approvals are required, except contract-owner approval on PRs that touch a protected path.
- Branches are not required to be up to date with `main`. Section 8.5 covers the resulting risk.

## 5. Issues

### 5.1 Labels
Created by `init-hackathon.sh`. Labels are the only source of truth for status, role, owner and type.

| Group | Labels |
| :--- | :--- |
| Status (exactly one) | `status:blocked`, `status:ready`, `status:in-progress`, `status:needs-architect`, `status:needs-human` |
| Role | `role:architect`, `role:builder` |
| Owner | `owner:<p-tag>` for each member in `team.json` |
| Type | `type:contract`, `type:feature`, `type:hotfix`, `type:integration` |
| Flags | `stale`, `dry-run` |

`status:needs-human` is created now but only used by Plan B. `status:needs-architect` is used for a rebase conflict in a protected path and for needing files owned by another slice.

### 5.2 Issue body
The body carries only the three fields that have no label, in a fenced block. The `---` front matter of v1.0 is gone: GitHub renders it as a rule plus a heading, and it clashes with the front matter the issue template file itself needs.

````markdown
```harness
Touches: frontend/src/components/**, frontend/src/pages/**
Blocked-By: #1, #2
Verify: pnpm --filter frontend typecheck && pnpm --filter frontend test
```

### Objective
One or two sentences describing the slice.

### Contract Reference
- Consumes `POST /api/v1/analyze` from `contracts/openapi.yaml`

### Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

<!-- harness-key: frontend-results-page -->
````

- `Blocked-By` is `none` or a comma-separated list of issue numbers.
- `Verify` is one shell command line, run exactly as written.
- The trailing comment is the manifest key; it makes bootstrap idempotent.
- Issues should be sized at roughly 30 to 90 minutes of work.

### 5.3 Glob semantics
Patterns are matched against repo-relative paths with forward slashes. `*` matches within one path segment, `**` matches any number of segments including none, `?` matches one character. A pattern with no wildcard matches that exact path only.

### 5.4 Lifecycle

```text
bootstrap-issues.sh
   ├─ no open blockers ─► status:ready
   └─ open blockers ────► status:blocked ─(unblock-issues.yml)─► status:ready

status:ready ─(claim.sh wins the lock)─► status:in-progress
status:in-progress ─(finish-pr.sh)─► PR open, auto-merge on
        PR merged ─► issue closed, branch issue/<id> deleted, blockers re-evaluated
status:in-progress ─(no push for 60 min)─► + stale ─(reclaim.sh --to)─► new owner
status:in-progress ─(protected-path rebase conflict, or escalate.sh)─► status:needs-architect
status:needs-architect ─(finish-pr.sh gets past the rebase)─► status:in-progress
```

## 6. Claim lock and branch lifecycle

- **Branch name**: every issue uses `issue/<id>`, whatever its type. v1.0's `feat/` and `fix/` split would let a relabelled issue hold two locks.
- **Acquiring**: `claim.sh` creates the ref through GitHub's create-reference API, pointing at the current `origin/main` commit. That call fails if the ref exists. A plain `git push` must not be used: pushing a commit a ref already points at succeeds as a no-op, so two claimers could both "win".
- **WIP**: the lock branch is also the work branch. There is no `wip/*`. Sessions commit and push to it as they go, so work survives a dead laptop.
- **Stale**: an in-progress issue is stale when 60 minutes have passed since the later of its claim comment and the commit date of the branch head.
- **Handover**: `reclaim.sh` moves ownership of the same branch to another teammate.
- **Release**: merging the PR deletes the branch (repo setting "delete head branch on merge"), which frees the lock.
- **Worktree path**: `../<repo>-wt-<id>`, where `<repo>` is the control clone's folder name.
- **Ports**: each worktree gets the lowest free slot on that machine, and `PORT_OFFSET` is slot times 10. Offsets are per worktree, not per teammate, because ports only collide on one machine.

## 7. Script contracts

Rules for every script:
- It never prompts. Anything that needs confirmation takes it as an argument.
- It is idempotent: re-running after a partial failure completes the work.
- It accepts `--as <p-tag>`.
- It refuses to act on an issue whose author is neither a `team.json` login nor a `trusted_bots` entry.

Exit codes:

| Code | Meaning |
| :--- | :--- |
| 0 | Success |
| 1 | Unexpected failure |
| 2 | Bad usage |
| 10 | Issue not claimable (lock held, not ready, wrong owner, untrusted author) |
| 11 | `Touches` violation |
| 12 | `Verify` failed |
| 13 | Rebase conflict that needs a human or the Architect |
| 14 | Waiting on an unmerged contract PR |
| 15 | Environment or repo precondition not met |

### 7.1 `doctor.sh [--fix]`
Read-only by default. Checks:
- Tools in `required_tools` exist; `gh` is authenticated with `repo` and `workflow` scopes.
- The current login is in `team.json` and has push access.
- The control clone is on clean `main`; `core.hooksPath` points at `scripts/git-hooks`.
- Remote state: all labels exist; the `main` ruleset is active; auto-merge, squash merge and delete-branch-on-merge are on; the collaborators-only interaction limit is active.

It exits 15 if `main` is unprotected. `--fix` applies local fixes only (the hooks path). It never changes remote settings.

### 7.2 `init-hackathon.sh`
Run once by the repo admin after filling in `team.json`. Idempotent. In order:
1. Invites any `team.json` member who is not yet a collaborator.
2. Regenerates `.github/CODEOWNERS` from `protected_paths` and `contract_owners`.
3. If `main` is not yet protected, commits `harness/team.json` and the regenerated CODEOWNERS and pushes them as the one setup commit.
4. Creates all labels.
5. Turns on auto-merge, squash merge and delete-branch-on-merge.
6. Applies the `main` ruleset: pull request required, required check `ci`, zero approvals, code-owner review required, no force pushes, no deletion.
7. Sets the repo interaction limit to collaborators only, for the maximum duration.
8. Creates the Phase 0 issue: `type:contract`, `role:architect`, `owner:<architect>`, `status:ready`, `Touches: **`.

### 7.3 `bootstrap-issues.sh [issues.json] [--allow-overlap] [--dry-run-fixture]`
Refuses to run unless the Phase 0 issue is closed and `contracts/` on `main` holds more than its README.

Manifest entry:

```json
{
  "key": "api-auth",
  "title": "Auth endpoints",
  "type": "feature",
  "role": "builder",
  "owner": "p3",
  "touches": ["backend/auth/**"],
  "blocked_by": ["db-schema"],
  "verify": "uv run pytest backend/auth",
  "objective": "…",
  "contract_reference": ["…"],
  "acceptance": ["…"]
}
```

Validation, all hard failures:
- Keys are unique; every `blocked_by` key exists; the dependency graph has no cycle.
- `owner` is in `team.json`; `touches` is non-empty.
- A `type:feature` or `type:integration` issue lists no protected path.
- No two issues have overlapping `touches` unless one blocks the other, directly or transitively. Two issues overlap when both match an existing tracked file, or when one pattern's literal folder prefix contains the other's. This is deliberately conservative; `--allow-overlap` downgrades it to a warning.

Creation runs in two passes. Pass one creates each issue whose `harness-key` marker isn't already on GitHub, with no status label. Pass two rewrites `Blocked-By` with real issue numbers and sets `status:ready` or `status:blocked`. An issue is not claimable between the passes.

`--dry-run-fixture` skips the Phase 0 gate and creates the two dry-run issues from section 11.

### 7.4 `claim.sh <issue-id> [--resume]`
Run from the control clone.
1. Runs `sync.sh`. A failure there is a warning, not a stop.
2. Checks the issue is open, `status:ready`, owned by the caller, and from a trusted author. Otherwise exits 10.
3. Acquires the lock (section 6). Exits 10 if the ref exists.
4. Swaps `status:ready` for `status:in-progress`, assigns the caller, and posts a claim comment.
5. Creates the worktree on `issue/<id>`.
6. Copies `.env` from the control clone, picks a port slot, and writes `.env.local` and `.harness/task.json` (issue id, branch, slot, `Touches`, `Verify`).
7. Runs `scripts/check.sh setup` in the worktree to install dependencies.
8. Prints `cd ../<repo>-wt-<id> && claude`.

`--resume` is for an issue handed over by `reclaim.sh`: it requires `status:in-progress` and the caller as owner, skips steps 3 and 4, and builds the worktree from the existing remote branch.

### 7.5 `finish-pr.sh`
Run inside the worktree; reads the issue from `.harness/task.json`.
1. Refuses if there are uncommitted changes. Re-reads the issue and refreshes `.harness/task.json`, so the hook and CI check the same `Touches`.
2. If the worktree is stacked on a contract issue that is still open, exits 14.
3. Rebases onto `origin/main`.
   - A conflict confined to `lockfiles` is resolved by taking `main`'s version and re-running `check.sh setup`.
   - A conflict in a protected path aborts the rebase, sets `status:needs-architect`, posts the conflict details, and exits 13.
   - Any other conflict aborts the rebase and exits 13 for the session to resolve.
4. Runs the `Touches` check (section 8.1) on everything changed relative to `origin/main`. Exits 11 on a violation.
5. Runs `Verify`. Exits 12 on failure.
6. Pushes `issue/<id>` and opens the PR with `Closes #<id>` in the body, or updates the existing PR.
7. Enables squash auto-merge and restores `status:in-progress` if the issue was `needs-architect`.

### 7.6 `propose-contract.sh "<title>" [--touches <globs>]` and `--stack <contract-issue-id>`
- The first form creates a `type:contract` issue owned by the caller (default `Touches: contracts/**`, `Verify: ./scripts/check.sh`) and claims it.
- `--stack`, run inside a feature worktree, rebases the feature branch onto the contract branch and records the contract issue and its head commit in `.harness/task.json`.
- When the contract PR has merged, `finish-pr.sh` rebases the feature commits onto `origin/main` from the recorded commit, so the squashed contract change is not replayed.

### 7.7 `escalate.sh "<what is needed>"`
Run inside a worktree. Sets `status:needs-architect` and posts the message on the issue. Used when a slice needs a file owned by another slice.

### 7.8 `reclaim.sh <issue-id> --to <p-tag> [--force]` and `--release`
- `--to` swaps the owner label and assignee, clears `stale`, keeps `status:in-progress`, and posts a handover comment. The new owner then runs `claim.sh <id> --resume`. Anyone may run it; `--force` is required unless the issue is flagged `stale`.
- `--release` undoes a mistaken claim: it deletes the lock branch and returns the issue to `status:ready`. It refuses if the branch has commits beyond `main`.

### 7.9 `sync.sh`
- Fetches `origin` and fast-forwards the control clone. Refuses to touch the control clone if it is dirty or off `main`.
- Prunes worktree records.
- Removes a worktree and its local branch only when GitHub reports its PR as merged, there are no uncommitted changes, and there are no unpushed commits. Anything else is skipped with a warning. It never forces a removal.

### 7.10 `reset-demo.sh --confirm <repo-name>`
- Closes (never deletes) issues labelled `dry-run`, and deletes only those issues' branches.
- Opens one cleanup issue and a PR closing it that removes the `dryrun/` folder. The issue is needed because CI fails any PR with no linked issue.
- Does nothing unless `--confirm` matches the repo name.

### 7.11 `check.sh [setup]`
The only stack-specific script. With no argument it runs lint, typecheck and unit tests. `setup` installs dependencies in a fresh worktree, preferring the local package cache. The Architect adjusts it in Phase 0 if the stack differs from the preset.

## 8. Enforcement

### 8.1 The `Touches` check
One function in `harness.py`, called from three places. A changed path passes if it matches the issue's `Touches` or `shared_paths`.

| Where | When | Catches |
| :--- | :--- | :--- |
| Pre-edit hook in `.claude/settings.json` | Before each file edit in a worktree session | The first bad edit, with the reason fed back to the agent |
| `finish-pr.sh` | Before the PR is opened | Edits made through shell commands, which the hook can't see |
| `ci.yml` | On every PR | A PR opened without `finish-pr.sh` |

CI finds the issue from `Closes #<id>` in the PR body. A PR with no linked issue, or linked to an issue from an untrusted author, fails.

The hook only acts in a worktree that has `.harness/task.json`. It does nothing in the control clone.

### 8.2 Protected paths
- Listed in `config.json`; CODEOWNERS assigns them to the contract owners, and the ruleset requires their review.
- Feature and integration issues cannot list them. Changes go through a `type:contract` or `type:hotfix` issue.

### 8.3 Repository settings
All set by `init-hackathon.sh` and verified by `doctor.sh`. The repo must be public, or on a plan that supports rulesets for private repos.

### 8.4 Public-repo exposure
Agent sessions read issue text, and `finish-pr.sh` executes `Verify` from an issue body. Two guards: the collaborators-only interaction limit stops outsiders filing or commenting, and the trusted-author check stops a stranger's issue being claimed or run even if someone mislabels it. CI uses the `pull_request` trigger, never `pull_request_target`.

### 8.5 Broken `main`
Two PRs that each pass CI can break `main` together, because branches need not be up to date. `main-health.yml` runs `check.sh` on every push to `main`. On failure it opens one `type:hotfix`, `status:ready` issue owned by whoever merged (the Architect if that login isn't in `team.json`), whose objective is the exact `git revert` of that commit. The policy is revert first, then re-land.

## 9. Phase 0 and contract changes

**Phase 0**
1. The Architect claims the Phase 0 issue created by `init-hackathon.sh`.
2. In that worktree they write `MASTER_PLAN.md`, `contracts/`, `.env.example`, the folder skeleton and `issues.json`, and adjust `check.sh` if needed.
3. `finish-pr.sh` opens the PR; the deputy approves it; it merges.
4. From the control clone, the Architect runs `bootstrap-issues.sh`.

**Changing a contract later**
1. The builder runs `propose-contract.sh "<title>"`, edits `contracts/` in the new worktree, and runs `finish-pr.sh` there.
2. Back in the feature worktree, they run `propose-contract.sh --stack <id>` and keep building against the proposed contract.
3. A contract owner reviews and approves; the contract PR merges.
4. The builder runs `finish-pr.sh` in the feature worktree. Until step 3 is done it exits 14.

## 10. `CLAUDE.md` and `SKILL.md`

### 10.1 `CLAUDE.md` (under 60 lines, always loaded)

```markdown
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
```

### 10.2 `.claude/skills/hackathon-harness/SKILL.md` (loaded on demand)

```markdown
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
```

## 11. Workflows, testing and timeline

### 11.1 Workflows
- **`ci.yml`**: on `pull_request` to `main`. One job named `ci` that always runs; no path filters, because a required check that is skipped never reports and the PR hangs. Steps: `Touches` check, harness unit tests if `scripts/` changed, `check.sh setup` with dependency caching, `check.sh`. Superseded runs on the same PR are cancelled. Under 60 seconds is the target; the job prints a warning past 120. It does not run the issue's `Verify`.
- **`main-health.yml`**: on push to `main`. See section 8.5. It opens at most one revert issue per failing commit.
- **`unblock-issues.yml`**: on issue closed. For each open `status:blocked` issue whose `Blocked-By` issues are all closed, swaps to `status:ready` and mentions the owner's login.
- **`stale-claim.yml`**: every 30 minutes. Adds `stale` and a comment to issues that meet the rule in section 6, and removes `stale` from issues that no longer do. GitHub's scheduler is best-effort, so flagging can lag.

There is no `cleanup-merged.yml`; the delete-branch-on-merge setting replaces it.

### 11.2 Testing the harness
- **Unit tests** (stdlib `unittest`): glob matching, issue-body parsing, manifest validation, overlap detection, port slot selection.
- **End to end**: the dry run below, against real GitHub. The GitHub API is not mocked.

### 11.3 Dry run
Run on the real repo, on at least two laptops, before the hackathon.
1. Everyone runs `doctor.sh --fix`, then `doctor.sh`, until it passes.
2. `bootstrap-issues.sh --dry-run-fixture` creates two `dry-run` issues: A is `type:contract` and touches `dryrun/contract/**` (a protected path), and B touches `dryrun/feature/**` and is blocked by A.
3. Two laptops run `claim.sh` on A at the same moment. Exactly one must win.
4. The winner finishes A. Confirm: the hook blocks an edit outside `dryrun/contract/`; the PR waits for a contract owner; after approval it merges with no click; the branch is deleted; B flips to `status:ready` and its owner is mentioned.
5. The other laptop claims and finishes B. Confirm it merges with zero approvals.
6. `reset-demo.sh --confirm <repo-name>`, then merge the cleanup PR.

### 11.4 Hackathon timeline
1. **Before**: admin runs `init-hackathon.sh`; the team runs the dry run.
2. **First 30 minutes (Phase 0)**: section 9. Builders review `MASTER_PLAN.md` and prepare toolchains.
3. **Sprints**: everyone cycles claim, build, finish. Contract owners review contract PRs as they arrive.
4. **Last 2 hours**: by team agreement, no new `type:feature` issues. Remaining bugs go through `type:hotfix` issues. The harness does not enforce this.

Teammates share `.env` secrets out of band. The harness only copies a local `.env` into each worktree.

## 12. Plan B (deferred)

Plan B replaces interactive sessions with headless workers on a shared API budget, using the same repo, issues, lock and scripts. v1 leaves three seams and builds nothing else:
1. Scripts never prompt and return the exit codes in section 7.
2. `--as <p-tag>` exists, because pooled workers share one GitHub login.
3. The `status:needs-human` label exists.

Design notes carried over from v1.0 for whoever builds it: a hard spend cap per worker (`--max-budget-usd` is confirmed on the current CLI), a turn cap, and a rule that two `Verify` failures move the issue to `status:needs-human` instead of retrying.

## 13. Open risks and derived details

### 13.1 Unverified — the dry run must confirm each
1. GitHub can require code-owner review while general approvals stay at zero. If not, the merge rule in section 4 and the contract flow in section 9 need another mechanism.
2. The create-reference API rejects an existing ref and lets exactly one of two simultaneous requests win.
3. Closing an issue through an auto-merged PR triggers `unblock-issues.yml`.
4. A Claude Code pre-edit hook can block an edit and return a reason to the agent, including in sessions that skip permission prompts.
5. `--max-turns` exists on the current CLI. It did not appear in `claude --help`. Plan B only.

### 13.2 Filled in while writing — not explicitly decided
1. The lock uses the create-reference API, not `git push` (section 6).
2. `reset-demo.sh` takes `--confirm <repo-name>` as an argument, so it can require the repo name without prompting.
3. `github-actions[bot]` is a trusted issue author, so the revert issue from `main-health.yml` can be claimed.
4. `init-hackathon.sh` makes one setup commit to `main` before protection is on.
5. New scripts `propose-contract.sh` and `escalate.sh`, and the `setup` mode of `check.sh`.
6. `claim.sh` refuses an issue owned by someone else; `claim.sh --resume` and `reclaim.sh --release` exist.
7. Staleness is measured from the later of the claim comment and the branch head's commit date.
8. Feature and integration issues may not list protected paths; the Phase 0 issue has `Touches: **`.
9. `harness/config.json` is separate from `team.json`; the exit code numbers; the conservative overlap test.

## 14. Changes from v1.0

| Area | v1.0 | v1.1 |
| :--- | :--- | :--- |
| Scope | Plan A and Plan B | Plan A only; three seams for Plan B |
| Claim | "Atomic" label swap | Create-only remote branch `issue/<id>`; labels are display state |
| Branches | `feat/<id>`, `fix/<id>`, `wip/<id>` | `issue/<id>` only, doubling as the WIP branch |
| Sessions | One session hops into `../wt-<id>` | One fresh session per issue, launched in `../<repo>-wt-<id>` |
| Ports | Offset per teammate | Offset per worktree slot |
| `main` protection | Configured, not verified | Required; `doctor.sh` fails without it; pre-push hook |
| Merging | Any teammate merges after CI | Squash auto-merge on green, zero approvals |
| `Touches` | Checked in `finish-pr.sh`; Architect exempt | Hook, `finish-pr.sh` and CI; no exemption |
| Dependencies | Not addressed | Shared-path allowlist; lockfiles regenerated |
| Contract changes | Stop, label, wait for the Architect | Builder opens a contract PR and stacks on it; two contract owners |
| Protected paths | `contracts/` | Also `.env.example`, `scripts/`, `.github/`, `.claude/`, `harness/`, `CLAUDE.md` |
| Phase 0 gate | Builder issues start blocked | Bootstrap refuses to run until Phase 0 merges |
| Issue metadata | `---` block with five fields | Labels plus a fenced block with three fields |
| Manifest | YAML or Markdown | `issues.json` with keys, idempotent, overlap-checked |
| Identity | Fixed `p1`–`p4` | `harness/team.json` |
| Stack | pnpm and uv assumed | `check.sh` seam plus `config.json`; pnpm + uv preset |
| Script language | Bash | Bash entrypoints over stdlib Python |
| Stale claims | No PR after 60 minutes | No push for 60 minutes |
| Broken `main` | Hotfix flow, manually started | `main-health.yml` opens a revert issue |
| Branch cleanup | `cleanup-merged.yml` | Repo setting |
| Dry-run reset | Deletes issues and branches by pattern | Label-scoped, closes only, needs `--confirm` |
| Public-repo safety | Not addressed | Interaction limit; trusted-author check |
| Harness tests | Not addressed | Unit tests plus the dry run |
