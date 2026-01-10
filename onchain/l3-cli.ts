#!/usr/bin/env npx ts-node
/**
 * Origin Memory Layer 3 CLI
 * 
 * Trustless memory verification via Solana.
 * 
 * Usage:
 *   npx ts-node l3-cli.ts verify <memory-id> <owner> <content>
 *   npx ts-node l3-cli.ts get-memory <memory-id> <owner>
 *   npx ts-node l3-cli.ts get-agent <agent-id>
 *   npx ts-node l3-cli.ts registry
 *   npx ts-node l3-cli.ts search-verify <query> <api-key> <owner>
 */

import { MemoryL3Client, createDevnetClient, MEMORY_TYPES } from './l3-client';
import * as fs from 'fs';
import * as path from 'path';

const MEMORIES_PATH = path.join(process.env.HOME || '', 'unified-memory', 'memories.json');

async function main() {
  const args = process.argv.slice(2);
  const command = args[0];
  
  const client = createDevnetClient();
  
  switch (command) {
    case 'verify': {
      const [, memoryId, owner, content] = args;
      if (!memoryId || !owner || !content) {
        console.error('Usage: verify <memory-id> <owner-pubkey> <content>');
        process.exit(1);
      }
      
      console.log('🔍 Verifying memory on-chain...\n');
      const result = await client.verifyContent(memoryId, owner, content);
      
      console.log('Verification Result:');
      console.log('━'.repeat(50));
      console.log(`  Valid:        ${result.valid ? '✅ YES' : '❌ NO'}`);
      console.log(`  Hash Match:   ${result.hashMatches ? '✅' : '❌'}`);
      console.log(`  Is Active:    ${result.isActive ? '✅' : '❌'}`);
      console.log(`  Not Expired:  ${result.notExpired ? '✅' : '❌'}`);
      console.log('');
      console.log(`  On-chain Hash:  ${result.onChainHash || 'N/A'}`);
      console.log(`  Computed Hash:  ${result.computedHash}`);
      
      if (result.memory) {
        console.log('');
        console.log('Memory Details:');
        console.log(`  Type:      ${result.memory.memoryType}`);
        console.log(`  Authority: ${result.memory.authorityLevel}`);
        console.log(`  Created:   ${result.memory.createdAt.toISOString()}`);
      }
      break;
    }
    
    case 'verify-local': {
      const [, memoryId, owner] = args;
      if (!memoryId) {
        console.error('Usage: verify-local <memory-id> [owner-pubkey]');
        process.exit(1);
      }
      
      // Load from local memories.json
      const memoriesData = JSON.parse(fs.readFileSync(MEMORIES_PATH, 'utf-8'));
      const memory = memoriesData.memories.find((m: any) => m.id === memoryId);
      
      if (!memory) {
        console.error(`Memory ${memoryId} not found in local store`);
        process.exit(1);
      }
      
      console.log('📋 Local Memory:');
      console.log(`  ID:      ${memory.id}`);
      console.log(`  Type:    ${memory.type}`);
      console.log(`  Content: ${memory.content.substring(0, 80)}...`);
      console.log('');
      
      if (owner) {
        console.log('🔍 Verifying against on-chain...\n');
        const result = await client.verifyContent(memoryId, owner, memory.content);
        console.log(`  Valid: ${result.valid ? '✅ YES' : '❌ NO'}`);
        console.log(`  Hash Match: ${result.hashMatches ? '✅' : '❌'}`);
      } else {
        console.log('ℹ️  Provide owner pubkey to verify on-chain');
        console.log(`  Content Hash: ${client.hashContent(memory.content)}`);
      }
      break;
    }
    
    case 'get-memory': {
      const [, memoryId, owner] = args;
      if (!memoryId || !owner) {
        console.error('Usage: get-memory <memory-id> <owner-pubkey>');
        process.exit(1);
      }
      
      console.log('🔍 Fetching memory from Solana...\n');
      const memory = await client.getMemory(memoryId, owner);
      
      if (!memory) {
        console.log('❌ Memory not found on-chain');
        process.exit(1);
      }
      
      console.log('Memory Record:');
      console.log('━'.repeat(50));
      console.log(`  ID:           ${memory.memoryId}`);
      console.log(`  Type:         ${memory.memoryType}`);
      console.log(`  Authority:    ${memory.authorityLevel}`);
      console.log(`  Owner:        ${memory.owner}`);
      console.log(`  Agent:        ${memory.agent}`);
      console.log(`  Content Hash: ${memory.contentHash}`);
      console.log(`  Created:      ${memory.createdAt.toISOString()}`);
      console.log(`  Active:       ${memory.isActive ? '✅' : '❌'}`);
      break;
    }
    
    case 'get-agent': {
      const [, agentId] = args;
      if (!agentId) {
        console.error('Usage: get-agent <agent-id-hex>');
        process.exit(1);
      }
      
      console.log('🔍 Fetching agent from Solana...\n');
      const agent = await client.getAgent(agentId);
      
      if (!agent) {
        console.log('❌ Agent not found on-chain');
        process.exit(1);
      }
      
      console.log('Agent Record:');
      console.log('━'.repeat(50));
      console.log(`  ID:           ${agent.agentId}`);
      console.log(`  Pubkey:       ${agent.pubkey}`);
      console.log(`  Role:         ${agent.role}`);
      console.log(`  Source:       ${agent.sourceType}`);
      console.log(`  Trust Score:  ${agent.trustScore}`);
      console.log(`  Memories:     ${agent.memoryCount}`);
      console.log(`  Created:      ${agent.createdAt.toISOString()}`);
      console.log(`  Last Active:  ${agent.lastActive.toISOString()}`);
      break;
    }
    
    case 'registry': {
      console.log('🔍 Fetching registry from Solana...\n');
      const registry = await client.getRegistry();
      
      if (!registry) {
        console.log('❌ Registry not found');
        process.exit(1);
      }
      
      console.log('Memory Registry:');
      console.log('━'.repeat(50));
      console.log(`  Admin:            ${registry.admin}`);
      console.log(`  Schema Version:   ${registry.schemaVersion}`);
      console.log(`  Memory Count:     ${registry.memoryCount}`);
      console.log(`  Agent Count:      ${registry.agentCount}`);
      console.log(`  Merkle Root:      ${registry.merkleRoot.substring(0, 16)}...`);
      console.log(`  Last Attestation: ${registry.lastAttestation.toISOString()}`);
      break;
    }
    
    case 'hash': {
      const [, content] = args;
      if (!content) {
        console.error('Usage: hash <content>');
        process.exit(1);
      }
      
      const hash = client.hashContent(content);
      console.log(`Content Hash: ${hash}`);
      break;
    }
    
    case 'types': {
      console.log('Memory Types (by authority):');
      console.log('━'.repeat(50));
      
      const sorted = Object.entries(MEMORY_TYPES)
        .sort((a, b) => b[1].authority - a[1].authority);
      
      for (const [type, meta] of sorted) {
        console.log(`  ${type.padEnd(12)} Authority: ${meta.authority}`);
      }
      break;
    }
    
    default:
      console.log('Origin Memory Layer 3 CLI');
      console.log('━'.repeat(50));
      console.log('');
      console.log('Commands:');
      console.log('  verify <id> <owner> <content>  Verify memory on-chain');
      console.log('  verify-local <id> [owner]      Verify local memory');
      console.log('  get-memory <id> <owner>        Get memory from chain');
      console.log('  get-agent <agent-id>           Get agent from chain');
      console.log('  registry                       Get registry info');
      console.log('  hash <content>                 Compute content hash');
      console.log('  types                          List memory types');
      console.log('');
      console.log('Network: Solana Devnet');
      console.log('Program: ym712R3CRNG1iTeZ8f5jzNAsoC2FFWNjMDSAoiRuxnt');
  }
}

main().catch((err) => {
  console.error('Error:', err.message);
  process.exit(1);
});
