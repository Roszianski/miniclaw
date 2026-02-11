/**
 * WhatsApp client wrapper using Baileys.
 * Based on OpenClaw's working implementation.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
} from '@whiskeysockets/baileys';

import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import pino from 'pino';
import { mkdir, writeFile } from 'fs/promises';
import { join } from 'path';
import { homedir } from 'os';

const VERSION = '0.1.0';

export interface InboundMessage {
  id: string;
  sender: string;
  pn: string;
  content: string;
  quotedId?: string;
  mediaPath?: string;
  mediaType?: string;
  timestamp: number;
  isGroup: boolean;
}

export interface WhatsAppClientOptions {
  authDir: string;
  onMessage: (msg: InboundMessage) => void;
  onQR: (qr: string) => void;
  onStatus: (status: string) => void;
}

export class WhatsAppClient {
  private sock: any = null;
  private options: WhatsAppClientOptions;
  private reconnecting = false;
  private logger = pino({ level: 'silent' });
  private mediaDir = process.env.MEDIA_DIR || join(homedir(), '.miniclaw', 'media');
  private recentMessages: Map<string, any> = new Map();

  constructor(options: WhatsAppClientOptions) {
    this.options = options;
  }

  async connect(): Promise<void> {
    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    const { version } = await fetchLatestBaileysVersion();

    console.log(`Using Baileys version: ${version.join('.')}`);

    // Create socket following OpenClaw's pattern
    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, this.logger),
      },
      version,
      logger: this.logger,
      printQRInTerminal: false,
      browser: ['miniclaw', 'cli', VERSION],
      syncFullHistory: false,
      markOnlineOnConnect: false,
    });

    // Handle WebSocket errors
    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        console.error('WebSocket error:', err.message);
      });
    }

    // Handle connection updates
    this.sock.ev.on('connection.update', async (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // Display QR code in terminal
        console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
        qrcode.generate(qr, { small: true });
        this.options.onQR(qr);
      }

      if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        console.log(`Connection closed. Status: ${statusCode}, Will reconnect: ${shouldReconnect}`);
        this.options.onStatus('disconnected');

        if (shouldReconnect && !this.reconnecting) {
          this.reconnecting = true;
          console.log('Reconnecting in 5 seconds...');
          setTimeout(() => {
            this.reconnecting = false;
            this.connect();
          }, 5000);
        }
      } else if (connection === 'open') {
        console.log('✅ Connected to WhatsApp');
        this.options.onStatus('connected');
      }
    });

    // Save credentials on update
    this.sock.ev.on('creds.update', saveCreds);

    // Handle incoming messages
    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      if (type !== 'notify') return;

      for (const msg of messages) {
        // Skip own messages
        if (msg.key.fromMe) continue;

        // Skip status updates
        if (msg.key.remoteJid === 'status@broadcast') continue;

        const extracted = await this.extractMessageContent(msg);
        if (!extracted) continue;
        if (msg.key.remoteJid && msg.key.id) {
          this.recentMessages.set(`${msg.key.remoteJid}:${msg.key.id}`, msg);
          if (this.recentMessages.size > 5000) {
            const first = this.recentMessages.keys().next().value as string | undefined;
            if (first) this.recentMessages.delete(first);
          }
        }

        const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;

        this.options.onMessage({
          id: msg.key.id || '',
          sender: msg.key.remoteJid || '',
          pn: msg.key.remoteJidAlt || '',
          content: extracted.content,
          quotedId: extracted.quotedId,
          mediaPath: extracted.mediaPath,
          mediaType: extracted.mediaType,
          timestamp: msg.messageTimestamp as number,
          isGroup,
        });
      }
    });
  }

  private async extractMessageContent(
    msg: any
  ): Promise<{ content: string; quotedId?: string; mediaPath?: string; mediaType?: string } | null> {
    const message = msg.message;
    if (!message) return null;
    const quotedId = message.extendedTextMessage?.contextInfo?.stanzaId || undefined;

    // Text message
    if (message.conversation) {
      return { content: message.conversation, quotedId };
    }

    // Extended text (reply, link preview)
    if (message.extendedTextMessage?.text) {
      return { content: message.extendedTextMessage.text, quotedId };
    }

    // Image with caption
    if (message.imageMessage) {
      const caption = message.imageMessage.caption || '[Image]';
      const mediaPath = await this.downloadMedia(msg, message.imageMessage.mimetype || 'image/jpeg', 'image');
      return { content: caption, quotedId, mediaPath, mediaType: 'image' };
    }

    // Video with caption
    if (message.videoMessage?.caption) {
      return { content: `[Video] ${message.videoMessage.caption}`, quotedId };
    }

    // Document with caption
    if (message.documentMessage) {
      const caption = message.documentMessage.caption || '[Document]';
      const mime = message.documentMessage.mimetype || 'application/octet-stream';
      const mediaPath = await this.downloadMedia(msg, mime, 'document');
      return { content: caption, quotedId, mediaPath, mediaType: 'document' };
    }

    // Voice/Audio message
    if (message.audioMessage) {
      const mime = message.audioMessage.mimetype || 'audio/ogg';
      const mediaPath = await this.downloadMedia(msg, mime, 'audio');
      return { content: `[Voice Message]`, quotedId, mediaPath, mediaType: 'audio' };
    }

    return null;
  }

  private async downloadMedia(msg: any, mime: string, kind: string): Promise<string | undefined> {
    if (!this.sock) return undefined;
    try {
      await mkdir(this.mediaDir, { recursive: true });
      const buffer = await downloadMediaMessage(
        msg,
        'buffer',
        {},
        { logger: this.logger, reuploadRequest: this.sock.updateMediaMessage }
      );
      if (!buffer) return undefined;
      const ext = (mime.split('/')[1] || 'bin').split(';')[0];
      const safeExt = ext === 'jpeg' ? 'jpg' : ext;
      const filename = `${msg.key.id || Date.now()}.${safeExt}`;
      const filePath = join(this.mediaDir, filename);
      await writeFile(filePath, buffer as Buffer);
      return filePath;
    } catch {
      return undefined;
    }
  }

  async sendMessage(to: string, text: string, replyTo?: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    if (replyTo) {
      const quoted = this.recentMessages.get(`${to}:${replyTo}`);
      if (quoted) {
        await this.sock.sendMessage(to, { text }, { quoted });
        return;
      }
    }

    await this.sock.sendMessage(to, { text });
  }

  async sendPresence(to: string, state: 'composing' | 'paused'): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }
    await this.sock.sendPresenceUpdate(state, to);
  }

  async disconnect(): Promise<void> {
    if (this.sock) {
      this.sock.end(undefined);
      this.sock = null;
    }
  }
}
