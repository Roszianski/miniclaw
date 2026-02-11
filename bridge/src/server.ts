/**
 * WebSocket server for Python-Node.js bridge communication.
 */

import { WebSocketServer, WebSocket } from 'ws';
import { timingSafeEqual } from 'crypto';
import { IncomingMessage } from 'http';
import { WhatsAppClient } from './whatsapp.js';

interface SendCommand {
  type: 'send';
  to: string;
  text: string;
  replyTo?: string;
}

interface PresenceCommand {
  type: 'presence';
  to: string;
  state: 'composing' | 'paused';
}

interface BridgeMessage {
  type: 'message' | 'status' | 'qr' | 'error';
  [key: string]: unknown;
}

type BridgeCommand = SendCommand | PresenceCommand;

export class BridgeServer {
  private wss: WebSocketServer | null = null;
  private wa: WhatsAppClient | null = null;
  private clients: Set<WebSocket> = new Set();

  constructor(
    private port: number,
    private authDir: string,
    private host: string,
    private authToken: string
  ) {}

  async start(): Promise<void> {
    // Create WebSocket server
    this.wss = new WebSocketServer({ port: this.port, host: this.host });
    console.log(`🌉 Bridge server listening on ws://${this.host}:${this.port}`);

    // Initialize WhatsApp client
    this.wa = new WhatsAppClient({
      authDir: this.authDir,
      onMessage: (msg) => this.broadcast({ type: 'message', ...msg }),
      onQR: (qr) => this.broadcast({ type: 'qr', qr }),
      onStatus: (status) => this.broadcast({ type: 'status', status }),
    });

    // Handle WebSocket connections
    this.wss.on('connection', (ws, request) => {
      if (!this._isAuthorized(request)) {
        console.warn('⚠️ Rejected unauthenticated bridge client');
        ws.close(1008, 'Unauthorized');
        return;
      }

      console.log('🔗 Python client connected');
      this.clients.add(ws);

      ws.on('message', async (data) => {
        try {
          const cmd = JSON.parse(data.toString()) as BridgeCommand;
          await this.handleCommand(cmd);
          ws.send(JSON.stringify({ type: 'sent', to: cmd.to }));
        } catch (error) {
          console.error('Error handling command:', error);
          ws.send(JSON.stringify({ type: 'error', error: String(error) }));
        }
      });

      ws.on('close', () => {
        console.log('🔌 Python client disconnected');
        this.clients.delete(ws);
      });

      ws.on('error', (error) => {
        console.error('WebSocket error:', error);
        this.clients.delete(ws);
      });
    });

    // Connect to WhatsApp
    await this.wa.connect();
  }

  private _isAuthorized(request: IncomingMessage): boolean {
    if (!this.authToken) {
      return true;
    }
    const header = request.headers['x-bridge-token'];
    const provided = Array.isArray(header) ? header[0] : header;
    if (!provided) {
      return false;
    }

    const expectedBytes = Buffer.from(this.authToken, 'utf-8');
    const providedBytes = Buffer.from(String(provided), 'utf-8');
    if (expectedBytes.length !== providedBytes.length) {
      return false;
    }
    return timingSafeEqual(providedBytes, expectedBytes);
  }

  private async handleCommand(cmd: BridgeCommand): Promise<void> {
    if (!this.wa) return;
    if (cmd.type === 'send') {
      await this.wa.sendMessage(cmd.to, cmd.text, cmd.replyTo);
      return;
    }
    if (cmd.type === 'presence') {
      await this.wa.sendPresence(cmd.to, cmd.state);
    }
  }

  private broadcast(msg: BridgeMessage): void {
    const data = JSON.stringify(msg);
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(data);
      }
    }
  }

  async stop(): Promise<void> {
    // Close all client connections
    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();

    // Close WebSocket server
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }

    // Disconnect WhatsApp
    if (this.wa) {
      await this.wa.disconnect();
      this.wa = null;
    }
  }
}
