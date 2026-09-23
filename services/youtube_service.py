# services/youtube_service.py
import os
import json
import logging
import tempfile
from typing import Optional, Dict, Any
import asyncio

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

# Import Config from the centralized location
from services.config import Config

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


class YouTubeService:
    """Handles YouTube API operations.

    Important: Do NOT import AIService at module import time here — pass an AI service
    instance into any higher-level runner that needs both services. This avoids circular imports.
    """

    def __init__(self, config: Config):
        self.config = config
        self.youtube = None
        # Deliberately process-lifetime: restarting monitoring must not reset it.
        self._nonessential_insert_count = 0
        self._nonessential_insert_cap = 60
        self._nonessential_warning_at = 48
        self._nonessential_warning_logged = False
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
                    logger.info("✅ Client secrets written")

            token_json = os.getenv("TOKEN_JSON")
            if token_json:
                parsed_token = self._validate_json_string(token_json, "TOKEN_JSON")
                if parsed_token:
                    with open(self.config.token_file, "w") as f:
                        json.dump(parsed_token, f, indent=2)
                    logger.info("✅ Token written")

        except Exception as e:
            logger.error(f"❌ Failed to setup credentials: {e}")

    def authenticate(self) -> bool:
        """Authenticate with YouTube API (blocking). Call via thread from async code if needed."""
        try:
            if not os.path.exists(self.config.client_secrets_file):
                logger.error("❌ Client secrets file not found")
                return False

            if not os.path.exists(self.config.token_file):
                logger.error("❌ Token file not found")
                return False

            with open(self.config.token_file, "r") as f:
                token_data = json.load(f)
            creds = Credentials.from_authorized_user_info(token_data)
            logger.info("✅ Loaded credentials")

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    logger.info("🔄 Refreshing token...")
                    creds.refresh(Request())
                    with open(self.config.token_file, "w") as f:
                        f.write(creds.to_json())
                    logger.info("✅ Token refreshed")
                else:
                    logger.error("❌ Invalid credentials")
                    return False

            self.youtube = build("youtube", "v3", credentials=creds)
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

        except HttpError:
            # Let ChatMonitor inspect quotaExceeded and stop without retrying.
            raise
        except Exception as e:
            logger.error(f"Failed to get chat messages: {e}")
            return {}

    def send_message(self, live_chat_id: str, message: str, *, message_kind: str = "reply") -> bool:
        """Blocking call to send a message; run via thread in async context."""
        nonessential = message_kind in {"idle", "welcome"}
        if nonessential and self._nonessential_insert_count >= self._nonessential_insert_cap:
            logger.warning("Non-essential insert cap (%d) reached; suppressing %s message", self._nonessential_insert_cap, message_kind)
            return False
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
            if nonessential:
                self._nonessential_insert_count += 1
                if self._nonessential_insert_count >= self._nonessential_warning_at and not self._nonessential_warning_logged:
                    self._nonessential_warning_logged = True
                    logger.warning("Non-essential insert usage is at 80%% of the %d-call cap", self._nonessential_insert_cap)
            logger.info(f"✅ Message sent: {message[:50]}...")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to send message: {e}")
            logger.error(f"Message was: {message}")
            return False
