import random
import logging
import asyncio
from datetime import datetime, timedelta
from collections import deque
from discord.ext import commands

logger = logging.getLogger(__name__)

class WelcomeMessages(commands.Cog):
    """Advanced cog for welcoming new viewers in YouTube chat with AI integration"""
    
    def __init__(self, bot):
        self.bot = bot
        self.youtube = bot.youtube_service
        self.chat_monitor = bot.chat_monitor
        self.ai = bot.ai_service
        
        # Track greeted users with timestamps for session management
        self.greeted_users = {}  # {user: timestamp}
        self.session_timeout = timedelta(hours=2)  # Reset after 2 hours
        
        # AI response caching to reduce API calls
        self.ai_cache = deque(maxlen=20)  # Cache last 20 AI responses
        self.cache_hit_count = 0
        
        # Rate limiting to prevent spam
        self.last_welcome_time = None
        self.min_welcome_interval = 3  # Minimum 3 seconds between welcomes
        
        # AI configuration
        self.ai_enabled = True
        self.ai_timeout = 3.0  # 3 second timeout for AI generation
        self.ai_failure_count = 0
        self.max_ai_failures = 3  # Disable AI temporarily after 3 failures
        self.ai_cooldown_until = None
        
        # Tiered fallback system
        self.premium_welcomes = [
            "Arre {user}, aap aa gaye! Stream ab complete ho gayi 🎉",
            "Welcome {user}, aapka intezaar tha! Enjoy the vibes ✨",
            "{user} ji, swagat hai! Baith jao aaram se 🪑",
            "Dekho kaun aaya - {user}! Finally stream interesting ho gayi 😎",
            "Ayy {user}, perfect timing! Ab maza aayega 🔥"
        ]
        
        self.standard_welcomes = [
            "Arre {user}, finally aa gaye tum! 👋",
            "Swagat hai {user}, chat ab zinda lag rahi hai 😏",
            "{user} aa gaye, ab maja aayega! 🔥",
            "Welcome {user}, bas tumhari hi kami thi 😎",
            "Oho {user}, entry maarte hi dhamaka! 💥"
        ]
        
        self.simple_welcomes = [
            "Welcome {user}! 👋",
            "Hey {user}, enjoy the stream! 🎉",
            "{user} joined! 😊",
            "Namaste {user}! 🙏",
            "Hi {user}, glad you're here! ✨"
        ]
        
        # Subscribe to YouTube chat messages
        self.chat_monitor.subscribe(self.on_chat_message)
        
        # Start background cleanup task
        self.cleanup_task = asyncio.create_task(self._cleanup_old_users())
        logger.info("✅ WelcomeMessages cog initialized with advanced features")
    
    def _should_welcome(self, user: str) -> bool:
        """Check if user should be welcomed based on multiple criteria"""
        # Check if user was already greeted recently
        if user in self.greeted_users:
            last_greeted = self.greeted_users[user]
            if datetime.now() - last_greeted < self.session_timeout:
                return False
        
        # Rate limiting - prevent spam
        if self.last_welcome_time:
            elapsed = (datetime.now() - self.last_welcome_time).total_seconds()
            if elapsed < self.min_welcome_interval:
                logger.debug(f"⏱️ Rate limit: Skipping welcome for {user}")
                return False
        
        return True
    
    def _is_ai_available(self) -> bool:
        """Check if AI service is available and not in cooldown"""
        if not self.ai_enabled:
            return False
        
        # Check if AI is in cooldown due to failures
        if self.ai_cooldown_until:
            if datetime.now() < self.ai_cooldown_until:
                return False
            else:
                # Cooldown expired, reset failure count
                self.ai_cooldown_until = None
                self.ai_failure_count = 0
                logger.info("🔄 AI cooldown expired, re-enabling AI welcomes")
        
        return True
    
    async def _generate_ai_welcome(self, user: str) -> str:
        """Generate AI welcome with timeout and error handling"""
        try:
            # Check cache first for similar patterns
            if self.ai_cache and random.random() < 0.3:  # 30% chance to reuse
                cached = random.choice(self.ai_cache).format(user=user)
                self.cache_hit_count += 1
                logger.debug(f"💾 Cache hit #{self.cache_hit_count}: Using cached AI response")
                return cached
            
            # Generate fresh AI response with timeout
            prompt = (
                f"Generate a short, friendly, and funny Hinglish welcome message for a YouTube "
                f"chat user named '{user}'. Use casual Indian slang, emojis, and keep it under 12 words. "
                f"Make it unique and engaging. Don't use offensive language."
            )
            
            welcome_msg = await asyncio.wait_for(
                self.ai.generate_text(prompt),
                timeout=self.ai_timeout
            )
            
            if not welcome_msg or len(welcome_msg.strip()) < 5:
                raise ValueError("Invalid AI response: too short or empty")
            
            # Cache the response template (replace username with placeholder)
            template = welcome_msg.replace(user, "{user}")
            if template not in self.ai_cache:
                self.ai_cache.append(template)
            
            # Reset failure count on success
            self.ai_failure_count = 0
            
            return welcome_msg.strip()
            
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ AI timeout for {user}, using fallback")
            self._handle_ai_failure()
            return None
        except Exception as e:
            logger.error(f"❌ AI generation failed for {user}: {e}")
            self._handle_ai_failure()
            return None
    
    def _handle_ai_failure(self):
        """Handle AI failures and implement cooldown if needed"""
        self.ai_failure_count += 1
        
        if self.ai_failure_count >= self.max_ai_failures:
            # Put AI in cooldown for 5 minutes
            self.ai_cooldown_until = datetime.now() + timedelta(minutes=5)
            logger.warning(
                f"⚠️ AI disabled temporarily due to {self.ai_failure_count} failures. "
                f"Cooldown until {self.ai_cooldown_until.strftime('%H:%M:%S')}"
            )
    
    def _get_fallback_welcome(self, user: str, tier: int = 1) -> str:
        """Get fallback welcome message based on tier (1=premium, 2=standard, 3=simple)"""
        if tier == 1:
            return random.choice(self.premium_welcomes).format(user=user)
        elif tier == 2:
            return random.choice(self.standard_welcomes).format(user=user)
        else:
            return random.choice(self.simple_welcomes).format(user=user)
    
    async def on_chat_message(self, message: str, author: str):
        """Handle incoming chat messages and send welcomes for new users"""
        user = author.strip()
        
        # Validate user
        if not user or len(user) < 2:
            return
        
        # Check if should welcome
        if not self._should_welcome(user):
            return
        
        welcome_msg = None
        fallback_tier = 1
        
        try:
            # Try AI generation first if available
            if self._is_ai_available():
                logger.debug(f"🤖 Attempting AI welcome for {user}")
                welcome_msg = await self._generate_ai_welcome(user)
                
                if welcome_msg:
                    logger.info(f"✨ AI Welcome: {user} → {welcome_msg}")
                else:
                    fallback_tier = 1  # Use premium fallback
            else:
                logger.debug(f"⚠️ AI unavailable, using fallback tier {fallback_tier}")
                fallback_tier = 2  # Use standard fallback
            
            # Use fallback if AI failed or unavailable
            if not welcome_msg:
                welcome_msg = self._get_fallback_welcome(user, fallback_tier)
                logger.info(f"🎯 Fallback Welcome (tier {fallback_tier}): {user} → {welcome_msg}")
            
            # Send the message
            await self.youtube.send_message(welcome_msg)
            
            # Update tracking
            self.greeted_users[user] = datetime.now()
            self.last_welcome_time = datetime.now()
            
        except Exception as e:
            # Final emergency fallback
            logger.error(f"❌ Critical error welcoming {user}: {e}")
            try:
                emergency_msg = self._get_fallback_welcome(user, 3)
                await self.youtube.send_message(emergency_msg)
                self.greeted_users[user] = datetime.now()
                logger.info(f"🆘 Emergency fallback used for {user}")
            except Exception as critical_error:
                logger.critical(f"💥 Failed to send any welcome to {user}: {critical_error}")
    
    async def _cleanup_old_users(self):
        """Background task to clean up old greeted users"""
        while True:
            try:
                await asyncio.sleep(600)  # Run every 10 minutes
                
                now = datetime.now()
                expired = [
                    user for user, timestamp in self.greeted_users.items()
                    if now - timestamp > self.session_timeout
                ]
                
                for user in expired:
                    del self.greeted_users[user]
                
                if expired:
                    logger.info(f"🧹 Cleaned up {len(expired)} expired user sessions")
                
            except Exception as e:
                logger.error(f"❌ Error in cleanup task: {e}")
    
    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()
        logger.info("👋 WelcomeMessages cog unloaded")
    
    @commands.command(name="welcomestats")
    @commands.has_permissions(administrator=True)
    async def welcome_stats(self, ctx):
        """Display welcome system statistics (Admin only)"""
        stats = (
            f"📊 **Welcome System Stats**\n"
            f"└ Greeted users: {len(self.greeted_users)}\n"
            f"└ AI enabled: {'✅' if self.ai_enabled else '❌'}\n"
            f"└ AI failures: {self.ai_failure_count}\n"
            f"└ Cache hits: {self.cache_hit_count}\n"
            f"└ Cache size: {len(self.ai_cache)}/20\n"
            f"└ Cooldown: {'Active' if self.ai_cooldown_until and datetime.now() < self.ai_cooldown_until else 'None'}"
        )
        await ctx.send(stats)

async def setup(bot):
    await bot.add_cog(WelcomeMessages(bot))
