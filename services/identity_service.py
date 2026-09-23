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
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Optional
from services.models import ChatMessage, UserIdentity

logger = logging.getLogger(__name__)


class IdentityService:
    """Handles cross-platform identity resolution and mapping."""

    def __init__(self, memory_service=None):
        self.memory_service = memory_service
        # In-memory fast mapping cache for verified links: secondary_canonical_id -> primary_canonical_id
        self._verified_mappings: Dict[str, str] = {}
        # In-memory display name to canonical_id lookup cache (only used as fallback)
        self._display_name_lookup: Dict[str, str] = {}

    def link_identities(self, primary_canonical_id: str, secondary_canonical_id: str) -> None:
        """Explicitly link two platform identities (e.g. owner links discord:123 to youtube:UC456)."""
        self._verified_mappings[secondary_canonical_id] = primary_canonical_id
        logger.info("Linked identity: %s -> %s", secondary_canonical_id, primary_canonical_id)

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

        # Step 2: Check verified stored mapping
        canonical_id = self._verified_mappings.get(raw_canonical, raw_canonical)

        # Check persistent storage if available
        if self.memory_service:
            existing = self.memory_service.get_user(canonical_id)
            if existing:
                # Update display name / username if changed, update last seen
                existing.last_seen = message.timestamp
                if display_name and display_name != existing.display_name:
                    existing.display_name = display_name
                if username and username != existing.username:
                    existing.username = username
                self.memory_service.save_user(existing)
                return existing

        # Create new identity
        user_identity = UserIdentity(
            canonical_id=canonical_id,
            platform=platform,
            user_id=raw_uid or (f"dn_{display_name}" if display_name else "unknown"),
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
