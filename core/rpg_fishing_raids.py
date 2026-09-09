"""Publish durable fishing encounters through the existing raid lifecycle."""
import asyncio
from dataclasses import asdict
import logging
import time

import discord

from core.rpg_fishing_bosses import FISHING_BOSSES
from core.rpg_monsters import prepare_monster


logger = logging.getLogger(__name__)


async def publish_fishing_encounters(service, guild_id=None):
    db = service.repo.db
    # Lightweight test cogs and older callers may not initialize fishing.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='rpg_fishing_encounters'").fetchone():
        return
    # Recover failed/unconfirmed posts. A cancelled published raid is final.
    for encounter_id, raid_id in db.execute('''SELECT e.id,e.raid_id
            FROM rpg_fishing_encounters e JOIN rpg_raids r ON r.id=e.raid_id
            WHERE e.status='assigned' AND r.status='cancelled' ''').fetchall():
        raid = service.repo.get(raid_id)
        if raid['message_id'] is None:
            with db:
                db.execute("UPDATE rpg_fishing_encounters SET status='queued',raid_id=NULL WHERE id=?",
                           (encounter_id,))
    queued = db.execute('''SELECT id,guild_id,user_id,spot_id FROM rpg_fishing_encounters
        WHERE status='queued' ORDER BY created_at,id''').fetchall()
    seen_guilds = set()
    for encounter_id, guild, user, spot_id in queued:
        if guild_id is not None and guild != guild_id or guild in seen_guilds:
            continue
        seen_guilds.add(guild)
        pending = service.repo.pending()
        if guild in service.spawning_guilds or any(r['guild_id'] == guild for r in pending):
            continue
        if any(user in r.get('members', ()) and r['status'] in ('lobby', 'running') for r in pending):
            continue
        channels = [service.bot.get_channel(cid) for cid in sorted(service.special_channels)]
        channel = next((c for c in channels if isinstance(c, discord.TextChannel)
                        and c.guild.id == guild and not c.guild.unavailable
                        and service.settings_for_channel(c.id).enabled), None)
        if channel is None:
            continue
        service.spawning.add(channel.id)
        service.spawning_guilds.add(guild)
        task = asyncio.current_task()
        service.spawn_tasks.add(task)
        raid = None
        try:
            boss = FISHING_BOSSES[spot_id]
            monster = prepare_monster(dict(kind=boss.kind, name=boss.name,
                                           description=boss.description, strength=1.0), quality='普通')
            raid = service.repo.create(guild, channel.id, monster, time.time(),
                asdict(service.settings_for_channel(channel.id)), {'drop_chance': 0.0},
                pool='special', use_dynamic=False, fishing_encounter=encounter_id)
            message = await channel.send(
                content=f'<@{user}> 釣出了稀有魔物，快來參加特殊討伐！',
                embed=service.lobby_embed(raid), view=service.signup(raid),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=[discord.Object(id=user)], roles=False, replied_user=False))
            raid.update(message_id=message.id, status='lobby', deadline=time.time() + 300)
            service.repo.save(raid)
        except (Exception, asyncio.CancelledError) as exc:
            if raid is not None:
                raid.update(status='cancelled', delivered=True)
                service.repo.save(raid)
                view = service.views.pop(raid['id'], None)
                if view:
                    view.stop()
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.exception('Fishing encounter publication failed: %s', encounter_id)
        finally:
            service.spawning.discard(channel.id)
            service.spawning_guilds.discard(guild)
            service.spawn_tasks.discard(task)
