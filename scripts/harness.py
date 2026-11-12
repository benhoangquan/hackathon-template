#!/usr/bin/env python3
"""Hackathon harness: all logic behind scripts/*.sh. Python 3.9+, stdlib only."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------- constants

OK, FAIL, USAGE, NOT_CLAIMABLE, TOUCHES, VERIFY, REBASE, CONTRACT_WAIT, PRECOND = (
    0, 1, 2, 10, 11, 12, 13, 14, 15)

STATUSES = ["blocked", "ready", "in-progress", "needs-architect", "needs-human"]
TYPES = ["contract", "feature", "hotfix", "integration"]
ROLES = ["architect", "builder"]
RULESET_NAME = "harness-main"
CLAIM_MARK = "<!-- harness:claim -->"
MAIN = "main"

LABEL_COLORS = {"status": "fbca04", "role": "5319e7", "owner": "0e8a16",
                "type": "1d76db", "flag": "d93f0b"}


class Fail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


class GhError(Fail):
    def __init__(self, status, msg):
        super().__init__(FAIL, msg)
        self.status = status


def warn(msg):
    print("warning: " + msg, file=sys.stderr)


def info(msg):
    print(msg)


# ------------------------------------------------------------ pure helpers

def glob_to_regex(pattern):
    """`*` within a segment, `**` across segments (incl. none), `?` one char."""
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                if pattern[i:i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path, pattern):
    return glob_to_regex(pattern).match(path) is not None


def matches_any(path, patterns):
    return any(matches(path, p) for p in patterns)


def touches_violations(changed, touches, shared):
    return [p for p in changed if not matches_any(p, list(touches) + list(shared))]


def literal_prefix(pattern):
    """None for an exact path, else the folder prefix before the first wildcard."""
    m = re.search(r"[*?]", pattern)
    if not m:
        return None
    head = pattern[:m.start()]
    return head.rsplit("/", 1)[0] + "/" if "/" in head else ""


def patterns_overlap(a, b, tracked=()):
    ra, rb = glob_to_regex(a), glob_to_regex(b)
    if any(ra.match(f) and rb.match(f) for f in tracked):
        return True
    pa, pb = literal_prefix(a), literal_prefix(b)
    if pa is None and pb is None:
        return a == b
    if pa is None:
        return rb.match(a) is not None
    if pb is None:
        return ra.match(b) is not None
    return pa.startswith(pb) or pb.startswith(pa)


def find_cycle(deps):
    """deps: key -> list of keys. Returns a cycle as a list, or None."""
    state, stack = {}, []

    def visit(k):
        state[k] = 1
        stack.append(k)
        for d in deps.get(k, []):
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if state.get(d) is None:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        state[k] = 2
        return None

    for k in list(deps):
        if state.get(k) is None:
            found = visit(k)
            if found:
                return found
    return None


def ancestors(key, deps):
    seen, todo = set(), list(deps.get(key, []))
    while todo:
        k = todo.pop()
        if k not in seen:
            seen.add(k)
            todo.extend(deps.get(k, []))
    return seen


def validate_manifest(entries, members, protected, tracked=(), allow_overlap=False):
    errors, warnings = [], []
    keys = [e.get("key") for e in entries]
    for k in set(keys):
        if keys.count(k) > 1:
            errors.append("duplicate key: %s" % k)
    keyset = set(keys)
    deps = {}
    for e in entries:
        k = e.get("key")
        if not k:
            errors.append("entry without key: %r" % (e.get("title"),))
            continue
        for f in ("title", "type", "owner", "verify"):
            if not e.get(f):
                errors.append("%s: missing %s" % (k, f))
        if e.get("type") and e["type"] not in TYPES:
            errors.append("%s: unknown type %s" % (k, e["type"]))
        if e.get("role", "builder") not in ROLES:
            errors.append("%s: unknown role %s" % (k, e.get("role")))
        if e.get("owner") and e["owner"] not in members:
            errors.append("%s: owner %s is not in team.json" % (k, e["owner"]))
        if not e.get("touches"):
            errors.append("%s: touches is empty" % k)
        if "\n" in (e.get("verify") or ""):
            errors.append("%s: verify must be a single line" % k)
        if e.get("type") in ("feature", "integration"):
            for p in e.get("touches", []):
                if any(patterns_overlap(p, q, tracked) for q in protected):
                    errors.append("%s: %s issue lists protected path %s" % (k, e["type"], p))
        deps[k] = list(e.get("blocked_by", []))
        for d in deps[k]:
            if d not in keyset:
                errors.append("%s: blocked_by unknown key %s" % (k, d))
    if not errors:
        cyc = find_cycle(deps)
        if cyc:
            errors.append("dependency cycle: " + " -> ".join(cyc))
    if not errors:
        anc = {k: ancestors(k, deps) for k in deps}
        for i, a in enumerate(entries):
            for b in entries[i + 1:]:
                ka, kb = a["key"], b["key"]
                if ka in anc[kb] or kb in anc[ka]:
                    continue
                for pa in a["touches"]:
                    hit = next((pb for pb in b["touches"]
                                if patterns_overlap(pa, pb, tracked)), None)
                    if hit:
                        msg = "%s and %s overlap: %s vs %s" % (ka, kb, pa, hit)
                        (warnings if allow_overlap else errors).append(msg)
                        break
    return errors, warnings


def pick_slot(used):
    used = set(used)
    n = 1
    while n in used:
        n += 1
    return n


def parse_body(body):
    """Extract Touches, Blocked-By, Verify and harness-key from an issue body."""
    out = {"touches": [], "blocked_by": [], "verify": "", "key": None}
    m = re.search(r"```harness\s*\n(.*?)\n```", body or "", re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "touches":
                out["touches"] = [t.strip() for t in v.split(",") if t.strip()]
            elif k == "blocked-by":
                out["blocked_by"] = [int(x) for x in re.findall(r"#(\d+)", v)]
            elif k == "verify":
                out["verify"] = v
    k = re.search(r"<!--\s*harness-key:\s*(\S+)\s*-->", body or "")
    if k:
        out["key"] = k.group(1)
    return out


def build_body(touches, blocked, verify, objective, contract_ref, acceptance, key):
    bl = ", ".join("#%d" % n for n in blocked) if blocked else "none"
    lines = ["```harness",
             "Touches: " + ", ".join(touches),
             "Blocked-By: " + bl,
             "Verify: " + verify,
             "```", "",
             "### Objective", objective or "", "",
             "### Contract Reference"]
    lines += ["- " + c for c in (contract_ref or [])] or ["- none"]
    lines += ["", "### Acceptance Criteria"]
    lines += ["- [ ] " + a for a in (acceptance or [])] or ["- [ ] Verify passes"]
    lines += ["", "<!-- harness-key: %s -->" % key]
    return "\n".join(lines)


def codeowners_text(protected, owner_logins):
    owners = " ".join("@" + l for l in owner_logins)
    lines = ["# Generated by scripts/init-hackathon.sh from harness/config.json and team.json.",
             "# Do not edit by hand."]
    for p in protected:
        if p.endswith("/**"):
            pat = "/" + p[:-3] + "/"
        else:
            pat = "/" + p.lstrip("/")
        lines.append("%s %s" % (pat, owners))
    return "\n".join(lines) + "\n"


def parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def is_stale(claim_ts, head_ts, now, minutes):
    times = [t for t in (claim_ts, head_ts) if t]
    return bool(times) and (now - max(times)).total_seconds() > minutes * 60


# --------------------------------------------------------- process helpers

def run(cmd, cwd=None, input=None, check=True, env=None):
    p = subprocess.run(cmd, cwd=cwd, input=input, text=True, capture_output=True, env=env)
    if check and p.returncode != 0:
        raise Fail(FAIL, "command failed: %s\n%s" % (" ".join(cmd),
                                                     (p.stderr or p.stdout or "").strip()))
    return p


def git(*args, cwd=None, check=True):
    return run(["git"] + list(args), cwd=cwd, check=check)


_slug = None


def repo_slug():
    global _slug
    if _slug is None:
        _slug = (os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY") or
                 run(["gh", "repo", "view", "--json", "nameWithOwner", "-q",
                      ".nameWithOwner"]).stdout.strip())
    return _slug


def gh_api(method, path, body=None):
    cmd = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
    inp = None
    if body is not None:
        cmd += ["--input", "-"]
        inp = json.dumps(body)
    p = run(cmd, input=inp, check=False)
    if p.returncode != 0:
        text = (p.stderr or "") + (p.stdout or "")
        m = re.search(r"HTTP (\d+)", text)
        raise GhError(int(m.group(1)) if m else 0, text.strip())
    out = p.stdout.strip()
    return json.loads(out) if out else None


def gh_list(path):
    p = run(["gh", "api", "--paginate", "--slurp", path], check=False)
    if p.returncode != 0:
        raise GhError(0, (p.stderr or p.stdout).strip())
    data = json.loads(p.stdout or "[]")
    flat = []
    for page in data:
        flat.extend(page if isinstance(page, list) else [page])
    return flat


def repo_root(cwd=None):
    return os.path.realpath(git("rev-parse", "--show-toplevel", cwd=cwd).stdout.strip())


def list_worktrees(cwd=None):
    out = git("worktree", "list", "--porcelain", cwd=cwd).stdout
    items, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
                cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    return items


def control_root(cwd=None):
    return os.path.realpath(list_worktrees(cwd)[0]["worktree"])


def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def load_cfg(root):
    team = load_json(os.path.join(root, "harness", "team.json"))
    cfg = load_json(os.path.join(root, "harness", "config.json"))
    return team, cfg


class Member(object):
    def __init__(self, tag, login, role):
        self.tag, self.login, self.role = tag, login, role


def resolve_member(team, as_tag):
    members = team["members"]
    if as_tag:
        if as_tag not in members:
            raise Fail(USAGE, "--as %s is not in team.json" % as_tag)
        m = members[as_tag]
        return Member(as_tag, m["login"], m["role"])
    login = run(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
    for tag, m in members.items():
        if m["login"].lower() == login.lower():
            return Member(tag, m["login"], m["role"])
    raise Fail(PRECOND, "gh login %s is not in harness/team.json (use --as)" % login)


def trusted_logins(team, cfg):
    return set(m["login"].lower() for m in team["members"].values()) | \
        set(b.lower() for b in cfg.get("trusted_bots", []))


# ------------------------------------------------------------------ issues

class Issue(object):
    def __init__(self, data):
        self.number = data["number"]
        self.title = data["title"]
        self.body = data.get("body") or ""
        self.state = data["state"]
        self.labels = [l["name"] for l in data.get("labels", [])]
        self.assignees = [a["login"] for a in data.get("assignees", [])]
        self.author = (data.get("user") or {}).get("login", "")
        parsed = parse_body(self.body)
        self.touches, self.blocked_by = parsed["touches"], parsed["blocked_by"]
        self.verify, self.key = parsed["verify"], parsed["key"]

    def _label(self, prefix):
        for l in self.labels:
            if l.startswith(prefix):
                return l[len(prefix):]
        return None

    status = property(lambda s: s._label("status:"))
    owner = property(lambda s: s._label("owner:"))
    type = property(lambda s: s._label("type:"))
    role = property(lambda s: s._label("role:"))


def get_issue(n):
    data = gh_api("GET", "repos/%s/issues/%d" % (repo_slug(), n))
    if "pull_request" in data:
        raise Fail(USAGE, "#%d is a pull request, not an issue" % n)
    return Issue(data)


def all_issues(state="all", labels=None):
    path = "repos/%s/issues?state=%s&per_page=100" % (repo_slug(), state)
    if labels:
        path += "&labels=" + ",".join(labels)
    return [Issue(d) for d in gh_list(path) if "pull_request" not in d]


def check_trusted(issue, team, cfg):
    if issue.author.lower() not in trusted_logins(team, cfg):
        raise Fail(NOT_CLAIMABLE, "issue #%d was opened by untrusted author %s" %
                   (issue.number, issue.author))


def edit_labels(n, add=(), remove=()):
    cur = [l["name"] for l in gh_api("GET", "repos/%s/issues/%d/labels" % (repo_slug(), n))]
    new = sorted((set(cur) - set(remove)) | set(add))
    if new != sorted(cur):
        gh_api("PUT", "repos/%s/issues/%d/labels" % (repo_slug(), n), {"labels": new})


def set_status(n, status):
    edit_labels(n, add=["status:" + status],
                remove=["status:" + s for s in STATUSES if s != status])


def comment(n, body):
    gh_api("POST", "repos/%s/issues/%d/comments" % (repo_slug(), n), {"body": body})


def create_issue(title, body, labels):
    return gh_api("POST", "repos/%s/issues" % repo_slug(),
                  {"title": title, "body": body, "labels": labels})["number"]


def set_assignee(n, login):
    cur = [a["login"] for a in gh_api("GET", "repos/%s/issues/%d" %
                                      (repo_slug(), n))["assignees"]]
    stale = [a for a in cur if a.lower() != login.lower()]
    if stale:
        gh_api("DELETE", "repos/%s/issues/%d/assignees" % (repo_slug(), n),
               {"assignees": stale})
    gh_api("POST", "repos/%s/issues/%d/assignees" % (repo_slug(), n), {"assignees": [login]})


def find_by_key(key, issues=None):
    for i in (issues if issues is not None else all_issues()):
        if i.key == key:
            return i
    return None


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------- worktree helpers

def worktree_path(root, n):
    return os.path.join(os.path.dirname(root), "%s-wt-%d" % (os.path.basename(root), n))


def read_task(wt):
    p = os.path.join(wt, ".harness", "task.json")
    if not os.path.exists(p):
        raise Fail(PRECOND, "no .harness/task.json in %s: not a worktree made by claim.sh" % wt)
    return load_json(p)


def write_task(wt, task):
    save_json(os.path.join(wt, ".harness", "task.json"), task)


def dirty(wt):
    return bool(git("status", "--porcelain", cwd=wt).stdout.strip())


def run_check(wt, mode=None):
    cmd = [os.path.join(wt, "scripts", "check.sh")] + ([mode] if mode else [])
    p = subprocess.run(cmd, cwd=wt)
    return p.returncode


def default_ref_sha():
    return gh_api("GET", "repos/%s/git/ref/heads/%s" % (repo_slug(), MAIN))["object"]["sha"]


# -------------------------------------------------------------------- sync

def pr_for_branch(branch, state="all"):
    owner = repo_slug().split("/")[0]
    prs = gh_api("GET", "repos/%s/pulls?head=%s:%s&state=%s" %
                 (repo_slug(), owner, branch.replace("/", "%2F"), state)) or []
    return prs


def do_sync(root):
    warnings = []
    git("fetch", "origin", "--prune", cwd=root)
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip()
    if branch != MAIN or dirty(root):
        warnings.append("control clone is dirty or off main; not fast-forwarding")
    else:
        git("merge", "--ff-only", "origin/" + MAIN, cwd=root)
    git("worktree", "prune", cwd=root)
    for wt in list_worktrees(root)[1:]:
        ref = wt.get("branch", "")
        m = re.match(r"refs/heads/(issue/\d+)$", ref)
        if not m:
            continue
        br, path = m.group(1), wt["worktree"]
        merged = [p for p in pr_for_branch(br, "closed") if p.get("merged_at")]
        if not merged:
            continue
        if dirty(path):
            warnings.append("%s has uncommitted changes; keeping" % path)
            continue
        head = git("rev-parse", "HEAD", cwd=path).stdout.strip()
        pushed = merged[0]["head"]["sha"]
        if head != pushed and git("merge-base", "--is-ancestor", head, pushed,
                                  cwd=root, check=False).returncode != 0:
            warnings.append("%s has unpushed commits; keeping" % path)
            continue
        if git("worktree", "remove", path, cwd=root, check=False).returncode != 0:
            warnings.append("could not remove %s; keeping" % path)
            continue
        git("branch", "-D", br, cwd=root, check=False)
        info("removed merged worktree %s" % path)
    return warnings


def cmd_sync(args):
    for w in do_sync(control_root()):
        warn(w)
    return OK


# ------------------------------------------------------------------- claim

def claim_issue(root, team, cfg, me, n, resume=False):
    try:
        for w in do_sync(root):
            warn(w)
    except Fail as e:
        warn("sync failed: %s" % e.msg)
    iss = get_issue(n)
    check_trusted(iss, team, cfg)
    if iss.state != "open":
        raise Fail(NOT_CLAIMABLE, "issue #%d is not open" % n)
    if iss.owner != me.tag:
        raise Fail(NOT_CLAIMABLE, "issue #%d is owned by %s, not %s" % (n, iss.owner, me.tag))
    branch = "issue/%d" % n
    wt = worktree_path(root, n)
    local = os.path.exists(os.path.join(wt, ".harness", "task.json"))
    acquired = False
    if resume:
        if iss.status != "in-progress":
            raise Fail(NOT_CLAIMABLE, "--resume needs status:in-progress, got %s" % iss.status)
    elif iss.status == "in-progress" and local:
        info("re-running claim for a worktree that already exists")
    elif iss.status != "ready":
        raise Fail(NOT_CLAIMABLE, "issue #%d is not status:ready (%s)" % (n, iss.status))
    else:
        try:
            gh_api("POST", "repos/%s/git/refs" % repo_slug(),
                   {"ref": "refs/heads/" + branch, "sha": default_ref_sha()})
            acquired = True
        except GhError as e:
            if e.status == 422 or "already exists" in e.msg:
                if not local:
                    raise Fail(NOT_CLAIMABLE, "lock held: %s already exists on origin" % branch)
                info("lock already held by this machine; continuing")
            else:
                raise
    if acquired:
        edit_labels(n, add=["status:in-progress"], remove=["status:ready"])
        set_assignee(n, me.login)
        comment(n, "%s\nClaimed by @%s (%s) at %s on branch `%s`." %
                (CLAIM_MARK, me.login, me.tag, now_iso(), branch))
    # worktree
    if not os.path.exists(os.path.join(wt, ".git")):
        git("fetch", "origin", "%s:refs/remotes/origin/%s" % (branch, branch), cwd=root)
        have_local = git("show-ref", "--verify", "--quiet", "refs/heads/" + branch,
                         cwd=root, check=False).returncode == 0
        if have_local:
            git("worktree", "add", wt, branch, cwd=root)
        else:
            git("worktree", "add", "--track", "-b", branch, wt, "origin/" + branch, cwd=root)
    env_src = os.path.join(root, ".env")
    if os.path.exists(env_src) and not os.path.exists(os.path.join(wt, ".env")):
        shutil.copy(env_src, os.path.join(wt, ".env"))
    prev = os.path.join(wt, ".harness", "task.json")
    task = load_json(prev) if os.path.exists(prev) else {}
    if "slot" not in task:
        used = set()
        for other in list_worktrees(root)[1:]:
            tp = os.path.join(other["worktree"], ".harness", "task.json")
            if os.path.exists(tp):
                try:
                    used.add(load_json(tp).get("slot"))
                except ValueError:
                    pass
        task["slot"] = pick_slot(x for x in used if x)
    task.update({"issue": n, "branch": branch, "title": iss.title, "type": iss.type,
                 "touches": iss.touches, "verify": iss.verify})
    write_task(wt, task)
    with open(os.path.join(wt, ".env.local"), "w") as f:
        f.write("PORT_OFFSET=%d\n" % (task["slot"] * 10))
    if run_check(wt, "setup") != 0:
        raise Fail(PRECOND, "check.sh setup failed in %s; fix and re-run claim.sh %d" % (wt, n))
    info("Claimed #%d. Next:\n  cd ../%s && claude" % (n, os.path.basename(wt)))
    return wt


def cmd_claim(args):
    root = control_root()
    team, cfg = load_cfg(root)
    claim_issue(root, team, cfg, resolve_member(team, args.as_tag), args.issue, args.resume)
    return OK


# ------------------------------------------------------------------ finish

def contract_is_merged(c):
    iss = get_issue(c)
    if iss.state != "closed":
        return False
    return any(p.get("merged_at") for p in pr_for_branch("issue/%d" % c, "closed"))


def rebase_onto_main(wt, task, cfg, n):
    git("fetch", "origin", MAIN, cwd=wt)
    if task.get("contract_issue"):
        cmd = ["rebase", "--onto", "origin/" + MAIN, task["contract_head"]]
    else:
        cmd = ["rebase", "origin/" + MAIN]
    p = git(*cmd, cwd=wt, check=False)
    env = dict(os.environ, GIT_EDITOR="true")
    resolved_lock = False
    for _ in range(200):
        if p.returncode == 0:
            break
        conflicted = git("diff", "--name-only", "--diff-filter=U", cwd=wt).stdout.split()
        if not conflicted:
            if "empty" in (p.stderr + p.stdout) and \
                    git("rebase", "--skip", cwd=wt, check=False).returncode == 0:
                break
            git("rebase", "--abort", cwd=wt, check=False)
            raise Fail(REBASE, "rebase failed:\n%s" % (p.stderr or p.stdout))
        prot = [f for f in conflicted if matches_any(f, cfg["protected_paths"])]
        if prot:
            git("rebase", "--abort", cwd=wt, check=False)
            set_status(n, "needs-architect")
            comment(n, "Rebase onto `main` conflicts in protected paths: %s\n"
                       "A contract owner or the Architect needs to resolve this." %
                    ", ".join("`%s`" % f for f in prot))
            raise Fail(REBASE, "conflict in protected path(s): %s; status:needs-architect set"
                       % ", ".join(prot))
        other = [f for f in conflicted if not matches_any(f, cfg["lockfiles"])]
        if other:
            git("rebase", "--abort", cwd=wt, check=False)
            raise Fail(REBASE, "rebase conflict in: %s\nRe-run `git rebase origin/%s`, resolve, "
                       "`git rebase --continue`, then re-run finish-pr.sh." %
                       (", ".join(other), MAIN))
        for f in conflicted:
            git("checkout", "--ours", "--", f, cwd=wt)
            git("add", f, cwd=wt)
        resolved_lock = True
        p = run(["git", "rebase", "--continue"], cwd=wt, check=False, env=env)
    else:
        raise Fail(REBASE, "rebase did not converge")
    if resolved_lock:
        if run_check(wt, "setup") != 0:
            raise Fail(PRECOND, "check.sh setup failed after lockfile resolution")
        if git("status", "--porcelain", "--", *cfg["lockfiles"], cwd=wt).stdout.strip():
            git("add", *cfg["lockfiles"], cwd=wt, check=False)
            git("commit", "-m", "chore: regenerate lockfile after rebase", cwd=wt)


def do_finish(wt, team, cfg):
    task = read_task(wt)
    n = task["issue"]
    branch = task["branch"]
    if dirty(wt):
        raise Fail(PRECOND, "uncommitted changes in %s; commit them first" % wt)
    iss = get_issue(n)
    check_trusted(iss, team, cfg)
    task.update({"touches": iss.touches, "verify": iss.verify, "title": iss.title})
    write_task(wt, task)
    c = task.get("contract_issue")
    if c and not contract_is_merged(c):
        raise Fail(CONTRACT_WAIT, "contract PR for #%d has not merged yet; ask a contract "
                   "owner to review it" % c)
    git("fetch", "origin", branch, cwd=wt, check=False)
    rebase_onto_main(wt, task, cfg, n)
    if c:
        task.pop("contract_issue", None)
        task.pop("contract_head", None)
        write_task(wt, task)
    changed = git("diff", "--name-only", "origin/%s...HEAD" % MAIN, cwd=wt).stdout.split()
    bad = touches_violations(changed, iss.touches, cfg["shared_paths"])
    if bad:
        raise Fail(TOUCHES, "files outside Touches:\n  " + "\n  ".join(bad) +
                   "\nRevert them, or run ./scripts/escalate.sh \"<what you need>\".")
    if not iss.verify:
        raise Fail(VERIFY, "issue #%d has no Verify command" % n)
    info("Running Verify: %s" % iss.verify)
    if subprocess.run(iss.verify, shell=True, cwd=wt).returncode != 0:
        raise Fail(VERIFY, "Verify failed")
    git("push", "--force-with-lease", "-u", "origin", branch, cwd=wt)
    slug = repo_slug()
    prs = pr_for_branch(branch, "open")
    if prs:
        pr = prs[0]
        if not re.search(r"(?i)\bcloses\s+#%d\b" % n, pr.get("body") or ""):
            gh_api("PATCH", "repos/%s/pulls/%d" % (slug, pr["number"]),
                   {"body": "Closes #%d\n\n%s" % (n, pr.get("body") or "")})
    else:
        pr = gh_api("POST", "repos/%s/pulls" % slug,
                    {"title": iss.title, "head": branch, "base": MAIN,
                     "body": "Closes #%d\n\n%s" % (n, iss.title)})
    iss = get_issue(n)
    if iss.status == "needs-architect":
        set_status(n, "in-progress")
    p = run(["gh", "pr", "merge", str(pr["number"]), "--auto", "--squash", "-R", slug],
            check=False)
    if p.returncode != 0 and "already" not in (p.stderr + p.stdout).lower():
        warn("could not enable auto-merge: %s" % (p.stderr or p.stdout).strip())
    info("PR #%d open with auto-merge: %s" % (pr["number"], pr.get("html_url", "")))


def cmd_finish(args):
    wt = repo_root()
    root = control_root()
    team, cfg = load_cfg(wt)
    resolve_member(team, args.as_tag)
    do_finish(wt, team, cfg)
    return OK


# ------------------------------------------------- propose-contract/escalate

def cmd_propose_contract(args):
    here = repo_root()
    root = control_root()
    team, cfg = load_cfg(here)
    me = resolve_member(team, args.as_tag)
    if args.stack is not None:
        task = read_task(here)
        if dirty(here):
            raise Fail(PRECOND, "commit your changes before stacking")
        c = args.stack
        git("fetch", "origin", "issue/%d" % c + ":refs/remotes/origin/issue/%d" % c, cwd=here)
        git("rebase", "origin/issue/%d" % c, cwd=here)
        task["contract_issue"] = c
        task["contract_head"] = git("rev-parse", "origin/issue/%d" % c, cwd=here).stdout.strip()
        write_task(here, task)
        info("Stacked on contract issue #%d. finish-pr.sh will wait for it to merge." % c)
        return OK
    if not args.title:
        raise Fail(USAGE, "usage: propose-contract.sh \"<title>\" [--touches <globs>]")
    touches = [t.strip() for t in (args.touches or "contracts/**").split(",") if t.strip()]
    key = "contract-%s-%d" % (me.tag, int(datetime.datetime.now().timestamp()))
    body = build_body(touches, [], "./scripts/check.sh", args.title, [],
                      ["Contract change reviewed by a contract owner"], key)
    n = create_issue(args.title, body, ["type:contract", "role:" + me.role, "owner:" + me.tag,
                                        "status:ready"])
    info("Created contract issue #%d" % n)
    claim_issue(root, team, cfg, me, n)
    return OK


def cmd_escalate(args):
    wt = repo_root()
    task = read_task(wt)
    n = task["issue"]
    set_status(n, "needs-architect")
    comment(n, "Escalated from `%s`:\n\n%s" % (task["branch"], args.message))
    info("Issue #%d set to status:needs-architect." % n)
    return OK


# ----------------------------------------------------------------- reclaim

def cmd_reclaim(args):
    root = control_root()
    team, cfg = load_cfg(root)
    n = args.issue
    iss = get_issue(n)
    check_trusted(iss, team, cfg)
    if args.release:
        branch = "issue/%d" % n
        cmp_ = gh_api("GET", "repos/%s/compare/%s...%s" % (repo_slug(), MAIN, branch.replace("/", "%2F")))
        if cmp_.get("ahead_by", 0) > 0:
            raise Fail(NOT_CLAIMABLE, "%s has %d commit(s) beyond main; not releasing" %
                       (branch, cmp_["ahead_by"]))
        try:
            gh_api("DELETE", "repos/%s/git/refs/heads/%s" % (repo_slug(), branch))
        except GhError as e:
            if e.status != 422 and e.status != 404:
                raise
        stale = [a for a in iss.assignees]
        if stale:
            gh_api("DELETE", "repos/%s/issues/%d/assignees" % (repo_slug(), n),
                   {"assignees": stale})
        edit_labels(n, add=["status:ready"], remove=["stale", "status:in-progress"])
        comment(n, "Claim released; the issue is `status:ready` again.")
        info("Released #%d. Remove any local worktree with `git worktree remove`." % n)
        return OK
    if not args.to:
        raise Fail(USAGE, "usage: reclaim.sh <issue-id> --to <p-tag> [--force] | --release")
    if args.to not in team["members"]:
        raise Fail(USAGE, "%s is not in team.json" % args.to)
    if iss.status != "in-progress":
        raise Fail(NOT_CLAIMABLE, "#%d is not in progress" % n)
    if "stale" not in iss.labels and not args.force:
        raise Fail(NOT_CLAIMABLE, "#%d is not flagged stale; use --force" % n)
    new = team["members"][args.to]
    edit_labels(n, add=["owner:" + args.to], remove=["owner:" + iss.owner, "stale"] if iss.owner
                else ["stale"])
    set_assignee(n, new["login"])
    comment(n, "%s\nHanded over to @%s (%s) at %s. They should run "
               "`./scripts/claim.sh %d --resume`." % (CLAIM_MARK, new["login"], args.to,
                                                      now_iso(), n))
    info("#%d now belongs to %s. They run: ./scripts/claim.sh %d --resume" % (n, args.to, n))
    return OK


# -------------------------------------------------------------- reset-demo

def cmd_reset_demo(args):
    root = control_root()
    team, cfg = load_cfg(root)
    name = repo_slug().split("/")[1]
    if args.confirm != name:
        raise Fail(USAGE, "--confirm must equal the repo name (%s)" % name)
    me = resolve_member(team, args.as_tag)
    for iss in all_issues("all", ["dry-run"]):
        if iss.state == "open":
            gh_api("PATCH", "repos/%s/issues/%d" % (repo_slug(), iss.number), {"state": "closed"})
            info("closed #%d" % iss.number)
        try:
            gh_api("DELETE", "repos/%s/git/refs/heads/issue/%d" % (repo_slug(), iss.number))
            info("deleted branch issue/%d" % iss.number)
        except GhError as e:
            if e.status not in (404, 422):
                raise
    git("fetch", "origin", cwd=root)
    tracked = git("ls-tree", "-r", "--name-only", "origin/" + MAIN, "dryrun/", cwd=root).stdout
    if not tracked.strip():
        info("no dryrun/ folder on main; nothing to clean up")
        return OK
    key = "dryrun-cleanup"
    existing = find_by_key(key)
    if existing and existing.state == "open":
        n = existing.number
    else:
        body = build_body(["dryrun/**"], [], "./scripts/check.sh",
                          "Remove the dryrun/ folder after the dry run.", [],
                          ["dryrun/ no longer exists"], key)
        n = create_issue("Clean up dry run", body,
                         ["type:hotfix", "role:" + me.role, "owner:" + me.tag, "status:ready"])
    wt = claim_issue(root, team, cfg, me, n)
    git("rm", "-r", "-q", "dryrun", cwd=wt, check=False)
    if git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode != 0:
        git("commit", "-m", "chore: remove dry-run fixtures", cwd=wt)
    do_finish(wt, team, cfg)
    info("Merge the cleanup PR (it waits for a contract owner because dryrun/contract/** is protected).")
    return OK


# --------------------------------------------------------------- bootstrap

def dry_run_fixture(team):
    owners = team.get("contract_owners", [])
    a_owner = owners[0]
    b_owner = owners[1] if len(owners) > 1 else owners[0]
    return [
        {"key": "dryrun-a", "title": "Dry run A: contract", "type": "contract", "role": "builder",
         "owner": a_owner, "touches": ["dryrun/contract/**"], "blocked_by": [],
         "verify": "test -n \"$(ls dryrun/contract 2>/dev/null)\"",
         "objective": "Add a file under dryrun/contract/. First try editing a file outside it and confirm the hook blocks you.",
         "contract_reference": ["none"],
         "acceptance": ["Hook blocked an edit outside Touches", "PR waits for a contract owner",
                        "PR merges with no click and the branch is deleted"]},
        {"key": "dryrun-b", "title": "Dry run B: feature", "type": "feature", "role": "builder",
         "owner": b_owner, "touches": ["dryrun/feature/**"], "blocked_by": ["dryrun-a"],
         "verify": "test -n \"$(ls dryrun/feature 2>/dev/null)\"",
         "objective": "Add a file under dryrun/feature/ once A has merged.",
         "contract_reference": ["none"],
         "acceptance": ["Issue flipped to status:ready and owner was mentioned",
                        "PR merges with zero approvals"]},
    ]


def cmd_bootstrap(args):
    root = control_root()
    team, cfg = load_cfg(root)
    resolve_member(team, args.as_tag)
    git("fetch", "origin", cwd=root)
    extra = []
    if args.dry_run_fixture:
        entries = dry_run_fixture(team)
        extra = ["dry-run"]
    else:
        phase0 = find_by_key("phase-0")
        if not phase0 or phase0.state != "closed":
            raise Fail(PRECOND, "Phase 0 issue is not closed yet; merge the Phase 0 PR first")
        files = git("ls-tree", "-r", "--name-only", "origin/" + MAIN, "contracts/",
                    cwd=root).stdout.split()
        if [f for f in files if f != "contracts/README.md"] == []:
            raise Fail(PRECOND, "contracts/ on main holds nothing beyond its README")
        path = os.path.join(root, args.manifest or "issues.json")
        data = load_json(path)
        entries = data["issues"] if isinstance(data, dict) else data
        if not entries:
            raise Fail(USAGE, "%s has no issues" % path)
    tracked = git("ls-tree", "-r", "--name-only", "origin/" + MAIN, cwd=root).stdout.split()
    errors, warnings = validate_manifest(entries, team["members"], cfg["protected_paths"],
                                         tracked, args.allow_overlap)
    for w in warnings:
        warn(w)
    if errors:
        raise Fail(USAGE, "manifest invalid:\n  " + "\n  ".join(errors))
    existing = all_issues()
    numbers, created = {}, []
    # pass one: create anything whose key is not on GitHub yet, with no status label
    for e in entries:
        found = find_by_key(e["key"], existing)
        if found:
            numbers[e["key"]] = found
            continue
        body = build_body(e["touches"], [], e["verify"], e.get("objective", ""),
                          e.get("contract_reference", []), e.get("acceptance", []), e["key"])
        n = create_issue(e["title"], body,
                         ["type:" + e["type"], "role:" + e.get("role", "builder"),
                          "owner:" + e["owner"]] + extra)
        numbers[e["key"]] = get_issue(n)
        created.append(n)
        info("created #%d %s" % (n, e["title"]))
    # pass two: real Blocked-By numbers and status labels
    for e in entries:
        iss = numbers[e["key"]]
        if iss.status or iss.state != "open":
            continue
        blockers = [numbers[k] for k in e.get("blocked_by", [])]
        nums = [b.number for b in blockers]
        body = build_body(e["touches"], nums, e["verify"], e.get("objective", ""),
                          e.get("contract_reference", []), e.get("acceptance", []), e["key"])
        gh_api("PATCH", "repos/%s/issues/%d" % (repo_slug(), iss.number), {"body": body})
        open_blockers = [b for b in blockers if get_issue(b.number).state == "open"]
        set_status(iss.number, "blocked" if open_blockers else "ready")
        info("#%d -> %s" % (iss.number, "blocked" if open_blockers else "ready"))
    info("done: %d created, %d already existed" % (len(created), len(entries) - len(created)))
    return OK


# ------------------------------------------------------------------ doctor

def ruleset_summary(slug):
    for r in gh_api("GET", "repos/%s/rulesets" % slug) or []:
        if r["name"] == RULESET_NAME:
            return r
    return None


def expected_labels(team):
    labels = {}
    for s in STATUSES:
        labels["status:" + s] = "status"
    for r in ROLES:
        labels["role:" + r] = "role"
    for t in team["members"]:
        labels["owner:" + t] = "owner"
    for t in TYPES:
        labels["type:" + t] = "type"
    labels["stale"] = "flag"
    labels["dry-run"] = "flag"
    return labels


def cmd_doctor(args):
    root = control_root()
    results = []

    def chk(ok, msg, hint=""):
        print("%s  %s%s" % ("PASS" if ok else "FAIL", msg, ("  -> " + hint) if hint and not ok else ""))
        results.append(ok)
        return ok

    team, cfg = load_cfg(root)
    for t in cfg["required_tools"]:
        chk(shutil.which(t) is not None, "tool: %s" % t, "install %s" % t)
    if shutil.which("gh") is None:
        return PRECOND
    try:
        p = run(["gh", "api", "-i", "user"], check=False)
        m = re.search(r"(?im)^x-oauth-scopes:\s*(.*)$", p.stdout)
        if m:
            scopes = [s.strip() for s in m.group(1).split(",")]
            chk("repo" in scopes and "workflow" in scopes, "gh scopes include repo, workflow",
                "gh auth refresh -s repo,workflow")
        else:
            chk(p.returncode == 0, "gh authenticated (fine-grained token: scopes not checked)",
                "gh auth login")
        me = resolve_member(team, args.as_tag)
        chk(True, "login %s is member %s" % (me.login, me.tag))
    except Fail as e:
        chk(False, "gh identity: " + e.msg.splitlines()[0], "gh auth login; add yourself to team.json")
    slug = None
    try:
        slug = repo_slug()
        perms = gh_api("GET", "repos/%s" % slug)
        chk(bool(perms.get("permissions", {}).get("push")), "push access to %s" % slug)
    except Fail as e:
        chk(False, "repo access: " + e.msg.splitlines()[0])
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip()
    chk(branch == MAIN and not dirty(root), "control clone on clean main",
        "git checkout main && git status")
    hp = git("config", "core.hooksPath", cwd=root, check=False).stdout.strip()
    if hp != "scripts/git-hooks" and args.fix:
        git("config", "core.hooksPath", "scripts/git-hooks", cwd=root)
        hp = "scripts/git-hooks"
        info("fixed: core.hooksPath")
    chk(hp == "scripts/git-hooks", "core.hooksPath = scripts/git-hooks", "run doctor.sh --fix")
    protected = False
    if slug:
        try:
            have = set(l["name"] for l in gh_list("repos/%s/labels?per_page=100" % slug))
            missing = [l for l in expected_labels(team) if l not in have]
            chk(not missing, "labels exist", "missing: %s (admin runs init-hackathon.sh)" %
                ", ".join(missing[:6]))
            rs = ruleset_summary(slug)
            protected = bool(rs and rs.get("enforcement") == "active")
            if protected:
                detail = gh_api("GET", "repos/%s/rulesets/%d" % (slug, rs["id"]))
                types = dict((r["type"], r.get("parameters", {})) for r in detail["rules"])
                ok = ("pull_request" in types and
                      types["pull_request"].get("require_code_owner_review") and
                      "required_status_checks" in types and
                      any(c["context"] == "ci" for c in
                          types["required_status_checks"]["required_status_checks"]))
                chk(bool(ok), "main ruleset requires PR, code-owner review, check `ci`",
                    "admin re-runs init-hackathon.sh")
            chk(protected, "main ruleset is active", "admin runs init-hackathon.sh")
            repo = gh_api("GET", "repos/%s" % slug)
            for k in ("allow_auto_merge", "allow_squash_merge", "delete_branch_on_merge"):
                chk(bool(repo.get(k)), "repo setting %s" % k, "admin runs init-hackathon.sh")
            lim = gh_api("GET", "repos/%s/interaction-limits" % slug) or {}
            chk(lim.get("limit") == "collaborators_only", "interaction limit: collaborators only",
                "admin runs init-hackathon.sh (limit expires after 6 months)")
        except Fail as e:
            chk(False, "remote state: " + e.msg.splitlines()[0])
    if not protected:
        return PRECOND
    return OK if all(results) else PRECOND


# -------------------------------------------------------------------- init

def cmd_init(args):
    root = control_root()
    team, cfg = load_cfg(root)
    slug = repo_slug()
    owner = slug.split("/")[0]
    members = team["members"]
    if len(team.get("contract_owners", [])) < 2:
        raise Fail(PRECOND, "contract_owners must name at least two members")
    for t in team["contract_owners"]:
        if t not in members:
            raise Fail(PRECOND, "contract owner %s is not in members" % t)
    admin = run(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
    if admin.lower() not in trusted_logins(team, cfg):
        warn("you (%s) are not in team.json; issues you create will be untrusted" % admin)
    # 1. invites
    for tag, m in members.items():
        login = m["login"]
        if login.lower() == owner.lower():
            continue
        try:
            gh_api("GET", "repos/%s/collaborators/%s" % (slug, login))
        except GhError as e:
            if e.status != 404:
                raise
            gh_api("PUT", "repos/%s/collaborators/%s" % (slug, login), {"permission": "push"})
            info("invited %s" % login)
    # 2. CODEOWNERS
    owners = [members[t]["login"] for t in team["contract_owners"]]
    co = os.path.join(root, ".github", "CODEOWNERS")
    os.makedirs(os.path.dirname(co), exist_ok=True)
    with open(co, "w") as f:
        f.write(codeowners_text(cfg["protected_paths"], owners))
    # 3. setup commit
    rs = ruleset_summary(slug)
    if not (rs and rs.get("enforcement") == "active"):
        if git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip() != MAIN:
            raise Fail(PRECOND, "run init-hackathon.sh from main")
        git("add", "harness/team.json", ".github/CODEOWNERS", cwd=root)
        if git("diff", "--cached", "--quiet", cwd=root, check=False).returncode != 0:
            git("commit", "-m", "chore: harness setup (team.json, CODEOWNERS)", cwd=root)
            env = dict(os.environ, HARNESS_ALLOW_MAIN_PUSH="1")
            run(["git", "push", "origin", MAIN], cwd=root, env=env)
            info("pushed the setup commit to main")
    # 4. labels
    for name, group in expected_labels(team).items():
        run(["gh", "label", "create", name, "--color", LABEL_COLORS[group], "--force",
             "-R", slug])
    # 5. repo settings
    gh_api("PATCH", "repos/%s" % slug, {"allow_auto_merge": True, "allow_squash_merge": True,
                                        "delete_branch_on_merge": True})
    # 6. ruleset
    ruleset = {
        "name": RULESET_NAME, "target": "branch", "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"}, {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": True, "require_last_push_approval": False,
                "required_review_thread_resolution": False}},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": "ci"}]}}]}
    if rs:
        gh_api("PUT", "repos/%s/rulesets/%d" % (slug, rs["id"]), ruleset)
    else:
        gh_api("POST", "repos/%s/rulesets" % slug, ruleset)
    info("main ruleset applied")
    # 7. interaction limit
    gh_api("PUT", "repos/%s/interaction-limits" % slug,
           {"limit": "collaborators_only", "expiry": "six_months"})
    # 8. Phase 0 issue
    if find_by_key("phase-0"):
        info("Phase 0 issue already exists")
        return OK
    architect = next((t for t, m in members.items() if m["role"] == "architect"),
                     next(iter(members)))
    body = build_body(
        ["**"], [], "./scripts/check.sh",
        "Write MASTER_PLAN.md, contracts/, .env.example, the folder skeleton and issues.json; "
        "adjust scripts/check.sh if the stack differs from the pnpm + uv preset.",
        ["none"],
        ["MASTER_PLAN.md describes architecture, slices and milestones",
         "contracts/ holds the shared interfaces",
         "issues.json passes bootstrap validation (30-90 minute slices, non-overlapping Touches)"],
        "phase-0")
    n = create_issue("Phase 0: plan and contracts", body,
                     ["type:contract", "role:architect", "owner:" + architect, "status:ready"])
    info("created Phase 0 issue #%d for %s" % (n, architect))
    return OK


# ------------------------------------------------------------ CI / hooks

def cmd_check_pr(args):
    root = repo_root()
    team, cfg = load_cfg(root)
    slug = repo_slug()
    pr = gh_api("GET", "repos/%s/pulls/%d" % (slug, args.pr))
    m = re.search(r"(?i)\b(?:closes|fixes|resolves)\s+#(\d+)", pr.get("body") or "")
    if not m:
        raise Fail(TOUCHES, "PR has no `Closes #<issue>` line")
    iss = get_issue(int(m.group(1)))
    if iss.author.lower() not in trusted_logins(team, cfg):
        raise Fail(TOUCHES, "linked issue #%d is from untrusted author %s" %
                   (iss.number, iss.author))
    changed = []
    for f in gh_list("repos/%s/pulls/%d/files?per_page=100" % (slug, args.pr)):
        changed.append(f["filename"])
        if f.get("previous_filename"):
            changed.append(f["previous_filename"])
    bad = touches_violations(changed, iss.touches, cfg["shared_paths"])
    if iss.type in ("feature", "integration"):
        bad += [p for p in changed if matches_any(p, cfg["protected_paths"]) and p not in bad]
    if bad:
        raise Fail(TOUCHES, "PR changes files outside issue #%d Touches:\n  %s" %
                   (iss.number, "\n  ".join(bad)))
    info("Touches check passed for #%d (%d files)" % (iss.number, len(changed)))
    return OK


def cmd_hook_edit(args):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return OK
    ti = payload.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    if not path:
        return OK
    cwd = payload.get("cwd") or os.getcwd()
    try:
        top = repo_root(cwd)
    except Fail:
        return OK
    tf = os.path.join(top, ".harness", "task.json")
    if not os.path.exists(tf):
        return OK
    task = load_json(tf)
    cfg = load_json(os.path.join(top, "harness", "config.json"))
    ap = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    rel = os.path.relpath(ap, top)
    if rel.startswith(".."):
        ctrl = control_root(top)
        if ap == ctrl or ap.startswith(ctrl + os.sep):
            print("Blocked: this session works only in its worktree (%s). Do not edit the "
                  "control clone." % top, file=sys.stderr)
            return 2
        return OK
    rel = rel.replace(os.sep, "/")
    if touches_violations([rel], task.get("touches", []), cfg["shared_paths"]):
        print("Blocked: %s is outside this issue's Touches (%s) and the shared paths. "
              "Do not work around this with shell commands. If you need this file, run "
              "./scripts/escalate.sh \"<what you need>\"; for a contract change use "
              "./scripts/propose-contract.sh." % (rel, ", ".join(task.get("touches", []))),
              file=sys.stderr)
        return 2
    return OK


def cmd_pre_push(args):
    if os.environ.get("HARNESS_ALLOW_MAIN_PUSH") == "1":
        return OK
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "refs/heads/" + MAIN:
            print("Blocked: never push to main. Work in a worktree and run "
                  "./scripts/finish-pr.sh.", file=sys.stderr)
            return 1
    return OK


# ------------------------------------------------------- workflow commands

def cmd_unblock(args):
    root = repo_root()
    team, cfg = load_cfg(root)
    for iss in all_issues("open", ["status:blocked"]):
        if any(get_issue(b).state == "open" for b in iss.blocked_by):
            continue
        set_status(iss.number, "ready")
        login = team["members"].get(iss.owner or "", {}).get("login")
        comment(iss.number, "Unblocked: all blockers are closed. %s" %
                ("@%s it is ready to claim." % login if login else "Ready to claim."))
        info("#%d -> ready" % iss.number)
    return OK


def cmd_stale_scan(args):
    root = repo_root()
    team, cfg = load_cfg(root)
    now = datetime.datetime.now(datetime.timezone.utc)
    for iss in all_issues("open", ["status:in-progress"]):
        comments = gh_list("repos/%s/issues/%d/comments?per_page=100" % (repo_slug(), iss.number))
        claims = [c["created_at"] for c in comments if CLAIM_MARK in c.get("body", "")]
        claim_ts = parse_ts(max(claims)) if claims else None
        head_ts = None
        try:
            br = gh_api("GET", "repos/%s/branches/issue%%2F%d" % (repo_slug(), iss.number))
            head_ts = parse_ts(br["commit"]["commit"]["committer"]["date"])
        except GhError:
            pass
        stale = is_stale(claim_ts, head_ts, now, cfg.get("stale_minutes", 60))
        if stale and "stale" not in iss.labels:
            edit_labels(iss.number, add=["stale"])
            comment(iss.number, "No push for %d minutes: flagged stale. Anyone can run "
                                "`./scripts/reclaim.sh %d --to <p-tag>`." %
                    (cfg.get("stale_minutes", 60), iss.number))
            info("#%d flagged stale" % iss.number)
        elif not stale and "stale" in iss.labels:
            edit_labels(iss.number, remove=["stale"])
            info("#%d no longer stale" % iss.number)
    return OK


def cmd_main_health(args):
    root = repo_root()
    team, cfg = load_cfg(root)
    sha = args.sha
    key = "revert-" + sha[:12]
    if find_by_key(key):
        info("revert issue for %s already exists" % sha[:12])
        return OK
    tag = None
    try:
        for pr in gh_api("GET", "repos/%s/commits/%s/pulls" % (repo_slug(), sha)) or []:
            who = ((gh_api("GET", "repos/%s/pulls/%d" % (repo_slug(), pr["number"])) or {})
                   .get("merged_by") or {}).get("login", "")
            for t, m in team["members"].items():
                if m["login"].lower() == who.lower():
                    tag = t
    except GhError:
        pass
    if not tag:
        tag = next((t for t, m in team["members"].items() if m["role"] == "architect"),
                   next(iter(team["members"])))
    role = team["members"][tag]["role"]
    body = build_body(["**"], [], "./scripts/check.sh",
                      "`main` is broken at %s. Run `git revert %s`, then `./scripts/finish-pr.sh`. "
                      "Revert first; re-land the change afterwards." % (sha[:12], sha),
                      ["none"], ["main passes ./scripts/check.sh again"], key)
    n = create_issue("Revert broken main (%s)" % sha[:8], body,
                     ["type:hotfix", "role:" + role, "owner:" + tag, "status:ready"])
    info("opened hotfix #%d for %s" % (n, sha[:12]))
    return OK


# -------------------------------------------------------------------- main

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="as_tag", metavar="P-TAG")
    ap = argparse.ArgumentParser(prog="harness.py")
    sub = ap.add_subparsers(dest="cmd")

    def add(name, fn, *a, **kw):
        sp = sub.add_parser(name, parents=[common], **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("doctor", cmd_doctor).add_argument("--fix", action="store_true")
    add("init", cmd_init)
    sp = add("bootstrap", cmd_bootstrap)
    sp.add_argument("manifest", nargs="?")
    sp.add_argument("--allow-overlap", action="store_true")
    sp.add_argument("--dry-run-fixture", action="store_true")
    sp = add("claim", cmd_claim)
    sp.add_argument("issue", type=int)
    sp.add_argument("--resume", action="store_true")
    add("finish", cmd_finish)
    sp = add("propose-contract", cmd_propose_contract)
    sp.add_argument("title", nargs="?")
    sp.add_argument("--touches")
    sp.add_argument("--stack", type=int)
    add("escalate", cmd_escalate).add_argument("message")
    sp = add("reclaim", cmd_reclaim)
    sp.add_argument("issue", type=int)
    sp.add_argument("--to")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--release", action="store_true")
    add("sync", cmd_sync)
    add("reset-demo", cmd_reset_demo).add_argument("--confirm", required=True)
    add("check-pr", cmd_check_pr).add_argument("--pr", type=int, required=True)
    add("hook-edit", cmd_hook_edit)
    add("pre-push", cmd_pre_push, add_help=False)
    add("unblock", cmd_unblock)
    add("stale-scan", cmd_stale_scan)
    add("main-health", cmd_main_health).add_argument("--sha", required=True)
    return ap


def main(argv=None):
    ap = build_parser()
    args, unknown = ap.parse_known_args(argv)
    if not getattr(args, "fn", None):
        ap.print_usage(sys.stderr)
        return USAGE
    if args.cmd != "pre-push" and unknown:
        ap.error("unrecognized arguments: %s" % " ".join(unknown))
    try:
        return args.fn(args) or OK
    except Fail as e:
        print("error: " + e.msg, file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return FAIL


if __name__ == "__main__":
    sys.exit(main())
