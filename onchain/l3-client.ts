/**
 * Origin Memory - Layer 3 Client SDK
 * 
 * Trustless memory queries via Solana on-chain verification.
 * Any agent can verify memory authenticity without trusting a central server.
 * 
 * Usage:
 *   const client = new MemoryL3Client({ rpcUrl: 'https://api.devnet.solana.com' });
 *   const memory = await client.getMemory(memoryId, ownerPubkey);
 *   const isValid = await client.verifyContent(memory, actualContent);
 */

import {
  Connection,
  PublicKey,
  Keypair,
  Transaction,
  TransactionInstruction,
  SystemProgram,
} from '@solana/web3.js';
import * as anchor from '@coral-xyz/anchor';
import { createHash } from 'crypto';
import fetch from 'node-fetch';

// ============================================================================
// Constants
// ============================================================================

export const PROGRAM_ID = new PublicKey('ym712R3CRNG1iTeZ8f5jzNAsoC2FFWNjMDSAoiRuxnt');

export const MEMORY_TYPES = {
  observation: { authority: 1, index: 0 },
  preference: { authority: 1, index: 1 },
  hypothesis: { authority: 0, index: 2 },
  lesson: { authority: 3, index: 3 },
  goal: { authority: 3, index: 4 },
  procedure: { authority: 4, index: 5 },
  decision: { authority: 4, index: 6 },
  constraint: { authority: 5, index: 7 },
} as const;

export type MemoryType = keyof typeof MEMORY_TYPES;

// ============================================================================
// Types
// ============================================================================

export interface OnChainMemory {
  memoryId: string;
  owner: string;
  agent: string;
  memoryType: MemoryType;
  authorityLevel: number;
  contentHash: string;
  createdAt: Date;
  isActive: boolean;
  confidence?: number;
  tags: string[];
}

export interface VerificationResult {
  valid: boolean;
  hashMatches: boolean;
  isActive: boolean;
  notExpired: boolean;
  onChainHash: string;
  computedHash: string;
  memory?: OnChainMemory;
}

export interface AgentInfo {
  agentId: string;
  pubkey: string;
  role: 'observer' | 'writer' | 'trusted_publisher' | 'admin';
  sourceType: 'human' | 'claude' | 'agent' | 'system';
  trustScore: number;
  memoryCount: number;
  createdAt: Date;
  lastActive: Date;
}

export interface RegistryInfo {
  admin: string;
  schemaVersion: number;
  memoryCount: number;
  agentCount: number;
  merkleRoot: string;
  lastAttestation: Date;
}

export interface L3ClientConfig {
  rpcUrl?: string;
  programId?: PublicKey;
  ipfsGateway?: string;
  httpApiUrl?: string;
}

// ============================================================================
// SDK Client
// ============================================================================

export class MemoryL3Client {
  private connection: Connection;
  private programId: PublicKey;
  private ipfsGateway: string;
  private httpApiUrl: string;

  constructor(config: L3ClientConfig = {}) {
    this.connection = new Connection(
      config.rpcUrl || 'https://api.devnet.solana.com',
      'confirmed'
    );
    this.programId = config.programId || PROGRAM_ID;
    this.ipfsGateway = config.ipfsGateway || 'https://ipfs.io/ipfs/';
    this.httpApiUrl = config.httpApiUrl || 'http://localhost:7438';
  }

  // --------------------------------------------------------------------------
  // PDA Derivation
  // --------------------------------------------------------------------------

  getRegistryPda(): [PublicKey, number] {
    return PublicKey.findProgramAddressSync(
      [Buffer.from('registry')],
      this.programId
    );
  }

  getAgentPda(agentId: Uint8Array | string): [PublicKey, number] {
    const idBytes = typeof agentId === 'string' 
      ? this.hexToBytes(agentId) 
      : agentId;
    return PublicKey.findProgramAddressSync(
      [Buffer.from('agent'), Buffer.from(idBytes)],
      this.programId
    );
  }

  getMemoryPda(owner: PublicKey, memoryId: Uint8Array | string): [PublicKey, number] {
    const idBytes = typeof memoryId === 'string' 
      ? this.hexToBytes(memoryId) 
      : memoryId;
    return PublicKey.findProgramAddressSync(
      [Buffer.from('memory'), owner.toBuffer(), Buffer.from(idBytes)],
      this.programId
    );
  }

  // --------------------------------------------------------------------------
  // Read Operations (Trustless)
  // --------------------------------------------------------------------------

  /**
   * Get registry info from on-chain
   */
  async getRegistry(): Promise<RegistryInfo | null> {
    const [pda] = this.getRegistryPda();
    const accountInfo = await this.connection.getAccountInfo(pda);
    
    if (!accountInfo) return null;
    
    return this.parseRegistryAccount(accountInfo.data);
  }

  /**
   * Get agent info from on-chain
   */
  async getAgent(agentId: string): Promise<AgentInfo | null> {
    const [pda] = this.getAgentPda(agentId);
    const accountInfo = await this.connection.getAccountInfo(pda);
    
    if (!accountInfo) return null;
    
    return this.parseAgentAccount(accountInfo.data);
  }

  /**
   * Get memory record from on-chain
   */
  async getMemory(memoryId: string, owner: PublicKey | string): Promise<OnChainMemory | null> {
    const ownerPubkey = typeof owner === 'string' ? new PublicKey(owner) : owner;
    const [pda] = this.getMemoryPda(ownerPubkey, memoryId);
    const accountInfo = await this.connection.getAccountInfo(pda);
    
    if (!accountInfo) return null;
    
    return this.parseMemoryAccount(accountInfo.data);
  }

  /**
   * Verify content against on-chain hash (trustless verification)
   */
  async verifyContent(
    memoryId: string,
    owner: PublicKey | string,
    content: string
  ): Promise<VerificationResult> {
    const memory = await this.getMemory(memoryId, owner);
    const computedHash = this.hashContent(content);
    
    if (!memory) {
      return {
        valid: false,
        hashMatches: false,
        isActive: false,
        notExpired: true,
        onChainHash: '',
        computedHash,
      };
    }

    const hashMatches = memory.contentHash === computedHash;
    
    return {
      valid: hashMatches && memory.isActive,
      hashMatches,
      isActive: memory.isActive,
      notExpired: true, // TODO: Check TTL
      onChainHash: memory.contentHash,
      computedHash,
      memory,
    };
  }

  /**
   * Batch verify multiple memories
   */
  async verifyBatch(
    items: Array<{ memoryId: string; owner: string; content: string }>
  ): Promise<Map<string, VerificationResult>> {
    const results = new Map<string, VerificationResult>();
    
    // Parallel verification
    await Promise.all(
      items.map(async (item) => {
        const result = await this.verifyContent(item.memoryId, item.owner, item.content);
        results.set(item.memoryId, result);
      })
    );
    
    return results;
  }

  /**
   * Get all memories for an owner (via getProgramAccounts)
   */
  async getMemoriesByOwner(owner: PublicKey | string): Promise<OnChainMemory[]> {
    const ownerPubkey = typeof owner === 'string' ? new PublicKey(owner) : owner;
    
    const accounts = await this.connection.getProgramAccounts(this.programId, {
      filters: [
        { dataSize: 300 }, // Approximate memory record size
        {
          memcmp: {
            offset: 8 + 16 + 1, // After discriminator, memory_id, schema_version
            bytes: ownerPubkey.toBase58(),
          },
        },
      ],
    });
    
    return accounts
      .map((a) => this.parseMemoryAccount(a.account.data))
      .filter((m): m is OnChainMemory => m !== null);
  }

  /**
   * Get memories by type (via getProgramAccounts)
   */
  async getMemoriesByType(memoryType: MemoryType): Promise<OnChainMemory[]> {
    const typeIndex = MEMORY_TYPES[memoryType].index;
    
    const accounts = await this.connection.getProgramAccounts(this.programId, {
      filters: [
        { dataSize: 300 },
        {
          memcmp: {
            offset: 8 + 16 + 1 + 32 + 32 + 8, // After memory_type offset
            bytes: Buffer.from([typeIndex]).toString('base64'),
          },
        },
      ],
    });
    
    return accounts
      .map((a) => this.parseMemoryAccount(a.account.data))
      .filter((m): m is OnChainMemory => m !== null && m.isActive);
  }

  // --------------------------------------------------------------------------
  // IPFS Integration
  // --------------------------------------------------------------------------

  /**
   * Fetch content from IPFS by CID
   */
  async fetchFromIpfs(cid: string): Promise<string> {
    const response = await fetch(`${this.ipfsGateway}${cid}`);
    if (!response.ok) {
      throw new Error(`IPFS fetch failed: ${response.status}`);
    }
    return response.text();
  }

  /**
   * Verify IPFS content against on-chain hash
   */
  async verifyIpfsContent(
    memoryId: string,
    owner: PublicKey | string,
    ipfsCid: string
  ): Promise<VerificationResult> {
    const content = await this.fetchFromIpfs(ipfsCid);
    return this.verifyContent(memoryId, owner, content);
  }

  // --------------------------------------------------------------------------
  // HTTP API Fallback (Layer 2)
  // --------------------------------------------------------------------------

  /**
   * Search via HTTP API (faster, but requires trust)
   */
  async searchViaApi(
    query: string,
    apiKey: string,
    options: { topK?: number; memoryType?: MemoryType } = {}
  ): Promise<any[]> {
    const response = await fetch(`${this.httpApiUrl}/v1/search`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        query,
        top_k: options.topK || 5,
        memory_type: options.memoryType,
      }),
    });
    
    if (!response.ok) {
      throw new Error(`API search failed: ${response.status}`);
    }
    
    const data = await response.json();
    return data.results || [];
  }

  /**
   * Search via API then verify on-chain (trust but verify)
   */
  async searchAndVerify(
    query: string,
    apiKey: string,
    owner: PublicKey | string
  ): Promise<Array<{ result: any; verification: VerificationResult }>> {
    const results = await this.searchViaApi(query, apiKey);
    
    const verified = await Promise.all(
      results.map(async (result) => {
        const verification = await this.verifyContent(
          result.id,
          owner,
          result.content
        );
        return { result, verification };
      })
    );
    
    return verified;
  }

  // --------------------------------------------------------------------------
  // Utility Methods
  // --------------------------------------------------------------------------

  /**
   * Compute SHA-256 hash of content (matches on-chain hashing)
   */
  hashContent(content: string): string {
    return createHash('sha256').update(content).digest('hex');
  }

  /**
   * Convert hex string to bytes
   */
  private hexToBytes(hex: string): Uint8Array {
    const cleanHex = hex.startsWith('0x') ? hex.slice(2) : hex;
    const bytes = new Uint8Array(cleanHex.length / 2);
    for (let i = 0; i < bytes.length; i++) {
      bytes[i] = parseInt(cleanHex.substr(i * 2, 2), 16);
    }
    return bytes;
  }

  /**
   * Convert bytes to hex string
   */
  private bytesToHex(bytes: Uint8Array): string {
    return Array.from(bytes)
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  }

  // --------------------------------------------------------------------------
  // Account Parsing
  // --------------------------------------------------------------------------

  private parseRegistryAccount(data: Buffer): RegistryInfo | null {
    try {
      // Skip 8-byte discriminator
      let offset = 8;
      
      const admin = new PublicKey(data.slice(offset, offset + 32)).toBase58();
      offset += 32;
      
      const schemaVersion = data[offset];
      offset += 1;
      
      const memoryCount = data.readBigUInt64LE(offset);
      offset += 8;
      
      const agentCount = data.readUInt32LE(offset);
      offset += 4;
      
      const merkleRoot = this.bytesToHex(data.slice(offset, offset + 32));
      offset += 32;
      
      const lastAttestationTs = Number(data.readBigInt64LE(offset));
      
      return {
        admin,
        schemaVersion,
        memoryCount: Number(memoryCount),
        agentCount,
        merkleRoot,
        lastAttestation: new Date(lastAttestationTs * 1000),
      };
    } catch {
      return null;
    }
  }

  private parseAgentAccount(data: Buffer): AgentInfo | null {
    try {
      let offset = 8; // Skip discriminator
      
      const agentId = this.bytesToHex(data.slice(offset, offset + 16));
      offset += 16;
      
      const pubkey = new PublicKey(data.slice(offset, offset + 32)).toBase58();
      offset += 32;
      
      const roleIndex = data[offset];
      const roles = ['observer', 'writer', 'trusted_publisher', 'admin'] as const;
      offset += 1;
      
      const sourceIndex = data[offset];
      const sources = ['human', 'claude', 'agent', 'system'] as const;
      offset += 1;
      
      const trustScore = Number(data.readBigUInt64LE(offset));
      offset += 8;
      
      const memoryCount = Number(data.readBigUInt64LE(offset));
      offset += 8;
      
      const createdAtTs = Number(data.readBigInt64LE(offset));
      offset += 8;
      
      const lastActiveTs = Number(data.readBigInt64LE(offset));
      
      return {
        agentId,
        pubkey,
        role: roles[roleIndex] || 'observer',
        sourceType: sources[sourceIndex] || 'agent',
        trustScore,
        memoryCount,
        createdAt: new Date(createdAtTs * 1000),
        lastActive: new Date(lastActiveTs * 1000),
      };
    } catch {
      return null;
    }
  }

  private parseMemoryAccount(data: Buffer): OnChainMemory | null {
    try {
      let offset = 8; // Skip discriminator
      
      const memoryId = this.bytesToHex(data.slice(offset, offset + 16));
      offset += 16;
      
      const schemaVersion = data[offset];
      offset += 1;
      
      const owner = new PublicKey(data.slice(offset, offset + 32)).toBase58();
      offset += 32;
      
      const agent = new PublicKey(data.slice(offset, offset + 32)).toBase58();
      offset += 32;
      
      const createdAtTs = Number(data.readBigInt64LE(offset));
      offset += 8;
      
      const typeIndex = data[offset];
      const types: MemoryType[] = [
        'observation', 'preference', 'hypothesis', 'lesson',
        'goal', 'procedure', 'decision', 'constraint'
      ];
      offset += 1;
      
      const authorityLevel = data[offset];
      offset += 1;
      
      const contentHash = this.bytesToHex(data.slice(offset, offset + 32));
      offset += 32;
      
      // Skip privacy (1) + ttl option (9) + tags vec header (4)
      offset += 1 + 9 + 4;
      
      // For now, skip complex parsing of tags, rationale, etc.
      
      // Jump to is_active near end
      const isActive = data[data.length - 2] === 1;
      
      return {
        memoryId,
        owner,
        agent,
        memoryType: types[typeIndex] || 'observation',
        authorityLevel,
        contentHash,
        createdAt: new Date(createdAtTs * 1000),
        isActive,
        tags: [],
      };
    } catch {
      return null;
    }
  }
}

// ============================================================================
// Convenience Functions
// ============================================================================

/**
 * Create a client for devnet
 */
export function createDevnetClient(): MemoryL3Client {
  return new MemoryL3Client({
    rpcUrl: 'https://api.devnet.solana.com',
  });
}

/**
 * Create a client for mainnet
 */
export function createMainnetClient(): MemoryL3Client {
  return new MemoryL3Client({
    rpcUrl: 'https://api.mainnet-beta.solana.com',
  });
}

/**
 * Quick verification helper
 */
export async function quickVerify(
  memoryId: string,
  owner: string,
  content: string,
  network: 'devnet' | 'mainnet' = 'devnet'
): Promise<boolean> {
  const client = network === 'devnet' ? createDevnetClient() : createMainnetClient();
  const result = await client.verifyContent(memoryId, owner, content);
  return result.valid;
}

export default MemoryL3Client;
