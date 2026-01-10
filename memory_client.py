#!/usr/bin/env python3
"""
Memory Client

Simple client for agents to query memory.
Works with either local index or API server.

Usage:
    from memory_client import recall, remember
    
    # Search memories
    context = recall("how to deploy solana")
    
    # Write new memory
    remember("Learned that X causes Y", type="lesson", tags=["debugging"])
"""

import json
import urllib.request
from pathlib import Path
from typing import List, Optional, Dict

API_URL = "http://localhost:7437"

def _api_available() -> bool:
    """Check if API server is running"""
    try:
        req = urllib.request.Request(f"{API_URL}/health")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status == 200
    except:
        return False

def _local_search(query: str, n: int = 5, memory_type: str = None) -> str:
    """Direct local search (no server)"""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from index import search_for_context
    return search_for_context(query, max_tokens=2000)

def recall(
    query: str,
    n: int = 5,
    memory_type: Optional[str] = None,
    max_tokens: int = 2000,
    raw: bool = False,
):
    """
    Search memories semantically.
    
    Args:
        query: What to search for
        n: Max results
        memory_type: Filter by type (lesson, procedure, constraint, etc.)
        max_tokens: Max context size
        raw: If True, return list of dicts instead of formatted string
    
    Returns:
        Formatted context string for LLM injection, or raw results if raw=True
    """
    if _api_available():
        endpoint = "/search" if raw else "/context"
        data = json.dumps({
            "query": query,
            "n": n,
            "type": memory_type,
            "max_tokens": max_tokens,
        }).encode()
        
        req = urllib.request.Request(
            f"{API_URL}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        
        return result.get("results", []) if raw else result.get("context", "")
    
    else:
        # Fall back to local
        return _local_search(query, n, memory_type)

def remember(
    content: str,
    type: str = "observation",
    tags: List[str] = None,
    source: str = "agent",
    rationale: str = None,
    confidence: float = None,
) -> Dict:
    """
    Write a new memory.
    
    Args:
        content: The memory content
        type: Memory type (observation, lesson, procedure, decision, constraint, hypothesis, goal, preference)
        tags: Optional tags for categorization
        source: Who created this (human, claude, agent, system)
        rationale: For decisions/constraints - why this was decided
        confidence: For lessons/hypotheses - 0.0 to 1.0
    
    Returns:
        Dict with id and status
    """
    if _api_available():
        data = json.dumps({
            "content": content,
            "type": type,
            "tags": tags or [],
            "source": source,
            "rationale": rationale,
            "confidence": confidence,
        }).encode()
        
        req = urllib.request.Request(
            f"{API_URL}/write",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    
    else:
        # Direct file write
        mem_path = Path.home() / "unified-memory" / "memories.json"
        
        with open(mem_path) as f:
            store = json.load(f)
        
        memories = store.get("memories", [])
        
        import hashlib
        import time
        mem_id = f"mem-{hashlib.sha256(f'{content}{time.time()}'.encode()).hexdigest()[:8]}"
        
        new_mem = {
            "id": mem_id,
            "type": type,
            "content": content,
            "tags": tags or [],
            "provenance": {
                "source": source,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        }
        
        if rationale:
            new_mem["rationale"] = rationale
        if confidence is not None:
            new_mem["confidence"] = confidence
        
        memories.append(new_mem)
        store["memories"] = memories
        
        with open(mem_path, "w") as f:
            json.dump(store, f, indent=2)
        
        return {"id": mem_id, "status": "created"}

# Convenience aliases
search = recall
write = remember

if __name__ == "__main__":
    # Quick test
    print("Testing recall...")
    result = recall("solana anchor compile")
    print(result[:500] if result else "No results")
