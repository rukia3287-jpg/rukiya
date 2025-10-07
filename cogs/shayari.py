import random
import logging
import asyncio
from datetime import datetime, timedelta
from collections import deque
from discord.ext import commands
import google.generativeai as genai
import os

logger = logging.getLogger(__name__)

class Shayari(commands.Cog):
    """Advanced cog for generating shayaris in YouTube chat with Gemini AI integration"""
    
    def __init__(self, bot):
        self.bot = bot
        self.youtube = bot.youtube_service
        self.chat_monitor = bot.chat_monitor
        
        # Initialize Gemini AI (Flash 2.0)
        self._init_gemini()
        
        # Rate limiting to prevent spam
        self.last_shayari_time = None
        self.min_shayari_interval = 5  # Minimum 5 seconds between shayaris
        self.user_cooldowns = {}  # {user: timestamp} - per-user cooldown
        self.user_cooldown_duration = timedelta(seconds=30)  # 30 sec per user
        
        # AI configuration
        self.ai_enabled = True
        self.ai_timeout = 5.0  # 5 second timeout for AI generation (shayaris need more time)
        self.ai_failure_count = 0
        self.max_ai_failures = 3  # Disable AI temporarily after 3 failures
        self.ai_cooldown_until = None
        
        # AI response caching to reduce API calls
        self.ai_cache = deque(maxlen=30)  # Cache last 30 AI shayaris
        self.cache_hit_count = 0
        
        # Trigger patterns (case-insensitive) - Added 'ru' prefix
        self.triggers = [
            "ru shayari",
            "ru shayri",
            "shayari suna",
            "shayari sunao",
            "koi shayari",
            "ek shayari",
            "shayri suna",
            "shayri sunao"
        ]
        
        # Tiered fallback system - Premium quality shayaris
        self.premium_shayaris = [
            "Khwaab woh jo neend mein aaye,\nSapne woh jo dil ko bhaaye,\nZindagi mein ek hi armaan hai,\nTumhare saath har pal bitaaye 💫",
            
            "Manzil mil jaaye ya na mile,\nRaaste chalte rahenge hum,\nHar mushkil ko muskaan se paar karenge,\nYeh hausla kabhi nahi tootega 🔥",
            
            "Rishte woh jo dil se bante hain,\nWaqt se nahi, jazbaat se bante hain,\nDosti woh mithaas hai,\nJo zindagi ko khoobsurat banaati hai 🌸",
            
            "Subah ki pehli kiran ho tum,\nRaat ka aakhri taara ho tum,\nZindagi ki har khushi mein,\nMera sabse pyaara sahaara ho tum ✨",
        
            "Duniya ki reet alag hai,\nDil ki baat alag hai,\nSach mein jeene ka andaaz,\nBas apno ka saath alag hai 💝",
            
            "Chaha tha usse dil se, par voh kabhi samjhi nahi,\nHar dafa chuna kisi aur ko, jaise main kahin tha hi nahi.\nMain chahta hoon sirf usko, har pal, har saans mein,\nKash ek baar keh de voh… 'main bhi chahti hoon tujhe, har ek aas mein.' 💔",
            
            "Tabah hokar bhi Tabahi nhi Dikhti,\nYeh Ishq hai Huzoor iski koi Davai nhi Bikti,\nDard sambhale hain seene mein chup chap,\nAankhon mein chehra tera, dil mein teri kami nhi Bikti 😔",
            
            "Tere saath ki chaah mein khud ko khona pada,\nSach bolna tha jurm, isliye chup rehna pada.\nNa hasi bachi, na aansuon ka dastaan raha,\nTujhse door jaake bhi tera hi hona pada. 🥀",
            
            "Khamoshi se matlab nahi, Matlab toh baaton ka hai,\nDin toh guzar hi jaata hai, Masla to raaton ka hai.\nWaqt se zyada takleef in yaadon ki hai,\nJo reh gayi hain beech dil ke kone aur raaton ka hai 🌙",
            
            "Khuda ke saaye mein socha kab tak sahenge,\nSabar ke daaman ko aur kitna thaamenge?\nDard ne har shabd lafz mein rang bhar diya,\nKhamoshi poochti rahi, jawab kahaan mile? 🙏",
            
            "Mohabbat na hoti to ghazal kaun likhta,\nKeechad mein khile us phool ko kamal kaun kehta.\nPyaar to kudrat ka karishma hai,\nWarna laash ke ghar ko Taj Mahal kaun kehta 🌹",
            
            "Akela tha, akela rahe gaya,\nAb aadat hi ban gaya chhupane ki...\nDost banane ka junoon gum ho gaya,\nNa baat ho kisi se, dil udaas ho gaya 😞",
            
            "Na main kahani, na koi kirdar,\nBas ek mohra, bas ek intezar.\nJab zaroorat thi tab kiya yaad,\nAb bhool gaye jaise bekaar 🎭",
            
            "Hum bhi Bazaar mein the, tum bhi bazaar mein the,\nHam pe bhi phool baras rahe the, tum pe bhi phool baras rahe the,\nBas farq itna hain, tum doli se aa rahe the, ham janaze se ja rahe the. ⚰️",
            
            "Dil ke jazbaat ko lafzon se kahoon kaise,\nPyaar ka izhaar kru toh kru kaise.\nWo samne ho aur chup sa mai rahu,\nIn aankhon ki zuban se usse samjhau kaise 👀"
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
        logger.info("✅ Shayari cog initialized with Gemini Flash 2.0")
    
    def _init_gemini(self):
        """Initialize Gemini AI with Flash 2.0 model for shayari generation"""
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
            
            # Initialize Flash 2.0 model with optimized settings for creative shayaris
            generation_config = {
                "temperature": 1.0,  # Maximum creativity for poetic content
                "top_p": 0.95,
                "top_k": 40,
                "max_output_tokens": 200,  # Longer for 4-line shayaris
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
            
            logger.info("✅ Gemini Flash 2.0 initialized successfully for shayari generation")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Gemini: {e}")
            self.gemini_model = None
            self.ai_enabled = False
    
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
                logger.info("🔄 AI cooldown expired, re-enabling Gemini shayaris")
        
        return True
    
    async def _generate_ai_shayari(self) -> str:
        """Generate AI shayari using Gemini Flash 2.0 with timeout and error handling"""
        try:
            # Check cache first for variety (20% reuse chance)
            if self.ai_cache and random.random() < 0.2:
                cached = random.choice(self.ai_cache)
                self.cache_hit_count += 1
                logger.debug(f"💾 Cache hit #{self.cache_hit_count}: Using cached AI shayari")
                return cached
            
            # Generate fresh AI shayari with timeout
            themes = [
                "dosti aur wafadari (friendship and loyalty)",
                "zindagi ka safar aur umeed (life's journey and hope)",
                "mohabbat aur jazbaat (love and emotions)",
                "hausla aur kamyabi (motivation and success)",
                "khushi aur jashn (happiness and celebration)",
                "yaadein aur nostalgia (memories and nostalgia)",
                "dard-e-dil aur tanhai (heartbreak and loneliness)",
                "pyaar mein dhoka (betrayal in love)",
                "khwaab aur armaan (dreams and aspirations)"
            ]
            theme = random.choice(themes)
            
            prompt = (
                f"Write a beautiful 4-line Hindi/Urdu shayari in Hinglish (Hindi/Urdu words written in English script) "
                f"on the theme of '{theme}'.\n\n"
                f"Requirements:\n"
                f"- Write EXACTLY 4 lines, each line should rhyme or flow poetically\n"
                f"- Make it emotional, deep, and touching\n"
                f"- Use simple yet poetic Hinglish words\n"
                f"- Each line should be 8-15 words\n"
                f"- Add 1-2 relevant emojis at the very end\n"
                f"- Make it feel authentic like classic Urdu/Hindi poetry\n"
                f"- Use words like: dil, mohabbat, zindagi, khwaab, yaad, dard, etc.\n\n"
                f"Format:\n"
                f"Line 1,\n"
                f"Line 2,\n"
                f"Line 3,\n"
                f"Line 4 [emoji]\n\n"
                f"Example style:\n"
                f"Khwaab woh jo neend mein aaye,\n"
                f"Sapne woh jo dil ko bhaaye,\n"
                f"Zindagi mein ek hi armaan hai,\n"
                f"Tumhare saath har pal bitaaye 💫\n\n"
                f"Now write a NEW shayari (don't repeat the example):"
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
                shayari = response.text.strip()
                
                # Validate response
                if len(shayari) < 20 or len(shayari) > 500:
                    raise ValueError(f"Invalid shayari length: {len(shayari)}")
                
                # Remove quotes if Gemini wrapped the shayari
                shayari = shayari.strip('"\'')
                
                # Remove any extra explanations or notes from Gemini
                if '\n\n' in shayari:
                    shayari = shayari.split('\n\n')[0]
                
                # Cache the response
                if shayari not in self.ai_cache:
                    self.ai_cache.append(shayari)
                
                # Reset failure count on success
                self.ai_failure_count = 0
                
                return shayari
            else:
                raise ValueError("Empty response from Gemini")
            
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ Gemini timeout for shayari generation, using fallback")
            self._handle_ai_failure()
            return None
        except Exception as e:
            logger.error(f"❌ Gemini shayari generation failed: {e}")
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
    
    def _get_fallback_shayari(self, tier: int = 1) -> str:
        """Get fallback shayari based on tier (1=premium, 2=standard, 3=simple)"""
        if tier == 1:
            return random.choice(self.premium_shayaris)
        elif tier == 2:
            return random.choice(self.standard_shayaris)
        else:
            return random.choice(self.simple_shayaris)
    
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
                logger.debug(f"🤖 Attempting Gemini shayari generation for {user}")
                shayari = await self._generate_ai_shayari()
                
                if shayari:
                    logger.info(f"✨ Gemini Shayari sent for {user}")
                else:
                    fallback_tier = 1  # Use premium fallback
            else:
                logger.debug(f"⚠️ Gemini unavailable, using fallback tier {fallback_tier}")
                fallback_tier = 2  # Use standard fallback
            
            # Use fallback if AI failed or unavailable
            if not shayari:
                shayari = self._get_fallback_shayari(fallback_tier)
                logger.info(f"🎯 Fallback Shayari (tier {fallback_tier}) sent for {user}")
            
            # Send the shayari using safe wrapper
            success = await self._send_youtube_message(shayari)
            
            if success:
                # Update rate limit tracking only if successful
                self.last_shayari_time = datetime.now()
                if user:
                    self.user_cooldowns[user] = datetime.now()
            else:
                logger.warning(f"⚠️ Failed to send shayari to {user}")
            
        except Exception as e:
            # Final emergency fallback
            logger.error(f"❌ Critical error sending shayari for {user}: {e}")
            try:
                emergency_shayari = self._get_fallback_shayari(3)
                success = await self._send_youtube_message(emergency_shayari)
                
                if success:
                    self.last_shayari_time = datetime.now()
                    logger.info(f"🆘 Emergency fallback shayari sent for {user}")
                else:
                    logger.critical(f"💥 Emergency fallback also failed for {user}")
                    
            except Exception as critical_error:
                logger.critical(f"💥 All fallback attempts failed for {user}: {critical_error}")
    
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
        
        ai_status = "✅ Gemini Flash 2.0" if self.ai_enabled and self.gemini_model else "❌ Disabled"
        
        stats = (
            f"📜 **Shayari System Stats**\n"
            f"└ AI: {ai_status}\n"
            f"└ AI failures: {self.ai_failure_count}/{self.max_ai_failures}\n"
            f"└ Cache hits: {self.cache_hit_count}\n"
            f"└ Cache size: {len(self.ai_cache)}/30\n"
            f"└ Active user cooldowns: {len(self.user_cooldowns)}\n"
            f"└ AI cooldown: {cooldown_status}\n"
            f"└ Triggers: {', '.join(self.triggers[:5])}..."
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
    
    @commands.command(name="testshayari")
    @commands.has_permissions(administrator=True)
    async def test_shayari(self, ctx):
        """Test Gemini shayari generation (Admin only)"""
        if not self._is_ai_available():
            await ctx.send("❌ Gemini AI is not available or in cooldown")
            return
        
        await ctx.send("🤖 Generating shayari using Gemini Flash 2.0...")
        
        shayari = await self._generate_ai_shayari()
        
        if shayari:
            await ctx.send(f"✅ Generated:\n\n{shayari}")
        else:
            await ctx.send("❌ Failed to generate shayari")

async def setup(bot):
    await bot.add_cog(Shayari(bot))
