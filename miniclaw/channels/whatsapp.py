"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import json
from typing import TYPE_CHECKING

from loguru import logger

from miniclaw.bus.events import OutboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.channels.base import BaseChannel
from miniclaw.config.schema import WhatsAppConfig

if TYPE_CHECKING:
    from miniclaw.providers.transcription import TranscriptionManager


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.
    
    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """
    
    name = "whatsapp"
    
    def __init__(
        self,
        config: WhatsAppConfig,
        bus: MessageBus,
        identity_store: object | None = None,
        transcription_manager: "TranscriptionManager | None" = None,
    ):
        super().__init__(config, bus, identity_store=identity_store)
        self.config: WhatsAppConfig = config
        if transcription_manager is None:
            from miniclaw.providers.transcription import TranscriptionManager

            self.transcription_manager = TranscriptionManager.from_config(None, groq_api_key=None)
        else:
            self.transcription_manager = transcription_manager
        self._ws = None
        self._connected = False
    
    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets
        
        bridge_url = self.config.bridge_url
        auth_token = str(self.config.bridge_auth_token or "").strip()
        if not auth_token:
            raise RuntimeError("channels.whatsapp.bridge_auth_token is required for bridge authentication.")
        headers = {"x-bridge-token": auth_token}
        
        logger.info(f"Connecting to WhatsApp bridge at {bridge_url}...")
        
        self._running = True
        
        while self._running:
            try:
                try:
                    ws_ctx = websockets.connect(bridge_url, additional_headers=headers)
                except TypeError:
                    ws_ctx = websockets.connect(bridge_url, extra_headers=headers)
                async with ws_ctx as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")
                    
                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning(f"WhatsApp bridge connection error: {e}")
                
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)
    
    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False
        
        if self._ws:
            await self._ws.close()
            self._ws = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return
        
        try:
            if msg.control:
                state = "composing" if msg.control == "typing_start" else "paused"
                payload = {
                    "type": "presence",
                    "to": msg.chat_id,
                    "state": state,
                }
                await self._ws.send(json.dumps(payload))
                return

            payload = {
                "type": "send",
                "to": msg.chat_id,
                "text": msg.content,
            }
            if msg.reply_to:
                payload["replyTo"] = str(msg.reply_to)
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")
    
    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from bridge: {raw[:100]}")
            return
        
        msg_type = data.get("type")
        
        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatspp.net
            pn = data.get("pn", "")
            # New LID sytle typically: 
            sender = data.get("sender", "")
            content = data.get("content", "")
            media_path = data.get("mediaPath")
            media_type = data.get("mediaType")
            media_paths: list[str] = []
            if media_path:
                media_paths.append(media_path)
            
            # Extract just the phone number or lid as chat_id
            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            logger.info(f"Sender {sender}")
            
            # Handle voice/audio transcription
            if media_path and str(media_type or "").lower() in {"voice", "audio"}:
                transcription = await self.transcription_manager.transcribe(media_path)
                if transcription:
                    content = f"[transcription: {transcription}]"
                elif not content:
                    content = f"[{media_type or 'audio'}: {media_path}]"
            elif content == "[Voice Message]":
                content = "[Voice Message: transcription unavailable]"

            # Extract PDF text if present
            if media_path and str(media_path).lower().endswith(".pdf"):
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(str(media_path))
                    extracted = []
                    for page in reader.pages:
                        text = page.extract_text() or ""
                        if text:
                            extracted.append(text)
                    pdf_text = "\n".join(extracted).strip()
                    if pdf_text:
                        content += f"\n[pdf text]\n{pdf_text[:4000]}"
                except Exception:
                    pass
            elif media_path and not content:
                content = f"[{media_type or 'file'}: {media_path}]"

            content = self._normalize_slash_command(content)
            
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_paths,
                metadata={
                    "message_id": data.get("id"),
                    "reply_to_message_id": data.get("quotedId"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False)
                }
            )
        
        elif msg_type == "status":
            # Connection status update
            status = data.get("status")
            logger.info(f"WhatsApp status: {status}")
            
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False
        
        elif msg_type == "qr":
            # QR code for authentication
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")
        
        elif msg_type == "error":
            logger.error(f"WhatsApp bridge error: {data.get('error')}")

    @staticmethod
    def _normalize_slash_command(content: str) -> str:
        text = (content or "").strip()
        if not text.startswith("/"):
            return content
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command in {"/cancel", "/status", "/reset"}:
            return command
        if command == "/think":
            return f"/think {arg}".strip()
        if command.startswith("/think:"):
            mode = command.split(":", 1)[1]
            if mode in {"off", "low", "medium", "high"}:
                if arg:
                    return f"/think:{mode} {arg}"
                return f"/think:{mode}"
        return content
