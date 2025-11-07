import os
import json
import logging
import tempfile
import time
from typing import Optional, Dict, Any
from datetime import datetime
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Import your AI service
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
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        self.ai_cooldown = int(os.getenv("AI_COOLDOWN", "10"))
        self.max_message_length = int(os.getenv("MAX_MESSAGE_LENGTH", "200"))
        
        # Bot behavior
        self.ai_triggers = ["rukiya", "bot", "hey rukiya", "@rukiya"]
        self.bot_users = ["rukiya", self.bot_name.lower()]
        self.banned_words = []  # Add words to filter


class YouTubeService:
    """Handles YouTube API operations"""

    def __init__(self, config: Config):
        self.config = config
        self.youtube = None
        self._setup_credentials()

    def _validate_json_string(self, json_string: str, var_name: str) -> Optional[dict]:
        """Validate and parse JSON string"""
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
        """Setup YouTube credentials from environment variables"""
        try:
            temp_dir = tempfile.gettempdir()
            self.config.client_secrets_file = os.path.join(temp_dir, "client_secret.json")
            self.config.token_file = os.path.join(temp_dir, "token.json")
            
            logger.info(f"Using temp directory: {temp_dir}")
            
            # Setup CLIENT_SECRET_JSON
            client_secret_json = os.getenv("CLIENT_SECRET_JSON")
            if client_secret_json:
                parsed_secret = self._validate_json_string(client_secret_json, "CLIENT_SECRET_JSON")
                if parsed_secret:
                    with open(self.config.client_secrets_file, "w") as f:
                        json.dump(parsed_secret, f, indent=2)
                    logger.info(f"✅ Client secrets written")

            # Setup TOKEN_JSON
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
        """Authenticate with YouTube API"""
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

            if chat_id:
                logger.info(f"Found live chat ID: {chat_id}")
            else:
                logger.warning(f"No active live chat for video: {video_id}")

            return chat_id

        except Exception as e:
            logger.error(f"Failed to get live chat ID: {e}")
            return None

    def get_chat_messages(self, live_chat_id: str, page_token: Optional[str] = None) -> Dict[str, Any]:
        """Get live chat messages"""
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
        """Send message to live chat"""
        try:
            # Build the request body as a plain dict
            message_body = {
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {
                        "messageText": message
                    }
                }
            }
            
            # Execute the request
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
                video_data = response["items"][0]
                logger.info(f"Retrieved video info: {video_data.get('snippet', {}).get('title', 'Unknown')}")
                return video_data
            
            logger.warning(f"No video found with ID: {video_id}")
            return None

        except Exception as e:
            logger.error(f"Failed to get video info: {e}")
            return None

    def test_connection(self) -> bool:
        """Test the YouTube API connection"""
        try:
            if not self.youtube:
                logger.error("YouTube service not initialized")
                return False
            
            response = self.youtube.channels().list(part="snippet", mine=True).execute()
            logger.info("✅ YouTube API connection test successful")
            return True
            
        except Exception as e:
            logger.error(f"❌ YouTube API connection test failed: {e}")
            return False


class ChatBot:
    """YouTube Live Chat Bot with AI integration"""
    
    def __init__(self, youtube_service: YouTubeService, ai_service: AIService, config: Config):
        self.youtube = youtube_service
        self.ai = ai_service
        self.config = config
        self.processed_messages: set = set()
        self.live_chat_id: Optional[str] = None
        self.next_page_token: Optional[str] = None
        self.running = False
        
    def process_message(self, message_data: Dict[str, Any]) -> Optional[str]:
        """Process incoming message and generate response"""
        try:
            snippet = message_data.get("snippet", {})
            author_details = message_data.get("authorDetails", {})
            
            message_text = snippet.get("displayMessage", "")
            author_name = author_details.get("displayName", "Unknown")
            message_id = message_data.get("id", "")
            
            # Skip if already processed
            if message_id in self.processed_messages:
                return None
            
            self.processed_messages.add(message_id)
            
            # Skip bot's own messages and moderators
            if author_details.get("isChatOwner", False):
                return None
            
            logger.info(f"📨 Message from {author_name}: {message_text}")
            
            # Try AI response first
            ai_response = self.ai.generate_response(message_text, author_name)
            if ai_response:
                return ai_response
            
            # Fallback to simple commands
            message_lower = message_text.lower()
            
            if "hello" in message_lower and self.config.bot_name.lower() in message_lower:
                return f"Namaste {author_name}! 🙏"
            elif "time" in message_lower:
                return f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            elif "help" in message_lower and self.config.bot_name.lower() in message_lower:
                return f"Hey {author_name}! Just mention my name and chat with me! 💬"
            
            return None
            
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return None
    
    def start(self, video_id: str):
        """Start monitoring the chat"""
        try:
            logger.info("=" * 60)
            logger.info(f"🚀 Starting {self.config.bot_name} for video: {video_id}")
            logger.info("=" * 60)
            
            # Get live chat ID
            self.live_chat_id = self.youtube.get_live_chat_id(video_id)
            if not self.live_chat_id:
                logger.error("❌ Could not get live chat ID. Is the stream live?")
                return False
            
            logger.info(f"✅ Monitoring chat: {self.live_chat_id}")
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
                        logger.warning("⚠️  Empty response from chat API")
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
                            if not success:
                                logger.error("❌ Failed to send response")
                    
                    # Wait before next poll
                    poll_interval_ms = response.get("pollingIntervalMillis", self.config.poll_interval * 1000)
                    time.sleep(poll_interval_ms / 1000)
                    
                except KeyboardInterrupt:
                    logger.info("⚠️  Received interrupt signal")
                    self.running = False
                    break
                except Exception as e:
                    logger.error(f"❌ Error in main loop: {e}")
                    time.sleep(self.config.poll_interval)
            
            logger.info("✅ Chat bot stopped")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to start chat bot: {e}")
            return False
    
    def stop(self):
        """Stop the bot"""
        self.running = False
        logger.info("🛑 Stopping chat bot...")


def main():
    """Main entry point"""
    try:
        logger.info("=" * 60)
        logger.info("🤖 YouTube Live Chat Bot with AI")
        logger.info("=" * 60)
        
        # Create config
        config = Config()
        
        # Validate configuration
        if not config.video_id:
            logger.error("❌ YOUTUBE_VIDEO_ID not set!")
            return
        
        if not config.gemini_api_key:
            logger.warning("⚠️  GEMINI_API_KEY not set - AI responses disabled")
        
        logger.info(f"📹 Video ID: {config.video_id}")
        logger.info(f"⏱️  Poll Interval: {config.poll_interval}s")
        logger.info(f"🤖 Bot Name: {config.bot_name}")
        logger.info(f"❄️  AI Cooldown: {config.ai_cooldown}s")
        
        # Create services
        youtube_service = YouTubeService(config)
        ai_service = AIService(config)
        
        # Authenticate
        if not youtube_service.authenticate():
            logger.error("❌ Failed to authenticate with YouTube")
            return
        
        # Create and start bot
        bot = ChatBot(youtube_service, ai_service, config)
        bot.start(config.video_id)
        
    except KeyboardInterrupt:
        logger.info("\n👋 Shutting down gracefully...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
