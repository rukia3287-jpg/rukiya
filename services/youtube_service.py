import os
import json
import logging
import tempfile
from typing import Optional, Dict, Any
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

class YouTubeService:
    """Handles YouTube API operations optimized for Render deployment"""

    def __init__(self, config):
        self.config = config
        self.youtube = None
        self._setup_credentials()

    def _validate_json_string(self, json_string: str, var_name: str) -> Optional[dict]:
        """Validate and parse JSON string with detailed error reporting"""
        try:
            if not json_string or not json_string.strip():
                logger.warning(f"{var_name} is empty or not set")
                return None
            
            # Clean the string
            clean_string = json_string.strip()
            
            # Remove BOM if present
            if clean_string.startswith('\ufeff'):
                clean_string = clean_string[1:]
                logger.info(f"Removed BOM from {var_name}")
            
            # Parse JSON
            parsed = json.loads(clean_string)
            logger.info(f"✅ {var_name} parsed successfully")
            return parsed
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON parsing failed for {var_name}: {e}")
            logger.error(f"Error at position {e.pos}: {repr(clean_string[max(0, e.pos-5):e.pos+5])}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error parsing {var_name}: {e}")
            return None

    def _setup_credentials(self):
        """Setup YouTube credentials from environment variables (Render-friendly)"""
        try:
            # For Render, we'll use temporary files since filesystem is ephemeral
            temp_dir = tempfile.gettempdir()
            
            # Update config paths to use temp directory
            self.config.client_secrets_file = os.path.join(temp_dir, "client_secret.json")
            self.config.token_file = os.path.join(temp_dir, "token.json")
            
            logger.info(f"Using temp directory: {temp_dir}")
            
            # Setup CLIENT_SECRET_JSON
            client_secret_json = os.getenv("CLIENT_SECRET_JSON")
            if client_secret_json:
                logger.info("Found CLIENT_SECRET_JSON environment variable")
                parsed_secret = self._validate_json_string(client_secret_json, "CLIENT_SECRET_JSON")
                if parsed_secret:
                    with open(self.config.client_secrets_file, "w") as f:
                        json.dump(parsed_secret, f, indent=2)
                    logger.info(f"✅ Client secrets written to {self.config.client_secrets_file}")
                else:
                    logger.error("❌ Failed to parse CLIENT_SECRET_JSON")
            else:
                logger.warning("⚠️  CLIENT_SECRET_JSON not found in environment")

            # Setup TOKEN_JSON
            token_json = os.getenv("TOKEN_JSON")
            if token_json:
                logger.info("Found TOKEN_JSON environment variable")
                parsed_token = self._validate_json_string(token_json, "TOKEN_JSON")
                if parsed_token:
                    with open(self.config.token_file, "w") as f:
                        json.dump(parsed_token, f, indent=2)
                    logger.info(f"✅ Token written to {self.config.token_file}")
                else:
                    logger.error("❌ Failed to parse TOKEN_JSON")
            else:
                logger.warning("⚠️  TOKEN_JSON not found in environment")

        except Exception as e:
            logger.error(f"❌ Failed to setup credentials: {e}")
            logger.error(f"Current working directory: {os.getcwd()}")
            logger.error(f"Temp directory permissions: {oct(os.stat(temp_dir).st_mode)[-3:]}")

    def authenticate(self) -> bool:
        """Authenticate with YouTube API"""
        try:
            # Check if credential files exist
            if not os.path.exists(self.config.client_secrets_file):
                logger.error(f"❌ Client secrets file not found: {self.config.client_secrets_file}")
                logger.error("Make sure CLIENT_SECRET_JSON environment variable is set correctly")
                return False

            if not os.path.exists(self.config.token_file):
                logger.error(f"❌ Token file not found: {self.config.token_file}")
                logger.error("Make sure TOKEN_JSON environment variable is set correctly")
                return False

            creds = None

            # Load existing token
            try:
                with open(self.config.token_file, 'r') as f:
                    token_data = json.load(f)
                creds = Credentials.from_authorized_user_info(token_data)
                logger.info("✅ Loaded credentials from token file")
            except Exception as e:
                logger.error(f"❌ Failed to load token file: {e}")
                return False

            # Check if credentials need refresh
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    logger.info("🔄 Token expired, attempting refresh...")
                    try:
                        creds.refresh(Request())
                        # Save refreshed token
                        with open(self.config.token_file, 'w') as f:
                            f.write(creds.to_json())
                        logger.info("✅ Token refreshed successfully")
                    except Exception as e:
                        logger.error(f"❌ Token refresh failed: {e}")
                        return False
                else:
                    logger.error("❌ Invalid credentials and no refresh token available")
                    return False

            # Build YouTube service
            self.youtube = build('youtube', 'v3', credentials=creds)
            logger.info("✅ YouTube authentication successful")
            return True

        except Exception as e:
            logger.error(f"❌ YouTube authentication failed: {e}")
            return False

    def test_connection(self) -> bool:
        """Test the YouTube API connection"""
        try:
            if not self.youtube:
                logger.error("YouTube service not initialized")
                return False
            
            # Try a simple API call
            response = self.youtube.channels().list(part="snippet", mine=True).execute()
            logger.info("✅ YouTube API connection test successful")
            return True
            
        except Exception as e:
            logger.error(f"❌ YouTube API connection test failed: {e}")
            return False

    def get_live_chat_id(self, video_id: str) -> Optional[str]:
        """Get live chat ID for a video"""
        try:
            if not self.youtube:
                logger.error("YouTube service not authenticated")
                return None

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
            if not self.youtube:
                logger.error("YouTube service not authenticated")
                return {}

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
            if not self.youtube:
                logger.error("YouTube service not authenticated")
                return False

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
            if not self.youtube:
                logger.error("YouTube service not authenticated")
                return None

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

    def get_status(self) -> Dict[str, Any]:
        """Get service status for debugging"""
        return {
            "authenticated": self.youtube is not None,
            "client_secrets_exists": os.path.exists(self.config.client_secrets_file) if hasattr(self.config, 'client_secrets_file') else False,
            "token_exists": os.path.exists(self.config.token_file) if hasattr(self.config, 'token_file') else False,
            "client_secrets_path": getattr(self.config, 'client_secrets_file', None),
            "token_path": getattr(self.config, 'token_file', None)
        }
