import os
import json
import logging
import tempfile
import time
from typing import Optional, Dict, Any, List
from datetime import datetime
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Config:
    """Configuration class"""
    def __init__(self):
        self.client_secrets_file = None
        self.token_file = None
        self.video_id = os.getenv("YOUTUBE_VIDEO_ID", "")
        self.poll_interval = int(os.getenv("POLL_INTERVAL", "5"))
        self.bot_name = os.getenv("BOT_NAME", "AI Assistant")


class YouTubeService:
    """Handles YouTube API operations optimized for Render deployment"""

    def __init__(self, config: Config):
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
                        # Save refreshed token back to temp file
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

    def get_chat_messages(self, live_chat_id: str, page_token: Optional[str] = None) -> Dict[str, Any]:
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


class ChatBot:
    """Main chat bot that monitors and responds to YouTube live chat"""
    
    def __init__(self, youtube_service: YouTubeService, config: Config):
        self.youtube = youtube_service
        self.config = config
        self.processed_messages: set = set()
        self.live_chat_id: Optional[str] = None
        self.next_page_token: Optional[str] = None
        self.running = False
        
    def process_message(self, message_data: Dict[str, Any]) -> Optional[str]:
        """
        Process incoming message and generate response
        Override this method to implement custom bot logic
        """
        try:
            snippet = message_data.get("snippet", {})
            author_details = message_data.get("authorDetails", {})
            
            message_text = snippet.get("displayMessage", "")
            author_name = author_details.get("displayName", "Unknown")
            message_id = message_data.get("id", "")
            
            # Skip if already processed
            if message_id in self.processed_messages:
                return None
            
            # Mark as processed
            self.processed_messages.add(message_id)
            
            # Skip bot's own messages
            if author_details.get("isChatOwner", False) or author_details.get("isChatModerator", False):
                return None
            
            logger.info(f"Processing message from {author_name}: {message_text}")
            
            # Simple echo bot logic - customize this!
            if message_text.lower().startswith("!hello"):
                return f"Hello {author_name}! 👋"
            elif message_text.lower().startswith("!time"):
                return f"Current time: {datetime.now().strftime('%H:%M:%S')}"
            elif message_text.lower().startswith("!help"):
                return "Commands: !hello, !time, !help"
            
            return None
            
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return None
    
    def start(self, video_id: str):
        """Start monitoring the chat"""
        try:
            logger.info(f"Starting chat bot for video: {video_id}")
            
            # Get live chat ID
            self.live_chat_id = self.youtube.get_live_chat_id(video_id)
            if not self.live_chat_id:
                logger.error("Could not get live chat ID. Is the stream live?")
                return False
            
            logger.info(f"Monitoring chat: {self.live_chat_id}")
            self.running = True
            
            # Main loop
            while self.running:
                try:
                    # Get messages
                    response = self.youtube.get_chat_messages(
                        self.live_chat_id, 
                        self.next_page_token
                    )
                    
                    if not response:
                        logger.warning("Empty response from chat API")
                        time.sleep(self.config.poll_interval)
                        continue
                    
                    # Update next page token
                    self.next_page_token = response.get("nextPageToken")
                    
                    # Process messages
                    messages = response.get("items", [])
                    for message in messages:
                        response_text = self.process_message(message)
                        if response_text:
                            # Send response
                            success = self.youtube.send_message(
                                self.live_chat_id, 
                                response_text
                            )
                            if success:
                                logger.info(f"Sent response: {response_text}")
                            else:
                                logger.error("Failed to send response")
                    
                    # Wait before next poll
                    poll_interval_ms = response.get("pollingIntervalMillis", self.config.poll_interval * 1000)
                    time.sleep(poll_interval_ms / 1000)
                    
                except KeyboardInterrupt:
                    logger.info("Received interrupt signal")
                    self.running = False
                    break
                except Exception as e:
                    logger.error(f"Error in main loop: {e}")
                    time.sleep(self.config.poll_interval)
            
            logger.info("Chat bot stopped")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start chat bot: {e}")
            return False
    
    def stop(self):
        """Stop the bot"""
        self.running = False
        logger.info("Stopping chat bot...")


def main():
    """Main entry point"""
    try:
        logger.info("=" * 60)
        logger.info("YouTube Live Chat Bot Starting")
        logger.info("=" * 60)
        
        # Create config
        config = Config()
        
        # Validate configuration
        if not config.video_id:
            logger.error("YOUTUBE_VIDEO_ID environment variable not set!")
            logger.error("Please set it to your YouTube video ID")
            return
        
        logger.info(f"Video ID: {config.video_id}")
        logger.info(f"Poll Interval: {config.poll_interval}s")
        
        # Create YouTube service
        youtube_service = YouTubeService(config)
        
        # Authenticate
        if not youtube_service.authenticate():
            logger.error("Failed to authenticate with YouTube")
            return
        
        # Test connection
        if not youtube_service.test_connection():
            logger.error("YouTube API connection test failed")
            return
        
        # Get service status
        status = youtube_service.get_status()
        logger.info(f"Service Status: {json.dumps(status, indent=2)}")
        
        # Create and start bot
        bot = ChatBot(youtube_service, config)
        bot.start(config.video_id)
        
    except KeyboardInterrupt:
        logger.info("\nShutting down gracefully...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
