#!/usr/bin/env node
/**
 * miniclaw WhatsApp Bridge
 *
 * This bridge connects WhatsApp Web to miniclaw's Python backend
 * via WebSocket. It handles authentication, message forwarding,
 * and reconnection logic.
 *
 * Usage:
 *   npm run build && npm start
 *
 * Or with custom settings:
 *   BRIDGE_HOST=127.0.0.1 BRIDGE_PORT=3001 BRIDGE_AUTH_TOKEN=... AUTH_DIR=~/.miniclaw/whatsapp npm start
 */

// Polyfill crypto for Baileys in ESM
import { webcrypto } from 'crypto';
if (!globalThis.crypto) {
  (globalThis as any).crypto = webcrypto;
}

import { BridgeServer } from './server.js';
import { homedir } from 'os';
import { join } from 'path';

const PORT = parseInt(process.env.BRIDGE_PORT || '3001', 10);
const HOST = process.env.BRIDGE_HOST || '127.0.0.1';
const AUTH_DIR = process.env.AUTH_DIR || join(homedir(), '.miniclaw', 'whatsapp-auth');
const AUTH_TOKEN = (process.env.BRIDGE_AUTH_TOKEN || '').trim();
const IS_DEV = (process.env.NODE_ENV || '').toLowerCase() === 'development';

if (!AUTH_TOKEN && !IS_DEV) {
  console.error('Missing BRIDGE_AUTH_TOKEN. Refusing to start bridge in non-development mode.');
  process.exit(1);
}

if (!AUTH_TOKEN && IS_DEV) {
  console.warn('BRIDGE_AUTH_TOKEN is empty in development mode; bridge auth is disabled.');
}

console.log('🦀 miniclaw WhatsApp Bridge');
console.log('===========================\n');
console.log(`Host: ${HOST}`);
console.log(`Port: ${PORT}`);

const server = new BridgeServer(PORT, AUTH_DIR, HOST, AUTH_TOKEN);

// Handle graceful shutdown
process.on('SIGINT', async () => {
  console.log('\n\nShutting down...');
  await server.stop();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  await server.stop();
  process.exit(0);
});

// Start the server
server.start().catch((error) => {
  console.error('Failed to start bridge:', error);
  process.exit(1);
});
