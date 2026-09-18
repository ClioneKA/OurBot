"""One process-wide check for player-created manual rooms."""
import asyncio


def room_lock(cog):
    lock = getattr(cog, 'manual_room_lock', None)
    if lock is None:
        lock = cog.manual_room_lock = asyncio.Lock()
    return lock


def occupied_room(cog, guild_id, user_id, *, exclude=None):
    sources = []
    if getattr(cog, 'total_raids', None):
        sources.append(('total', cog.total_raids.repo.active()))
    if getattr(cog, 'painted_maze', None):
        sources.append(('maze', cog.painted_maze.repo.active(guild_id)))
    if getattr(cog, 'witch_rest', None):
        sources.append(('witch_rest', cog.witch_rest.active_rooms(guild_id)))
    for mode, rooms in sources:
        for room in rooms:
            if (mode, room['id']) != exclude and user_id in room.get('members', ()):
                return mode, room
    return None
