#!/usr/bin/env python3
"""
Origin Memory API Server - Layer 2

Authenticated HTTP API for multi-agent memory access.
Enables GPT-5-mini, custom agents, and any HTTP client to access memories.

Endpoints:
    POST /v1/search         - Semantic search (requires auth)
    POST /v1/context        - Get LLM context block (requires auth)
    POST /v1/write          - Write new memory (requires auth)
    GET  /v1/stats          - Memory statistics (requires auth)
    GET  /v1/types          - List memory types (public)
    
    POST /admin/agents      - Register new agent (admin only)
    GET  /admin/agents      - List agents (admin only)
    DELETE /admin/agents/{id} - Revoke agent (admin only)
    GET  /admin/audit       - View audit log (admin only)

Auth:
    Authorization: Bearer <api_key>

Run:
    python3 api_server.py                    # Default port 7438
    python3 api_server.py --port 8080        # Custom port
    uvicorn api_server:app --host 0.0.0.0    # Production
"""

import json
import time
import hashlib
import secrets
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum
from contextlib import asynccontextmanager
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
import httpx

# ============================================================================
# Configuration
# ============================================================================

MEMORY_PATH = Path.home() / "unified-memory" / "memories.json"
AGENTS_PATH = Path.home() / "unified-memory" / "agents.json"
AUDIT_PATH = Path.home() / "unified-memory" / "logs" / "api_audit.jsonl"
INTERNAL_API = "http://localhost:7437"  # FAISS server

# Admin key - in production, use env var
ADMIN_KEY = "originos-admin-2026"  # Change this!

# Rate limits
DEFAULT_RATE_LIMIT = 100  # requests per hour
DEFAULT_MAX_AUTHORITY = 3  # max authority level agent can write

# ============================================================================
# Models
# ============================================================================

class MemoryType(str, Enum):
    HYPOTHESIS = "hypothesis"
    OBSERVATION = "observation"
    PREFERENCE = "preference"
    LESSON = "lesson"
    GOAL = "goal"
    PROCEDURE = "procedure"
    DECISION = "decision"
    CONSTRAINT = "constraint"


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    
    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    memory_type: Optional[MemoryType] = None
    min_authority: int = Field(default=0, ge=0, le=5)


class ContextRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    
    query: str = Field(..., min_length=1, max_length=500)
    max_tokens: int = Field(default=2000, ge=100, le=8000)


class WriteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    
    content: str = Field(..., min_length=1, max_length=5000)
    memory_type: MemoryType
    tags: List[str] = Field(default_factory=list, max_length=20)
    rationale: Optional[str] = Field(default=None, max_length=500)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class RegisterAgentRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    
    agent_id: str = Field(..., min_length=1, max_length=50, pattern=r'^[a-z0-9_-]+$')
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    rate_limit: int = Field(default=DEFAULT_RATE_LIMIT, ge=1, le=10000)
    max_authority: int = Field(default=DEFAULT_MAX_AUTHORITY, ge=0, le=5)


class AgentInfo(BaseModel):
    agent_id: str
    name: str
    description: Optional[str]
    rate_limit: int
    max_authority: int
    created_at: str
    requests_today: int = 0
    reputation: int = 5000  # 0-10000, starts at 5000
    total_writes: int = 0
    total_reads: int = 0


# ============================================================================
# State Management
# ============================================================================

class AgentStore:
    """Manages agent registration and API keys."""
    
    def __init__(self):
        self.agents: Dict[str, AgentInfo] = {}
        self.key_to_agent: Dict[str, str] = {}  # key_hash -> agent_id
        self.rate_counters: Dict[str, List[float]] = defaultdict(list)  # agent_id -> timestamps
        self._load()
    
    def _load(self):
        """Load agents from disk."""
        if AGENTS_PATH.exists():
            try:
                data = json.loads(AGENTS_PATH.read_text())
                for agent_id, info in data.get("agents", {}).items():
                    self.agents[agent_id] = AgentInfo(**info)
                self.key_to_agent = data.get("keys", {})
            except Exception as e:
                print(f"Warning: Could not load agents: {e}")
    
    def _save(self):
        """Save agents to disk."""
        AGENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "agents": {k: v.model_dump() for k, v in self.agents.items()},
            "keys": self.key_to_agent
        }
        AGENTS_PATH.write_text(json.dumps(data, indent=2))
    
    def register(self, req: RegisterAgentRequest) -> tuple[AgentInfo, str]:
        """Register new agent, return info and API key."""
        if req.agent_id in self.agents:
            raise ValueError(f"Agent {req.agent_id} already exists")
        
        # Generate API key
        api_key = f"omem_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        
        # Create agent
        agent = AgentInfo(
            agent_id=req.agent_id,
            name=req.name,
            description=req.description,
            rate_limit=req.rate_limit,
            max_authority=req.max_authority,
            created_at=datetime.utcnow().isoformat() + "Z"
        )
        
        self.agents[req.agent_id] = agent
        self.key_to_agent[key_hash] = req.agent_id
        self._save()
        
        return agent, api_key
    
    def verify_key(self, api_key: str) -> Optional[AgentInfo]:
        """Verify API key and return agent info."""
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        agent_id = self.key_to_agent.get(key_hash)
        if agent_id:
            return self.agents.get(agent_id)
        return None
    
    def check_rate_limit(self, agent_id: str) -> bool:
        """Check if agent is within rate limit. Returns True if allowed."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        
        now = time.time()
        hour_ago = now - 3600
        
        # Clean old entries
        self.rate_counters[agent_id] = [
            t for t in self.rate_counters[agent_id] if t > hour_ago
        ]
        
        # Check limit
        if len(self.rate_counters[agent_id]) >= agent.rate_limit:
            return False
        
        # Record this request
        self.rate_counters[agent_id].append(now)
        return True
    
    def increment_stats(self, agent_id: str, is_write: bool):
        """Increment agent statistics."""
        agent = self.agents.get(agent_id)
        if agent:
            if is_write:
                agent.total_writes += 1
            else:
                agent.total_reads += 1
            self._save()
    
    def revoke(self, agent_id: str) -> bool:
        """Revoke agent access."""
        if agent_id not in self.agents:
            return False
        
        del self.agents[agent_id]
        
        # Remove associated keys
        self.key_to_agent = {
            k: v for k, v in self.key_to_agent.items() if v != agent_id
        }
        
        self._save()
        return True
    
    def list_agents(self) -> List[AgentInfo]:
        """List all registered agents."""
        return list(self.agents.values())


class AuditLog:
    """Append-only audit log for all API requests."""
    
    def __init__(self):
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    def log(self, agent_id: str, action: str, details: Dict[str, Any]):
        """Log an API action."""
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "agent_id": agent_id,
            "action": action,
            **details
        }
        with open(AUDIT_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def get_recent(self, limit: int = 100, agent_id: Optional[str] = None) -> List[Dict]:
        """Get recent audit entries."""
        if not AUDIT_PATH.exists():
            return []
        
        entries = []
        with open(AUDIT_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if agent_id and entry.get("agent_id") != agent_id:
                        continue
                    entries.append(entry)
                except:
                    pass
        
        return entries[-limit:]


# Global instances
agent_store = AgentStore()
audit_log = AuditLog()


# ============================================================================
# Auth Dependencies
# ============================================================================

async def verify_agent(authorization: str = Header(...)) -> AgentInfo:
    """Verify API key and return agent info."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header. Use: Bearer <api_key>")
    
    api_key = authorization[7:]
    agent = agent_store.verify_key(api_key)
    
    if not agent:
        raise HTTPException(401, "Invalid API key")
    
    if not agent_store.check_rate_limit(agent.agent_id):
        raise HTTPException(429, f"Rate limit exceeded ({agent.rate_limit}/hour)")
    
    return agent


async def verify_admin(x_admin_key: str = Header(...)):
    """Verify admin key."""
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(403, "Invalid admin key")


# ============================================================================
# FastAPI App
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifespan - startup and shutdown."""
    print(f"🚀 Origin Memory API starting on port 7438")
    print(f"   Internal FAISS API: {INTERNAL_API}")
    print(f"   Agents registered: {len(agent_store.agents)}")
    yield
    print("👋 Shutting down")


app = FastAPI(
    title="Origin Memory API",
    description="Multi-agent memory system with authentication and trust scoring",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# Helper Functions
# ============================================================================

def get_authority(memory_type: str) -> int:
    """Get authority level for memory type."""
    return {
        "hypothesis": 0, "observation": 1, "preference": 1,
        "lesson": 3, "goal": 3,
        "procedure": 4, "decision": 4,
        "constraint": 5,
    }.get(memory_type, 0)


async def call_internal_api(method: str, endpoint: str, data: dict = None) -> dict:
    """Call internal FAISS API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        url = f"{INTERNAL_API}{endpoint}"
        if method == "GET":
            resp = await client.get(url)
        else:
            resp = await client.post(url, json=data)
        
        if resp.status_code != 200:
            raise HTTPException(502, f"Internal API error: {resp.text}")
        
        return resp.json()


# ============================================================================
# Public Endpoints
# ============================================================================

@app.get("/")
async def root():
    """API info."""
    return {
        "name": "Origin Memory API",
        "version": "2.0.0",
        "docs": "/docs",
        "endpoints": {
            "search": "POST /v1/search",
            "context": "POST /v1/context",
            "write": "POST /v1/write",
            "stats": "GET /v1/stats",
            "types": "GET /v1/types"
        }
    }


@app.get("/health")
async def health():
    """Health check."""
    # Check internal API
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{INTERNAL_API}/health")
            internal_ok = resp.status_code == 200
    except:
        internal_ok = False
    
    return {
        "status": "ok" if internal_ok else "degraded",
        "internal_api": "ok" if internal_ok else "unavailable",
        "agents_registered": len(agent_store.agents)
    }


@app.get("/v1/types")
async def list_types():
    """List memory types (public endpoint)."""
    return {
        "types": [
            {"type": "constraint", "authority": 5, "description": "Hard rules, must-follow guidelines"},
            {"type": "decision", "authority": 4, "description": "Choices made with rationale"},
            {"type": "procedure", "authority": 4, "description": "How to do things, steps"},
            {"type": "goal", "authority": 3, "description": "Objectives, targets"},
            {"type": "lesson", "authority": 3, "description": "Learned from experience"},
            {"type": "preference", "authority": 1, "description": "User/agent preferences"},
            {"type": "observation", "authority": 1, "description": "Noticed patterns"},
            {"type": "hypothesis", "authority": 0, "description": "Untested ideas"},
        ]
    }


# ============================================================================
# Authenticated Endpoints
# ============================================================================

@app.post("/v1/search")
async def search(req: SearchRequest, agent: AgentInfo = Depends(verify_agent)):
    """Semantic search over memories."""
    
    result = await call_internal_api("POST", "/search", {
        "query": req.query,
        "n": req.top_k,
        "type": req.memory_type.value if req.memory_type else None,
        "min_authority": req.min_authority
    })
    
    # Log
    audit_log.log(agent.agent_id, "search", {
        "query": req.query,
        "results": len(result.get("results", []))
    })
    agent_store.increment_stats(agent.agent_id, is_write=False)
    
    return {
        "agent": agent.agent_id,
        "query": req.query,
        "results": result.get("results", []),
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }


@app.post("/v1/context")
async def get_context(req: ContextRequest, agent: AgentInfo = Depends(verify_agent)):
    """Get LLM-formatted context block."""
    
    result = await call_internal_api("POST", "/context", {
        "query": req.query,
        "max_tokens": req.max_tokens
    })
    
    audit_log.log(agent.agent_id, "context", {"query": req.query})
    agent_store.increment_stats(agent.agent_id, is_write=False)
    
    return {
        "agent": agent.agent_id,
        "query": req.query,
        "context": result.get("context", ""),
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }


@app.post("/v1/write")
async def write_memory(req: WriteRequest, agent: AgentInfo = Depends(verify_agent)):
    """Write a new memory."""
    
    # Check authority limit
    required_authority = get_authority(req.memory_type.value)
    if required_authority > agent.max_authority:
        raise HTTPException(
            403, 
            f"Agent limited to authority {agent.max_authority}, "
            f"but {req.memory_type.value} requires {required_authority}"
        )
    
    # Add agent tag
    tags = req.tags + [f"agent:{agent.agent_id}"]
    
    result = await call_internal_api("POST", "/write", {
        "content": req.content,
        "type": req.memory_type.value,
        "tags": tags,
        "source": agent.agent_id,
        "rationale": req.rationale,
        "confidence": req.confidence
    })
    
    audit_log.log(agent.agent_id, "write", {
        "memory_id": result.get("id"),
        "memory_type": req.memory_type.value,
        "content_length": len(req.content)
    })
    agent_store.increment_stats(agent.agent_id, is_write=True)
    
    return {
        "agent": agent.agent_id,
        "memory_id": result.get("id"),
        "status": "created",
        "type": req.memory_type.value,
        "authority": required_authority,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }


@app.get("/v1/stats")
async def get_stats(agent: AgentInfo = Depends(verify_agent)):
    """Get memory statistics."""
    
    result = await call_internal_api("GET", "/stats")
    
    audit_log.log(agent.agent_id, "stats", {})
    
    return {
        "agent": agent.agent_id,
        **result,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }


@app.get("/v1/me")
async def get_me(agent: AgentInfo = Depends(verify_agent)):
    """Get current agent info."""
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "description": agent.description,
        "rate_limit": agent.rate_limit,
        "max_authority": agent.max_authority,
        "reputation": agent.reputation,
        "total_reads": agent.total_reads,
        "total_writes": agent.total_writes
    }


# ============================================================================
# Admin Endpoints
# ============================================================================

@app.post("/admin/agents", dependencies=[Depends(verify_admin)])
async def register_agent(req: RegisterAgentRequest):
    """Register a new agent (admin only)."""
    try:
        agent, api_key = agent_store.register(req)
        
        audit_log.log("admin", "register_agent", {"agent_id": req.agent_id})
        
        return {
            "status": "created",
            "agent": agent.model_dump(),
            "api_key": api_key,
            "warning": "Save this API key - it will not be shown again!"
        }
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/admin/agents", dependencies=[Depends(verify_admin)])
async def list_agents():
    """List all registered agents (admin only)."""
    return {
        "agents": [a.model_dump() for a in agent_store.list_agents()]
    }


@app.delete("/admin/agents/{agent_id}", dependencies=[Depends(verify_admin)])
async def revoke_agent(agent_id: str):
    """Revoke agent access (admin only)."""
    if agent_store.revoke(agent_id):
        audit_log.log("admin", "revoke_agent", {"agent_id": agent_id})
        return {"status": "revoked", "agent_id": agent_id}
    raise HTTPException(404, f"Agent {agent_id} not found")


@app.get("/admin/audit", dependencies=[Depends(verify_admin)])
async def get_audit(
    limit: int = Query(default=100, ge=1, le=1000),
    agent_id: Optional[str] = None
):
    """Get audit log (admin only)."""
    return {
        "entries": audit_log.get_recent(limit, agent_id)
    }


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    import argparse
    import uvicorn
    
    parser = argparse.ArgumentParser(description="Origin Memory API Server")
    parser.add_argument("--port", type=int, default=7438, help="Port to run on")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    args = parser.parse_args()
    
    uvicorn.run(app, host=args.host, port=args.port)
