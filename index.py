#!/usr/bin/env python3
"""
Unified Memory Vector Index

Semantic search over memories using embeddings + FAISS.
This is what makes LLMs fast - not blockchain.

Usage:
    python3 index.py build              # Build/rebuild index
    python3 index.py search "query"     # Semantic search
    python3 index.py search "query" -n 5 -t lesson  # Filter by type
"""

import json
import os
import sys
import argparse
import pickle
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass
import numpy as np

# Lazy imports for speed
_model = None
_index = None
_memories = None

MEMORY_PATH = Path.home() / "unified-memory" / "memories.json"
INDEX_DIR = Path.home() / "unified-memory" / "index"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # Fast, good quality, 384 dims

@dataclass
class SearchResult:
    memory_id: str
    memory_type: str
    content: str
    score: float
    authority: int
    tags: List[str]

def get_authority(memory_type: str) -> int:
    """Authority level by type - higher = more trusted"""
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

def load_model():
    """Lazy load embedding model"""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        print(f"Loading embedding model: {EMBEDDING_MODEL}")
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model

def load_memories() -> List[Dict]:
    """Load memories from JSON"""
    if not MEMORY_PATH.exists():
        print(f"No memories found at {MEMORY_PATH}")
        return []
    
    with open(MEMORY_PATH) as f:
        data = json.load(f)
    
    # Handle both formats
    if isinstance(data, dict) and "memories" in data:
        return data["memories"]
    elif isinstance(data, list):
        return data
    else:
        return []

def build_index():
    """Build FAISS index from memories"""
    import faiss
    
    memories = load_memories()
    if not memories:
        print("No memories to index")
        return
    
    print(f"Indexing {len(memories)} memories...")
    
    model = load_model()
    
    # Create text for each memory (content + type + tags)
    texts = []
    for mem in memories:
        text = f"{mem.get('type', 'unknown')}: {mem.get('content', '')}"
        if mem.get('tags'):
            text += f" [{', '.join(mem['tags'])}]"
        if mem.get('rationale'):
            text += f" Rationale: {mem['rationale']}"
        texts.append(text)
    
    # Generate embeddings
    print("Generating embeddings...")
    embeddings = model.encode(texts, show_progress_bar=True, convert_to_numpy=True)
    
    # Normalize for cosine similarity
    faiss.normalize_L2(embeddings)
    
    # Build FAISS index (Inner Product = cosine similarity after normalization)
    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)
    
    # Save
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    
    faiss.write_index(index, str(INDEX_DIR / "faiss.index"))
    
    # Save memory metadata (for retrieval)
    metadata = []
    for mem in memories:
        metadata.append({
            "id": mem.get("id", "unknown"),
            "type": mem.get("type", "unknown"),
            "content": mem.get("content", ""),
            "tags": mem.get("tags", []),
            "authority": get_authority(mem.get("type", "")),
            "provenance": mem.get("provenance", {}),
        })
    
    with open(INDEX_DIR / "metadata.pkl", "wb") as f:
        pickle.dump(metadata, f)
    
    print(f"✅ Index built: {len(memories)} memories, {dim} dimensions")
    print(f"   Saved to: {INDEX_DIR}")
    
    # Print type distribution
    types = {}
    for mem in memories:
        t = mem.get("type", "unknown")
        types[t] = types.get(t, 0) + 1
    print("\nBy type:")
    for t, count in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {t}: {count}")

def load_index():
    """Load FAISS index and metadata"""
    global _index, _memories
    
    if _index is None:
        import faiss
        index_path = INDEX_DIR / "faiss.index"
        meta_path = INDEX_DIR / "metadata.pkl"
        
        if not index_path.exists():
            print("Index not found. Run: python3 index.py build")
            sys.exit(1)
        
        _index = faiss.read_index(str(index_path))
        with open(meta_path, "rb") as f:
            _memories = pickle.load(f)
    
    return _index, _memories

def search(
    query: str,
    n: int = 5,
    memory_type: Optional[str] = None,
    min_authority: int = 0,
) -> List[SearchResult]:
    """Semantic search over memories"""
    
    model = load_model()
    index, memories = load_index()
    
    # Embed query
    query_vec = model.encode([query], convert_to_numpy=True)
    import faiss
    faiss.normalize_L2(query_vec)
    
    # Search (get more results if filtering)
    k = min(n * 3, len(memories)) if memory_type or min_authority else n
    scores, indices = index.search(query_vec, k)
    
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        
        mem = memories[idx]
        
        # Apply filters
        if memory_type and mem["type"] != memory_type:
            continue
        if mem["authority"] < min_authority:
            continue
        
        results.append(SearchResult(
            memory_id=mem["id"],
            memory_type=mem["type"],
            content=mem["content"],
            score=float(score),
            authority=mem["authority"],
            tags=mem["tags"],
        ))
        
        if len(results) >= n:
            break
    
    return results

def search_for_context(query: str, max_tokens: int = 2000) -> str:
    """
    Search and format results for LLM context injection.
    This is the main function agents call.
    """
    results = search(query, n=10)
    
    if not results:
        return ""
    
    # Sort by authority (highest first), then score
    results.sort(key=lambda r: (-r.authority, -r.score))
    
    lines = ["<relevant_memories>"]
    total_chars = 0
    
    for r in results:
        entry = f"[{r.memory_type}|auth:{r.authority}|score:{r.score:.2f}] {r.content}"
        
        # Rough token estimate (4 chars per token)
        if total_chars + len(entry) > max_tokens * 4:
            break
        
        lines.append(entry)
        total_chars += len(entry)
    
    lines.append("</relevant_memories>")
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Unified Memory Vector Index")
    subparsers = parser.add_subparsers(dest="command")
    
    # Build command
    subparsers.add_parser("build", help="Build/rebuild the index")
    
    # Search command
    search_parser = subparsers.add_parser("search", help="Semantic search")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("-n", type=int, default=5, help="Number of results")
    search_parser.add_argument("-t", "--type", help="Filter by memory type")
    search_parser.add_argument("-a", "--authority", type=int, default=0, help="Minimum authority")
    search_parser.add_argument("--context", action="store_true", help="Output for LLM context")
    
    args = parser.parse_args()
    
    if args.command == "build":
        build_index()
    
    elif args.command == "search":
        if args.context:
            print(search_for_context(args.query))
        else:
            results = search(args.query, n=args.n, memory_type=args.type, min_authority=args.authority)
            
            if not results:
                print("No results found")
                return
            
            print(f"\n🔍 Results for: \"{args.query}\"\n")
            for i, r in enumerate(results, 1):
                print(f"{i}. [{r.memory_type}] (score: {r.score:.3f}, auth: {r.authority})")
                print(f"   {r.content[:100]}{'...' if len(r.content) > 100 else ''}")
                if r.tags:
                    print(f"   tags: {', '.join(r.tags)}")
                print()
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
