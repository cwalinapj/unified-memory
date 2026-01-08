#!/usr/bin/env python3
"""
GitHub Sync for Unified Memory
==============================
Bi-directional sync between local memory store and GitHub repo.

Usage:
  sync_github.py push     # Push local changes to GitHub
  sync_github.py pull     # Pull remote changes to local
  sync_github.py status   # Show sync status

Environment variables:
  UNIFIED_MEMORY_REPO: GitHub repo (default: cwalinapj/unified-memory)
  GITHUB_TOKEN: Personal access token with repo scope
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

# Configuration
DEFAULT_REPO = "cwalinapj/unified-memory"
LOCAL_PATH = Path.home() / "unified-memory"
MEMORY_FILE = "memories.json"


def get_repo() -> str:
    return os.environ.get("UNIFIED_MEMORY_REPO", DEFAULT_REPO)


def run_git(args: list, cwd: Path = LOCAL_PATH) -> tuple:
    """Run a git command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def ensure_repo_initialized() -> bool:
    """Ensure local directory is a git repo connected to remote."""
    LOCAL_PATH.mkdir(parents=True, exist_ok=True)
    
    # Check if already a git repo
    if (LOCAL_PATH / ".git").exists():
        return True
    
    # Initialize new repo
    repo = get_repo()
    token = os.environ.get("GITHUB_TOKEN", "")
    
    if not token:
        print("Warning: GITHUB_TOKEN not set. Using SSH authentication.")
        remote_url = f"git@github.com:{repo}.git"
    else:
        remote_url = f"https://{token}@github.com/{repo}.git"
    
    # Try to clone first
    code, out, err = run_git(["clone", remote_url, str(LOCAL_PATH)], cwd=Path.home())
    
    if code != 0:
        # Repo might not exist, initialize locally
        print(f"Creating new repo (clone failed: {err})")
        run_git(["init"], cwd=LOCAL_PATH)
        run_git(["remote", "add", "origin", remote_url], cwd=LOCAL_PATH)
        
        # Create initial memory file if needed
        memory_path = LOCAL_PATH / MEMORY_FILE
        if not memory_path.exists():
            initial_store = {
                "version": "1.0.0",
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "memories": []
            }
            with open(memory_path, "w") as f:
                json.dump(initial_store, f, indent=2)
        
        # Initial commit
        run_git(["add", MEMORY_FILE])
        run_git(["commit", "-m", "Initialize unified memory store"])
    
    return True


def get_local_hash() -> str:
    """Get hash of local memory file."""
    memory_path = LOCAL_PATH / MEMORY_FILE
    if not memory_path.exists():
        return ""
    with open(memory_path, "rb") as f:
        import hashlib
        return hashlib.sha256(f.read()).hexdigest()[:12]


def push() -> bool:
    """Push local changes to GitHub."""
    ensure_repo_initialized()
    
    # Check for changes
    code, status, _ = run_git(["status", "--porcelain"])
    if not status:
        print("No local changes to push.")
        return True
    
    # Stage and commit
    run_git(["add", MEMORY_FILE])
    
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_git(["commit", "-m", f"Memory sync: {now}"])
    
    # Push
    code, out, err = run_git(["push", "-u", "origin", "main"])
    if code != 0:
        # Try master branch
        code, out, err = run_git(["push", "-u", "origin", "master"])
    
    if code == 0:
        print(f"✓ Pushed to {get_repo()}")
        return True
    else:
        print(f"✗ Push failed: {err}")
        return False


def pull() -> bool:
    """Pull remote changes to local."""
    ensure_repo_initialized()
    
    # Fetch and merge
    code, out, err = run_git(["pull", "--rebase", "origin", "main"])
    if code != 0:
        code, out, err = run_git(["pull", "--rebase", "origin", "master"])
    
    if code == 0:
        print(f"✓ Pulled from {get_repo()}")
        return True
    else:
        print(f"✗ Pull failed: {err}")
        return False


def status() -> None:
    """Show sync status."""
    ensure_repo_initialized()
    
    print(f"Repo: {get_repo()}")
    print(f"Local: {LOCAL_PATH}")
    print(f"Local hash: {get_local_hash()}")
    
    # Check git status
    code, out, _ = run_git(["status", "--porcelain"])
    if out:
        print("Local changes:")
        for line in out.split("\n"):
            print(f"  {line}")
    else:
        print("No local changes")
    
    # Check remote
    code, out, _ = run_git(["log", "HEAD..origin/main", "--oneline"])
    if code == 0 and out:
        print("Remote changes:")
        for line in out.split("\n"):
            print(f"  {line}")
    
    # Memory stats
    memory_path = LOCAL_PATH / MEMORY_FILE
    if memory_path.exists():
        with open(memory_path) as f:
            store = json.load(f)
        print(f"\nMemories: {len(store['memories'])}")
        print(f"Last sync: {store.get('last_sync', 'never')}")
        
        # Type breakdown
        types = {}
        for mem in store["memories"]:
            t = mem["type"]
            types[t] = types.get(t, 0) + 1
        if types:
            print("By type:")
            for t, count in sorted(types.items()):
                print(f"  {t}: {count}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: sync_github.py <push|pull|status>")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "push":
        success = push()
        sys.exit(0 if success else 1)
    elif cmd == "pull":
        success = pull()
        sys.exit(0 if success else 1)
    elif cmd == "status":
        status()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
