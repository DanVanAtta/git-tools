#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///
"""Behavior tests for 'gup-all': each test builds throwaway repos with local
bare remotes and a stub 'gh', runs the real script, then checks branches and
the report. Never touches a real work folder or the network.
"""

import importlib.machinery
import importlib.util
import os
import pty
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

GUP_ALL = Path(__file__).resolve().parent.parent / "gup-all"

# Merged PRs are files at $GH_STUB_DIR/<-R repo>/<base>/<head> holding the head
# sha; like the real CLI, no --base means PRs into any base. A call shaped
# unlike gup-all's fails loudly, since the stub can't vouch for what real gh
# would print for it.
GH_STUB = """#!/bin/bash
if [ "${GH_STUB_FAIL:-}" = expired ]; then
  printf 'HTTP 401: Bad credentials (https://api.github.com/graphql)\nTry authenticating with:  gh auth login\n' >&2
  exit 1
fi
if [ -n "${GH_STUB_FAIL:-}" ]; then
  echo "To get started with GitHub CLI, please run:  gh auth login" >&2
  exit 4
fi
args="$*" repo="" state="" base="" head="" json="" jq=""
while [ $# -gt 0 ]; do
  case $1 in
    -R) repo=$2 ;; --state) state=$2 ;; --base) base=$2 ;; --head) head=$2 ;; --json) json=$2 ;; --jq) jq=$2 ;;
  esac
  shift
done
if [ "$state" != merged ] || [ "$json" != headRefOid ] || [ "$jq" != ".[].headRefOid" ]; then
  echo "gh stub: unexpected args: $args" >&2
  exit 2
fi
if [ -n "$base" ]; then
  cat "$GH_STUB_DIR/$repo/$base/$head" 2>/dev/null
else
  cat "$GH_STUB_DIR/$repo"/*/"$head" 2>/dev/null
fi
exit 0
"""


@dataclass
class Result:
    out: str
    code: int

    def reported(self, text: str) -> bool:
        return text in self.out

    def in_section(self, heading: str, text: str) -> bool:
        """text appears between the heading and the next blank line."""
        lines = self.out.splitlines()
        if heading not in lines:
            return False
        body = []
        for line in lines[lines.index(heading) + 1:]:
            if not line:
                break
            body.append(line)
        return text in "\n".join(body)


class World:
    """A work root, bare remotes and gh stub data for one test."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.work = tmp / "work"
        self.remotes = tmp / "remotes"
        self.gh_dir = tmp / "gh"
        (self.work / "org").mkdir(parents=True)
        self.remotes.mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(GH_STUB)
        (bin_dir / "gh").chmod(0o755)
        # A PATH with what gup-all needs, minus 'gh'.
        self.nogh = tmp / "nogh"
        self.nogh.mkdir()
        (self.nogh / "git").symlink_to(shutil.which("git"))
        env = {k: v for k, v in os.environ.items() if k not in ("GIT_SSH", "GIT_SSH_COMMAND", "NO_COLOR")}
        env.update(
            TERM="xterm",
            GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com",
            GH_STUB_DIR=str(self.gh_dir), PATH=f"{bin_dir}:{os.environ['PATH']}",
        )
        self.env = env

    def git(self, *args: str, cwd: Path) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, check=False, text=True)
        assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
        return result.stdout.strip()

    def new_repo(self, name: str) -> "Repo":
        """A repo at work/org/<name> with one pushed commit on main. origin
        reads as a GitHub URL, and 'insteadOf' points it at a local bare repo."""
        bare = self.remotes / f"{name}.git"
        self.git("init", "-q", "--bare", "-b", "main", str(bare), cwd=self.tmp)
        path = self.work / "org" / name
        self.git("clone", "-q", str(bare), str(path), cwd=self.tmp)
        repo = Repo(self, path)
        repo.git("config", f"url.{self.remotes}/.insteadOf", "https://github.com/org/")
        repo.git("remote", "set-url", "origin", f"https://github.com/org/{name}.git")
        repo.commit("init")
        repo.git("push", "-q", "origin", "main")
        repo.git("remote", "set-head", "origin", "--auto")
        return repo

    def push_from_elsewhere(self, name: str, message: str, file: str = "file") -> None:
        """Pushes a commit to origin's main from a second clone."""
        other = self.tmp / f"other-{name}-{message}"
        self.git("clone", "-q", str(self.remotes / f"{name}.git"), str(other), cwd=self.tmp)
        Repo(self, other).commit(message, file)
        self.git("push", "-q", "origin", "main", cwd=other)

    def gh_merged_pr(self, repo: str, base: str, head: str, sha: str) -> None:
        """A PR merged in github.com/org/<repo>."""
        file = self.gh_dir / "github.com" / "org" / repo / base / head
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(sha + "\n")

    def gup_all(self, *args: str, cwd: Path | None = None, **env: str) -> Result:
        result = subprocess.run(
            [sys.executable, str(GUP_ALL), *args], cwd=cwd or self.tmp, env={**self.env, **env},
            capture_output=True, check=False, encoding="utf-8", errors="surrogateescape")
        # Shown under "Captured stdout call" when a test fails.
        print(result.stdout + result.stderr)
        return Result(result.stdout + result.stderr, result.returncode)

    def run(self, **env: str) -> Result:
        """Runs gup-all over the work root, checking a report printed and no
        git or Python error leaked into it."""
        result = self.gup_all(str(self.work), **env)
        assert result.code == 0, result.out
        assert any(re.match(r"\d+ repos? under ", line) for line in result.out.splitlines()), result.out
        for line in result.out.splitlines():
            assert not line.startswith(("fatal:", "error:", "Traceback")), result.out
        assert "\033[" not in result.out, "piped output should have no color"
        return result

    def run_on_terminal(self, **env: str) -> str:
        """gup-all's stdout when it's a terminal, with ANSI codes kept."""
        controller, terminal = pty.openpty()
        subprocess.run(
            [sys.executable, str(GUP_ALL), str(self.work)], cwd=self.tmp, env={**self.env, **env},
            stdout=terminal, stderr=subprocess.DEVNULL, check=False)
        os.close(terminal)
        chunks = []
        # Reading a pty whose other end is closed raises EIO instead of returning b"".
        while True:
            try:
                chunk = os.read(controller, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        os.close(controller)
        out = b"".join(chunks).decode().replace("\r\n", "\n")
        print(out)
        return out


class Repo:
    def __init__(self, world: World, path: Path):
        self.world, self.path = world, path

    def git(self, *args: str) -> str:
        return self.world.git(*args, cwd=self.path)

    def try_git(self, *args: str) -> None:
        """A git command expected to stop partway, eg: a conflicting rebase."""
        subprocess.run(["git", *args], cwd=self.path, env=self.world.env, capture_output=True, check=False)

    def commit(self, message: str, file: str = "file") -> None:
        with open(self.path / file, "a") as f:
            f.write(message + "\n")
        self.git("add", file)
        self.git("commit", "-q", "-m", message)

    def merged_branch(self, name: str) -> None:
        """A branch that was pushed, fast-forward merged into main, and deleted on origin."""
        self.git("switch", "-q", "-c", name)
        self.commit(name)
        self.git("push", "-q", "-u", "origin", name)
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--ff-only", name)
        self.git("push", "-q", "origin", "main")
        self.git("push", "-q", "origin", "--delete", name)

    def pushed_and_gone_branch(self, name: str) -> None:
        """A branch that was pushed and then deleted on origin, eg: after a squash merge."""
        self.git("switch", "-q", "-c", name)
        self.commit(name)
        self.git("push", "-q", "-u", "origin", name)
        self.git("push", "-q", "origin", "--delete", name)
        self.git("switch", "-q", "main")

    def pushed_branch(self, name: str) -> None:
        """A branch with one commit, pushed and still on origin."""
        self.git("switch", "-q", "-c", name)
        self.commit(name)
        self.git("push", "-q", "-u", "origin", name)
        self.git("switch", "-q", "main")

    def has_branch(self, name: str) -> bool:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=self.path, env=self.world.env, capture_output=True, check=False).returncode == 0

    def current_branch(self) -> str:
        return self.git("branch", "--show-current")

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", ref)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def main_in_process(world: World, monkeypatch: pytest.MonkeyPatch, raise_in: dict[str, BaseException]) -> int:
    """Runs gup-all's main() in this process, with RepoSync.run raising in the
    named repos. Only for the backstops that no real repo state reaches.
    """
    loader = importlib.machinery.SourceFileLoader("gup_all", str(GUP_ALL))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("gup_all", loader))
    loader.exec_module(module)
    real_run = module.RepoSync.run

    def run(self) -> None:
        if self.name in raise_in:
            raise raise_in[self.name]
        real_run(self)

    monkeypatch.setattr(module.RepoSync, "run", run)
    monkeypatch.setattr(sys, "argv", ["gup-all", str(world.work)])
    for key in ("GIT_SSH", "GIT_SSH_COMMAND"):
        monkeypatch.delenv(key, raising=False)
    for key, value in world.env.items():
        monkeypatch.setenv(key, value)
    return module.main()


not_as_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod 0")


def test_merged_branch_with_pruned_remote_is_deleted(world):
    r = world.new_repo("r")
    r.merged_branch("feat")
    tip = r.git("rev-parse", "--short", "feat")
    result = world.run()
    assert not r.has_branch("feat")
    assert result.reported(f"feat ({tip})"), "deletion should be reported with its sha"


def test_merged_branch_still_on_origin_is_kept(world):
    r = world.new_repo("r")
    r.pushed_branch("develop")
    r.git("merge", "-q", "--ff-only", "develop")
    r.git("push", "-q", "origin", "main")
    result = world.run()
    assert r.has_branch("develop"), "branch still on origin, with no merged PR, was deleted"
    assert result.reported("Up to date: 1 repo")


def test_branch_still_on_origin_with_merged_pr_is_deleted(world):
    r = world.new_repo("r")
    r.pushed_branch("feat")
    world.gh_merged_pr("r", "main", "feat", r.rev("feat"))
    result = world.run()
    assert not r.has_branch("feat"), "a merged PR with the exact tip should delete it"
    assert result.in_section("Deleted merged branches (undo: git branch <name> <sha>):", "feat (")


def test_checked_out_branch_still_on_origin_with_merged_pr_is_switched_off_and_deleted(world):
    r = world.new_repo("r")
    r.pushed_branch("feat")
    world.gh_merged_pr("r", "main", "feat", r.rev("feat"))
    r.git("switch", "-q", "feat")
    result = world.run()
    assert r.current_branch() == "main"
    assert not r.has_branch("feat")
    assert result.reported("org/r/  from feat to main (merged)")


def test_branch_still_on_origin_with_commits_after_its_merged_pr_is_kept(world):
    r = world.new_repo("r")
    r.pushed_branch("feat")
    world.gh_merged_pr("r", "main", "feat", r.rev("feat"))
    r.git("switch", "-q", "feat")
    r.commit("after-merge")
    r.git("push", "-q", "origin", "feat")
    r.git("switch", "-q", "main")
    world.run()
    assert r.has_branch("feat"), "commits after the merged PR's head were deleted"


def test_checked_out_merged_branch_is_switched_off_and_deleted(world):
    r = world.new_repo("r")
    r.merged_branch("feat")
    r.git("switch", "-q", "feat")
    tip = r.git("rev-parse", "--short", "feat")
    result = world.run()
    assert r.current_branch() == "main"
    assert not r.has_branch("feat")
    assert result.reported("org/r/  from feat to main (merged)")
    assert result.reported(f"feat ({tip})")


def test_unpushed_local_branch_is_kept(world):
    r = world.new_repo("r")
    r.git("switch", "-q", "-c", "local")
    r.commit("local")
    r.git("switch", "-q", "main")
    result = world.run()
    assert r.has_branch("local")
    assert result.reported("Up to date: 1 repo")


def test_unborn_branch_stays_checked_out(world):
    r = world.new_repo("r")
    r.git("switch", "-q", "--orphan", "fresh")
    result = world.run()
    assert r.current_branch() == "fresh", "a branch with no commits was switched off"
    assert result.in_section("Resume here:", "fresh: no commits yet")


def test_never_pushed_fresh_branch_is_kept(world):
    r = world.new_repo("r")
    r.git("switch", "-q", "-c", "fresh")
    world.run()
    assert r.has_branch("fresh")
    assert r.current_branch() == "fresh"


def test_branch_with_origin_remote_but_no_upstream_branch_is_kept(world):
    r = world.new_repo("r")
    r.git("branch", "-q", "fresh")
    r.git("config", "branch.fresh.remote", "origin")
    world.run()
    assert r.has_branch("fresh"), "a branch never pushed was deleted by ancestry"


def test_fresh_branch_tracking_main_is_kept(world):
    r = world.new_repo("r")
    r.git("branch", "-q", "--track", "fresh", "origin/main")
    world.gh_merged_pr("r", "main", "main", r.rev("fresh"))
    result = world.run()
    assert r.has_branch("fresh"), "a branch tracking origin/main was deleted"
    assert result.reported("Up to date: 1 repo")


def test_checked_out_branch_tracking_main_stays_checked_out(world):
    r = world.new_repo("r")
    r.git("switch", "-q", "-c", "newwork", "origin/main")
    result = world.run()
    assert r.current_branch() == "newwork", "unpushed new branch was switched off"
    assert not result.reported("pushed, branch kept")


def test_feature_branch_with_unpushed_commit_stays_checked_out(world):
    r = world.new_repo("r")
    r.git("switch", "-q", "-c", "feat")
    r.commit("feat")
    r.git("push", "-q", "-u", "origin", "feat")
    r.commit("more")
    result = world.run()
    assert r.current_branch() == "feat"
    assert result.in_section("Resume here:", "feat: 1 unpushed")
    assert not result.reported("feat -> main")


def test_squash_merged_branch_is_deleted(world):
    r = world.new_repo("r")
    r.pushed_and_gone_branch("sq")
    world.gh_merged_pr("r", "main", "sq", r.rev("sq"))
    world.run()
    assert not r.has_branch("sq")


def test_squash_merge_found_by_remote_branch_name(world):
    r = world.new_repo("r")
    r.git("switch", "-q", "-c", "feat")
    r.commit("feat")
    r.git("push", "-q", "-u", "origin", "feat:dan/feat")
    r.git("push", "-q", "origin", "--delete", "dan/feat")
    world.gh_merged_pr("r", "main", "dan/feat", r.rev("feat"))
    r.git("switch", "-q", "main")
    world.run()
    assert not r.has_branch("feat"), "feat pushed as dan/feat should be deleted"


def test_branch_with_commits_after_squash_merge_is_kept(world):
    r = world.new_repo("r")
    r.pushed_and_gone_branch("sq")
    world.gh_merged_pr("r", "main", "sq", r.rev("sq"))
    r.git("switch", "-q", "sq")
    r.commit("after-merge")
    r.git("switch", "-q", "main")
    result = world.run()
    assert r.has_branch("sq")
    assert result.reported("Up to date: 1 repo")


def test_pr_merged_into_another_base_is_kept(world):
    r = world.new_repo("r")
    r.pushed_and_gone_branch("stacked")
    world.gh_merged_pr("r", "parent", "stacked", r.rev("stacked"))
    result = world.run()
    assert r.has_branch("stacked")
    assert result.reported("Up to date: 1 repo")


def test_gone_branch_without_merged_pr_is_flagged(world):
    r = world.new_repo("r")
    r.pushed_and_gone_branch("closed")
    result = world.run()
    assert r.has_branch("closed")
    assert result.in_section(
        "Needs you:", "closed: remote branch gone, no merged PR found (closed?); 1 commit not on any remote")


def test_non_github_origin_skips_pr_check(world):
    r = world.new_repo("r")
    r.git("remote", "set-url", "origin", str(world.remotes / "r.git"))
    r.pushed_and_gone_branch("closed")
    world.gh_merged_pr("r", "main", "closed", r.rev("closed"))
    result = world.run()
    assert r.has_branch("closed"), "gh should not be asked about a non-GitHub origin"
    assert result.reported("(origin isn't on GitHub)")


def test_clean_pushed_feature_branch_is_switched_off_and_kept(world):
    r = world.new_repo("r")
    r.git("switch", "-q", "-c", "feat")
    r.commit("feat")
    r.git("push", "-q", "-u", "origin", "feat")
    result = world.run()
    assert r.current_branch() == "main"
    assert r.has_branch("feat")
    assert result.reported("org/r/  from feat to main (pushed, branch kept)")


def test_dirty_repo_is_left_alone(world):
    r = world.new_repo("r")
    r.merged_branch("old")
    r.git("switch", "-q", "-c", "wip")
    with open(r.path / "file", "a") as f:
        f.write("dirty\n")
    result = world.run()
    assert r.current_branch() == "wip"
    assert r.has_branch("old"), "merged branch deleted in a dirty repo"
    assert result.in_section("Resume here:", "wip: 1 uncommitted")


def test_repo_mid_rebase_is_left_alone(world):
    r = world.new_repo("r")
    r.merged_branch("old")
    r.git("switch", "-q", "-c", "conflicting")
    r.commit("side")
    r.git("switch", "-q", "main")
    r.commit("main-side")
    r.git("switch", "-q", "conflicting")
    r.try_git("rebase", "-q", "main")
    result = world.run()
    assert (r.path / ".git" / "rebase-merge").is_dir(), "rebase in progress was disturbed"
    assert r.has_branch("old")
    assert result.in_section("Resume here:", "conflicting: rebase in progress")


def test_repo_mid_merge_is_left_alone(world):
    r = world.new_repo("r")
    r.merged_branch("old")
    r.git("switch", "-q", "-c", "side")
    r.commit("side")
    r.git("switch", "-q", "main")
    r.commit("main-side")
    r.try_git("merge", "-q", "side")
    result = world.run()
    assert (r.path / ".git" / "MERGE_HEAD").exists(), "merge in progress was disturbed"
    assert r.has_branch("old")
    assert result.in_section("Resume here:", "main: merge in progress")


def test_repo_between_cherry_pick_stops_is_left_alone(world):
    r = world.new_repo("r")
    r.merged_branch("old")
    r.git("switch", "-q", "-c", "picks")
    r.commit("one")
    r.commit("two")
    r.git("switch", "-q", "main")
    r.commit("main-side")
    r.try_git("cherry-pick", "picks~1", "picks")
    (r.path / "file").write_text("resolved\n")
    r.git("add", "file")
    r.git("-c", "core.editor=true", "commit", "-q")
    result = world.run()
    assert (r.path / ".git" / "sequencer").is_dir(), "cherry-pick sequence was disturbed"
    assert r.has_branch("old")
    assert result.in_section("Resume here:", "main: cherry-pick/revert in progress")


def test_branch_in_other_worktree_is_kept(world):
    r = world.new_repo("r")
    r.merged_branch("wtb")
    r.git("worktree", "add", "-q", str(world.tmp / "wt"), "wtb")
    result = world.run()
    assert r.has_branch("wtb")
    assert not result.reported("couldn't be deleted"), "worktree branch should be skipped, not attempted"
    assert result.in_section("Resume here:", "on wtb (worktree of org/r): remote branch gone")


def test_worktree_under_root_is_not_a_separate_repo(world):
    r = world.new_repo("r")
    r.git("branch", "-q", "feat")
    r.git("worktree", "add", "-q", str(world.work / "worktrees" / "r-feat"), "feat")
    result = world.run()
    assert result.reported("1 repo under"), "worktree counted as a repo"
    assert result.reported("worktrees/r-feat/  on feat (worktree of org/r): never pushed"), \
        "worktree should be named relative to root"


def test_linked_worktrees_reported_when_main_checkout_is_detached(world):
    r = world.new_repo("r")
    r.git("branch", "-q", "b1")
    r.git("worktree", "add", "-q", str(world.tmp / "wt1"), "b1")
    r.git("worktree", "add", "-q", "--detach", str(world.tmp / "wt2"))
    r.git("switch", "-q", "--detach")
    result = world.run()
    assert result.reported("on b1 (worktree of org/r): never pushed")
    assert result.reported("detached (worktree of org/r): clean")


@not_as_root
def test_unreadable_worktree_is_not_called_missing(world):
    r = world.new_repo("r")
    locked = world.tmp / "locked"
    r.git("worktree", "add", "-q", str(locked / "wt"), "-b", "wb")
    locked.chmod(0)
    try:
        result = world.run()
    finally:
        locked.chmod(0o755)
    assert result.in_section("Resume here:", str(locked / "wt") + "/  on wb (worktree of org/r): can't read it, not checked")
    assert not result.reported("is missing")


def test_worktree_path_that_is_now_a_file_is_flagged_missing(world):
    r = world.new_repo("r")
    wt = world.tmp / "wt"
    r.git("worktree", "add", "-q", str(wt), "-b", "wb")
    shutil.rmtree(wt)
    wt.write_text("#!/bin/sh\n")
    wt.chmod(0o755)
    result = world.run()
    assert result.in_section("Needs you:", f"{wt}/  on wb (worktree of org/r): missing; run: git worktree prune")


def test_missing_worktree_is_flagged(world):
    r = world.new_repo("r")
    r.git("worktree", "add", "-q", str(world.tmp / "gone"), "-b", "gb")
    shutil.rmtree(world.tmp / "gone")
    result = world.run()
    assert result.in_section("Needs you:", ": missing; run: git worktree prune")


def test_conflicting_local_commit_on_main_is_left_unchanged(world):
    r = world.new_repo("r")
    r.commit("local")
    before = r.rev("HEAD")
    world.push_from_elsewhere("r", "upstream")
    result = world.run()
    assert not (r.path / ".git" / "rebase-merge").exists(), "rebase left in progress"
    assert r.rev("HEAD") == before, "main moved"
    assert result.in_section("Needs you:", "main: 1 local commit conflicts with 1 new on origin/main")


def test_local_commit_on_main_is_rebased_onto_origin(world):
    r = world.new_repo("r")
    r.commit("local", "other-file")
    world.push_from_elsewhere("r", "upstream")
    result = world.run()
    assert r.rev("HEAD~1") == r.rev("origin/main"), "main should sit on top of origin/main"
    assert r.git("log", "-1", "--format=%s") == "local"
    assert result.reported("Pulled:     org/r/ (+1)")
    assert result.in_section("Needs you:", "main: 1 unpushed")


def test_main_behind_origin_is_fast_forwarded(world):
    r = world.new_repo("r")
    world.push_from_elsewhere("r", "upstream")
    result = world.run()
    assert r.rev("HEAD") == r.rev("origin/main")
    assert result.reported("Pulled:     org/r/ (+1)")
    assert result.reported("All clear: nothing needs you")


def test_relative_root_syncs_every_repo(world):
    for name in ("a", "b"):
        world.new_repo(name)
        world.push_from_elsewhere(name, "upstream")
    result = world.gup_all("work", cwd=world.tmp)
    assert result.reported("org/a/ (+1), org/b/ (+1)"), result.out


def test_no_origin_repo_is_skipped(world):
    world.git("init", "-q", "-b", "main", str(world.work / "org" / "local"), cwd=world.tmp)
    result = world.run()
    assert result.reported("Skipped:    no origin: org/local")


def test_nested_repo_inside_a_repo_is_found(world):
    world.git("init", "-q", "-b", "main", str(world.work / "top"), cwd=world.tmp)
    world.git("init", "-q", "-b", "main", str(world.work / "top" / "inner"), cwd=world.tmp)
    result = world.run()
    assert result.reported("2 repos under")
    assert result.reported("no origin: top/, top/inner/")


@not_as_root
def test_unreadable_dirs_under_root_are_reported_as_skipped(world):
    world.new_repo("r")
    locked = [world.work / "locked", world.work / "org" / "locked"]
    for d in locked:
        d.mkdir()
        d.chmod(0)
    try:
        result = world.run()
    finally:
        for d in locked:
            d.chmod(0o755)
    assert result.reported("1 repo under")
    assert result.reported("Up to date: 1 repo")
    assert result.reported("Skipped:    can't read: locked/, org/locked/")
    assert result.reported("All clear"), "an unreadable dir is informational, like no origin"


@not_as_root
def test_unreadable_dir_inside_a_repo_is_not_reported(world):
    world.new_repo("r")
    world.git("init", "-q", "-b", "main", str(world.work / "top"), cwd=world.tmp)
    private = world.work / "top" / "private"
    private.mkdir()
    private.chmod(0)
    try:
        result = world.run()
    finally:
        private.chmod(0o755)
    assert result.reported("2 repos under")
    assert not result.reported("can't read")


@not_as_root
def test_symlink_into_an_unreadable_dir_doesnt_hide_its_siblings(world):
    world.new_repo("r")
    locked = world.tmp / "locked"
    (locked / "x").mkdir(parents=True)
    (world.work / "org" / "link").symlink_to(locked / "x")
    locked.chmod(0)
    try:
        result = world.run()
    finally:
        locked.chmod(0o755)
    assert result.reported("1 repo under")
    assert not result.reported("can't read")


@not_as_root
def test_unreadable_dir_under_a_root_that_is_a_repo_is_reported(world):
    world.new_repo("r")
    world.git("init", "-q", "-b", "main", str(world.work), cwd=world.tmp)
    locked = world.work / "org" / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        result = world.run()
    finally:
        locked.chmod(0o755)
    assert result.reported("Skipped:    no origin: ./; can't read: org/locked/")


@not_as_root
def test_root_with_only_unreadable_dirs_says_so(world):
    (world.work / "org").chmod(0)
    try:
        result = world.gup_all(str(world.work))
    finally:
        (world.work / "org").chmod(0o755)
    assert result.code == 1
    assert result.reported("no readable git repos under")
    assert result.reported("can't read: org")


def test_missing_git_is_one_error(world):
    result = world.gup_all(str(world.work), PATH=str(world.tmp / "bin"))
    assert result.code == 1
    assert result.reported("gup-all: error: git not found on PATH")


def test_discovery_skips_hidden_symlinked_and_too_deep_dirs(world):
    for repo in (world.work / ".hidden" / "r", world.work / "org" / "r" / "deep", world.tmp / "outside"):
        world.git("init", "-q", "-b", "main", str(repo), cwd=world.tmp)
    world.git("init", "-q", "-b", "main", str(world.work / "org" / "r"), cwd=world.tmp)
    (world.work / "org" / "link").symlink_to(world.tmp / "outside")
    result = world.run()
    assert result.reported("1 repo under")
    assert "Skipped:    no origin: org/r/" in result.out.splitlines()


def test_dirty_repo_without_a_remote_reads_as_path_and_branch(world):
    notes = world.work / "notes"
    world.git("init", "-q", "-b", "master", str(notes), cwd=world.tmp)
    Repo(world, notes).commit("init")
    (notes / "file").write_text("changed\n")
    (notes / "new").write_text("new\n")
    result = world.run()
    assert result.in_section("Resume here:", "notes/  on master: 2 uncommitted, no remote")


def test_root_that_is_a_repo_is_named_dot(world):
    r = world.new_repo("r")
    world.push_from_elsewhere("r", "upstream")
    result = world.gup_all(str(r.path))
    assert result.code == 0
    assert result.reported("1 repo under")
    assert result.reported("Pulled:     ./ (+1)")


def test_crash_in_one_repo_still_reports_other_repos_deletions(world, monkeypatch, capsys):
    world.new_repo("a")
    b = world.new_repo("b")
    b.merged_branch("feat")
    tip = b.git("rev-parse", "--short", "feat")
    code = main_in_process(world, monkeypatch, {"org/a/": RuntimeError("boom")})
    result = Result(capsys.readouterr().out, code)
    assert result.code == 0
    assert result.in_section("Needs you:", "org/a/  gup-all bug, repo partly synced (traceback above): RuntimeError: boom")
    assert result.in_section("Deleted merged branches (undo: git branch <name> <sha>):", f"org/b/  branch feat ({tip})")
    assert not b.has_branch("feat")


def test_ctrl_c_while_syncing_still_reports_deletions_so_far(world, monkeypatch, capsys):
    a = world.new_repo("a")
    a.merged_branch("feat")
    tip = a.git("rev-parse", "--short", "feat")
    world.new_repo("b")
    world.new_repo("c")
    code = main_in_process(world, monkeypatch, {"org/b/": KeyboardInterrupt()})
    result = Result(capsys.readouterr().out, code)
    assert result.code == 130
    assert result.in_section("Deleted merged branches (undo: git branch <name> <sha>):", f"org/a/  branch feat ({tip})")
    assert result.in_section("Needs you:", "org/b/  interrupted here, repo may be partly synced; later repos not synced")
    assert result.reported("Up to date: 1 repo"), "org/c was synced after the interrupt"


def test_branch_names_that_arent_utf8_are_synced(world):
    r = world.new_repo("r")
    # A str with surrogates reaches git as the raw byte, 0xe9 here.
    r.git("branch", "-q", "merged-\udce9")
    r.git("push", "-q", "-u", "origin", "merged-\udce9")
    r.git("push", "-q", "origin", "--delete", "merged-\udce9")
    r.git("branch", "-q", "kept-\udce9")
    result = world.run()
    assert not r.has_branch("merged-\udce9")
    assert r.has_branch("kept-\udce9")
    assert result.in_section("Deleted merged branches (undo: git branch <name> <sha>):", "merged-\udce9 (")


def test_foreign_owned_repo_in_safe_directory_is_synced(world):
    world.new_repo("r")
    world.push_from_elsewhere("r", "upstream")
    gitconfig = world.tmp / "gitconfig"
    gitconfig.write_text("[safe]\n\tdirectory = *\n")
    # Makes git treat every repo as owned by someone else.
    result = world.run(GIT_TEST_ASSUME_DIFFERENT_OWNER="1", GIT_CONFIG_GLOBAL=str(gitconfig))
    assert result.reported("Pulled:     org/r/ (+1)")


def test_repo_git_refuses_is_reported_not_skipped(world):
    world.new_repo("r")
    result = world.run(GIT_TEST_ASSUME_DIFFERENT_OWNER="1")
    assert result.in_section("Needs you:", "org/r/  owned by another user, so git refuses it; to trust it: "
                             f"git config --global --add safe.directory {world.work / 'org' / 'r'}")
    assert not result.reported("no origin")
    assert not result.reported("Resume here:")


def test_fetch_failure_still_reports_active_work(world):
    r = world.new_repo("r")
    with open(r.path / "file", "a") as f:
        f.write("dirty\n")
    r.git("remote", "set-url", "origin", str(world.remotes / "missing.git"))
    result = world.run()
    assert result.in_section("Needs you:", "fetch failed")
    assert result.in_section("Resume here:", "main: 1 uncommitted")
    lines = result.out.splitlines()
    assert lines.index("Needs you:") < lines.index("Resume here:")


def test_fetch_failure_leaves_clean_repo_unsynced(world):
    r = world.new_repo("r")
    r.merged_branch("old")
    r.git("remote", "set-url", "origin", str(world.remotes / "missing.git"))
    result = world.run()
    assert r.has_branch("old"), "branch deleted after a failed fetch"
    assert result.in_section("Needs you:", "fetch failed")
    assert not result.reported("Up to date:")


def test_gh_that_wont_run_is_reported_and_repo_still_pulled(world):
    r = world.new_repo("r")
    r.pushed_branch("feat")
    world.push_from_elsewhere("r", "upstream")
    broken = world.tmp / "broken"
    broken.mkdir()
    (broken / "gh").write_text("#!/bin/sh\n")
    result = world.run(PATH=f"{broken}:{world.nogh}")
    assert result.reported("Pulled:     org/r/ (+1)")
    assert result.in_section("Needs you:", "gh  merged PRs not checked in 1 repo, so their branches were kept: Permission denied: gh")


def test_without_gh_ancestry_merges_still_delete(world):
    r = world.new_repo("r")
    r.merged_branch("feat")
    r.pushed_and_gone_branch("closed")
    result = world.run(PATH=str(world.nogh))
    assert not r.has_branch("feat"), "feat should be deleted without gh"
    assert result.reported("couldn't check for a merged PR (gh not installed)")
    assert not result.reported("All clear")


def test_gh_failure_is_reported_once(world):
    for name in ("a", "b"):
        r = world.new_repo(name)
        r.pushed_and_gone_branch("one")
        r.pushed_and_gone_branch("two")
    result = world.run(GH_STUB_FAIL="1")
    assert result.out.count("merged PRs not checked") == 1
    assert result.reported("gh      merged PRs not checked in 2 repos, so their branches were kept: To get started")
    assert result.reported("one: remote branch gone; couldn't check for a merged PR (gh failed, see above)")


def test_gh_failure_keeps_its_login_hint(world):
    r = world.new_repo("r")
    r.pushed_and_gone_branch("one")
    result = world.run(GH_STUB_FAIL="expired")
    assert result.in_section("Needs you:", "HTTP 401: Bad credentials (https://api.github.com/graphql); to log in: gh auth login")


def test_help_prints_usage(world):
    result = world.gup_all("--help")
    assert result.code == 0
    assert result.reported("usage: gup-all")


def test_runs_as_its_own_executable_under_uv(world):
    if not shutil.which("uv"):
        pytest.skip("uv isn't installed")
    result = subprocess.run([str(GUP_ALL), "--help"], capture_output=True, check=False, text=True)
    assert result.returncode == 0, result.stderr
    assert "usage: gup-all" in result.stdout


def test_unknown_option_is_rejected(world):
    result = world.gup_all("--dry-run")
    assert result.code == 2
    assert result.reported("unrecognized arguments: --dry-run")


def test_root_without_repos_is_an_error(world):
    result = world.gup_all(str(world.work))
    assert result.code == 1
    assert result.reported("no git repos found")
    assert not result.reported("All clear")


def test_missing_root_is_an_error(world):
    result = world.gup_all(str(world.tmp / "nowhere"))
    assert result.code == 1
    assert result.reported("is not a directory")


def test_squash_merge_found_with_scp_origin(world):
    r = world.new_repo("r")
    r.git("config", f"url.{world.remotes}/.insteadOf", "git@github.com:org/")
    r.git("remote", "set-url", "origin", "git@github.com:org/r.git")
    r.pushed_and_gone_branch("sq")
    world.gh_merged_pr("r", "main", "sq", r.rev("sq"))
    world.run()
    assert not r.has_branch("sq"), "squash-merged sq should be deleted with an scp-style origin"


def test_detached_main_checkout_is_left_alone(world):
    r = world.new_repo("r")
    r.merged_branch("old")
    r.git("switch", "-q", "--detach")
    result = world.run()
    assert r.has_branch("old")
    assert result.in_section("Resume here:", "org/r/  detached at")


def test_worktree_mid_rebase_keeps_its_branch(world):
    r = world.new_repo("r")
    r.merged_branch("wtb")
    wt = world.tmp / "wt"
    r.git("worktree", "add", "-q", str(wt), "wtb")
    Repo(world, wt).try_git("rebase", "-q", "-f", "-x", "false", "HEAD~1")
    result = world.run()
    assert r.has_branch("wtb"), "branch being rebased in a worktree was deleted"
    assert not result.reported("couldn't be deleted")
    assert result.in_section("Resume here:", "on wtb (worktree of org/r): rebase in progress")


def test_rebase_failure_without_conflict_reports_git_error(world):
    r = world.new_repo("r")
    r.commit("local", "other-file")
    before = r.rev("HEAD")
    world.push_from_elsewhere("r", "upstream")
    hook = r.path / ".git" / "hooks" / "pre-rebase"
    hook.write_text('#!/bin/sh\necho "pre-rebase: refusing" >&2\nexit 1\n')
    hook.chmod(0o755)
    result = world.run()
    assert result.in_section("Needs you:", "main: rebase onto origin/main failed: pre-rebase: refusing")
    assert not result.reported("conflict"), "a refused rebase isn't a conflict"
    assert r.rev("HEAD") == before, "main moved"


def test_fetch_failure_is_attributed_to_its_repo(world):
    world.new_repo("a")
    b = world.new_repo("b")
    b.git("remote", "set-url", "origin", str(world.remotes / "missing.git"))
    result = world.run()
    assert result.in_section("Needs you:", "org/b/  fetch failed")
    assert not result.reported("org/a/  fetch failed")
    assert result.reported("Up to date: 1 repo")


def test_fetch_failure_shows_gits_reason_without_its_prefix(world):
    r = world.new_repo("r")
    r.git("remote", "set-url", "origin", str(world.remotes / "missing.git"))
    result = world.run()
    assert result.in_section("Needs you:", "fetch failed (prompts off): '")
    assert result.in_section("Needs you:", "does not appear to be a git repository")
    assert not result.reported("fatal:")


def test_empty_remote_is_flagged_without_a_fix_that_fails(world):
    bare = world.remotes / "empty.git"
    world.git("init", "-q", "--bare", "-b", "main", str(bare), cwd=world.tmp)
    world.git("clone", "-q", str(bare), str(world.work / "org" / "empty"), cwd=world.tmp)
    result = world.run()
    assert result.in_section("Needs you:", "origin has no branches yet; nothing to sync until one is pushed")
    assert not result.reported("set-head")


def test_unknown_default_branch_hint_works(world):
    r = world.new_repo("r")
    world.git("symbolic-ref", "HEAD", "refs/heads/nowhere", cwd=world.remotes / "r.git")
    r.git("remote", "set-head", "origin", "--delete")
    result = world.run()
    hint = "git remote set-head origin <branch>"
    assert result.in_section("Needs you:", f"origin's default branch is unknown; set it with: {hint}")
    r.git("remote", "set-head", "origin", "main")
    assert not world.run().reported("default branch is unknown")


def test_fetch_runs_ssh_without_prompts(world):
    r = world.new_repo("r")
    log = world.tmp / "ssh.log"
    fake_ssh = world.tmp / "fake-ssh"
    fake_ssh.write_text(f'#!/bin/sh\necho "prompt=$GIT_TERMINAL_PROMPT $@" >>"{log}"\nexit 1\n')
    fake_ssh.chmod(0o755)
    r.git("remote", "set-url", "origin", "ssh://example.invalid/org/r.git")
    r.git("config", "core.sshCommand", str(fake_ssh))
    world.run()
    assert "BatchMode=yes" in log.read_text(), "core.sshCommand should get BatchMode"
    assert "prompt=0 " in log.read_text(), "git's own credential prompts should be off"
    log.unlink()
    world.gup_all(str(world.work), GIT_SSH="/bin/false")
    assert "BatchMode=yes" in log.read_text(), "core.sshCommand beats GIT_SSH and should get BatchMode"
    r.git("config", "--unset", "core.sshCommand")
    log.unlink()
    world.gup_all(str(world.work), GIT_SSH=str(fake_ssh))
    assert log.exists() and log.read_text(), "GIT_SSH should still be used"
    assert "BatchMode" not in log.read_text(), "GIT_SSH should be left alone"


def test_set_head_runs_ssh_without_prompts(world):
    r = world.new_repo("r")
    log = world.tmp / "ssh.log"
    # Logs its args, then runs the remote command locally so the fetch and
    # 'set-head' succeed.
    fake_ssh = world.tmp / "fake-ssh"
    fake_ssh.write_text(f'#!/bin/sh\necho "$@" >>"{log}"\nfor a; do last=$a; done\nexec sh -c "$last"\n')
    fake_ssh.chmod(0o755)
    r.git("remote", "set-url", "origin", f"ssh://example.invalid{world.remotes / 'r.git'}")
    r.git("config", "core.sshCommand", str(fake_ssh))
    # Else git 2.48+ fetch recreates origin/HEAD and set-head never runs.
    r.git("config", "remote.origin.followRemoteHEAD", "never")
    r.git("remote", "set-head", "origin", "--delete")
    world.run()
    assert r.git("symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/main", "set-head didn't run"
    calls = log.read_text().splitlines()
    assert calls and all("BatchMode=yes" in call for call in calls), calls


def test_renamed_default_branch_is_followed(world):
    r = world.new_repo("r")
    bare = world.remotes / "r.git"
    world.git("branch", "-m", "main", "trunk", cwd=bare)
    world.git("symbolic-ref", "HEAD", "refs/heads/trunk", cwd=bare)
    result = world.run()
    assert r.current_branch() == "trunk", "should move to origin's renamed default branch"
    assert result.reported("from main to trunk")


def test_option_like_default_branch_isnt_run_as_an_option(world):
    r = world.new_repo("r")
    bare = world.remotes / "r.git"
    world.git("update-ref", "refs/heads/-Ckeep", "main", cwd=bare)
    world.git("symbolic-ref", "HEAD", "refs/heads/-Ckeep", cwd=bare)
    world.git("update-ref", "-d", "refs/heads/main", cwd=bare)
    r.git("switch", "-q", "-c", "keep")
    r.commit("unpushed")
    keep = r.rev("keep")
    r.git("switch", "-q", "-c", "feat", "main")
    r.commit("feat")
    r.git("push", "-q", "-u", "origin", "feat")
    result = world.run()
    assert r.rev("keep") == keep, "'git switch -Ckeep' reset the local branch 'keep'"
    assert result.in_section("Needs you:", "feat: couldn't switch to -Ckeep")


def test_single_branch_clone_keeps_branch_still_on_origin(world):
    r = world.new_repo("r")
    r.git("config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    r.pushed_branch("feat")
    r.git("merge", "-q", "--ff-only", "feat")
    r.git("push", "-q", "origin", "main")
    world.run()
    assert r.has_branch("feat"), "branch still on origin was deleted in a single-branch clone"


def test_branch_tracking_another_remote_is_kept(world):
    r = world.new_repo("r")
    fork = world.remotes / "fork.git"
    world.git("init", "-q", "--bare", "-b", "main", str(fork), cwd=world.tmp)
    r.git("remote", "add", "fork", str(fork))
    r.git("switch", "-q", "-c", "feat")
    r.commit("feat")
    r.git("push", "-q", "-u", "fork", "feat")
    r.git("switch", "-q", "main")
    r.git("merge", "-q", "--ff-only", "feat")
    r.git("push", "-q", "origin", "main")
    r.git("push", "-q", "fork", "--delete", "feat")
    result = world.run()
    assert r.has_branch("feat"), "branch tracking another remote was deleted"
    assert not result.reported("feat: remote branch gone")


def test_gone_branch_whose_commits_survive_gets_delete_hint(world):
    r = world.new_repo("r")
    r.git("switch", "-q", "-c", "parent")
    r.commit("parent")
    r.git("push", "-q", "-u", "origin", "parent")
    r.git("switch", "-q", "-c", "child")
    r.git("push", "-q", "-u", "origin", "child")
    r.git("push", "-q", "origin", "--delete", "child")
    r.git("switch", "-q", "main")
    result = world.run()
    assert result.in_section(
        "Needs you:", "child: remote branch gone, no merged PR found (closed?); delete with: git branch -D child")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))


def test_report_is_colored_on_a_terminal(world):
    r = world.new_repo("r")
    r.merged_branch("done")
    tip = r.git("rev-parse", "--short", "done")
    r.commit("local")
    out = world.run_on_terminal()
    assert "\033[1;31mNeeds you:\033[0m" in out
    assert "\033[1mDeleted merged branches" in out
    assert f"branch done (\033[33m{tip}\033[0m)" in out
    plain = re.sub(r"\033\[[0-9;]*m", "", out)
    assert "  org/r/  on main: 1 unpushed" in plain, "color shouldn't shift the columns"


def test_all_clear_is_green_on_a_terminal(world):
    world.new_repo("r")
    out = world.run_on_terminal()
    assert "\033[32mAll clear: nothing needs you, nothing in progress.\033[0m" in out


def test_no_color_turns_color_off_on_a_terminal(world):
    r = world.new_repo("r")
    r.commit("local")
    out = world.run_on_terminal(NO_COLOR="1")
    assert "Needs you:" in out
    assert "\033[" not in out
