import random
import logging
import asyncio
from datetime import datetime, timedelta
from collections import deque
from discord.ext import commands

logger = logging.getLogger(__name__)

class Shayari(commands.Cog):
    """Advanced cog for generating shayaris in YouTube chat with AI integration"""
    
    def __init__(self, bot):
        self.bot = bot
        self.youtube = bot.youtube_service
        self.chat_monitor = bot.chat_monitor
        self.ai = bot.ai_service
        
        # Rate limiting to prevent spam
        self.last_shayari_time = None
        self.min_shayari_interval = 5  # Minimum 5 seconds between shayaris
        self.user_cooldowns = {}  # {user: timestamp} - per-user cooldown
        self.user_cooldown_duration = timedelta(seconds=30)  # 30 sec per user
        
        # AI configuration
        self.ai_enabled = True
        self.ai_timeout = 4.0  # 4 second timeout for AI generation
        self.ai_failure_count = 0
        self.max_ai_failures = 3  # Disable AI temporarily after 3 failures
        self.ai_cooldown_until = None
        
        # AI response caching to reduce API calls
        self.ai_cache = deque(maxlen=30)  # Cache last 30 AI shayaris
        self.cache_hit_count = 0
        
        # Trigger patterns (case-insensitive)
        self.triggers = [
            "ru shayari",
            "shayari suna",
            "shayari sunao",
            "koi shayari",
            "ek shayari"
        ]
        
        # Tiered fallback system - Premium quality shayaris
        self.premium_shayaris = [
            "Khwaab woh jo neend mein aaye,\nSapne woh jo dil ko bhaaye,\nZindagi mein ek hi armaan hai,\nTumhare saath har pal bitaaye 💫",
            
            "Manzil mil jaaye ya na mile,\nRaaste chalte rahenge hum,\nHar mushkil ko muskaan se paar karenge,\nYeh hausla kabhi nahi tootega 🔥",
            
            "Rishte woh jo dil se bante hain,\nWaqt se nahi, jazbaat se bante hain,\nDosti woh mithaas hai,\nJo zindagi ko khoobsurat banaati hai 🌸",
            
            "Subah ki pehli kiran ho tum,\nRaat ka aakhri taara ho tum,\nZindagi ki har khushi mein,\nMera sabse pyaara sahaara ho tum ✨",
            
            "Duniya ki reet alag hai,\nDil ki baat alag hai,\nSach mein jeene ka andaaz,\nBas apno ka saath alag hai 💝"
        ]
        
        # Standard quality shayaris
        self.standard_shayaris = [
            "Dosti mein na koi din hota hai,\nNa koi raat hoti hai,\nBas ek dost hota hai,\nJo hamesha saath hota hai 💕",
            
            "Zindagi ek kitaab hai,\nHar din ek naya panna,\nKuch dard likhe hote hain,\nAur kuch khushiyon ka afsana 📖",
            
            "Raat ka chaand tumhaari roshni ho,\nSubah ka suraj tumhaari khushi ho,\nDua hai meri Rab se,\nZindagi mein sirf muskaan tumhaari hansi ho 🌞",
            
            "Waqt ke saath sab badal jaata hai,\nPar dosti ka rishta nahi badalta,\nChahe kitni bhi door ho jaaye,\nDil mein jagah kabhi kam nahi hoti 🫂",
            
            "Khushiyon ki baraat ho zindagi,\nGham ka koi asar na ho,\nHar pal mein pyaar ho,\nDukh ka koi shehar na ho 🎊"
        ]
        
        # Simple emergency fallbacks
        self.simple_shayaris = [
            "Zindagi ek safar hai suhana,\nYahan kal kya ho kisne jaana 🌈",
            
            "Muskurahat se sab kuch haseen ho jaata hai,\nDil ka dard bhi kam ho jaata hai 😊",
            
            "Dost woh jo mushkil mein saath de,\nWoh rishta sabse khaas hai 🤝"
        ]
        
        # Subscribe to YouTube chat messages
        self.chat_monitor.subscribe(self.on_chat_message)
        
        # Start background cleanup task
        self.cleanup_task = asyncio.create_task(self._cleanup_old_cooldowns())
        logger.info("✅ Shayari cog initialized with advanced features")
    
    def _check_trigger(self, message: str) -> bool:
        """Check if message contains any shayari trigger"""
        msg_lower = message.lower().strip()
        return any(trigger in msg_lower for trigger in self.triggers)
    
    def _can_send_shayari(self, user: str = None) -> tuple[bool, str]:
        """
        Check if shayari can be sent (rate limiting)
        Returns: (can_send: bool, reason: str)
        """
        # Global rate limiting
        if self.last_shayari_time:
            elapsed = (datetime.now() - self.last_shayari_time).total_seconds()
            if elapsed < self.min_shayari_interval:
                return False, f"global_cooldown ({self.min_shayari_interval - elapsed:.1f}s remaining)"
        
        # Per-user cooldown
        if user and user in self.user_cooldowns:
            last_request = self.user_cooldowns[user]
            elapsed = datetime.now() - last_request
            if elapsed < self.user_cooldown_duration:
                remaining = (self.user_cooldown_duration - elapsed).total_seconds()
                return False, f"user_cooldown ({remaining:.1f}s remaining)"
        
        return True, "allowed"
    
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
                logger.info("🔄 AI cooldown expired, re-enabling AI shayaris")
        
        return True
    
    async def _generate_ai_shayari(self) -> str:
        """Generate AI shayari with timeout and error handling"""
        try:
            # Check cache first for variety (20% reuse chance)
            if self.ai_cache and random.random() < 0.2:
                cached = random.choice(self.ai_cache)
                self.cache_hit_count += 1
                logger.debug(f"💾 Cache hit #{self.cache_hit_count}: Using cached AI shayari")
                return cached
            
            # Generate fresh AI shayari with timeout
            themes = [
                "friendship and loyalty",
                "life's journey and hope",
                "love and emotions",
                "motivation and success",
                "happiness and celebration",
                "memories and nostalgia"
            ]
            theme = random.choice(themes)
            
            prompt = (
                f"Write a beautiful 4-line Hindi shayari in Hinglish (Hindi words in English script) "
                f"on the theme of '{theme}'. Make it poetic, emotional, and simple to understand. "
                f"Include 1-2 relevant emojis at the end. Each line should be around 8-12 words. "
                f"Format: Line 1\\nLine 2\\nLine 3\\nLine 4 [emoji]"
            )
            
            shayari = await asyncio.wait_for(
                self.ai.generate_text(prompt),
                timeout=self.ai_timeout
            )
            
            if not shayari or len(shayari.strip()) < 20:
                raise ValueError("Invalid AI response: too short or empty")
            
            # Clean up the response
            shayari = shayari.strip()
            
            # Cache the response
            if shayari not in self.ai_cache:
                self.ai_cache.append(shayari)
            
            # Reset failure count on success
            self.ai_failure_count = 0
            
            return shayari
            
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ AI timeout for shayari generation, using fallback")
            self._handle_ai_failure()
            return None
        except Exception as e:
            logger.error(f"❌ AI shayari generation failed: {e}")
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
    
    def _get_fallback_shayari(self, tier: int = 1) -> str:
        """Get fallback shayari based on tier (1=premium, 2=standard, 3=simple)"""
        if tier == 1:
            return random.choice(self.premium_shayaris)
        elif tier == 2:
            return random.choice(self.standard_shayaris)
        else:
            return random.choice(self.simple_shayaris)
    
    async def on_chat_message(self, message: str, author: str):
        """Handle shayari requests in YouTube chat"""
        # Check if message contains trigger
        if not self._check_trigger(message):
            return
        
        user = author.strip()
        
        # Check rate limits
        can_send, reason = self._can_send_shayari(user)
        if not can_send:
            logger.debug(f"⏱️ Shayari request from {user} blocked: {reason}")
            return
        
        shayari = None
        fallback_tier = 1
        
        try:
            # Try AI generation first if available
            if self._is_ai_available():
                logger.debug(f"🤖 Attempting AI shayari generation for {user}")
                shayari = await self._generate_ai_shayari()
                
                if shayari:
                    logger.info(f"✨ AI Shayari sent for {user}")
                else:
                    fallback_tier = 1  # Use premium fallback
            else:
                logger.debug(f"⚠️ AI unavailable, using fallback tier {fallback_tier}")
                fallback_tier = 2  # Use standard fallback
            
            # Use fallback if AI failed or unavailable
            if not shayari:
                shayari = self._get_fallback_shayari(fallback_tier)
                logger.info(f"🎯 Fallback Shayari (tier {fallback_tier}) sent for {user}")
            
            # Send the shayari
            await self.youtube.send_message(shayari)
            
            # Update rate limit tracking
            self.last_shayari_time = datetime.now()
            if user:
                self.user_cooldowns[user] = datetime.now()
            
        except Exception as e:
            # Final emergency fallback
            logger.error(f"❌ Critical error sending shayari for {user}: {e}")
            try:
                emergency_shayari = self._get_fallback_shayari(3)
                await self.youtube.send_message(emergency_shayari)
                self.last_shayari_time = datetime.now()
                logger.info(f"🆘 Emergency fallback shayari sent for {user}")
            except Exception as critical_error:
                logger.critical(f"💥 Failed to send any shayari to {user}: {critical_error}")
    
    async def _cleanup_old_cooldowns(self):
        """Background task to clean up expired user cooldowns"""
        while True:
            try:
                await asyncio.sleep(300)  # Run every 5 minutes
                
                now = datetime.now()
                expired = [
                    user for user, timestamp in self.user_cooldowns.items()
                    if now - timestamp > self.user_cooldown_duration
                ]
                
                for user in expired:
                    del self.user_cooldowns[user]
                
                if expired:
                    logger.info(f"🧹 Cleaned up {len(expired)} expired user cooldowns")
                
            except Exception as e:
                logger.error(f"❌ Error in cleanup task: {e}")
    
    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()
        logger.info("👋 Shayari cog unloaded")
    
    @commands.command(name="shayaristats")
    @commands.has_permissions(administrator=True)
    async def shayari_stats(self, ctx):
        """Display shayari system statistics (Admin only)"""
        cooldown_status = "None"
        if self.ai_cooldown_until and datetime.now() < self.ai_cooldown_until:
            remaining = (self.ai_cooldown_until - datetime.now()).total_seconds()
            cooldown_status = f"Active ({remaining:.0f}s remaining)"
        
        stats = (
            f"📜 **Shayari System Stats**\n"
            f"└ AI enabled: {'✅' if self.ai_enabled else '❌'}\n"
            f"└ AI failures: {self.ai_failure_count}/{self.max_ai_failures}\n"
            f"└ Cache hits: {self.cache_hit_count}\n"
            f"└ Cache size: {len(self.ai_cache)}/30\n"
            f"└ Active user cooldowns: {len(self.user_cooldowns)}\n"
            f"└ AI cooldown: {cooldown_status}\n"
            f"└ Triggers: {', '.join(self.triggers)}"
        )
        await ctx.send(stats)
    
    @commands.command(name="addshayaritrigger")
    @commands.has_permissions(administrator=True)
    async def add_trigger(self, ctx, *, trigger: str):
        """Add a new shayari trigger (Admin only)"""
        trigger = trigger.lower().strip()
        if trigger not in self.triggers:
            self.triggers.append(trigger)
            await ctx.send(f"✅ Added new trigger: `{trigger}`")
            logger.info(f"➕ New shayari trigger added: {trigger}")
        else:
            await ctx.send(f"⚠️ Trigger already exists: `{trigger}`")

async def setup(bot):
    await bot.add_cog(Shayari(bot))
