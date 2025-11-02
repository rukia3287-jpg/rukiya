import discord
from discord.ext import commands, tasks
import logging
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import asyncio
import google.generativeai as genai

logger = logging.getLogger(__name__)

class Welcome(commands.Cog):
    """Cog for welcoming and saying farewell to users in YouTube chat with AI"""
    
    def __init__(self, bot):
        self.bot = bot
        self.user_data_file = "user_activity.json"
        self.backup_file = "user_activity_backup.json"
        self.user_last_seen: Dict[str, str] = {}  # username -> last_seen timestamp
        self.user_message_count: Dict[str, int] = {}  # username -> message count
        self.user_last_welcomed: Dict[str, str] = {}  # username -> last_welcomed timestamp
        self.active_users: set = set()  # Currently active users
        
        # Time thresholds (in seconds)
        self.welcome_back_after = 3600  # Welcome back after 1 hour of absence
        self.farewell_after = 600  # Say farewell after 10 minutes of inactivity
        self.new_user_threshold = 3  # Consider user new if less than 3 messages
        
        # Trigger words for greeting
        self.greeting_triggers = [
            'hi', 'hello', 'hey', 'greetings', 'sup', 'yo', 'hola',
            'namaste', 'good morning', 'good evening', 'good afternoon',
            'what\'s up', 'whats up', 'wassup', 'hii', 'heya', 'hiya'
        ]
        
        # Fallback settings
        self.max_retries = 3
        self.retry_delay = 2  # seconds
        self.fallback_enabled = True
        
        # Message queue for failed sends
        self.message_queue: List[dict] = []
        self.max_queue_size = 50
        
        # Error tracking
        self.error_count = 0
        self.last_error_time = None
        
        # Initialize Gemini AI
        self.setup_gemini()
        
        # Initialize data
        self.load_user_data()
        
        # Start background tasks
        self.check_inactive_users.start()
        self.retry_failed_messages.start()
        self.auto_backup.start()
        
        logger.info("✅ Welcome cog initialized with AI and fallback system")
    
    def setup_gemini(self):
        """Setup Gemini AI for intelligent responses"""
        try:
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                logger.warning("⚠️ GEMINI_API_KEY not found, AI features disabled")
                self.ai_enabled = False
                return
            
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel('gemini-1.5-flash')
            self.ai_enabled = True
            logger.info("✅ Gemini AI initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Gemini AI: {e}")
            self.ai_enabled = False
    
    async def generate_ai_response(self, username: str, message: str, context: str) -> Optional[str]:
        """Generate AI response using Gemini"""
        if not self.ai_enabled:
            return None
        
        try:
            prompt = f"""You are Rukiya, a friendly and welcoming YouTube chat bot. 

Context: {context}
User: {username}
Message: {message}

Generate a warm, personalized welcome message (max 2 sentences). Be casual, friendly, and mention something about their message if relevant. Keep it short and natural."""

            response = await asyncio.to_thread(
                self.model.generate_content,
                prompt
            )
            
            ai_message = response.text.strip()
            logger.info(f"🤖 AI generated response for {username}")
            return ai_message
            
        except Exception as e:
            logger.error(f"❌ AI generation failed: {e}")
            return None
    
    def cog_unload(self):
        """Clean up when cog is unloaded"""
        try:
            self.check_inactive_users.cancel()
            self.retry_failed_messages.cancel()
            self.auto_backup.cancel()
            self.save_user_data()
            logger.info("🛑 Welcome cog unloaded safely")
        except Exception as e:
            logger.error(f"❌ Error during cog unload: {e}")
    
    def load_user_data(self):
        """Load user activity data from JSON file with fallback"""
        loaded = False
        
        # Try loading main file
        try:
            if os.path.exists(self.user_data_file):
                with open(self.user_data_file, 'r') as f:
                    data = json.load(f)
                    self.user_last_seen = data.get('last_seen', {})
                    self.user_last_welcomed = data.get('last_welcomed', {})
                    self.user_message_count = data.get('message_count', {})
                logger.info(f"📂 Loaded data for {len(self.user_last_seen)} users")
                loaded = True
        except json.JSONDecodeError as e:
            logger.error(f"⚠️ Corrupted main data file: {e}")
        except Exception as e:
            logger.error(f"❌ Failed to load main data file: {e}")
        
        # Fallback to backup file if main file failed
        if not loaded and os.path.exists(self.backup_file):
            try:
                with open(self.backup_file, 'r') as f:
                    data = json.load(f)
                    self.user_last_seen = data.get('last_seen', {})
                    self.user_last_welcomed = data.get('last_welcomed', {})
                    self.user_message_count = data.get('message_count', {})
                logger.info(f"🔄 Restored from backup: {len(self.user_last_seen)} users")
                # Try to repair main file
                self.save_user_data()
            except Exception as e:
                logger.error(f"❌ Failed to load backup file: {e}")
        
        # Initialize empty if both failed
        if not loaded and not os.path.exists(self.backup_file):
            self.user_last_seen = {}
            self.user_last_welcomed = {}
            self.user_message_count = {}
            logger.info("📝 Initialized with empty user data")
    
    def save_user_data(self, create_backup: bool = True):
        """Save user activity data to JSON file with error handling"""
        data = {
            'last_seen': self.user_last_seen,
            'last_welcomed': self.user_last_welcomed,
            'message_count': self.user_message_count,
            'timestamp': datetime.now().isoformat()
        }
        
        # Try saving to main file
        try:
            with open(self.user_data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"❌ Failed to save main data file: {e}")
            return False
        
        # Create backup if requested
        if create_backup:
            try:
                with open(self.backup_file, 'w') as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                logger.error(f"⚠️ Failed to create backup: {e}")
        
        return True
    
    def is_new_user(self, username: str) -> bool:
        """Check if user is new (less than threshold messages)"""
        return self.user_message_count.get(username, 0) < self.new_user_threshold
    
    def has_greeting_trigger(self, message: str) -> bool:
        """Check if message contains greeting trigger words"""
        message_lower = message.lower()
        return any(trigger in message_lower for trigger in self.greeting_triggers)
    
    def should_welcome(self, username: str, message: str) -> tuple[bool, str]:
        """
        Check if user should be welcomed
        Returns: (should_welcome: bool, reason: str)
        """
        try:
            now = datetime.now()
            
            # Check if user is new
            is_new = self.is_new_user(username)
            has_greeting = self.has_greeting_trigger(message)
            
            # First time user with greeting
            if username not in self.user_last_seen and has_greeting:
                return (True, "new_user_greeting")
            
            # New user (less than 3 messages)
            if is_new:
                return (True, "new_user")
            
            # User with greeting trigger word
            if has_greeting:
                # Check if we already welcomed them recently
                if username in self.user_last_welcomed:
                    last_welcomed = datetime.fromisoformat(self.user_last_welcomed[username])
                    if (now - last_welcomed).total_seconds() < 300:  # 5 minutes cooldown
                        return (False, "recently_welcomed")
                return (True, "greeting_trigger")
            
            # Check if user was away for a while
            if username in self.user_last_seen:
                last_seen = datetime.fromisoformat(self.user_last_seen[username])
                time_away = (now - last_seen).total_seconds()
                
                if time_away > self.welcome_back_after:
                    # Check if we already welcomed them recently
                    if username in self.user_last_welcomed:
                        last_welcomed = datetime.fromisoformat(self.user_last_welcomed[username])
                        if (now - last_welcomed).total_seconds() < 300:
                            return (False, "recently_welcomed")
                    return (True, "returning_user")
            
            return (False, "no_trigger")
            
        except Exception as e:
            logger.error(f"❌ Error in should_welcome for {username}: {e}")
            return (False, "error")
    
    async def process_youtube_message(self, username: str, message: str):
        """Process incoming YouTube chat message with AI analysis"""
        try:
            # Validate username
            if not username or not isinstance(username, str):
                logger.warning(f"⚠️ Invalid username received: {username}")
                return
            
            if not message or not isinstance(message, str):
                logger.warning(f"⚠️ Invalid message received from {username}")
                return
            
            now = datetime.now().isoformat()
            
            # Update message count
            self.user_message_count[username] = self.user_message_count.get(username, 0) + 1
            
            # Update last seen time
            self.user_last_seen[username] = now
            
            # Add to active users if not already
            if username not in self.active_users:
                self.active_users.add(username)
            
            # Check if we should welcome them
            should_greet, reason = self.should_welcome(username, message)
            
            if should_greet:
                logger.info(f"🎯 Welcoming {username} - Reason: {reason}")
                success = await self.send_welcome(username, message, reason)
                if success:
                    self.user_last_welcomed[username] = now
            
            # Save data periodically (but don't block on failure)
            try:
                self.save_user_data(create_backup=False)
            except Exception as e:
                logger.error(f"⚠️ Failed to save data: {e}")
                
        except Exception as e:
            logger.error(f"❌ Error processing message from {username}: {e}")
            self.error_count += 1
            self.last_error_time = datetime.now()
    
    async def send_message_with_retry(self, message: str, message_type: str = "general") -> bool:
        """Send message with retry logic and fallback"""
        for attempt in range(self.max_retries):
            try:
                # Check if chat monitor is available
                if not hasattr(self.bot, 'chat_monitor'):
                    logger.error("❌ Chat monitor not available")
                    break
                
                # Try sending the message
                await self.bot.chat_monitor.send_chat_message(message)
                logger.info(f"✅ Message sent: {message[:50]}...")
                return True
                
            except AttributeError as e:
                logger.error(f"❌ Chat monitor method not found: {e}")
                break
                
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Timeout on attempt {attempt + 1}/{self.max_retries}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    
            except Exception as e:
                logger.error(f"❌ Error sending message (attempt {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
        
        # All retries failed - add to queue if fallback is enabled
        if self.fallback_enabled and len(self.message_queue) < self.max_queue_size:
            self.message_queue.append({
                'message': message,
                'type': message_type,
                'timestamp': datetime.now().isoformat(),
                'attempts': 0
            })
            logger.info(f"📥 Message queued for retry")
        
        return False
    
    async def send_welcome(self, username: str, message: str, reason: str) -> bool:
        """Send welcome message with AI or fallback templates"""
        try:
            welcome_msg = None
            
            # Try AI-generated response first
            if self.ai_enabled and reason in ["new_user_greeting", "greeting_trigger"]:
                context_map = {
                    "new_user_greeting": f"This is {username}'s first time in chat and they're greeting us!",
                    "greeting_trigger": f"{username} is greeting the chat",
                    "new_user": f"{username} is new to the chat",
                    "returning_user": f"{username} is returning after being away"
                }
                
                context = context_map.get(reason, "User is in chat")
                welcome_msg = await self.generate_ai_response(username, message, context)
            
            # Fallback to templates if AI fails or not applicable
            if not welcome_msg:
                if reason == "new_user" or reason == "new_user_greeting":
                    templates = [
                        f"🎉 Welcome @{username}! Great to have you here with Rukiya!",
                        f"👋 Hey @{username}! Welcome to the stream! Rukiya is happy you're here!",
                        f"✨ Hi @{username}! Thanks for joining us! Rukiya welcomes you!",
                    ]
                elif reason == "greeting_trigger":
                    templates = [
                        f"👋 Hey @{username}! Rukiya says hi back!",
                        f"😊 Hello @{username}! Great to see you!",
                        f"🌟 Hi @{username}! Welcome!",
                    ]
                elif reason == "returning_user":
                    templates = [
                        f"👋 Welcome back @{username}! Rukiya missed you!",
                        f"🎊 @{username} is back! Rukiya is glad to see you again!",
                        f"✨ Hey @{username}! Nice to have you back!",
                    ]
                else:
                    templates = [f"👋 Hi @{username}!"]
                
                import random
                welcome_msg = random.choice(templates)
            
            # Send the message
            success = await self.send_message_with_retry(welcome_msg, "welcome")
            
            if success:
                logger.info(f"💬 Welcomed {username} ({reason})")
            else:
                logger.warning(f"⚠️ Failed to welcome {username}")
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Failed to send welcome for {username}: {e}")
            return False
    
    async def send_farewell(self, username: str) -> bool:
        """Send farewell message"""
        try:
            import random
            farewell_messages = [
                f"👋 See you later @{username}! Thanks for hanging out with Rukiya!",
                f"✨ Take care @{username}! Rukiya hopes to see you again soon!",
                f"🌟 Goodbye @{username}! Come back anytime!",
                f"💫 Catch you later @{username}! Rukiya enjoyed having you here!"
            ]
            
            farewell_msg = random.choice(farewell_messages)
            success = await self.send_message_with_retry(farewell_msg, "farewell")
            
            if success:
                logger.info(f"👋 Said farewell to: {username}")
            else:
                logger.warning(f"⚠️ Failed to send farewell to: {username}")
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Failed to send farewell for {username}: {e}")
            return False
    
    @tasks.loop(minutes=1)
    async def check_inactive_users(self):
        """Check for inactive users and say farewell"""
        try:
            now = datetime.now()
            users_to_remove = []
            
            for username in list(self.active_users):
                try:
                    if username in self.user_last_seen:
                        last_seen = datetime.fromisoformat(self.user_last_seen[username])
                        time_inactive = (now - last_seen).total_seconds()
                        
                        if time_inactive > self.farewell_after:
                            await self.send_farewell(username)
                            users_to_remove.append(username)
                except Exception as e:
                    logger.error(f"❌ Error checking user {username}: {e}")
            
            # Remove inactive users
            for username in users_to_remove:
                self.active_users.discard(username)
                
        except Exception as e:
            logger.error(f"❌ Error in check_inactive_users task: {e}")
    
    @check_inactive_users.before_loop
    async def before_check_inactive_users(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(minutes=5)
    async def retry_failed_messages(self):
        """Retry sending failed messages from queue"""
        if not self.message_queue:
            return
        
        try:
            logger.info(f"🔄 Retrying {len(self.message_queue)} queued messages")
            successful = []
            
            for item in self.message_queue[:10]:
                item['attempts'] += 1
                
                if item['attempts'] > 5:
                    logger.warning(f"⚠️ Giving up on message after 5 attempts")
                    successful.append(item)
                    continue
                
                try:
                    if hasattr(self.bot, 'chat_monitor'):
                        await self.bot.chat_monitor.send_chat_message(item['message'])
                        logger.info(f"✅ Successfully sent queued message")
                        successful.append(item)
                except Exception as e:
                    logger.error(f"❌ Failed to send queued message: {e}")
            
            for item in successful:
                self.message_queue.remove(item)
                
        except Exception as e:
            logger.error(f"❌ Error in retry_failed_messages task: {e}")
    
    @retry_failed_messages.before_loop
    async def before_retry_failed_messages(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(hours=1)
    async def auto_backup(self):
        """Automatically create backup of user data"""
        try:
            self.save_user_data(create_backup=True)
            logger.info("💾 Auto-backup completed")
        except Exception as e:
            logger.error(f"❌ Auto-backup failed: {e}")
    
    @auto_backup.before_loop
    async def before_auto_backup(self):
        await self.bot.wait_until_ready()
    
    @commands.command(name="welcomestats")
    @commands.has_permissions(administrator=True)
    async def welcome_stats(self, ctx):
        """Show welcome system statistics"""
        try:
            embed = discord.Embed(
                title="📊 Welcome System Statistics",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            # Calculate new users
            new_users = sum(1 for count in self.user_message_count.values() if count < self.new_user_threshold)
            
            embed.add_field(
                name="👥 Total Users",
                value=len(self.user_last_seen),
                inline=True
            )
            
            embed.add_field(
                name="🆕 New Users",
                value=new_users,
                inline=True
            )
            
            embed.add_field(
                name="🟢 Currently Active",
                value=len(self.active_users),
                inline=True
            )
            
            embed.add_field(
                name="📥 Queued Messages",
                value=len(self.message_queue),
                inline=True
            )
            
            embed.add_field(
                name="🤖 AI Status",
                value="✅ Enabled" if self.ai_enabled else "❌ Disabled",
                inline=True
            )
            
            embed.add_field(
                name="❌ Error Count",
                value=self.error_count,
                inline=True
            )
            
            embed.add_field(
                name="⏱️ Welcome Back Time",
                value=f"{self.welcome_back_after // 60} minutes",
                inline=True
            )
            
            embed.add_field(
                name="👋 Farewell Time",
                value=f"{self.farewell_after // 60} minutes",
                inline=True
            )
            
            embed.add_field(
                name="🎯 New User Threshold",
                value=f"{self.new_user_threshold} messages",
                inline=True
            )
            
            if self.last_error_time:
                embed.add_field(
                    name="🕐 Last Error",
                    value=self.last_error_time.strftime("%Y-%m-%d %H:%M:%S"),
                    inline=False
                )
            
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    
    @commands.command(name="addtrigger")
    @commands.has_permissions(administrator=True)
    async def add_trigger(self, ctx, *, trigger: str):
        """Add a new greeting trigger word"""
        try:
            trigger_lower = trigger.lower().strip()
            if trigger_lower not in self.greeting_triggers:
                self.greeting_triggers.append(trigger_lower)
                await ctx.send(f"✅ Added trigger: '{trigger_lower}'")
            else:
                await ctx.send(f"⚠️ Trigger '{trigger_lower}' already exists")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    
    @commands.command(name="listtriggers")
    @commands.has_permissions(administrator=True)
    async def list_triggers(self, ctx):
        """List all greeting trigger words"""
        try:
            triggers = ", ".join(self.greeting_triggers)
            await ctx.send(f"🎯 **Greeting Triggers:**\n{triggers}")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    
    @commands.command(name="toggleai")
    @commands.has_permissions(administrator=True)
    async def toggle_ai(self, ctx):
        """Toggle AI welcome messages"""
        try:
            self.ai_enabled = not self.ai_enabled
            status = "enabled" if self.ai_enabled else "disabled"
            await ctx.send(f"✅ AI welcome messages {status}")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    
    @commands.command(name="resetuser")
    @commands.has_permissions(administrator=True)
    async def reset_user(self, ctx, username: str):
        """Reset a user's tracking data"""
        try:
            if username in self.user_last_seen:
                del self.user_last_seen[username]
            if username in self.user_last_welcomed:
                del self.user_last_welcomed[username]
            if username in self.user_message_count:
                del self.user_message_count[username]
            
            self.save_user_data()
            await ctx.send(f"✅ Reset data for user: {username}")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

async def setup(bot):
    """Setup function to add the cog to the bot"""
    try:
        await bot.add_cog(Welcome(bot))
        logger.info("✅ Welcome cog added successfully")
    except Exception as e:
        logger.error(f"❌ Failed to add Welcome cog: {e}")
        raise
