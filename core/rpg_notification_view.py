"""Persistent DM actions for completed fishing and farming activities."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS
from core.rpg_farming import LOCATIONS, PLANTS
from core.rpg_fishing import DURATIONS, SPOTS


class _NotificationView(discord.ui.View):
    def __init__(self, cog, guild_id, user_id):
        super().__init__(timeout=None)
        self.cog, self.guild_id, self.user_id = cog, guild_id, user_id
        self.lock = asyncio.Lock()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('只有收到通知的冒險者可以操作。', ephemeral=True)
            return False
        return True


class FishingNotificationView(_NotificationView):
    def __init__(self, cog, guild_id, user_id, spot_id, duration_id, started_at):
        super().__init__(cog, guild_id, user_id)
        self.spot_id, self.duration_id, self.started_at = spot_id, duration_id, started_at
        token = int(round(started_at * 1000))
        for label, repeat, style in (
                ('收竿', False, discord.ButtonStyle.primary),
                ('收竿並再次釣魚', True, discord.ButtonStyle.success)):
            action = 'repeat' if repeat else 'claim'
            button = discord.ui.Button(label=label, style=style,
                custom_id=f'rpg:fish:{guild_id}:{user_id}:{token}:{action}')
            async def callback(interaction, repeat=repeat):
                await self.handle(interaction, repeat)
            button.callback = callback
            self.add_item(button)

    async def handle(self, interaction, repeat):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            try:
                result = self.cog.fishing.claim(
                    self.guild_id, self.user_id, expected_started_at=self.started_at)
            except CharacterError as exc:
                embed = discord.Embed(title='安安大冒險｜通知已失效', description=str(exc), color=0x94A3B8)
                await interaction.response.edit_message(embed=embed, view=None)
                self.stop()
                return
            restart = None
            restart_error = None
            if repeat:
                try:
                    restart = self.cog.fishing.start(
                        self.guild_id, self.user_id, self.spot_id, self.duration_id)
                except CharacterError as exc:
                    restart_error = str(exc)
            lines = [f'{ITEMS[key].name} ×{count}' for key, count in result['items'].items()]
            description = (f'已從 **{SPOTS[result["spot_id"]].name}** 收竿，共捕獲 {result["catches"]} 次。\n'
                           + '\n'.join(lines) + f'\n獲得 {result["xp"]:,} 釣魚 XP。')
            if result.get('mastery_bonus'):
                description += f'\n熟練產量額外取得 {result["mastery_bonus"]} 份物品。'
            if restart:
                description += (f'\n\n已再次前往 **{restart["spot"].name}** 釣魚 '
                                f'({DURATIONS[self.duration_id][0]})，<t:{int(restart["ready_at"])}:R>可以收竿。')
            elif restart_error:
                description += f'\n\n重新開始失敗：{restart_error}'
            embed = discord.Embed(title='安安大冒險｜收竿完成', description=description, color=0x38BDF8)
            await interaction.response.edit_message(embed=embed, view=None)
            self.stop()


class FarmingNotificationView(_NotificationView):
    def __init__(self, cog, guild_id, user_id, location_id, plant_id, planted_at):
        super().__init__(cog, guild_id, user_id)
        self.location_id, self.plant_id, self.planted_at = location_id, plant_id, planted_at
        token = int(round(planted_at * 1000))
        for label, repeat, style in (
                ('收成', False, discord.ButtonStyle.primary),
                ('收成並再次種植', True, discord.ButtonStyle.success)):
            action = 'repeat' if repeat else 'harvest'
            button = discord.ui.Button(label=label, style=style,
                custom_id=f'rpg:farm:{guild_id}:{user_id}:{location_id}:{token}:{action}')
            async def callback(interaction, repeat=repeat):
                await self.handle(interaction, repeat)
            button.callback = callback
            self.add_item(button)

    async def handle(self, interaction, repeat):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            try:
                result = self.cog.farming.harvest(
                    self.guild_id, self.user_id, self.location_id,
                    expected_planted_at=self.planted_at)
            except CharacterError as exc:
                embed = discord.Embed(title='安安大冒險｜通知已失效', description=str(exc), color=0x94A3B8)
                await interaction.response.edit_message(embed=embed, view=None)
                self.stop()
                return
            restart = None
            restart_error = None
            if repeat:
                try:
                    restart = self.cog.farming.plant(
                        self.guild_id, self.user_id, self.location_id, self.plant_id)
                except CharacterError as exc:
                    restart_error = str(exc)
            plant = PLANTS[result['plant_id']]
            description = (f'已從 **{LOCATIONS[result["location_id"]]}** 收成 **{plant.name} ×{result["quantity"]}**，'
                           f'獲得 {result["xp"]:,} 農耕 XP。')
            if restart:
                description += (f'\n\n已在 **{restart["location"]}** 再次種下 **{restart["plant"].name}**，'
                                f'<t:{int(restart["ready_at"])}:R>成熟。')
            elif restart_error:
                description += f'\n\n重新種植失敗：{restart_error}'
            embed = discord.Embed(title='安安大冒險｜收成完成', description=description, color=0x65A30D)
            await interaction.response.edit_message(embed=embed, view=None)
            self.stop()
