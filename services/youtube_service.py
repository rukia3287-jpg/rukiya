import os
import json
import logging
from typing import Optional, Dict, Any
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

class YouTubeService:
    """Handles YouTube API operations with better error handling"""

    def __init__(self, config):
        self.config = config
        self.youtube = None
        self._setup_credentials()

    def _setup_credentials(self):
        """Setup YouTube credentials from environment"""
        try:
            # Load client secrets from env
            client_secret = os.getenv("CLIENT_SECRET_JSON")
            if client_secret:
                with open(self.config.client_secrets_file, "w") as f:
                    json.dump(json.loads(client_secret), f, indent=2)

            # Load token from env
            token_json = os.getenv("TOKEN_JSON")
            if token_json:
                with open(self.config.token_file, "w") as f:
                    json.dump(json.loads(token_json), f, indent=2)

        except Exception as e:
            logger.error(f"Failed to setup credentials: {e}")

    def authenticate(self) -> bool:
        """Authenticate with YouTube API"""
        try:
            creds = None

            # Load existing token
            if os.path.exists(self.config.token_file):
                with open(self.config.token_file, 'r') as f:
                    token_data = json.load(f)
                creds = Credentials.from_authorized_user_info(token_data)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(self.config.token_file, 'w') as f:
                        f.write(creds.to_json())
                else:
                    logger.error("Invalid credentials - manual auth required")
                    return False

            self.youtube = build('youtube', 'v3', credentials=creds)
            logger.info("YouTube authentication successful")
            return True

        except Exception as e:
            logger.error(f"YouTube authentication failed: {e}")
            return False

    def get_live_chat_id(self, video_id: str) -> Optional[str]:
        """Get live chat ID for a video"""
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

            if not chat_id:
                logger.warning(f"No active live chat for video: {video_id}")
                return None

            logger.info(f"Found live chat ID: {chat_id}")
            return chat_id

        except Exception as e:
            logger.error(f"Failed to get live chat ID: {e}")
            return None

    def get_chat_messages(self, live_chat_id: str, page_token: str = None) -> Dict[str, Any]:
        """Get live chat messages"""
        try:
            request = self.youtube.liveChatMessages().list(
                liveChatId=live_chat_id,
                part="snippet,authorDetails",
                pageToken=page_token
            )
            response = request.execute()
            logger.debug(f"Retrieved {len(response.get('items', []))} messages")
            return response
        except Exception as e:
            logger.error(f"Failed to get chat messages: {e}")
            return {}

    def send_message(self, live_chat_id: str, message: str) -> bool:
        """Send message to live chat"""
        try:
            request = self.youtube.liveChatMessages().insert(
                part="snippet",
                body={
                    "snippet": {
                        "liveChatId": live_chat_id,
                        "type": "textMessageEvent",
                        "textMessageDetails": {"messageText": message}
                    }
                }
            )
            request.execute()
            logger.info(f"Message sent: {message[:50]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False

    def get_video_info(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Get basic video information"""
        try:
            response = self.youtube.videos().list(
                part="snippet,statistics,liveStreamingDetails",
                id=video_id
            ).execute()

            if response.get("items"):
                return response["items"][0]
            return None

        except Exception as e:
            logger.error(f"Failed to get video info: {e}")
            return None