#!/usr/bin/env python3
"""
Origin Memory MCP Server

Exposes unified-memory vector search to any Claude instance.
Enables multi-agent memory sharing through MCP protocol.

Tools:
    memory_recall     - Semantic search over memories
    memory_remember   - Store new memory
    memory_context    - Get formatted context for LLM injection
    memory_stats      - Get memory system statistics
    memory_types      - List available memory types

Usage:
    # Direct run (stdio transport)
    python3 mcp_server.py
    
    # Test with MCP Inspector
    npx @modelcontextprotocol/inspector python3 mcp_server.py
"""

import json
import sys
import time
import hashlib
from pathlib import Path
from typing import Optional, List
from enum import Enum

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# Paths
MEMORY_PATH = Path.home() / "unified-memory" / "memories.json"
INDEX_DIR = Path.home() / "unified-memory" / "index"

# Initialize MCP server
mcp = FastMCP("origin_memory_mcp")


# ============================================================================
# Enums and Input Models
# ============================================================================

class MemoryType(str, Enum):
    """Canonical memory types with authority levels."""
    HYPOTHESIS = "hypothesis"    # Authority 0 - Untested ideas
    OBSERVATION = "observation"  # Authority 1 - Noticed patterns
    PREFERENCE = "preference"    # Authority 1 - User/agent preferences
    LESSON = "lesson"            # Authority 3 - Learned from experience
    GOAL = "goal"                # Authority 3 - Objectives
    PROCEDURE = "procedure"      # Authority 4 - How to do things
    DECISION = "decision"        # Authority 4 - Choices made
    CONSTRAINT = "constraint"    # Authority 5 - Hard rules


class RecallInput(BaseModel):
    """Input for memory_recall tool."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    query: str = Field(
        ..., 
        description="Natural language search query (e.g., 'anchor compile rust', 'how to deploy solana')",
        min_length=1,
        max_length=500
    )
    top_k: int = Field(
        default=5,
        description="Number of results to return (1-20)",
        ge=1,
        le=20
    )
    memory_type: Optional[MemoryType] = Field(
        default=None,
        description="Filter by memory type (e.g., 'lesson', 'procedure')"
    )
    min_authority: int = Field(
        default=0,
        description="Minimum authority level (0-5). Higher = more trusted.",
        ge=0,
        le=5
    )


class RememberInput(BaseModel):
    """Input for memory_remember tool."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    content: str = Field(
        ...,
        description="The memory content to store",
        min_length=1,
        max_length=5000
    )
    memory_type: MemoryType = Field(
        ...,
        description="Type of memory: hypothesis, observation, preference, lesson, goal, procedure, decision, constraint"
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Searchable tags (e.g., ['solana', 'anchor', 'rust'])",
        max_length=20
    )
    rationale: Optional[str] = Field(
        default=None,
        description="Why this memory is being stored",
        max_length=500
    )
    confidence: Optional[float] = Field(
        default=None,
        description="Confidence level 0.0-1.0",
        ge=0.0,
        le=1.0
    )
    source: str = Field(
        default="agent",
        description="Source of the memory (e.g., 'claude', 'gpt-5-mini', 'user')",
        max_length=50
    )


class ContextInput(BaseModel):
    """Input for memory_context tool."""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    
    query: str = Field(
        ...,
        description="Query to find relevant context for",
        min_length=1,
        max_length=500
    )
    max_tokens: int = Field(
        default=2000,
        description="Approximate max tokens for context (100-8000)",
        ge=100,
        le=8000
    )


# ============================================================================
# Helper Functions
# ============================================================================

def get_authority(memory_type: str) -> int:
    """Get authority level for memory type."""
    return {
        "hypothesis": 0,
        "observation": 1,
        "preference": 1,
        "lesson": 3,
        "goal": 3,
        "procedure": 4,
        "decision": 4,
        "constraint": 5,
    }.get(memory_type, 0)


def load_index_module():
    """Lazy load the index module."""
    sys.path.insert(0, str(Path.home() / "unified-memory"))
    import index as idx
    return idx


def format_results_markdown(results: list) -> str:
    """Format search results as markdown for LLM consumption."""
    if not results:
        return "No relevant memories found."
    
    lines = []
    for i, r in enumerate(results, 1):
        score_bar = "█" * int(r["score"] * 10) + "░" * (10 - int(r["score"] * 10))
        lines.append(f"**{i}. [{r['type']}]** (auth:{r['authority']}, score:{r['score']:.2f} {score_bar})")
        lines.append(f"   {r['content'][:200]}{'...' if len(r['content']) > 200 else ''}")
        if r.get("tags"):
            lines.append(f"   _tags: {', '.join(r['tags'])}_")
        lines.append("")
    
    return "\n".join(lines)


def format_context_block(results: list, max_chars: int) -> str:
    """Format results as context block for LLM injection."""
    if not results:
        return ""
    
    # Sort by authority (highest first), then score
    sorted_results = sorted(results, key=lambda r: (-r["authority"], -r["score"]))
    
    lines = ["<relevant_memories>"]
    total_chars = 0
    
    for r in sorted_results:
        entry = f"[{r['type']}|auth:{r['authority']}|score:{r['score']:.2f}] {r['content']}"
        
        if total_chars + len(entry) > max_chars:
            break
        
        lines.append(entry)
        total_chars += len(entry)
    
    lines.append("</relevant_memories>")
    return "\n".join(lines)


# ============================================================================
# MCP Tools
# ============================================================================

@mcp.tool(
    name="memory_recall",
    annotations={
        "title": "Search Agent Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def memory_recall(params: RecallInput) -> str:
    """Search agent memories using semantic similarity.
    
    Returns memories ranked by relevance to your query, filtered by type and authority.
    Use this to find relevant context before making decisions or answering questions.
    
    Examples:
        - "anchor compile rust" → finds lessons about Anchor compilation
        - "deployment procedure" → finds how-to procedures
        - "user preferences" → finds stored preferences
    
    Args:
        params: RecallInput with query, top_k, memory_type filter, min_authority
    
    Returns:
        Markdown-formatted list of relevant memories with scores and metadata
    """
    try:
        idx = load_index_module()
        
        results = idx.search(
            params.query,
            n=params.top_k,
            memory_type=params.memory_type.value if params.memory_type else None,
            min_authority=params.min_authority
        )
        
        # Convert to dicts
        result_dicts = [
            {
                "id": r.memory_id,
                "type": r.memory_type,
                "content": r.content,
                "score": r.score,
                "authority": r.authority,
                "tags": r.tags,
            }
            for r in results
        ]
        
        return format_results_markdown(result_dicts)
    
    except FileNotFoundError:
        return "Error: Memory index not found. Run `python3 ~/unified-memory/index.py build` to create it."
    except Exception as e:
        return f"Error searching memories: {str(e)}"


@mcp.tool(
    name="memory_remember",
    annotations={
        "title": "Store New Memory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
async def memory_remember(params: RememberInput) -> str:
    """Store a new memory for future retrieval.
    
    Use this to save lessons learned, decisions made, procedures discovered,
    or any information that should persist across conversations.
    
    Memory types (by authority level):
        - hypothesis (0): Untested ideas, guesses
        - observation (1): Noticed patterns, data points
        - preference (1): User or agent preferences
        - lesson (3): Learned from experience
        - goal (3): Objectives, targets
        - procedure (4): How to do things, steps
        - decision (4): Choices made with rationale
        - constraint (5): Hard rules, must-follow guidelines
    
    Args:
        params: RememberInput with content, memory_type, tags, rationale, confidence, source
    
    Returns:
        Confirmation with memory ID and rebuild status
    """
    try:
        # Load existing memories
        if MEMORY_PATH.exists():
            with open(MEMORY_PATH) as f:
                store = json.load(f)
        else:
            store = {"memories": [], "schema_version": "1.0"}
        
        memories = store.get("memories", [])
        
        # Generate unique ID
        mem_id = f"mem-{hashlib.sha256(f'{params.content}{time.time()}'.encode()).hexdigest()[:12]}"
        
        # Build memory object
        new_mem = {
            "id": mem_id,
            "type": params.memory_type.value,
            "content": params.content,
            "tags": params.tags,
            "provenance": {
                "source": params.source,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        }
        
        if params.rationale:
            new_mem["rationale"] = params.rationale
        if params.confidence is not None:
            new_mem["confidence"] = params.confidence
        
        memories.append(new_mem)
        store["memories"] = memories
        
        # Save
        with open(MEMORY_PATH, "w") as f:
            json.dump(store, f, indent=2)
        
        # Trigger index rebuild via HTTP API if running
        try:
            import httpx
            httpx.post("http://localhost:7437/rebuild", timeout=1.0)
            rebuild_status = "Index rebuild triggered"
        except Exception:
            rebuild_status = "Index rebuild pending (run `python3 ~/unified-memory/index.py build`)"
        
        authority = get_authority(params.memory_type.value)
        
        return f"""✅ Memory stored successfully

**ID:** `{mem_id}`
**Type:** {params.memory_type.value} (authority: {authority})
**Tags:** {', '.join(params.tags) if params.tags else 'none'}
**Source:** {params.source}

{rebuild_status}"""
    
    except Exception as e:
        return f"Error storing memory: {str(e)}"


@mcp.tool(
    name="memory_context",
    annotations={
        "title": "Get LLM Context Block",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def memory_context(params: ContextInput) -> str:
    """Get formatted context block for LLM injection.
    
    Returns relevant memories in a structured format suitable for
    injecting into an LLM prompt. Results are sorted by authority
    (highest first) and truncated to fit token budget.
    
    Args:
        params: ContextInput with query and max_tokens
    
    Returns:
        XML-formatted context block with relevant memories
    """
    try:
        idx = load_index_module()
        
        results = idx.search(params.query, n=10)
        
        result_dicts = [
            {
                "id": r.memory_id,
                "type": r.memory_type,
                "content": r.content,
                "score": r.score,
                "authority": r.authority,
                "tags": r.tags,
            }
            for r in results
        ]
        
        # Rough token to char estimate (4 chars per token)
        max_chars = params.max_tokens * 4
        
        context = format_context_block(result_dicts, max_chars)
        
        if not context:
            return "No relevant memories found for context."
        
        return context
    
    except Exception as e:
        return f"Error getting context: {str(e)}"


@mcp.tool(
    name="memory_stats",
    annotations={
        "title": "Memory System Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def memory_stats() -> str:
    """Get statistics about the memory system.
    
    Returns counts by type, index status, and system health.
    
    Returns:
        Markdown-formatted statistics
    """
    try:
        if not MEMORY_PATH.exists():
            return "No memories found. Memory system not initialized."
        
        with open(MEMORY_PATH) as f:
            store = json.load(f)
        
        memories = store.get("memories", [])
        
        # Count by type
        types = {}
        for mem in memories:
            t = mem.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
        
        # Check index status
        index_exists = (INDEX_DIR / "faiss.index").exists()
        
        # Format output
        lines = [
            "## Memory System Statistics",
            "",
            f"**Total Memories:** {len(memories)}",
            f"**Index Status:** {'✅ Built' if index_exists else '❌ Not built'}",
            "",
            "### By Type",
            ""
        ]
        
        for t in ["constraint", "decision", "procedure", "goal", "lesson", "preference", "observation", "hypothesis"]:
            if t in types:
                auth = get_authority(t)
                lines.append(f"- **{t}** (auth:{auth}): {types[t]}")
        
        # Any unknown types
        for t, count in sorted(types.items()):
            if t not in ["constraint", "decision", "procedure", "goal", "lesson", "preference", "observation", "hypothesis"]:
                lines.append(f"- {t}: {count}")
        
        return "\n".join(lines)
    
    except Exception as e:
        return f"Error getting stats: {str(e)}"


@mcp.tool(
    name="memory_types",
    annotations={
        "title": "List Memory Types",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def memory_types() -> str:
    """List all available memory types with descriptions.
    
    Returns the 8 canonical memory types, their authority levels,
    and when to use each type.
    
    Returns:
        Markdown table of memory types
    """
    return """## Memory Types

| Type | Authority | Use For |
|------|-----------|---------|
| **constraint** | 5 | Hard rules, must-follow guidelines, safety limits |
| **decision** | 4 | Choices made with rationale, architectural decisions |
| **procedure** | 4 | How to do things, step-by-step processes |
| **goal** | 3 | Objectives, targets, desired outcomes |
| **lesson** | 3 | Learned from experience, "next time do X" |
| **preference** | 1 | User or agent preferences, style choices |
| **observation** | 1 | Noticed patterns, data points, correlations |
| **hypothesis** | 0 | Untested ideas, guesses, theories to validate |

**Authority levels** affect:
- Search result ranking (higher authority = ranked first)
- Trust scoring (higher = more trusted)
- Challenge windows (higher = longer verification period)
"""


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    mcp.run()
