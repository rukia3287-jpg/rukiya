import discord
from discord.ext import commands, tasks
import logging
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import asyncio

logger = logging.getLogger(__name__)

class Welcome(commands.Cog):
    """Cog for welcoming and saying farewell to users in YouTube chat"""
    
    def __init__(self, bot):
        self.bot = bot
        self.user_data_file = "user_activity.json"
        self.backup_file = "user_activity_backup.json"
        self.user_last_seen: Dict[str, str] = {}  # username -> last_seen timestamp
        self.user_last_welcomed: Dict[str, str] = {}  # username -> last_welcomed timestamp
        self.active_users: set = set()  # Currently active users
        
        # Time thresholds (in seconds)
        self.welcome_back_after = 3600  # Welcome back after 1 hour of absence
        self.farewell_after = 600  # Say farewell after 10 minutes of inactivity
        
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
        
        # Initialize data
        self.load_user_data()
        
        # Start background tasks
        self.check_inactive_users.start()
        self.retry_failed_messages.start()
        self.auto_backup.start()
        
        logger.info("✅ Welcome cog initialized with fallback system")
    
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
                logger.info(f"🔄 Restored from backup: {len(self.user_last_seen)} users")
                # Try to repair main file
                self.save_user_data()
            except Exception as e:
                logger.error(f"❌ Failed to load backup file: {e}")
        
        # Initialize empty if both failed
        if not loaded and not os.path.exists(self.backup_file):
            self.user_last_seen = {}
            self.user_last_welcomed = {}
            logger.info("📝 Initialized with empty user data")
    
    def save_user_data(self, create_backup: bool = True):
        """Save user activity data to JSON file with error handling"""
        data = {
            'last_seen': self.user_last_seen,
            'last_welcomed': self.user_last_welcomed,
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
    
    def should_welcome(self, username: str) -> bool:
        """Check if user should be welcomed with error handling"""
        try:
            now = datetime.now()
            
            # First time user
            if username not in self.user_last_seen:
                return True
            
            # Check if user was away for a while
            last_seen = datetime.fromisoformat(self.user_last_seen[username])
            time_away = (now - last_seen).total_seconds()
            
            if time_away > self.welcome_back_after:
                # Check if we already welcomed them recently
                if username in self.user_last_welcomed:
                    last_welcomed = datetime.fromisoformat(self.user_last_welcomed[username])
                    if (now - last_welcomed).total_seconds() < 60:  # Don't spam welcomes
                        return False
                return True
            
            return False
        except Exception as e:
            logger.error(f"❌ Error in should_welcome for {username}: {e}")
            return False
    
    async def process_youtube_message(self, username: str, message: str):
        """Process incoming YouTube chat message with error handling"""
        try:
            now = datetime.now().isoformat()
            
            # Validate username
            if not username or not isinstance(username, str):
                logger.warning(f"⚠️ Invalid username received: {username}")
                return
            
            # Update last seen time
            self.user_last_seen[username] = now
            
            # Add to active users
            if username not in self.active_users:
                self.active_users.add(username)
                
                # Check if we should welcome them
                if self.should_welcome(username):
                    success = await self.send_welcome(username)
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
                logger.info(f"✅ Message sent successfully: {message[:50]}...")
                return True
                
            except AttributeError as e:
                logger.error(f"❌ Chat monitor method not found: {e}")
                break  # Don't retry if method doesn't exist
                
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
            logger.info(f"📥 Message queued for retry: {message[:50]}...")
        
        return False
    
    async def send_welcome(self, username: str) -> bool:
        """Send welcome message to YouTube chat with error handling"""
        try:
            # Check if user is first time or returning
            if username not in self.user_last_welcomed:
                welcome_msg = f"🎉 Welcome @{username}! Great to have you here with Rukiya!"
            else:
                welcome_msg = f"👋 Welcome back @{username}! Rukiya is glad to see you again!"
            
            success = await self.send_message_with_retry(welcome_msg, "welcome")
            
            if success:
                logger.info(f"💬 Welcomed user: {username}")
            else:
                logger.warning(f"⚠️ Failed to welcome user: {username}")
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Failed to send welcome message for {username}: {e}")
            return False
    
    async def send_farewell(self, username: str) -> bool:
        """Send farewell message to YouTube chat with error handling"""
        try:
            import random
            farewell_messages = [
                f"👋 See you later @{username}! Thanks for hanging out with Rukiya!",
                f"✨ Take care @{username}! Rukiya hopes to see you again soon!",
                f"🌟 Goodbye @{username}! Come back anytime!"
            ]
            
            farewell_msg = random.choice(farewell_messages)
            success = await self.send_message_with_retry(farewell_msg, "farewell")
            
            if success:
                logger.info(f"👋 Said farewell to user: {username}")
            else:
                logger.warning(f"⚠️ Failed to send farewell to: {username}")
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Failed to send farewell message for {username}: {e}")
            return False
    
    @tasks.loop(minutes=1)
    async def check_inactive_users(self):
        """Check for inactive users and say farewell with error handling"""
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
        """Wait until bot is ready"""
        await self.bot.wait_until_ready()
    
    @tasks.loop(minutes=5)
    async def retry_failed_messages(self):
        """Retry sending failed messages from queue"""
        if not self.message_queue:
            return
        
        try:
            logger.info(f"🔄 Retrying {len(self.message_queue)} queued messages")
            successful = []
            
            for item in self.message_queue[:10]:  # Process up to 10 at a time
                item['attempts'] += 1
                
                # Give up after 5 attempts
                if item['attempts'] > 5:
                    logger.warning(f"⚠️ Giving up on message after 5 attempts: {item['message'][:50]}")
                    successful.append(item)
                    continue
                
                # Try sending again
                try:
                    if hasattr(self.bot, 'chat_monitor'):
                        await self.bot.chat_monitor.send_chat_message(item['message'])
                        logger.info(f"✅ Successfully sent queued message")
                        successful.append(item)
                except Exception as e:
                    logger.error(f"❌ Failed to send queued message: {e}")
            
            # Remove successful messages from queue
            for item in successful:
                self.message_queue.remove(item)
                
        except Exception as e:
            logger.error(f"❌ Error in retry_failed_messages task: {e}")
    
    @retry_failed_messages.before_loop
    async def before_retry_failed_messages(self):
        """Wait until bot is ready"""
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
        """Wait until bot is ready"""
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
            
            embed.add_field(
                name="👥 Total Users Tracked",
                value=len(self.user_last_seen),
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
                name="⏱️ Welcome Back Threshold",
                value=f"{self.welcome_back_after // 60} minutes",
                inline=True
            )
            
            embed.add_field(
                name="👋 Farewell Threshold",
                value=f"{self.farewell_after // 60} minutes",
                inline=True
            )
            
            embed.add_field(
                name="❌ Error Count",
                value=self.error_count,
                inline=True
            )
            
            if self.last_error_time:
                embed.add_field(
                    name="🕐 Last Error",
                    value=self.last_error_time.strftime("%Y-%m-%d %H:%M:%S"),
                    inline=False
                )
            
            embed.add_field(
                name="🔄 Fallback Status",
                value="✅ Enabled" if self.fallback_enabled else "❌ Disabled",
                inline=True
            )
            
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Error showing stats: {e}")
            logger.error(f"❌ Error in welcomestats command: {e}")
    
    @commands.command(name="setwelcometime")
    @commands.has_permissions(administrator=True)
    async def set_welcome_time(self, ctx, minutes: int):
        """Set the welcome back threshold in minutes"""
        try:
            if minutes < 1:
                await ctx.send("❌ Time must be at least 1 minute!")
                return
            
            self.welcome_back_after = minutes * 60
            await ctx.send(f"✅ Welcome back threshold set to {minutes} minutes")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            logger.error(f"❌ Error in setwelcometime command: {e}")
    
    @commands.command(name="setfarewelltime")
    @commands.has_permissions(administrator=True)
    async def set_farewell_time(self, ctx, minutes: int):
        """Set the farewell threshold in minutes"""
        try:
            if minutes < 1:
                await ctx.send("❌ Time must be at least 1 minute!")
                return
            
            self.farewell_after = minutes * 60
            await ctx.send(f"✅ Farewell threshold set to {minutes} minutes")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            logger.error(f"❌ Error in setfarewelltime command: {e}")
    
    @commands.command(name="togglefallback")
    @commands.has_permissions(administrator=True)
    async def toggle_fallback(self, ctx):
        """Toggle fallback system on/off"""
        try:
            self.fallback_enabled = not self.fallback_enabled
            status = "enabled" if self.fallback_enabled else "disabled"
            await ctx.send(f"✅ Fallback system {status}")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            logger.error(f"❌ Error in togglefallback command: {e}")
    
    @commands.command(name="clearqueue")
    @commands.has_permissions(administrator=True)
    async def clear_queue(self, ctx):
        """Clear the message queue"""
        try:
            count = len(self.message_queue)
            self.message_queue.clear()
            await ctx.send(f"✅ Cleared {count} messages from queue")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            logger.error(f"❌ Error in clearqueue command: {e}")
    
    @commands.command(name="backupdata")
    @commands.has_permissions(administrator=True)
    async def backup_data(self, ctx):
        """Manually create a backup of user data"""
        try:
            success = self.save_user_data(create_backup=True)
            if success:
                await ctx.send("✅ Backup created successfully")
            else:
                await ctx.send("❌ Failed to create backup")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            logger.error(f"❌ Error in backupdata command: {e}")

async def setup(bot):
    """Setup function to add the cog to the bot"""
    try:
        await bot.add_cog(Welcome(bot))
        logger.info("✅ Welcome cog added successfully")
    except Exception as e:
        logger.error(f"❌ Failed to add Welcome cog: {e}")
        raise
