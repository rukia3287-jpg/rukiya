"""Private, self-service Brain controls; global operator permissions stay separate."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.access import operator_only, operator_callback
from services.brain import STYLES
from services.discord_ui import defer_response, send_response
from services.models import ChatMessage
from services.scopes import persistent_scope


class BrainCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _owner(self, interaction):
        await defer_response(interaction, ephemeral=True)
        message = ChatMessage('discord', str(interaction.id), str(interaction.user.id),
                              interaction.user.display_name, interaction.user.display_name, '',
                              guild_id=str(interaction.guild_id) if interaction.guild_id else None,
                              channel_id=str(interaction.channel_id))
        orchestrator = self.bot.orchestrator
        user = await orchestrator.memory_service.run_async(orchestrator.identity_service.resolve, message)
        return orchestrator, user, message

    async def _cancel(self, orchestrator, user, message):
        await orchestrator.brain.cancel(message.platform, message.user_id, message.guild_id)
        engine = orchestrator.ai_engine
        if engine is not None and hasattr(engine, 'clear_user_cache'):
            await engine.clear_user_cache(user.canonical_id)

    @app_commands.command(name='memory_preferences', description='Set your history, fact extraction and external AI choices in this scope')
    async def preferences(self, interaction: discord.Interaction, history: bool, automatic_facts: bool, external_ai: bool):
        orchestrator, user, message = await self._owner(interaction)
        await self._cancel(orchestrator, user, message)
        memory = orchestrator.memory_service
        prefs = await memory.run_async(memory.get_brain_preferences, user.canonical_id)
        saved = await memory.run_async(memory.set_brain_preferences, user.canonical_id,
                                       history_enabled=history, auto_memory=automatic_facts,
                                       external_allowed=external_ai, style=prefs['style'])
        await send_response(interaction, 'Preferences saved in this scope.' if saved else 'Preferences apply in this process; persistence is unavailable. Retry after storage recovers.', ephemeral=True)

    @app_commands.command(name='my_memory', description='Privately inspect your approved facts and privacy settings in this scope')
    async def memory(self, interaction: discord.Interaction):
        orchestrator, user, _ = await self._owner(interaction)
        memory = orchestrator.memory_service
        prefs = await memory.run_async(memory.get_brain_preferences, user.canonical_id)
        facts = await memory.run_async(memory.get_all_user_memories, user.canonical_id)
        visible = [fact for fact in facts if prefs['auto_memory'] or fact.source == 'user_approved']
        header = f"Persistent history: {prefs['history_enabled']}\nAutomatic facts: {prefs['auto_memory']}\nExternal AI: {prefs['external_allowed']}\nStyle: {prefs['style']}\n"
        text = header + '\n'.join(f'{fact.key}: {fact.value}' for fact in visible)
        # Private file avoids silently dropping any of the 30 bounded facts.
        if len(text) > 1900:
            import io
            await send_response(interaction, 'Your memory export:', file=discord.File(io.BytesIO(text.encode()), filename='my_memory.txt'), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await send_response(interaction, text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='remember', description='Explicitly approve or correct one fact about yourself in this scope')
    async def remember(self, interaction: discord.Interaction, key: str, value: str):
        orchestrator, user, message = await self._owner(interaction)
        if not key.strip() or not value.strip() or len(key) > 64 or len(value) > 1000:
            await send_response(interaction, 'Use a key of 1–64 characters and a value of 1–1000 characters.', ephemeral=True)
            return
        await self._cancel(orchestrator, user, message)
        memory = orchestrator.memory_service
        await memory.run_async(memory.clear_brain_history, user.canonical_id)
        await memory.run_async(memory.set_user_memory, user.canonical_id, key, value, source='user_approved')
        saved = await memory.run_async(memory.persistence_available)
        await send_response(interaction, 'Approved memory saved.' if saved else 'Approved memory is temporary until storage recovers.', ephemeral=True)

    @app_commands.command(name='forget_memory', description='Delete one of your facts and clear conversation context in this scope')
    async def forget(self, interaction: discord.Interaction, key: str):
        orchestrator, user, message = await self._owner(interaction)
        await self._cancel(orchestrator, user, message)
        memory = orchestrator.memory_service
        saved = await memory.run_async(memory.forget_brain_fact, user.canonical_id, key)
        await send_response(interaction, 'Memory deleted and conversation context cleared.' if saved else 'Deletion applies now and is queued for storage recovery.', ephemeral=True)

    @app_commands.command(name='clear_my_memory', description='Erase your facts, conversation context and feedback in this scope')
    async def clear(self, interaction: discord.Interaction):
        orchestrator, user, message = await self._owner(interaction)
        await self._cancel(orchestrator, user, message)
        memory = orchestrator.memory_service
        saved = await memory.run_async(memory.clear_brain_user, user.canonical_id)
        await send_response(interaction, 'Your facts, history and feedback were erased; privacy choices were retained.' if saved else 'Erasure applies in memory now and is queued for storage recovery.', ephemeral=True)

    @app_commands.command(name='chat_style', description='Choose adaptive, casual, friendly, playful, sarcastic, serious, technical, helpful or concise')
    async def style(self, interaction: discord.Interaction, style: str):
        orchestrator, user, _ = await self._owner(interaction)
        if style not in STYLES:
            await send_response(interaction, 'Supported styles: ' + ', '.join(STYLES), ephemeral=True)
            return
        memory = orchestrator.memory_service
        prefs = await memory.run_async(memory.get_brain_preferences, user.canonical_id)
        saved = await memory.run_async(memory.set_brain_preferences, user.canonical_id,
                                       history_enabled=prefs['history_enabled'], auto_memory=prefs['auto_memory'],
                                       external_allowed=prefs['external_allowed'], style=style)
        await send_response(interaction, 'Style saved.' if saved else 'Style applies temporarily; storage is unavailable.', ephemeral=True)

    @app_commands.command(name='cancel_ai', description='Cancel your active Rukiya requests in this server or DM scope')
    async def cancel(self, interaction: discord.Interaction):
        orchestrator, user, message = await self._owner(interaction)
        await self._cancel(orchestrator, user, message)
        await send_response(interaction, 'Active requests stopped; already transmitted platform messages cannot be recalled.', ephemeral=True)

    @app_commands.command(name='ai_feedback', description='Give a usefulness signal without storing conversation contents')
    async def feedback(self, interaction: discord.Interaction, helpful: bool):
        orchestrator, user, _ = await self._owner(interaction)
        memory = orchestrator.memory_service
        saved = await memory.run_async(memory.add_brain_feedback, user.canonical_id, helpful)
        await send_response(interaction, 'Feedback saved.' if saved else 'Storage is unavailable; feedback was not saved.', ephemeral=True)

    @app_commands.command(name='knowledge_add', description='Operator: add curated public knowledge for this server or DM scope')
    @operator_only()
    @operator_callback
    async def knowledge(self, interaction: discord.Interaction, title: str, text: str):
        orchestrator, _, message = await self._owner(interaction)
        scope = persistent_scope(message)
        memory = orchestrator.memory_service
        saved = await memory.run_async(memory.add_brain_knowledge, scope, title, text)
        await send_response(interaction, 'Knowledge saved for this scope. Query it with `knowledge: <words>`.' if saved else 'Knowledge was not saved.', ephemeral=True)


async def setup(bot):
    await bot.add_cog(BrainCommands(bot))
