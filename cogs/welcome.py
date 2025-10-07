import random
import logging
import asyncio
from datetime import datetime, timedelta
from collections import deque
from discord.ext import commands
import google.generativeai as genai
import os

logger = logging.getLogger(__name__)

class WelcomeMessages(commands.Cog):
    """Advanced cog for welcoming new viewers in YouTube chat with Gemini AI integration"""
    
    def __init__(self, bot):
        self.bot = bot
        self.youtube = bot.youtube_service
        self.chat_monitor = bot.chat_monitor
        
        # Initialize Gemini AI (Flash 2.0)
        self._init_gemini()
        
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
        logger.info("✅ WelcomeMessages cog initialized with Gemini Flash 2.0")
    
    def _init_gemini(self):
        """Initialize Gemini AI with Flash 2.0 model"""
        try:
            # Get API key from environment variable
            api_key = os.getenv('GEMINI_API_KEY')
            
            if not api_key:
                logger.warning("⚠️ GEMINI_API_KEY not found in environment variables")
                self.gemini_model = None
                self.ai_enabled = False
                return
            
            # Configure Gemini
            genai.configure(api_key=api_key)
            
            # Initialize Flash 2.0 model with optimized settings
            generation_config = {
                "temperature": 0.9,  # Higher creativity for fun welcomes
                "top_p": 0.95,
                "top_k": 40,
                "max_output_tokens": 100,  # Short welcome messages
                "response_mime_type": "text/plain",
            }
            
            safety_settings = [
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
            ]
            
            self.gemini_model = genai.GenerativeModel(
                model_name="gemini-2.0-flash-exp",  # Flash 2.0 model
                generation_config=generation_config,
                safety_settings=safety_settings,
            )
            
            logger.info("✅ Gemini Flash 2.0 initialized successfully")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Gemini: {e}")
            self.gemini_model = None
            self.ai_enabled = False
    
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
        if not self.ai_enabled or not self.gemini_model:
            return False
        
        # Check if AI is in cooldown due to failures
        if self.ai_cooldown_until:
            if datetime.now() < self.ai_cooldown_until:
                return False
            else:
                # Cooldown expired, reset failure count
                self.ai_cooldown_until = None
                self.ai_failure_count = 0
                logger.info("🔄 AI cooldown expired, re-enabling Gemini welcomes")
        
        return True
    
    async def _generate_ai_welcome(self, user: str) -> str:
        """Generate AI welcome using Gemini Flash 2.0 with timeout and error handling"""
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
                f"livestream chat. The user's name is '{user}'. "
                f"Requirements:\n"
                f"- Use casual Indian slang and mix of Hindi and English\n"
                f"- Include 1-2 emojis\n"
                f"- Keep it under 12 words\n"
                f"- Make it unique, engaging, and fun\n"
                f"- Don't use offensive language\n"
                f"- Examples style: 'Arre {user}, finally! Stream ab complete 🎉' or 'Welcome {user} ji, perfect timing! 🔥'\n\n"
                f"Just give the welcome message, nothing else."
            )
            
            # Run Gemini generation in executor to make it async
            loop = asyncio.get_event_loop()
            
            async def generate_with_timeout():
                return await loop.run_in_executor(
                    None,
                    lambda: self.gemini_model.generate_content(prompt)
                )
            
            response = await asyncio.wait_for(
                generate_with_timeout(),
                timeout=self.ai_timeout
            )
            
            # Extract text from response
            if response and response.text:
                welcome_msg = response.text.strip()
                
                # Validate response
                if len(welcome_msg) < 5 or len(welcome_msg) > 200:
                    raise ValueError(f"Invalid response length: {len(welcome_msg)}")
                
                # Remove quotes if Gemini wrapped the message
                welcome_msg = welcome_msg.strip('"\'')
                
                # Cache the response template (replace username with placeholder)
                template = welcome_msg.replace(user, "{user}")
                if template not in self.ai_cache:
                    self.ai_cache.append(template)
                
                # Reset failure count on success
                self.ai_failure_count = 0
                
                return welcome_msg
            else:
                raise ValueError("Empty response from Gemini")
            
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ Gemini timeout for {user}, using fallback")
            self._handle_ai_failure()
            return None
        except Exception as e:
            logger.error(f"❌ Gemini generation failed for {user}: {e}")
            self._handle_ai_failure()
            return None
    
    def _handle_ai_failure(self):
        """Handle AI failures and implement cooldown if needed"""
        self.ai_failure_count += 1
        
        if self.ai_failure_count >= self.max_ai_failures:
            # Put AI in cooldown for 5 minutes
            self.ai_cooldown_until = datetime.now() + timedelta(minutes=5)
            logger.warning(
                f"⚠️ Gemini disabled temporarily due to {self.ai_failure_count} failures. "
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
    
    async def _send_youtube_message(self, message: str) -> bool:
        """Safely send a message to YouTube chat with validation"""
        try:
            # Validate the message
            if not message or not isinstance(message, str):
                logger.error(f"❌ Invalid message type or empty: {type(message)}")
                return False
            
            # Validate youtube service exists and is callable
            if not self.youtube:
                logger.error("❌ YouTube service not initialized")
                return False
            
            # Check if send_message is a method
            if not hasattr(self.youtube, 'send_message'):
                logger.error("❌ YouTube service missing send_message method")
                return False
            
            # Call the method - handle both async and sync versions
            send_method = getattr(self.youtube, 'send_message')
            if asyncio.iscoroutinefunction(send_method):
                await send_method(message)
            else:
                send_method(message)
            
            return True
            
        except TypeError as e:
            # This catches the "missing positional argument" error
            logger.error(f"❌ Method signature error: {e}")
            logger.debug(f"YouTube service type: {type(self.youtube)}")
            logger.debug(f"send_message type: {type(getattr(self.youtube, 'send_message', None))}")
            return False
        except Exception as e:
            logger.error(f"❌ Failed to send message: {e}")
            return False
    
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
                logger.debug(f"🤖 Attempting Gemini welcome for {user}")
                welcome_msg = await self._generate_ai_welcome(user)
                
                if welcome_msg:
                    logger.info(f"✨ Gemini Welcome: {user} → {welcome_msg}")
                else:
                    fallback_tier = 1  # Use premium fallback
            else:
                logger.debug(f"⚠️ Gemini unavailable, using fallback tier {fallback_tier}")
                fallback_tier = 2  # Use standard fallback
            
            # Use fallback if AI failed or unavailable
            if not welcome_msg:
                welcome_msg = self._get_fallback_welcome(user, fallback_tier)
                logger.info(f"🎯 Fallback Welcome (tier {fallback_tier}): {user} → {welcome_msg}")
            
            # Send the message using the safe wrapper
            success = await self._send_youtube_message(welcome_msg)
            
            if success:
                # Update tracking only if message was sent successfully
                self.greeted_users[user] = datetime.now()
                self.last_welcome_time = datetime.now()
            else:
                logger.warning(f"⚠️ Failed to send welcome to {user}, will retry on next message")
            
        except Exception as e:
            # Final emergency fallback
            logger.error(f"❌ Critical error welcoming {user}: {e}")
            try:
                emergency_msg = self._get_fallback_welcome(user, 3)
                success = await self._send_youtube_message(emergency_msg)
                
                if success:
                    self.greeted_users[user] = datetime.now()
                    logger.info(f"🆘 Emergency fallback used for {user}")
                else:
                    logger.critical(f"💥 Emergency fallback also failed for {user}")
                    
            except Exception as critical_error:
                logger.critical(f"💥 All fallback attempts failed for {user}: {critical_error}")
    
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
        ai_status = "✅ Gemini Flash 2.0" if self.ai_enabled and self.gemini_model else "❌ Disabled"
        stats = (
            f"📊 **Welcome System Stats**\n"
            f"└ Greeted users: {len(self.greeted_users)}\n"
            f"└ AI: {ai_status}\n"
            f"└ AI failures: {self.ai_failure_count}\n"
            f"└ Cache hits: {self.cache_hit_count}\n"
            f"└ Cache size: {len(self.ai_cache)}/20\n"
            f"└ Cooldown: {'Active' if self.ai_cooldown_until and datetime.now() < self.ai_cooldown_until else 'None'}"
        )
        await ctx.send(stats)
    
    @commands.command(name="testgemini")
    @commands.has_permissions(administrator=True)
    async def test_gemini(self, ctx, *, username: str = "TestUser"):
        """Test Gemini AI welcome generation (Admin only)"""
        if not self._is_ai_available():
            await ctx.send("❌ Gemini AI is not available or in cooldown")
            return
        
        await ctx.send(f"🤖 Generating welcome for '{username}' using Gemini Flash 2.0...")
        
        welcome = await self._generate_ai_welcome(username)
        
        if welcome:
            await ctx.send(f"✅ Generated: {welcome}")
        else:
            await ctx.send("❌ Failed to generate welcome")

async def setup(bot):
    await bot.add_cog(WelcomeMessages(bot))
