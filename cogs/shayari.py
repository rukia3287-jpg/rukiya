import random
import logging
import asyncio
from datetime import datetime, timedelta
from discord.ext import commands

logger = logging.getLogger(__name__)

class Shayari(commands.Cog):
    """Cog for sending shayaris in YouTube chat"""
    
    def __init__(self, bot):
        self.bot = bot
        self.youtube = bot.youtube_service
        self.chat_monitor = bot.chat_monitor
        
        # Rate limiting to prevent spam
        self.last_shayari_time = None
        self.min_shayari_interval = 5  # Minimum 5 seconds between shayaris
        self.user_cooldowns = {}  # {user: timestamp} - per-user cooldown
        self.user_cooldown_duration = timedelta(seconds=30)  # 30 sec per user
        
        # Trigger patterns (case-insensitive)
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
        
        # Premium quality shayaris
        self.shayaris = [
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
            
            "Dil ke jazbaat ko lafzon se kahoon kaise,\nPyaar ka izhaar kru toh kru kaise.\nWo samne ho aur chup sa mai rahu,\nIn aankhon ki zuban se usse samjhau kaise 👀",
            
            "Dosti mein na koi din hota hai,\nNa koi raat hoti hai,\nBas ek dost hota hai,\nJo hamesha saath hota hai 💕",
            
            "Zindagi ek kitaab hai,\nHar din ek naya panna,\nKuch dard likhe hote hain,\nAur kuch khushiyon ka afsana 📖",
            
            "Raat ka chaand tumhaari roshni ho,\nSubah ka suraj tumhaari khushi ho,\nDua hai meri Rab se,\nZindagi mein sirf muskaan tumhaari hansi ho 🌞",
            
            "Waqt ke saath sab badal jaata hai,\nPar dosti ka rishta nahi badalta,\nChahe kitni bhi door ho jaaye,\nDil mein jagah kabhi kam nahi hoti 🫂",
            
            "Khushiyon ki baraat ho zindagi,\nGham ka koi asar na ho,\nHar pal mein pyaar ho,\nDukh ka koi shehar na ho 🎊",
            
            "Zindagi ek safar hai suhana,\nYahan kal kya ho kisne jaana 🌈",
            
            "Muskurahat se sab kuch haseen ho jaata hai,\nDil ka dard bhi kam ho jaata hai 😊",
            
            "Dost woh jo mushkil mein saath de,\nWoh rishta sabse khaas hai 🤝",
            
            "Yaadon ka silsila chalta rahe,\nDil mein teri tasveer basi rahe,\nChahe kitna bhi waqt guzar jaye,\nTeri yaad mein yeh dil hasi rahe 🌺",
            
            "Zindagi ne sikhaya hai,\nHar ghum ke baad khushi aati hai,\nRaat kitni bhi andheri ho,\nSubah zaroor phir se aati hai 🌅"
        ]
        
        # Subscribe to YouTube chat messages
        self.chat_monitor.subscribe(self.on_chat_message)
        
        # Start background cleanup task
        self.cleanup_task = asyncio.create_task(self._cleanup_old_cooldowns())
        logger.info("✅ Shayari cog initialized")
    
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
    
    async def _send_youtube_message(self, message: str) -> bool:
        """Safely send a message to YouTube chat with validation"""
        try:
            # Validate the message
            if not message or not isinstance(message, str):
                logger.error(f"❌ Invalid message type or empty: {type(message)}")
                return False
            
            # Validate youtube service exists
            if not self.youtube:
                logger.error("❌ YouTube service not initialized")
                return False
            
            # Check if send_message exists
            if not hasattr(self.youtube, 'send_message'):
                logger.error("❌ YouTube service missing send_message method")
                return False
            
            # Try direct call first (normal instance method)
            try:
                send_method = self.youtube.send_message
                if asyncio.iscoroutinefunction(send_method):
                    await send_method(message)
                else:
                    send_method(message)
                return True
            except TypeError as type_err:
                # If that fails, self.youtube might be a class, not instance
                # Try calling as unbound method
                logger.debug(f"Direct call failed with {type_err}, trying unbound method pattern")
                send_method = self.youtube.send_message
                if asyncio.iscoroutinefunction(send_method):
                    await send_method(self.youtube, message)
                else:
                    send_method(self.youtube, message)
                return True
            
        except TypeError as e:
            logger.error(f"❌ Method signature error: {e}")
            logger.debug(f"YouTube service type: {type(self.youtube)}")
            logger.debug(f"send_message type: {type(getattr(self.youtube, 'send_message', None))}")
            return False
        except Exception as e:
            logger.error(f"❌ Failed to send message: {e}")
            import traceback
            logger.debug(f"Full traceback: {traceback.format_exc()}")
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
        
        try:
            # Get random shayari
            shayari = random.choice(self.shayaris)
            logger.info(f"📜 Sending shayari for {user}")
            
            # Send the shayari using safe wrapper
            success = await self._send_youtube_message(shayari)
            
            if success:
                # Update rate limit tracking only if successful
                self.last_shayari_time = datetime.now()
                if user:
                    self.user_cooldowns[user] = datetime.now()
                logger.info(f"✅ Shayari sent successfully for {user}")
            else:
                logger.warning(f"⚠️ Failed to send shayari to {user}")
            
        except Exception as e:
            logger.error(f"❌ Critical error sending shayari for {user}: {e}")
    
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
        stats = (
            f"📜 **Shayari System Stats**\n"
            f"└ Total shayaris: {len(self.shayaris)}\n"
            f"└ Active user cooldowns: {len(self.user_cooldowns)}\n"
            f"└ Triggers: {', '.join(self.triggers[:5])}...\n"
            f"└ Global cooldown: {self.min_shayari_interval}s\n"
            f"└ User cooldown: {self.user_cooldown_duration.total_seconds():.0f}s"
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
        """Test shayari selection (Admin only)"""
        shayari = random.choice(self.shayaris)
        await ctx.send(f"✅ Random Shayari:\n\n{shayari}")
    
    @commands.command(name="addshayari")
    @commands.has_permissions(administrator=True)
    async def add_shayari(self, ctx, *, shayari: str):
        """Add a new shayari to the collection (Admin only)"""
        if shayari and len(shayari) > 10:
            self.shayaris.append(shayari)
            await ctx.send(f"✅ Added new shayari! Total: {len(self.shayaris)}")
            logger.info(f"➕ New shayari added")
        else:
            await ctx.send("❌ Shayari too short. Please provide a complete shayari.")

async def setup(bot):
    await bot.add_cog(Shayari(bot))
