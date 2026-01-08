#!/usr/bin/env python3
"""
Memory Migration Tool
=====================
Migrate existing memories from various sources into the unified format.

Supported sources:
  - mac-agent KV store (~/mac-agent/memory_backup/kv_store.json)
  - runpod memories (~/runpod_memories.json)
  - Claude memory.md (~/claude_memory.md)
  - Memory service Redis export

Usage:
  migrate_existing.py --source mac-agent
  migrate_existing.py --source runpod
  migrate_existing.py --source claude-md
  migrate_existing.py --all
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

# Import from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from memory_client import add_memory, load_memories, save_memories, DEFAULT_LOCAL_PATH

# Source paths
MAC_AGENT_KV = Path.home() / "mac-agent" / "memory_backup" / "kv_store.json"
RUNPOD_MEMORIES = Path.home() / "runpod_memories.json"
CLAUDE_MEMORY_MD = Path.home() / "claude_memory.md"


def infer_type_from_content(content: str, key: str = "") -> str:
    """
    Heuristically infer memory type from content.
    
    This is imperfect - human review recommended for high-authority types.
    """
    content_lower = content.lower()
    key_lower = key.lower()
    
    # Constraint indicators (strict rules)
    if any(x in content_lower for x in ["never", "always must", "required", "forbidden", "illegal"]):
        return "constraint"
    
    # Decision indicators (resolved choices)
    if any(x in content_lower for x in ["decided", "chosen", "selected", "will use", "using"]):
        return "decision"
    
    # Procedure indicators (how-to)
    if any(x in content_lower for x in ["step", "process", "workflow", "run ", "execute", "script"]):
        return "procedure"
    
    # Goal indicators (time-bound objectives)
    if any(x in content_lower for x in ["goal", "target", "objective", "deadline", "by end of"]):
        return "goal"
    
    # Lesson indicators (learned from experience)
    if any(x in content_lower for x in ["learned", "found that", "discovered", "turns out"]):
        return "lesson"
    
    # Hypothesis indicators (unverified)
    if any(x in content_lower for x in ["might", "possibly", "hypothesis", "theory", "suspect"]):
        return "hypothesis"
    
    # Observation indicators (factual statements about environment)
    if any(x in key_lower for x in ["path", "port", "url", "config", "version"]):
        return "observation"
    
    # Default to preference (lowest authority, safest)
    return "preference"


def migrate_mac_agent_kv(dry_run: bool = False) -> int:
    """Migrate memories from mac-agent KV store."""
    if not MAC_AGENT_KV.exists():
        print(f"Source not found: {MAC_AGENT_KV}")
        return 0
    
    print(f"Loading: {MAC_AGENT_KV}")
    
    # This file can be large, stream-process it
    with open(MAC_AGENT_KV) as f:
        kv_store = json.load(f)
    
    migrated = 0
    for key, entry in kv_store.items():
        value = entry.get("value", "")
        if not value or len(value) < 3:  # Skip empty/trivial entries
            continue
        
        content = f"{key}: {value}"
        mtype = infer_type_from_content(content, key)
        tags = entry.get("tags", [])
        
        # Parse timestamp
        created = entry.get("created_at")
        
        if dry_run:
            print(f"  [{mtype}] {content[:80]}...")
        else:
            try:
                add_memory(
                    content=content,
                    memory_type=mtype,
                    source="agent",
                    tags=tags + ["migrated", "mac-agent"],
                    agent_id="mac-agent-v1",
                )
                migrated += 1
            except Exception as e:
                print(f"  Error: {e}")
    
    return migrated


def migrate_runpod_memories(dry_run: bool = False) -> int:
    """Migrate memories from runpod exports."""
    if not RUNPOD_MEMORIES.exists():
        print(f"Source not found: {RUNPOD_MEMORIES}")
        return 0
    
    print(f"Loading: {RUNPOD_MEMORIES}")
    
    with open(RUNPOD_MEMORIES) as f:
        memories = json.load(f)
    
    migrated = 0
    for key, entry in memories.items():
        value = entry.get("value", "")
        if not value:
            continue
        
        content = f"{key}: {value}"
        mtype = infer_type_from_content(content, key)
        
        if dry_run:
            print(f"  [{mtype}] {content[:80]}...")
        else:
            try:
                add_memory(
                    content=content,
                    memory_type=mtype,
                    source="agent",
                    tags=["migrated", "runpod"],
                    agent_id="runpod-agent",
                )
                migrated += 1
            except Exception as e:
                print(f"  Error: {e}")
    
    return migrated


def migrate_claude_memory_md(dry_run: bool = False) -> int:
    """Migrate memories from claude_memory.md."""
    if not CLAUDE_MEMORY_MD.exists():
        print(f"Source not found: {CLAUDE_MEMORY_MD}")
        return 0
    
    print(f"Loading: {CLAUDE_MEMORY_MD}")
    
    with open(CLAUDE_MEMORY_MD) as f:
        content = f.read()
    
    migrated = 0
    
    # Parse markdown structure
    current_section = None
    
    for line in content.split("\n"):
        line = line.strip()
        
        # Track sections
        if line.startswith("## "):
            current_section = line[3:].lower()
            continue
        if line.startswith("### "):
            current_section = line[4:].lower()
            continue
        
        # Skip empty lines and headers
        if not line or line.startswith("#"):
            continue
        
        # Parse table rows (| Port | Service |)
        if line.startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 2 and cells[0] and cells[1]:
                # Skip header rows
                if cells[0].lower() in ["port", "key", "name"]:
                    continue
                content_str = f"{cells[1]} on port {cells[0]}" if cells[0].isdigit() else f"{cells[0]}: {cells[1]}"
                mtype = "observation"  # Service configs are observations
                
                if dry_run:
                    print(f"  [{mtype}] {content_str}")
                else:
                    try:
                        add_memory(
                            content=content_str,
                            memory_type=mtype,
                            source="claude",
                            tags=["migrated", "claude-md", current_section or "general"],
                            context="origin-os" if "origin" in (current_section or "").lower() else None,
                        )
                        migrated += 1
                    except Exception as e:
                        print(f"  Error: {e}")
        
        # Parse bullet points
        elif line.startswith("- "):
            content_str = line[2:]
            mtype = infer_type_from_content(content_str)
            
            if dry_run:
                print(f"  [{mtype}] {content_str[:80]}...")
            else:
                try:
                    add_memory(
                        content=content_str,
                        memory_type=mtype,
                        source="claude",
                        tags=["migrated", "claude-md", current_section or "general"],
                    )
                    migrated += 1
                except Exception as e:
                    print(f"  Error: {e}")
    
    return migrated


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Migrate existing memories to unified format")
    parser.add_argument("--source", choices=["mac-agent", "runpod", "claude-md"], help="Source to migrate")
    parser.add_argument("--all", action="store_true", help="Migrate all sources")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated without doing it")
    args = parser.parse_args()
    
    if not args.source and not args.all:
        parser.print_help()
        return 1
    
    total = 0
    
    if args.all or args.source == "mac-agent":
        print("\n=== Mac Agent KV Store ===")
        count = migrate_mac_agent_kv(dry_run=args.dry_run)
        print(f"{'Would migrate' if args.dry_run else 'Migrated'}: {count}")
        total += count
    
    if args.all or args.source == "runpod":
        print("\n=== RunPod Memories ===")
        count = migrate_runpod_memories(dry_run=args.dry_run)
        print(f"{'Would migrate' if args.dry_run else 'Migrated'}: {count}")
        total += count
    
    if args.all or args.source == "claude-md":
        print("\n=== Claude Memory MD ===")
        count = migrate_claude_memory_md(dry_run=args.dry_run)
        print(f"{'Would migrate' if args.dry_run else 'Migrated'}: {count}")
        total += count
    
    print(f"\n{'Total would migrate' if args.dry_run else 'Total migrated'}: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
