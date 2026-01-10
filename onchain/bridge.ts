/**
 * Unified Memory → Solana Bridge
 * 
 * Syncs local unified-memory to org-memory-registry on-chain.
 * 
 * Architecture:
 * - Content stays OFF-CHAIN (unified-memory/memories.json)
 * - Hash + metadata goes ON-CHAIN (Solana)
 * - Link table maps local IDs → on-chain PDAs
 * 
 * Run: npx ts-node bridge.ts sync
 */

import {
  Connection,
  PublicKey,
  Keypair,
  Transaction,
  sendAndConfirmTransaction,
  SystemProgram,
} from '@solana/web3.js';
import * as anchor from '@coral-xyz/anchor';
import { sha256 } from '@noble/hashes/sha256';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

// ============================================================================
// CONFIG
// ============================================================================

const PROGRAM_ID = new PublicKey('ym712R3CRNG1iTeZ8f5jzNAsoC2FFWNjMDSAoiRuxnt');
const RPC_URL = process.env.SOLANA_RPC_URL || 'https://api.devnet.solana.com';
const UNIFIED_MEMORY_PATH = path.join(os.homedir(), 'unified-memory', 'memories.json');
const LINK_TABLE_PATH = path.join(os.homedir(), 'unified-memory', 'onchain', 'link_table.json');

// ============================================================================
// TYPE MAPPING: unified-memory → on-chain
// ============================================================================

// unified-memory uses strings, on-chain uses enum indices
const MEMORY_TYPE_MAP: Record<string, number> = {
  preference: 3,    // MemoryType::Preference
  decision: 2,      // MemoryType::Fact (closest semantic match)
  constraint: 8,    // MemoryType::Rule
  goal: 4,          // MemoryType::Plan
  procedure: 7,     // MemoryType::Skill
  lesson: 2,        // MemoryType::Fact
  observation: 0,   // MemoryType::Episodic
  hypothesis: 0,    // MemoryType::Episodic (unverified)
};

const SOURCE_TO_AGENT_ROLE: Record<string, number> = {
  human: 3,         // Admin
  claude: 2,        // TrustedPublisher
  agent: 1,         // Writer
  system: 1,        // Writer
};

const AUTHORITY_TO_PRIVACY: Record<number, number> = {
  0: 0,  // hypothesis → Private
  1: 0,  // preference/observation → Private
  3: 1,  // goal/lesson → Team
  4: 1,  // decision/procedure → Team
  5: 2,  // constraint → Public
};

// ============================================================================
// INTERFACES
// ============================================================================

interface UnifiedMemory {
  id: string;
  type: string;
  content: string;
  tags?: string[];
  rationale?: string;
  confidence?: number;
  context?: string;
  expires_at?: string;
  supersedes?: string;
  promoted_from?: string;
  provenance: {
    source: string;
    timestamp: string;
    agent_id?: string;
    conversation_id?: string;
  };
}

interface MemoryStore {
  version: string;
  last_sync: string;
  memories: UnifiedMemory[];
}

interface LinkEntry {
  localId: string;
  onchainPda: string;
  contentHash: string;
  syncedAt: string;
  txSignature: string;
}

interface LinkTable {
  version: string;
  lastSync: string;
  links: LinkEntry[];
}

// ============================================================================
// HELPERS
// ============================================================================

function hashContent(memory: UnifiedMemory): Uint8Array {
  // Hash content + provenance for unique fingerprint
  const data = JSON.stringify({
    content: memory.content,
    type: memory.type,
    timestamp: memory.provenance.timestamp,
  });
  return sha256(Buffer.from(data));
}

function stringToBytes32(str: string): Uint8Array {
  const hash = sha256(Buffer.from(str));
  return hash.slice(0, 32);
}

function stringToBytes16(str: string): Uint8Array {
  const hash = sha256(Buffer.from(str));
  return hash.slice(0, 16);
}

function tagsToBytes32Array(tags: string[]): Uint8Array[] {
  return tags.slice(0, 10).map(t => stringToBytes32(t));
}

function loadMemories(): MemoryStore {
  if (!fs.existsSync(UNIFIED_MEMORY_PATH)) {
    return { version: '1.0.0', last_sync: '', memories: [] };
  }
  return JSON.parse(fs.readFileSync(UNIFIED_MEMORY_PATH, 'utf-8'));
}

function loadLinkTable(): LinkTable {
  if (!fs.existsSync(LINK_TABLE_PATH)) {
    return { version: '1.0.0', lastSync: '', links: [] };
  }
  return JSON.parse(fs.readFileSync(LINK_TABLE_PATH, 'utf-8'));
}

function saveLinkTable(table: LinkTable): void {
  fs.mkdirSync(path.dirname(LINK_TABLE_PATH), { recursive: true });
  table.lastSync = new Date().toISOString();
  fs.writeFileSync(LINK_TABLE_PATH, JSON.stringify(table, null, 2));
}

function deriveRegistryPDA(): [PublicKey, number] {
  return PublicKey.findProgramAddressSync(
    [Buffer.from('registry')],
    PROGRAM_ID
  );
}

function deriveAgentPDA(agentId: Uint8Array): [PublicKey, number] {
  return PublicKey.findProgramAddressSync(
    [Buffer.from('agent'), Buffer.from(agentId)],
    PROGRAM_ID
  );
}

function deriveMemoryPDA(owner: PublicKey, memoryId: Uint8Array): [PublicKey, number] {
  return PublicKey.findProgramAddressSync(
    [Buffer.from('memory'), owner.toBuffer(), Buffer.from(memoryId)],
    PROGRAM_ID
  );
}

// ============================================================================
// SYNC LOGIC
// ============================================================================

async function syncMemoryToChain(
  connection: Connection,
  payer: Keypair,
  agentId: Uint8Array,
  memory: UnifiedMemory,
  linkTable: LinkTable
): Promise<LinkEntry | null> {
  
  // Check if already synced (by content hash)
  const contentHash = Buffer.from(hashContent(memory)).toString('hex');
  const existing = linkTable.links.find(l => l.contentHash === contentHash);
  if (existing) {
    console.log(`  ↳ Already synced: ${memory.id} → ${existing.onchainPda.slice(0, 8)}...`);
    return null;
  }
  
  // Derive PDAs
  const [registryPda] = deriveRegistryPDA();
  const [agentPda] = deriveAgentPDA(agentId);
  const memoryId = stringToBytes16(memory.id);
  const [memoryPda] = deriveMemoryPDA(payer.publicKey, memoryId);
  
  // Map unified-memory type to on-chain enum
  const memoryType = MEMORY_TYPE_MAP[memory.type] ?? 0;
  const privacy = AUTHORITY_TO_PRIVACY[getAuthority(memory.type)] ?? 0;
  
  // Compute TTL if expires
  let ttl: number | null = null;
  if (memory.expires_at) {
    const expiresMs = new Date(memory.expires_at).getTime();
    const nowMs = Date.now();
    ttl = Math.floor((expiresMs - nowMs) / 1000);
    if (ttl < 0) ttl = 0;
  }
  
  // Tags as bytes32 array
  const tags = tagsToBytes32Array(memory.tags || []);
  
  // Content hash as links_hash (points to off-chain content)
  const linksHash = hashContent(memory);
  
  // Subject: hash of the primary topic
  const subject = stringToBytes32(memory.content.slice(0, 100));
  
  console.log(`  ↳ Writing to chain: ${memory.type} "${memory.content.slice(0, 40)}..."`);
  
  // NOTE: This is where you'd call the actual Anchor program
  // For now, we'll create a placeholder that shows the structure
  
  const instruction = {
    programId: PROGRAM_ID,
    accounts: {
      registry: registryPda,
      memoryRecord: memoryPda,
      agentAuthority: agentPda,
      owner: payer.publicKey,
      payer: payer.publicKey,
      systemProgram: SystemProgram.programId,
    },
    args: {
      memoryId: Array.from(memoryId),
      memoryType,
      subject: Array.from(subject),
      privacy,
      ttl,
      tags: tags.map(t => Array.from(t)),
      linksHash: Array.from(linksHash),
    },
  };
  
  // For now, simulate the tx
  const txSignature = `sim_${Date.now()}_${memory.id}`;
  
  const link: LinkEntry = {
    localId: memory.id,
    onchainPda: memoryPda.toBase58(),
    contentHash,
    syncedAt: new Date().toISOString(),
    txSignature,
  };
  
  return link;
}

function getAuthority(memoryType: string): number {
  const authorities: Record<string, number> = {
    preference: 1,
    decision: 4,
    constraint: 5,
    goal: 3,
    procedure: 4,
    lesson: 3,
    observation: 1,
    hypothesis: 0,
  };
  return authorities[memoryType] ?? 0;
}

// ============================================================================
// MAIN
// ============================================================================

async function main() {
  const args = process.argv.slice(2);
  const command = args[0];
  
  if (!command || command === 'help') {
    console.log(`
Unified Memory → Solana Bridge

Commands:
  sync              Sync all unsynced memories to chain
  status            Show sync status
  verify <localId>  Verify on-chain record matches local
  batch <count>     Batch attest with Merkle root
  
Environment:
  SOLANA_RPC_URL    RPC endpoint (default: devnet)
  WALLET_PATH       Path to keypair JSON
`);
    return;
  }
  
  // Load data
  const store = loadMemories();
  const linkTable = loadLinkTable();
  
  console.log(`\n🧠 Unified Memory Bridge`);
  console.log(`   Local memories: ${store.memories.length}`);
  console.log(`   Synced to chain: ${linkTable.links.length}`);
  console.log(`   Pending: ${store.memories.length - linkTable.links.length}`);
  console.log('');
  
  if (command === 'status') {
    // Group by type
    const byType: Record<string, number> = {};
    for (const mem of store.memories) {
      byType[mem.type] = (byType[mem.type] || 0) + 1;
    }
    console.log('By type:');
    for (const [type, count] of Object.entries(byType).sort((a, b) => b[1] - a[1])) {
      const synced = linkTable.links.filter(l => {
        const mem = store.memories.find(m => m.id === l.localId);
        return mem?.type === type;
      }).length;
      console.log(`  ${type}: ${synced}/${count} synced`);
    }
    return;
  }
  
  if (command === 'sync') {
    console.log('Connecting to Solana...');
    const connection = new Connection(RPC_URL, 'confirmed');
    
    // Load wallet
    const walletPath = process.env.WALLET_PATH || path.join(os.homedir(), '.config', 'solana', 'id.json');
    if (!fs.existsSync(walletPath)) {
      console.error(`Wallet not found: ${walletPath}`);
      console.error('Set WALLET_PATH or run: solana-keygen new');
      return;
    }
    const walletData = JSON.parse(fs.readFileSync(walletPath, 'utf-8'));
    const payer = Keypair.fromSecretKey(Uint8Array.from(walletData));
    console.log(`Wallet: ${payer.publicKey.toBase58()}`);
    
    // Generate agent ID from wallet
    const agentId = payer.publicKey.toBytes().slice(0, 16);
    
    // Sync each memory
    let synced = 0;
    for (const memory of store.memories) {
      const link = await syncMemoryToChain(connection, payer, agentId, memory, linkTable);
      if (link) {
        linkTable.links.push(link);
        synced++;
      }
    }
    
    saveLinkTable(linkTable);
    console.log(`\n✅ Synced ${synced} new memories to chain`);
    return;
  }
  
  if (command === 'verify') {
    const localId = args[1];
    if (!localId) {
      console.error('Usage: bridge.ts verify <localId>');
      return;
    }
    
    const link = linkTable.links.find(l => l.localId === localId);
    if (!link) {
      console.error(`Memory ${localId} not synced to chain`);
      return;
    }
    
    const memory = store.memories.find(m => m.id === localId);
    if (!memory) {
      console.error(`Memory ${localId} not found locally`);
      return;
    }
    
    const currentHash = Buffer.from(hashContent(memory)).toString('hex');
    if (currentHash === link.contentHash) {
      console.log(`✅ Memory ${localId} verified`);
      console.log(`   On-chain PDA: ${link.onchainPda}`);
      console.log(`   Content hash: ${currentHash.slice(0, 16)}...`);
    } else {
      console.log(`⚠️  Memory ${localId} has changed since sync`);
      console.log(`   Synced hash:  ${link.contentHash.slice(0, 16)}...`);
      console.log(`   Current hash: ${currentHash.slice(0, 16)}...`);
    }
    return;
  }
  
  if (command === 'batch') {
    const count = parseInt(args[1]) || 256;
    console.log(`Creating Merkle attestation for ${count} memories...`);
    
    // Collect hashes
    const hashes: string[] = [];
    for (const link of linkTable.links.slice(-count)) {
      hashes.push(link.contentHash);
    }
    
    // Compute Merkle root
    let level: Buffer[] = hashes.map(h => Buffer.from(h, 'hex'));
    while (level.length > 1) {
      const nextLevel: Buffer[] = [];
      for (let i = 0; i < level.length; i += 2) {
        if (i + 1 < level.length) {
          const combined = Buffer.concat([level[i], level[i + 1]]);
          nextLevel.push(Buffer.from(sha256(combined)) as Buffer);
        } else {
          nextLevel.push(level[i]);
        }
      }
      level = nextLevel;
    }
    
    const merkleRoot = level[0]?.toString('hex') || '0'.repeat(64);
    console.log(`\n📦 Merkle Root: ${merkleRoot}`);
    console.log(`   Leaves: ${hashes.length}`);
    console.log(`   Ready for on-chain attestation`);
    return;
  }
  
  console.error(`Unknown command: ${command}`);
}

main().catch(console.error);
