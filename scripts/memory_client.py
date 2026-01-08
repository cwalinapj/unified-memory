#!/usr/bin/env python3
"""
Unified Memory Client
=====================
Read/write operations for the unified memory substrate.
Used by both Claude (via MCP filesystem) and Origin OS agents.

Storage locations:
  - Local: ~/unified-memory/memories.json
  - GitHub: <repo>/memories.json (synced separately)
"""

import json
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Literal

# Default paths
DEFAULT_LOCAL_PATH = Path.home() / "unified-memory" / "memories.json"
MEMORY_DIR = Path.home() / "unified-memory"

MemoryType = Literal[
    "preference", "decision", "constraint", "goal",
    "procedure", "lesson", "observation", "hypothesis"
]

SourceType = Literal["human", "claude", "agent", "system"]

# Type metadata for validation and conflict resolution
TYPE_METADATA = {
    "preference": {"authority": 1, "requires_rationale": False, "requires_confidence": False, "expires": False},
    "decision": {"authority": 4, "requires_rationale": True, "requires_confidence": False, "expires": False},
    "constraint": {"authority": 5, "requires_rationale": True, "requires_confidence": False, "expires": False},
    "goal": {"authority": 3, "requires_rationale": False, "requires_confidence": False, "expires": True},
    "procedure": {"authority": 4, "requires_rationale": False, "requires_confidence": False, "expires": False},
    "lesson": {"authority": 3, "requires_rationale": False, "requires_confidence": True, "expires": False},
    "observation": {"authority": 1, "requires_rationale": False, "requires_confidence": False, "expires": True},
    "hypothesis": {"authority": 0, "requires_rationale": False, "requires_confidence": True, "expires": True},
}


def generate_id(content: str) -> str:
    """Generate 8-char hex ID from content hash."""
    return hashlib.sha256(content.encode()).hexdigest()[:8]


def now_iso() -> str:
    """Current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def load_memories(path: Path = DEFAULT_LOCAL_PATH) -> dict:
    """Load memory store from disk."""
    if not path.exists():
        return {"version": "1.0.0", "last_sync": None, "memories": []}
    with open(path, "r") as f:
        return json.load(f)


def save_memories(store: dict, path: Path = DEFAULT_LOCAL_PATH) -> None:
    """Save memory store to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    store["last_sync"] = now_iso()
    with open(path, "w") as f:
        json.dump(store, f, indent=2)


def add_memory(
    content: str,
    memory_type: MemoryType,
    source: SourceType,
    rationale: Optional[str] = None,
    confidence: Optional[float] = None,
    tags: Optional[List[str]] = None,
    context: Optional[str] = None,
    expires_at: Optional[str] = None,
    agent_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    supersedes: Optional[str] = None,
    promoted_from: Optional[str] = None,
    path: Path = DEFAULT_LOCAL_PATH,
) -> dict:
    """
    Add a new memory to the store.
    
    Returns the created memory object.
    """
    meta = TYPE_METADATA[memory_type]
    
    # Validate required fields
    if meta["requires_rationale"] and not rationale:
        raise ValueError(f"{memory_type} requires rationale")
    if meta["requires_confidence"] and confidence is None:
        raise ValueError(f"{memory_type} requires confidence score")
    
    memory = {
        "id": generate_id(content + now_iso()),
        "type": memory_type,
        "content": content,
        "tags": tags or [],
        "provenance": {
            "source": source,
            "timestamp": now_iso(),
            "agent_id": agent_id,
            "conversation_id": conversation_id,
        }
    }
    
    if rationale:
        memory["rationale"] = rationale
    if confidence is not None:
        memory["confidence"] = confidence
    if context:
        memory["context"] = context
    if expires_at:
        memory["expires_at"] = expires_at
    if supersedes:
        memory["supersedes"] = supersedes
    if promoted_from:
        memory["promoted_from"] = promoted_from
    
    store = load_memories(path)
    store["memories"].append(memory)
    save_memories(store, path)
    
    return memory


def get_memories(
    memory_type: Optional[MemoryType] = None,
    source: Optional[SourceType] = None,
    tags: Optional[List[str]] = None,
    context: Optional[str] = None,
    include_expired: bool = False,
    path: Path = DEFAULT_LOCAL_PATH,
) -> List[dict]:
    """
    Query memories with optional filters.
    
    Returns list of matching memories, sorted by authority (highest first).
    """
    store = load_memories(path)
    now = datetime.now(timezone.utc)
    
    results = []
    for mem in store["memories"]:
        # Filter by type
        if memory_type and mem["type"] != memory_type:
            continue
        # Filter by source
        if source and mem["provenance"]["source"] != source:
            continue
        # Filter by tags (any match)
        if tags and not any(t in mem.get("tags", []) for t in tags):
            continue
        # Filter by context
        if context and mem.get("context") != context:
            continue
        # Filter expired
        if not include_expired and mem.get("expires_at"):
            exp = datetime.fromisoformat(mem["expires_at"].replace("Z", "+00:00"))
            if exp < now:
                continue
        
        results.append(mem)
    
    # Sort by authority (highest first)
    results.sort(key=lambda m: TYPE_METADATA[m["type"]]["authority"], reverse=True)
    
    return results


def search_memories(
    query: str = "",
    memory_type: Optional[MemoryType] = None,
    limit: int = 10,
    path: Path = DEFAULT_LOCAL_PATH,
) -> List[dict]:
    """
    Search memories by text query and/or type.
    
    Simple keyword matching - returns memories where content contains query.
    """
    store = load_memories(path)
    query_lower = query.lower()
    
    results = []
    for mem in store["memories"]:
        # Filter by type
        if memory_type and mem["type"] != memory_type:
            continue
        # Filter by query (case-insensitive content search)
        if query and query_lower not in mem.get("content", "").lower():
            # Also check tags
            if not any(query_lower in t.lower() for t in mem.get("tags", [])):
                continue
        results.append(mem)
    
    # Sort by authority (highest first)
    results.sort(key=lambda m: TYPE_METADATA[m["type"]]["authority"], reverse=True)
    
    return results[:limit]


def get_context_summary(context: Optional[str] = None, path: Path = DEFAULT_LOCAL_PATH) -> str:
    """
    Generate a human-readable summary for Claude to read at conversation start.
    
    Groups memories by type and formats for easy consumption.
    """
    memories = get_memories(context=context, path=path)
    
    if not memories:
        return "No memories stored yet."
    
    lines = [f"# Unified Memory Summary", f"Last sync: {load_memories(path).get('last_sync', 'never')}", ""]
    
    # Group by type in authority order
    type_order = ["constraint", "decision", "procedure", "goal", "lesson", "preference", "observation", "hypothesis"]
    
    for mtype in type_order:
        typed_mems = [m for m in memories if m["type"] == mtype]
        if not typed_mems:
            continue
        
        lines.append(f"## {mtype.title()}s ({len(typed_mems)})")
        for mem in typed_mems:
            source = mem["provenance"]["source"]
            tags = ", ".join(mem.get("tags", [])) or "none"
            lines.append(f"- [{source}] {mem['content']}")
            if mem.get("rationale"):
                lines.append(f"  ↳ Rationale: {mem['rationale']}")
            if mem.get("confidence") is not None:
                lines.append(f"  ↳ Confidence: {mem['confidence']:.0%}")
        lines.append("")
    
    return "\n".join(lines)


def promote_memory(
    memory_id: str,
    new_type: MemoryType,
    rationale: Optional[str] = None,
    confidence: Optional[float] = None,
    path: Path = DEFAULT_LOCAL_PATH,
) -> dict:
    """
    Promote a memory to a higher-authority type.
    
    Creates a new memory linked to the original via promoted_from.
    """
    store = load_memories(path)
    
    # Find original memory
    original = None
    for mem in store["memories"]:
        if mem["id"] == memory_id:
            original = mem
            break
    
    if not original:
        raise ValueError(f"Memory {memory_id} not found")
    
    # Create promoted memory
    return add_memory(
        content=original["content"],
        memory_type=new_type,
        source="system",
        rationale=rationale,
        confidence=confidence,
        tags=original.get("tags"),
        context=original.get("context"),
        promoted_from=memory_id,
        path=path,
    )


def supersede_memory(
    memory_id: str,
    new_content: str,
    source: SourceType,
    rationale: Optional[str] = None,
    path: Path = DEFAULT_LOCAL_PATH,
) -> dict:
    """
    Replace a memory with updated content.
    
    Creates a new memory linked to the original via supersedes.
    """
    store = load_memories(path)
    
    # Find original memory
    original = None
    for mem in store["memories"]:
        if mem["id"] == memory_id:
            original = mem
            break
    
    if not original:
        raise ValueError(f"Memory {memory_id} not found")
    
    # Create superseding memory
    return add_memory(
        content=new_content,
        memory_type=original["type"],
        source=source,
        rationale=rationale or original.get("rationale"),
        confidence=original.get("confidence"),
        tags=original.get("tags"),
        context=original.get("context"),
        supersedes=memory_id,
        path=path,
    )


# =============================================================================
# SESSION CHECKPOINT FUNCTIONS
# =============================================================================
# These allow Claude to log progress so it can self-recover after crashes

def get_session_state(path: Path = DEFAULT_LOCAL_PATH) -> dict:
    """
    Get current session state for Claude to read at conversation start.
    
    Returns the active task with steps completed and next steps.
    """
    store = load_memories(path)
    return store.get("session_log", {"active_task": None, "history": []})


def checkpoint(
    step_completed: Optional[str] = None,
    next_steps: Optional[List[str]] = None,
    blockers: Optional[List[str]] = None,
    notes: Optional[str] = None,
    path: Path = DEFAULT_LOCAL_PATH,
) -> dict:
    """
    Update session checkpoint. Call this after completing significant work.
    
    - step_completed: Add to steps_completed list
    - next_steps: Replace the next_steps list entirely
    - blockers: Replace blockers list
    - notes: Freeform notes for context
    
    Returns updated session state.
    """
    store = load_memories(path)
    
    if "session_log" not in store:
        store["session_log"] = {"active_task": None, "history": []}
    
    session = store["session_log"]
    
    if session.get("active_task"):
        if step_completed:
            session["active_task"]["steps_completed"].append(step_completed)
        if next_steps is not None:
            session["active_task"]["next_steps"] = next_steps
        if blockers is not None:
            session["active_task"]["blockers"] = blockers
        if notes:
            session["active_task"]["notes"] = notes
    
    session["last_checkpoint"] = now_iso()
    save_memories(store, path)
    
    return session


def start_task(
    name: str,
    description: str,
    next_steps: Optional[List[str]] = None,
    path: Path = DEFAULT_LOCAL_PATH,
) -> dict:
    """
    Start a new task, archiving any existing active task.
    """
    store = load_memories(path)
    
    if "session_log" not in store:
        store["session_log"] = {"active_task": None, "history": []}
    
    session = store["session_log"]
    
    # Archive current task if exists
    if session.get("active_task"):
        session["active_task"]["ended"] = now_iso()
        session["active_task"]["status"] = "interrupted"
        session["history"].append(session["active_task"])
    
    # Start new task
    session["active_task"] = {
        "name": name,
        "description": description,
        "started": now_iso(),
        "steps_completed": [],
        "next_steps": next_steps or [],
        "blockers": [],
    }
    session["last_checkpoint"] = now_iso()
    
    save_memories(store, path)
    return session


def end_task(
    status: str = "completed",
    summary: Optional[str] = None,
    path: Path = DEFAULT_LOCAL_PATH,
) -> dict:
    """
    End the current task and archive it.
    
    status: 'completed', 'paused', 'blocked', 'abandoned'
    """
    store = load_memories(path)
    session = store.get("session_log", {"active_task": None, "history": []})
    
    if session.get("active_task"):
        session["active_task"]["ended"] = now_iso()
        session["active_task"]["status"] = status
        if summary:
            session["active_task"]["summary"] = summary
        session["history"].append(session["active_task"])
        session["active_task"] = None
    
    session["last_checkpoint"] = now_iso()
    save_memories(store, path)
    
    return session


def get_recovery_context(path: Path = DEFAULT_LOCAL_PATH) -> str:
    """
    Generate recovery context for Claude to read after a crash.
    
    This tells Claude exactly where we left off.
    """
    session = get_session_state(path)
    
    if not session.get("active_task"):
        return "No active task. Ready for new work."
    
    task = session["active_task"]
    lines = [
        f"# Recovery Context",
        f"Last checkpoint: {session.get('last_checkpoint', 'unknown')}",
        f"",
        f"## Active Task: {task['name']}",
        f"{task['description']}",
        f"",
        f"## Steps Completed ({len(task['steps_completed'])})",
    ]
    
    for step in task["steps_completed"]:
        lines.append(f"- ✓ {step}")
    
    lines.append(f"")
    lines.append(f"## Next Steps ({len(task['next_steps'])})")
    
    for step in task["next_steps"]:
        lines.append(f"- ○ {step}")
    
    if task.get("blockers"):
        lines.append(f"")
        lines.append(f"## Blockers")
        for b in task["blockers"]:
            lines.append(f"- ⚠ {b}")
    
    if task.get("notes"):
        lines.append(f"")
        lines.append(f"## Notes")
        lines.append(task["notes"])
    
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: memory_client.py <command> [args]")
        print("Commands: summary, add, list, promote, supersede")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "summary":
        context = sys.argv[2] if len(sys.argv) > 2 else None
        print(get_context_summary(context))
    
    elif cmd == "list":
        mtype = sys.argv[2] if len(sys.argv) > 2 else None
        memories = get_memories(memory_type=mtype)
        print(json.dumps(memories, indent=2))
    
    elif cmd == "add":
        if len(sys.argv) < 5:
            print("Usage: memory_client.py add <type> <source> <content> [--rationale R] [--confidence C]")
            sys.exit(1)
        mtype, source, content = sys.argv[2], sys.argv[3], sys.argv[4]
        # Parse optional args
        rationale = None
        confidence = None
        i = 5
        while i < len(sys.argv):
            if sys.argv[i] == "--rationale" and i + 1 < len(sys.argv):
                rationale = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--confidence" and i + 1 < len(sys.argv):
                confidence = float(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        mem = add_memory(content, mtype, source, rationale=rationale, confidence=confidence)
        print(f"Created memory: {mem['id']}")
    
    elif cmd == "checkpoint":
        step = sys.argv[2] if len(sys.argv) > 2 else None
        session = checkpoint(step_completed=step)
        print(f"Checkpoint saved: {session.get('last_checkpoint')}")
    
    elif cmd == "recover":
        print(get_recovery_context())
    
    elif cmd == "start-task":
        if len(sys.argv) < 4:
            print("Usage: memory_client.py start-task <name> <description>")
            sys.exit(1)
        session = start_task(sys.argv[2], sys.argv[3])
        print(f"Started task: {session['active_task']['name']}")
    
    elif cmd == "end-task":
        status = sys.argv[2] if len(sys.argv) > 2 else "completed"
        session = end_task(status)
        print(f"Task ended with status: {status}")
    
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
