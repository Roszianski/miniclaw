"""Telegram channel implementation using python-telegram-bot."""

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from miniclaw.bus.events import OutboundMessage
from miniclaw.bus.queue import MessageBus
from miniclaw.channels.base import BaseChannel
from miniclaw.config.schema import TelegramConfig

if TYPE_CHECKING:
    from miniclaw.providers.transcription import TranscriptionManager


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""
    
    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"
    
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)
    
    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"
    
    text = re.sub(r'`([^`]+)`', save_inline_code, text)
    
    # 3. Headers # Title -> just the title text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    
    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    
    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    
    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    
    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)
    
    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    
    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")
    
    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")
    
    return text


MAX_TELEGRAM_LENGTH = 4096


def _chunk_message(text: str, max_len: int = MAX_TELEGRAM_LENGTH) -> list[str]:
    """Split a message into chunks that fit within Telegram's limit."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Try to split on double newline
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut <= 0:
            # Try single newline
            cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            # Hard cut
            cut = max_len
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.
    
    Simple and reliable - no webhook/public IP needed.
    """
    
    name = "telegram"
    
    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        groq_api_key: str = "",
        identity_store: object | None = None,
        transcription_manager: "TranscriptionManager | None" = None,
    ):
        super().__init__(config, bus, identity_store=identity_store)
        self.config: TelegramConfig = config
        if transcription_manager is None:
            from miniclaw.providers.transcription import TranscriptionManager

            self.transcription_manager = TranscriptionManager.from_config(
                None,
                groq_api_key=groq_api_key or None,
            )
        else:
            self.transcription_manager = transcription_manager
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[int, asyncio.Task[None]] = {}
        self._typing_interval_s: float = 4.0
    
    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return
        
        self._running = True
        
        # Build the application
        builder = Application.builder().token(self.config.token)
        if self.config.proxy:
            builder = builder.proxy(self.config.proxy).get_updates_proxy(self.config.proxy)
        self._app = builder.build()
        
        # Add message handler for text, photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL) 
                & ~filters.COMMAND, 
                self._on_message
            )
        )
        
        # Add command handlers
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler(["cancel", "status", "reset", "think"], self._on_command))
        
        logger.info("Starting Telegram bot (polling mode)...")
        
        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()
        
        # Get bot info
        bot_info = await self._app.bot.get_me()
        logger.info(f"Telegram bot @{bot_info.username} connected")
        
        # Start polling (this runs until stopped)
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True  # Ignore old messages on startup
        )
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False
        
        if self._app:
            logger.info("Stopping Telegram bot...")
            for chat_id in list(self._typing_tasks.keys()):
                self._stop_typing(chat_id)
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            logger.warning("Telegram bot not running")
            return

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            logger.error(f"Invalid chat_id: {msg.chat_id}")
            return

        if msg.control:
            await self._handle_control(chat_id, msg.control)
            return

        # Convert markdown to Telegram HTML
        html_content = _markdown_to_telegram_html(msg.content)
        reply_to_message_id = self._parse_reply_to(msg.reply_to)
        chunks = _chunk_message(html_content)

        try:
            for index, chunk in enumerate(chunks):
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_message_id if index == 0 else None,
                    allow_sending_without_reply=True,
                )
                if len(chunks) > 1:
                    await asyncio.sleep(0.3)
        except Exception as e:
            # Fallback to plain text if HTML parsing fails
            logger.warning(f"HTML parse failed, falling back to plain text: {e}")
            try:
                raw_chunks = _chunk_message(msg.content)
                for index, chunk in enumerate(raw_chunks):
                    await self._app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=reply_to_message_id if index == 0 else None,
                        allow_sending_without_reply=True,
                    )
                    if len(raw_chunks) > 1:
                        await asyncio.sleep(0.3)
            except Exception as e2:
                logger.error(f"Error sending Telegram message: {e2}")
    
    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return
        
        user = update.effective_user
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm miniclaw.\n\n"
            "Send me a message and I'll respond!"
        )

    async def _on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle supported slash commands and forward to the agent bus."""
        if not update.message or not update.effective_user:
            return

        command_text = self._normalize_command_text(update.message.text or "")
        if not command_text:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"

        self._chat_ids[sender_id] = chat_id
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content=command_text,
            media=None,
            metadata={
                "message_id": message.message_id,
                "reply_to_message_id": message.reply_to_message.message_id if message.reply_to_message else None,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_group": message.chat.type != "private",
            },
        )
    
    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return
        
        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        
        # Use stable numeric ID, but keep username for allowlist compatibility
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"
        
        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id
        
        # Build content from text and/or media
        content_parts = []
        media_paths = []
        
        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)
        
        # Handle media files
        media_file = None
        media_type = None
        
        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"
        
        # Download media if present
        if media_file and self._app:
            try:
                file = await self._app.bot.get_file(media_file.file_id)
                ext = self._get_extension(media_type, getattr(media_file, 'mime_type', None))
                
                # Save to workspace/media/
                media_dir = Path.home() / ".miniclaw" / "media"
                media_dir.mkdir(parents=True, exist_ok=True)
                
                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))
                
                media_paths.append(str(file_path))
                
                # Handle voice transcription
                if media_type == "voice" or media_type == "audio":
                    transcription = await self.transcription_manager.transcribe(file_path)
                    if transcription:
                        logger.info(f"Transcribed {media_type}: {transcription[:50]}...")
                        content_parts.append(f"[transcription: {transcription}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                else:
                    # Extract PDF text if possible
                    if media_type == "file" and file_path.suffix.lower() == ".pdf":
                        try:
                            from pypdf import PdfReader
                            reader = PdfReader(str(file_path))
                            extracted = []
                            for page in reader.pages:
                                text = page.extract_text() or ""
                                if text:
                                    extracted.append(text)
                            pdf_text = "\n".join(extracted).strip()
                            if pdf_text:
                                content_parts.append(f"[pdf text]\n{pdf_text[:4000]}")
                            else:
                                content_parts.append(f"[file: {file_path}]")
                        except Exception:
                            content_parts.append(f"[file: {file_path}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                    
                logger.debug(f"Downloaded {media_type} to {file_path}")
            except Exception as e:
                logger.error(f"Failed to download media: {e}")
                content_parts.append(f"[{media_type}: download failed]")
        
        content = "\n".join(content_parts) if content_parts else "[empty message]"
        
        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")
        
        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content=content,
            media=media_paths,
            metadata={
                "message_id": message.message_id,
                "reply_to_message_id": message.reply_to_message.message_id if message.reply_to_message else None,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_group": message.chat.type != "private"
            }
        )

    async def _handle_control(self, chat_id: int, control: str) -> None:
        if control == "typing_start":
            self._start_typing(chat_id)
            return
        if control == "typing_stop":
            self._stop_typing(chat_id)

    def _start_typing(self, chat_id: int) -> None:
        if not self._app or chat_id in self._typing_tasks:
            return

        async def _loop() -> None:
            while self._running and self._app:
                try:
                    await self._app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception:
                    return
                await asyncio.sleep(self._typing_interval_s)

        self._typing_tasks[chat_id] = asyncio.create_task(_loop())

    def _stop_typing(self, chat_id: int) -> None:
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    @staticmethod
    def _parse_reply_to(reply_to: str | None) -> int | None:
        if not reply_to:
            return None
        try:
            return int(str(reply_to))
        except ValueError:
            return None

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        raw = (text or "").strip()
        if not raw.startswith("/"):
            return ""
        parts = raw.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command in {"/cancel", "/status", "/reset"}:
            return command
        if command == "/think":
            return f"/think {arg}".strip()
        return ""
    
    def _get_extension(self, media_type: str, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]
        
        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
