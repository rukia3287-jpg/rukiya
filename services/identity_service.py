"""services/identity_service.py
Identity Resolution Service for Rukiya V2.

Resolves user identities across platforms (YouTube, Discord).
Priority:
1. Stable platform user ID (platform:user_id)
2. Verified stored identity mapping
3. Exact username mapping
4. Display name fallback
5. Unknown identity

Never merges two users solely because they have the same display name.

Explicit links (secondary -> primary) are persisted through MemoryService so they
survive restarts. Only stable platform IDs can be linked (Discord snowflakes and
YouTube channel IDs), only across platforms, and never in chains. Linking does not
move or merge stored memories: the secondary's rows stay under its own canonical ID
and become reachable again if the link is removed.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Optional
from services.models import ChatMessage, UserIdentity

logger = logging.getLogger(__name__)

# Stable, immutable platform account IDs. Links are only ever looked up for messages
# that carry a platform user ID, never for username/display-name fallback keys.
_LINKABLE_ID_PATTERNS = {
    "discord": re.compile(r"[0-9]{15,22}"),
    "youtube": re.compile(r"UC[A-Za-z0-9_-]{22}"),
}


def _linkable_platform(canonical_id: str) -> str:
    platform, sep, raw_id = (canonical_id or "").partition(":")
    pattern = _LINKABLE_ID_PATTERNS.get(platform)
    if not sep or pattern is None or not pattern.fullmatch(raw_id):
        raise ValueError(f"Not a stable platform account ID: {canonical_id!r}")
    return platform


class IdentityService:
    """Handles cross-platform identity resolution and mapping."""

    def __init__(self, memory_service=None):
        self.memory_service = memory_service
        # Verified links: secondary_canonical_id -> primary_canonical_id, loaded from storage.
        self._verified_mappings: Dict[str, str] = self._load_stored_links()
        # In-memory display name to canonical_id lookup cache (only used as fallback)
        self._display_name_lookup: Dict[str, str] = {}

    def _load_stored_links(self) -> Dict[str, str]:
        """Load stored links, re-validating each row; invalid rows are skipped, not trusted."""
        if not self.memory_service:
            return {}
        links: Dict[str, str] = {}
        for secondary, primary in self.memory_service.load_identity_links().items():
            try:
                if _linkable_platform(secondary) == _linkable_platform(primary):
                    raise ValueError("same platform")
            except ValueError:
                logger.warning("Ignoring invalid stored identity link row")
                continue
            links[secondary] = primary
        return links

    def link_identities(self, primary_canonical_id: str, secondary_canonical_id: str) -> bool:
        """Explicitly link two platform identities (e.g. owner links discord:123 to youtube:UC456).

        Returns True if the link was stored durably, False if it only applies to this
        process (no MemoryService, or the database is unavailable). Raises ValueError for
        invalid, same-platform, chained, or conflicting links.
        """
        primary_platform = _linkable_platform(primary_canonical_id)
        secondary_platform = _linkable_platform(secondary_canonical_id)
        if primary_platform == secondary_platform:
            raise ValueError("Identity links must join accounts on different platforms")

        existing = self._verified_mappings.get(secondary_canonical_id)
        if existing is not None and existing != primary_canonical_id:
            raise ValueError(f"{secondary_canonical_id} is already linked to {existing}; unlink it first")
        if primary_canonical_id in self._verified_mappings:
            raise ValueError(f"{primary_canonical_id} is itself a linked secondary; links cannot be chained")
        if secondary_canonical_id in self._verified_mappings.values():
            raise ValueError(f"{secondary_canonical_id} is the primary of another link; links cannot be chained")

        persisted = False
        if self.memory_service:
            try:
                persisted = self.memory_service.save_identity_link(secondary_canonical_id, primary_canonical_id)
            except ValueError:
                # Another process stored a different link first; adopt what storage holds.
                self._verified_mappings = self._load_stored_links()
                raise
        self._verified_mappings[secondary_canonical_id] = primary_canonical_id
        # The account pairing itself is personal data: keep it out of INFO logs.
        logger.info("Identity link created (persisted=%s)", persisted)
        logger.debug("Linked identity: %s -> %s", secondary_canonical_id, primary_canonical_id)
        return persisted

    def unlink_identity(self, secondary_canonical_id: str) -> bool:
        """Remove an explicit link. Returns True if the removal was stored durably.

        The link stops applying in this process immediately. If the stored row could
        not be removed (False), calling this again retries the removal. Raises KeyError
        if the link is unknown both here and in storage.
        """
        primary = self._verified_mappings.pop(secondary_canonical_id, None)
        if self.memory_service is None:
            if primary is None:
                raise KeyError(secondary_canonical_id)
            return False
        if primary is None and secondary_canonical_id not in self.memory_service.load_identity_links():
            raise KeyError(secondary_canonical_id)
        persisted = self.memory_service.delete_identity_link(secondary_canonical_id)
        logger.info("Identity link removed (persisted=%s)", persisted)
        logger.debug("Unlinked identity: %s", secondary_canonical_id)
        return persisted

    def resolve(self, message: ChatMessage) -> UserIdentity:
        """Resolve a ChatMessage to a canonical UserIdentity."""
        platform = message.platform.lower().strip()
        raw_uid = (message.user_id or "").strip()
        username = (message.username or "").strip()
        display_name = (message.display_name or "").strip()

        # Step 1: Stable platform user ID
        if raw_uid:
            raw_canonical = f"{platform}:{raw_uid}"
        elif username:
            raw_canonical = f"{platform}:{username}"
        elif display_name:
            # Clean display name fallback key (namespaced to avoid collision with genuine IDs)
            sanitized = re.sub(r"[^\w\-]", "", display_name).lower() or "anonymous"
            raw_canonical = f"{platform}:dn_{sanitized}"
        else:
            raw_canonical = f"{platform}:unknown"

        # Step 2: Check verified stored mapping. Only messages carrying a platform user ID
        # may follow a link; username/display-name fallback keys are mutable labels.
        canonical_id = self._verified_mappings.get(raw_canonical, raw_canonical) if raw_uid else raw_canonical
        linked = canonical_id != raw_canonical

        # Check persistent storage if available
        if self.memory_service:
            existing = self.memory_service.get_user(canonical_id)
            if existing:
                # Update mutable profile fields only. last_seen is intentionally
                # updated by MemoryService.record_interaction() AFTER the DecisionService
                # evaluates continuity, otherwise every message appears brand new.
                if display_name and display_name != existing.display_name:
                    existing.display_name = display_name
                if username and username != existing.username:
                    existing.username = username
                self.memory_service.save_user(existing)
                return existing

        # Create new identity. Through a link, the profile belongs to the primary account,
        # so its platform and user ID come from the primary canonical ID.
        profile_platform, profile_uid = canonical_id.split(":", 1) if linked else (platform, raw_uid)
        user_identity = UserIdentity(
            canonical_id=canonical_id,
            platform=profile_platform,
            user_id=profile_uid or (f"dn_{display_name}" if display_name else "unknown"),
            username=username or display_name or "viewer",
            display_name=display_name or username or "viewer",
            first_seen=message.timestamp,
            last_seen=message.timestamp,
            interaction_count=0,
            is_welcomed_in_stream=False
        )

        if self.memory_service:
            self.memory_service.save_user(user_identity)

        # Track display name in fallback lookup if not already claimed
        if display_name and display_name.lower() not in self._display_name_lookup:
            self._display_name_lookup[display_name.lower()] = canonical_id

        return user_identity
