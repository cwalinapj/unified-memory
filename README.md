# Unified Memory Framework - Local Instance

## Quick Commands

```bash
# Read memory summary
python3 ~/unified-memory/scripts/memory_client.py summary

# Check session recovery context (after crash)
python3 ~/unified-memory/scripts/memory_client.py recover

# Add a memory
python3 ~/unified-memory/scripts/memory_client.py add <type> <source> "<content>"

# Checkpoint progress
python3 ~/unified-memory/scripts/memory_client.py checkpoint "Completed step description"

# Start new task
python3 ~/unified-memory/scripts/memory_client.py start-task "task-name" "Task description"

# End task
python3 ~/unified-memory/scripts/memory_client.py end-task completed
```

## Session Recovery Protocol

**For Claude at conversation start:**

1. Check `~/unified-memory/memories.json` for `session_log.active_task`
2. If active task exists, read `steps_completed` and `next_steps`
3. Resume from where last checkpoint left off
4. Call `checkpoint()` after completing significant work

```bash
# Claude should run this at start of conversation if user mentions crash/continuation
python3 ~/unified-memory/scripts/memory_client.py recover
```

## Files

- `memories.json` - Memory store + session log
- `scripts/memory_client.py` - CRUD operations + checkpointing
- `scripts/sync_github.py` - GitHub push/pull
- `scripts/migrate_existing.py` - Legacy memory import

## Setup Status

- [x] Directory structure created
- [x] memory_client.py with 8 memory types
- [x] sync_github.py for GitHub sync
- [x] migrate_existing.py for legacy import
- [x] Session checkpoint system added
- [ ] GitHub repo created (cwalinapj/unified-memory)
- [ ] Git initialized locally
- [ ] Foundational memories seeded
