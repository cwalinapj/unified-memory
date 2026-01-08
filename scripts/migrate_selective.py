#!/usr/bin/env python3
"""
Selective Migration - High-value memories only
Skip preferences (too noisy), migrate everything else.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from memory_client import add_memory

MAC_AGENT_KV = Path.home() / "mac-agent" / "memory_backup" / "kv_store.json"

SKIP_TYPES = {"preference"}  # Skip noisy low-value memories

def infer_type(content: str, key: str) -> str:
    cl = content.lower()
    kl = key.lower()
    if any(x in cl for x in ["never", "always must", "required", "forbidden"]):
        return "constraint"
    if any(x in cl for x in ["decided", "chosen", "selected", "will use", "using"]):
        return "decision"
    if any(x in cl for x in ["step", "process", "workflow", "run ", "execute", "script"]):
        return "procedure"
    if any(x in cl for x in ["goal", "target", "objective", "deadline"]):
        return "goal"
    if any(x in cl for x in ["learned", "found that", "discovered", "turns out"]):
        return "lesson"
    if any(x in cl for x in ["might", "possibly", "hypothesis", "theory"]):
        return "hypothesis"
    if any(x in kl for x in ["path", "port", "url", "config", "version"]):
        return "observation"
    return "preference"


def migrate(dry_run: bool = False):
    with open(MAC_AGENT_KV) as f:
        data = json.load(f)
    
    migrated = 0
    skipped = 0
    
    for key, entry in data.items():
        value = entry.get("value", "")
        if not value or len(value) < 3:
            continue
        
        content = f"{key}: {value}"
        mtype = infer_type(content, key)
        
        if mtype in SKIP_TYPES:
            skipped += 1
            continue
        
        tags = entry.get("tags", [])
        
        if dry_run:
            print(f"[{mtype}] {content[:100]}...")
        else:
            # Truncate very long values
            if len(content) > 500:
                content = content[:500] + "..."
            
            add_memory(
                content=content,
                memory_type=mtype,
                source="agent",
                tags=tags + ["migrated", "mac-agent"],
                agent_id="mac-agent-v1",
                rationale="Migrated from mac-agent KV store" if mtype in ["constraint", "decision"] else None,
                confidence=0.7 if mtype in ["lesson", "hypothesis"] else None,
            )
            migrated += 1
    
    print(f"\nMigrated: {migrated}")
    print(f"Skipped (preferences): {skipped}")
    return migrated


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN ===\n")
    migrate(dry_run=dry_run)
