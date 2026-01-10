#!/usr/bin/env python3
"""
Memory API Server

HTTP endpoint for agents to query memories.
Runs on localhost:7437 (MEMO on phone keypad)

Auto-rebuilds index on writes.

Endpoints:
    POST /search     - Semantic search
    POST /context    - Get context for LLM injection
    POST /write      - Write new memory (auto-rebuilds index)
    POST /rebuild    - Force rebuild index
    GET  /health     - Health check
    GET  /stats      - Memory statistics
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from index import search, search_for_context, load_model, load_index, get_authority, build_index

MEMORY_PATH = Path.home() / "unified-memory" / "memories.json"
INDEX_DIR = Path.home() / "unified-memory" / "index"
LOG_DIR = Path.home() / "unified-memory" / "logs"
PORT = 7437

# Track writes for batched rebuilds
_pending_rebuild = False
_rebuild_lock = threading.Lock()
_last_rebuild = 0

def async_rebuild():
    """Rebuild index in background thread"""
    global _pending_rebuild, _last_rebuild
    
    # Debounce - wait 2 seconds for batch writes
    time.sleep(2)
    
    with _rebuild_lock:
        if not _pending_rebuild:
            return
        _pending_rebuild = False
    
    try:
        print("[MEMO] Rebuilding index...")
        build_index()
        _last_rebuild = time.time()
        
        # Reload index in memory
        global _index, _memories
        import index as idx
        idx._index = None
        idx._memories = None
        load_index()
        
        print("[MEMO] Index rebuilt successfully")
    except Exception as e:
        print(f"[MEMO] Rebuild failed: {e}")

def trigger_rebuild():
    """Trigger async index rebuild"""
    global _pending_rebuild
    
    with _rebuild_lock:
        if _pending_rebuild:
            return  # Already scheduled
        _pending_rebuild = True
    
    thread = threading.Thread(target=async_rebuild, daemon=True)
    thread.start()

class MemoryHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        return json.loads(body) if body else {}
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def do_GET(self):
        if self.path == "/health":
            self._send_json({"status": "ok", "port": PORT})
        
        elif self.path == "/stats":
            try:
                with open(MEMORY_PATH) as f:
                    data = json.load(f)
                memories = data.get("memories", [])
                
                types = {}
                for mem in memories:
                    t = mem.get("type", "unknown")
                    types[t] = types.get(t, 0) + 1
                
                self._send_json({
                    "total": len(memories),
                    "by_type": types,
                    "last_rebuild": _last_rebuild,
                    "index_exists": (INDEX_DIR / "faiss.index").exists(),
                })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        
        else:
            self._send_json({"error": "Not found"}, 404)
    
    def do_POST(self):
        try:
            data = self._read_json()
            
            if self.path == "/search":
                query = data.get("query", "")
                n = data.get("n", 5)
                memory_type = data.get("type")
                min_authority = data.get("min_authority", 0)
                
                results = search(query, n=n, memory_type=memory_type, min_authority=min_authority)
                
                self._send_json({
                    "query": query,
                    "results": [
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
                })
            
            elif self.path == "/context":
                query = data.get("query", "")
                max_tokens = data.get("max_tokens", 2000)
                
                context = search_for_context(query, max_tokens=max_tokens)
                
                self._send_json({
                    "query": query,
                    "context": context,
                })
            
            elif self.path == "/write":
                content = data.get("content")
                memory_type = data.get("type", "observation")
                tags = data.get("tags", [])
                source = data.get("source", "agent")
                rationale = data.get("rationale")
                confidence = data.get("confidence")
                
                if not content:
                    self._send_json({"error": "content required"}, 400)
                    return
                
                # Load existing
                with open(MEMORY_PATH) as f:
                    store = json.load(f)
                
                memories = store.get("memories", [])
                
                # Generate ID
                import hashlib
                mem_id = f"mem-{hashlib.sha256(f'{content}{time.time()}'.encode()).hexdigest()[:8]}"
                
                new_mem = {
                    "id": mem_id,
                    "type": memory_type,
                    "content": content,
                    "tags": tags,
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
                
                with open(MEMORY_PATH, "w") as f:
                    json.dump(store, f, indent=2)
                
                # Trigger async rebuild
                trigger_rebuild()
                
                self._send_json({
                    "id": mem_id,
                    "status": "created",
                    "rebuild": "scheduled"
                })
            
            elif self.path == "/rebuild":
                trigger_rebuild()
                self._send_json({"status": "rebuild scheduled"})
            
            else:
                self._send_json({"error": "Not found"}, 404)
        
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
    
    def log_message(self, format, *args):
        print(f"[MEMO] {args[0]}")

def main():
    # Create log directory
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    # Pre-load model and index
    print("[MEMO] Loading model and index...")
    try:
        load_model()
        load_index()
    except Exception as e:
        print(f"[MEMO] Warning: Could not preload index: {e}")
        print("[MEMO] Will build on first write")
    
    server = HTTPServer(("127.0.0.1", PORT), MemoryHandler)
    print(f"\n🧠 Memory API Server")
    print(f"   http://localhost:{PORT}")
    print(f"\nEndpoints:")
    print(f"   POST /search   - Semantic search")
    print(f"   POST /context  - LLM context injection")
    print(f"   POST /write    - Write new memory (auto-rebuilds)")
    print(f"   POST /rebuild  - Force rebuild index")
    print(f"   GET  /health   - Health check")
    print(f"   GET  /stats    - Memory statistics")
    print(f"\nPress Ctrl+C to stop\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[MEMO] Shutting down...")
        server.shutdown()

if __name__ == "__main__":
    main()
