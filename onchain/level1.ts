/**
 * Level 1: Minimal Anchor Client for org-memory-registry
 * 
 * Actually writes to Solana devnet. No simulation.
 */

import {
  Connection,
  PublicKey,
  Keypair,
  Transaction,
  TransactionInstruction,
  sendAndConfirmTransaction,
  SystemProgram,
} from '@solana/web3.js';
import * as borsh from 'borsh';
import { sha256 } from '@noble/hashes/sha256';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

const PROGRAM_ID = new PublicKey('ym712R3CRNG1iTeZ8f5jzNAsoC2FFWNjMDSAoiRuxnt');
const RPC_URL = process.env.SOLANA_RPC_URL || 'https://api.devnet.solana.com';

// Instruction discriminators (first 8 bytes of sha256("global:<instruction_name>"))
const DISCRIMINATORS = {
  initialize_registry: Buffer.from(sha256('global:initialize_registry')).slice(0, 8),
  register_agent: Buffer.from(sha256('global:register_agent')).slice(0, 8),
  write_memory: Buffer.from(sha256('global:write_memory')).slice(0, 8),
  attest_merkle_root: Buffer.from(sha256('global:attest_merkle_root')).slice(0, 8),
};

// PDA derivations
function deriveRegistryPDA(): [PublicKey, number] {
  return PublicKey.findProgramAddressSync([Buffer.from('registry')], PROGRAM_ID);
}

function deriveAgentPDA(agentId: Uint8Array): [PublicKey, number] {
  return PublicKey.findProgramAddressSync([Buffer.from('agent'), Buffer.from(agentId)], PROGRAM_ID);
}

function deriveMemoryPDA(owner: PublicKey, memoryId: Uint8Array): [PublicKey, number] {
  return PublicKey.findProgramAddressSync([Buffer.from('memory'), owner.toBuffer(), Buffer.from(memoryId)], PROGRAM_ID);
}

function deriveAttestationPDA(epoch: bigint): [PublicKey, number] {
  const epochBuf = Buffer.alloc(8);
  epochBuf.writeBigUInt64LE(epoch);
  return PublicKey.findProgramAddressSync([Buffer.from('attestation'), epochBuf], PROGRAM_ID);
}

// Load wallet
function loadWallet(): Keypair {
  const walletPath = process.env.WALLET_PATH || path.join(os.homedir(), '.config', 'solana', 'id.json');
  const walletData = JSON.parse(fs.readFileSync(walletPath, 'utf-8'));
  return Keypair.fromSecretKey(Uint8Array.from(walletData));
}

// ============================================================================
// INSTRUCTIONS
// ============================================================================

async function initializeRegistry(connection: Connection, payer: Keypair): Promise<string> {
  const [registryPda] = deriveRegistryPDA();
  
  // Check if already initialized
  const info = await connection.getAccountInfo(registryPda);
  if (info) {
    console.log('Registry already initialized at:', registryPda.toBase58());
    return 'already_initialized';
  }
  
  // RegistryConfig: 3 pubkeys + u16 + i64 = 96 + 2 + 8 = 106 bytes
  const configData = Buffer.alloc(106);
  let offset = 0;
  // session_escrow_program (32 bytes) - use system program as placeholder
  SystemProgram.programId.toBuffer().copy(configData, offset); offset += 32;
  // staking_program (32 bytes)
  SystemProgram.programId.toBuffer().copy(configData, offset); offset += 32;
  // lead_marketplace_program (32 bytes)
  SystemProgram.programId.toBuffer().copy(configData, offset); offset += 32;
  // max_claims_per_memory (u16)
  configData.writeUInt16LE(100, offset); offset += 2;
  // default_ttl (i64)
  configData.writeBigInt64LE(BigInt(86400 * 365), offset); // 1 year
  
  const data = Buffer.concat([DISCRIMINATORS.initialize_registry, configData]);
  
  const ix = new TransactionInstruction({
    keys: [
      { pubkey: registryPda, isSigner: false, isWritable: true },
      { pubkey: payer.publicKey, isSigner: true, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    programId: PROGRAM_ID,
    data,
  });
  
  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(connection, tx, [payer]);
  console.log('Registry initialized:', sig);
  return sig;
}

async function registerAgent(
  connection: Connection,
  payer: Keypair,
  agentId: Uint8Array,
  role: number,
  sourceType: number
): Promise<string> {
  const [registryPda] = deriveRegistryPDA();
  const [agentPda] = deriveAgentPDA(agentId);
  
  // Check if already registered
  const info = await connection.getAccountInfo(agentPda);
  if (info) {
    console.log('Agent already registered at:', agentPda.toBase58());
    return 'already_registered';
  }
  
  // Data: discriminator + agent_id (16) + role (1) + source_type (1)
  const data = Buffer.alloc(8 + 16 + 1 + 1);
  DISCRIMINATORS.register_agent.copy(data, 0);
  Buffer.from(agentId).copy(data, 8);
  data.writeUInt8(role, 24);
  data.writeUInt8(sourceType, 25);
  
  const ix = new TransactionInstruction({
    keys: [
      { pubkey: registryPda, isSigner: false, isWritable: true },
      { pubkey: agentPda, isSigner: false, isWritable: true },
      { pubkey: payer.publicKey, isSigner: true, isWritable: false }, // agent_signer
      { pubkey: payer.publicKey, isSigner: true, isWritable: true },  // payer
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    programId: PROGRAM_ID,
    data,
  });
  
  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(connection, tx, [payer]);
  console.log('Agent registered:', sig);
  return sig;
}

async function writeMemory(
  connection: Connection,
  payer: Keypair,
  agentId: Uint8Array,
  memoryId: Uint8Array,
  memoryType: number,
  contentHash: Uint8Array,
  privacy: number,
  ttl: bigint | null,
  tags: Uint8Array[],
  rationaleHash: Uint8Array | null,
  confidence: number | null,
  supersedes: Uint8Array | null,
  promotedFrom: Uint8Array | null,
): Promise<string> {
  const [registryPda] = deriveRegistryPDA();
  const [agentPda] = deriveAgentPDA(agentId);
  const [memoryPda] = deriveMemoryPDA(payer.publicKey, memoryId);
  
  // Check if memory exists
  const info = await connection.getAccountInfo(memoryPda);
  if (info) {
    console.log('Memory already exists at:', memoryPda.toBase58());
    return 'already_exists';
  }
  
  // Build instruction data (simplified - just core fields)
  // Format: disc(8) + memory_id(16) + memory_type(1) + content_hash(32) + privacy(1) + ttl_option(1+8) + tags_len(4) + tags(n*32)
  const tagsLen = Math.min(tags.length, 10);
  const dataLen = 8 + 16 + 1 + 32 + 1 + 9 + 4 + (tagsLen * 32) + 33 + 3 + 17 + 17;
  const data = Buffer.alloc(dataLen);
  let offset = 0;
  
  DISCRIMINATORS.write_memory.copy(data, offset); offset += 8;
  Buffer.from(memoryId).copy(data, offset); offset += 16;
  data.writeUInt8(memoryType, offset); offset += 1;
  Buffer.from(contentHash).copy(data, offset); offset += 32;
  data.writeUInt8(privacy, offset); offset += 1;
  
  // TTL as Option<i64>
  if (ttl !== null) {
    data.writeUInt8(1, offset); offset += 1;
    data.writeBigInt64LE(ttl, offset); offset += 8;
  } else {
    data.writeUInt8(0, offset); offset += 1;
    offset += 8;
  }
  
  // Tags as Vec<[u8;32]>
  data.writeUInt32LE(tagsLen, offset); offset += 4;
  for (let i = 0; i < tagsLen; i++) {
    Buffer.from(tags[i]).copy(data, offset); offset += 32;
  }
  
  // Rationale hash as Option<[u8;32]>
  if (rationaleHash) {
    data.writeUInt8(1, offset); offset += 1;
    Buffer.from(rationaleHash).copy(data, offset); offset += 32;
  } else {
    data.writeUInt8(0, offset); offset += 1;
    offset += 32;
  }
  
  // Confidence as Option<u16>
  if (confidence !== null) {
    data.writeUInt8(1, offset); offset += 1;
    data.writeUInt16LE(confidence, offset); offset += 2;
  } else {
    data.writeUInt8(0, offset); offset += 1;
    offset += 2;
  }
  
  // Supersedes as Option<[u8;16]>
  if (supersedes) {
    data.writeUInt8(1, offset); offset += 1;
    Buffer.from(supersedes).copy(data, offset); offset += 16;
  } else {
    data.writeUInt8(0, offset); offset += 1;
    offset += 16;
  }
  
  // Promoted_from as Option<[u8;16]>
  if (promotedFrom) {
    data.writeUInt8(1, offset); offset += 1;
    Buffer.from(promotedFrom).copy(data, offset); offset += 16;
  } else {
    data.writeUInt8(0, offset); offset += 1;
    offset += 16;
  }
  
  const ix = new TransactionInstruction({
    keys: [
      { pubkey: registryPda, isSigner: false, isWritable: true },
      { pubkey: memoryPda, isSigner: false, isWritable: true },
      { pubkey: agentPda, isSigner: false, isWritable: true },
      { pubkey: payer.publicKey, isSigner: false, isWritable: false }, // owner
      { pubkey: payer.publicKey, isSigner: true, isWritable: true },   // payer
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    programId: PROGRAM_ID,
    data: data.slice(0, offset),
  });
  
  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(connection, tx, [payer]);
  return sig;
}

// ============================================================================
// MAIN
// ============================================================================

async function main() {
  const args = process.argv.slice(2);
  const command = args[0];
  
  const connection = new Connection(RPC_URL, 'confirmed');
  const payer = loadWallet();
  
  console.log(`\n⛓️  Level 1 On-Chain Client`);
  console.log(`   Program: ${PROGRAM_ID.toBase58()}`);
  console.log(`   Wallet:  ${payer.publicKey.toBase58()}`);
  
  const balance = await connection.getBalance(payer.publicKey);
  console.log(`   Balance: ${balance / 1e9} SOL\n`);
  
  if (command === 'init') {
    await initializeRegistry(connection, payer);
    
    // Also register the wallet as an admin agent
    const agentId = payer.publicKey.toBytes().slice(0, 16);
    await registerAgent(connection, payer, agentId, 3, 0); // Admin, Human
    
    console.log('\n✅ Registry initialized and agent registered');
    return;
  }
  
  if (command === 'write') {
    const content = args[1] || 'Test memory from Level 1 client';
    const memoryType = parseInt(args[2]) || 0; // Observation
    
    const agentId = payer.publicKey.toBytes().slice(0, 16);
    const memoryId = sha256(Buffer.from(content + Date.now())).slice(0, 16);
    const contentHash = sha256(Buffer.from(content));
    
    console.log(`Writing memory: "${content.slice(0, 50)}..."`);
    
    const sig = await writeMemory(
      connection,
      payer,
      agentId,
      memoryId,
      memoryType,
      contentHash,
      0, // Private
      null, // No TTL
      [], // No tags
      null, // No rationale
      null, // No confidence
      null, // Not superseding
      null, // Not promoted
    );
    
    console.log(`\n✅ Memory written: ${sig}`);
    const [memoryPda] = deriveMemoryPDA(payer.publicKey, memoryId);
    console.log(`   PDA: ${memoryPda.toBase58()}`);
    return;
  }
  
  if (command === 'status') {
    const [registryPda] = deriveRegistryPDA();
    const info = await connection.getAccountInfo(registryPda);
    
    if (!info) {
      console.log('Registry not initialized. Run: npx ts-node level1.ts init');
      return;
    }
    
    // Parse registry data (skip 8 byte discriminator)
    const data = info.data.slice(8);
    const admin = new PublicKey(data.slice(0, 32));
    const schemaVersion = data.readUInt8(32);
    const memoryCount = data.readBigUInt64LE(33);
    const agentCount = data.readUInt32LE(41);
    
    console.log('Registry Status:');
    console.log(`  Admin: ${admin.toBase58()}`);
    console.log(`  Schema: v${schemaVersion}`);
    console.log(`  Memories: ${memoryCount}`);
    console.log(`  Agents: ${agentCount}`);
    return;
  }
  
  console.log(`
Usage:
  npx ts-node level1.ts init              Initialize registry + register agent
  npx ts-node level1.ts write "<text>"    Write a memory to chain
  npx ts-node level1.ts status            Check registry status
`);
}

main().catch(console.error);
