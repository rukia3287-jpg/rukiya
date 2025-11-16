# services/youtube_service.py
import os
import json
import logging
import tempfile
import time
from typing import Optional, Dict, Any
from datetime import datetime
import asyncio

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Import your AI service (assumes services.ai_service.AIService exists)
from services.ai_service import AIService

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Config:
    """Configuration class"""
    def __init__(self):
        # YouTube settings
        self.client_secrets_file = None
        self.token_file = None
        self.video_id = os.getenv("YOUTUBE_VIDEO_ID", "")
        self.poll_interval = int(os.getenv("POLL_INTERVAL", "5"))
        self.bot_name = os.getenv("BOT_NAME", "Rukiya")

        # AI settings
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        self.ai_cooldown = int(os.getenv("AI_COOLDOWN", "10"))
        self.max_message_length = int(os.getenv("MAX_MESSAGE_LENGTH", "200"))

        # Bot behavior
        self.ai_triggers = ["rukiya", "bot", "hey rukiya", "@rukiya"]
        self.bot_users = ["rukiya", self.bot_name.lower()]
        self.banned_words = []  # Add words to filter

    def update_from_obj(self, obj: Any):
        for k, v in vars(obj).items():
            setattr(self, k, v)


class YouTubeService:
    """Handles YouTube API operations"""

    def __init__(self, config: Config):
        self.config = config
        self.youtube = None
        self._setup_credentials()

    def _validate_json_string(self, json_string: str, var_name: str) -> Optional[dict]:
        try:
            if not json_string or not json_string.strip():
                logger.warning(f"{var_name} is empty or not set")
                return None

            clean_string = json_string.strip()

            if clean_string.startswith('\ufeff'):
                clean_string = clean_string[1:]
                logger.info(f"Removed BOM from {var_name}")

            parsed = json.loads(clean_string)
            logger.info(f"✅ {var_name} parsed successfully")
            return parsed

        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON parsing failed for {var_name}: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error parsing {var_name}: {e}")
            return None

    def _setup_credentials(self):
        try:
            temp_dir = tempfile.gettempdir()
            self.config.client_secrets_file = os.path.join(temp_dir, "client_secret.json")
            self.config.token_file = os.path.join(temp_dir, "token.json")

            logger.info(f"Using temp directory: {temp_dir}")

            client_secret_json = os.getenv("CLIENT_SECRET_JSON")
            if client_secret_json:
                parsed_secret = self._validate_json_string(client_secret_json, "CLIENT_SECRET_JSON")
                if parsed_secret:
                    with open(self.config.client_secrets_file, "w") as f:
                        json.dump(parsed_secret, f, indent=2)
                    logger.info(f"✅ Client secrets written")

            token_json = os.getenv("TOKEN_JSON")
            if token_json:
                parsed_token = self._validate_json_string(token_json, "TOKEN_JSON")
                if parsed_token:
                    with open(self.config.token_file, "w") as f:
                        json.dump(parsed_token, f, indent=2)
                    logger.info(f"✅ Token written")

        except Exception as e:
            logger.error(f"❌ Failed to setup credentials: {e}")

    def authenticate(self) -> bool:
        """Authenticate with YouTube API (blocking). Call via thread from async code if needed."""
        try:
            if not os.path.exists(self.config.client_secrets_file):
                logger.error(f"❌ Client secrets file not found")
                return False

            if not os.path.exists(self.config.token_file):
                logger.error(f"❌ Token file not found")
                return False

            with open(self.config.token_file, 'r') as f:
                token_data = json.load(f)
            creds = Credentials.from_authorized_user_info(token_data)
            logger.info("✅ Loaded credentials")

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    logger.info("🔄 Refreshing token...")
                    creds.refresh(Request())
                    with open(self.config.token_file, 'w') as f:
                        f.write(creds.to_json())
                    logger.info("✅ Token refreshed")
                else:
                    logger.error("❌ Invalid credentials")
                    return False

            self.youtube = build('youtube', 'v3', credentials=creds)
            logger.info("✅ YouTube authenticated")
            return True

        except Exception as e:
            logger.error(f"❌ Authentication failed: {e}")
            return False

    def get_live_chat_id(self, video_id: str) -> Optional[str]:
        try:
            response = self.youtube.videos().list(
                part="liveStreamingDetails",
                id=video_id
            ).execute()

            if not response.get("items"):
                logger.warning(f"No video found for ID: {video_id}")
                return None

            live_details = response["items"][0].get("liveStreamingDetails", {})
            chat_id = live_details.get("activeLiveChatId")

            if chat_id:
                logger.info(f"Found live chat ID: {chat_id}")
            else:
                logger.warning(f"No active live chat for video: {video_id}")

            return chat_id

        except Exception as e:
            logger.error(f"Failed to get live chat ID: {e}")
            return None

    def get_chat_messages(self, live_chat_id: str, page_token: Optional[str] = None) -> Dict[str, Any]:
        """Blocking call to fetch chat messages; run from thread when used in async context."""
        try:
            request = self.youtube.liveChatMessages().list(
                liveChatId=live_chat_id,
                part="snippet,authorDetails",
                pageToken=page_token
            )
            return request.execute()

        except Exception as e:
            logger.error(f"Failed to get chat messages: {e}")
            return {}

    def send_message(self, live_chat_id: str, message: str) -> bool:
        """Blocking call to send a message; run via thread in async context."""
        try:
            message_body = {
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {
                        "messageText": message
                    }
                }
            }
            self.youtube.liveChatMessages().insert(
                part="snippet",
                body=message_body
            ).execute()
            logger.info(f"✅ Message sent: {message[:50]}...")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to send message: {e}")
            logger.error(f"Message was: {message}")
            return False


class ChatBot:
    """Async-friendly ChatBot wrapper to run the polling loop without blocking."""

    def __init__(self, youtube_service: YouTubeService, ai_service: AIService, config: Config):
        self.youtube = youtube_service
        self.ai = ai_service
        self.config = config
        self.processed_messages: set = set()
        self.live_chat_id: Optional[str] = None
        self.next_page_token: Optional[str] = None
        self.running = False
        self._task: Optional[asyncio.Task] = None

    async def _process_once(self) -> None:
        """One iteration of processing (non-blocking)."""
        if not self.running:
            return

        try:
            response = await asyncio.to_thread(
                self.youtube.get_chat_messages,
                self.live_chat_id,
                self.next_page_token
            )

            if not response:
                return

            self.next_page_token = response.get("nextPageToken")
            messages = response.get("items", [])

            for message in messages:
                try:
                    # Reuse your original logic in a safe manner
                    snippet = message.get("snippet", {})
                    author_details = message.get("authorDetails", {})
                    message_text = snippet.get("displayMessage", "")
                    author_name = author_details.get("displayName", "Unknown")
                    message_id = message.get("id", "")

                    if not message_text or message_id in self.processed_messages:
                        continue

                    self.processed_messages.add(message_id)

                    logger.info(f"📨 Message from {author_name}: {message_text}")

                    # AI response (await the async AI)
                    ai_response = await self.ai.generate_response(message_text, author_name)
                    if ai_response:
                        # send message in thread
                        success = await asyncio.to_thread(self.youtube.send_message, self.live_chat_id, ai_response)
                        if not success:
                            logger.error("❌ Failed to send AI response")

                except Exception as e:
                    logger.error(f"Error processing a message: {e}")

        except Exception as e:
            logger.error(f"Error during _process_once: {e}")

    async def run_async(self, video_id: str):
        """Start the async run loop. Call this with asyncio.create_task or await it."""
        try:
            self.live_chat_id = await asyncio.to_thread(self.youtube.get_live_chat_id, video_id)
            if not self.live_chat_id:
                logger.error("❌ Could not get live chat ID. Is the stream live?")
                return False

            self.running = True
            logger.info(f"✅ Monitoring chat: {self.live_chat_id}")

            while self.running:
                await self._process_once()
                # Use nextPageToken's recommended polling interval if available; fallback to config
                await asyncio.sleep(self.config.poll_interval)

            logger.info("✅ Chat bot stopped")
            return True

        except asyncio.CancelledError:
            logger.info("Run loop cancelled")
            self.running = False
            return True
        except Exception as e:
            logger.error(f"❌ Error in run_async: {e}")
            self.running = False
            return False

    def stop(self):
        """Stop the async loop at next tick."""
        self.running = False
        logger.info("🛑 Stopping chat bot...")

