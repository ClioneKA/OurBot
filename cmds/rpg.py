import logging
from pathlib import Path
import time
from weakref import WeakSet

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.rpg import MAX_LEVEL, RPGStore, VoiceTracker, eligible_voice_members, level_floor, level_for
from core.settings import get_settings
from core.rpg_menu import AdventureView
from core.rpg_character import Characters, CharacterError, ITEMS, STAT_NAMES, stage_level
from core.rpg_battle import Tactics, CONDITIONS, TARGETS, FIXED_TARGETS, rule_skill
from core.rpg_raids import RaidService


class RPG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.settings = get_settings().rpg
        self.store = RPGStore(Path(__file__).resolve().parent.parent / 'data/rpg.db')
        self.characters = Characters(self.store, self.settings)
        self.tracker = VoiceTracker()
        self.menu_views = WeakSet()
        self.tactics = Tactics(self.store)
        self.ai_model = get_settings().ai.model
        self.raids = RaidService(self)

    async def cog_load(self):
        self.voice_tick.start()
        self.raids.start()

    async def cog_unload(self):
        self.voice_tick.cancel()
        await self.raids.close()
        for view in tuple(self.menu_views):
            view.closed = True
            view.stop()
        self.store.close()

    @commands.Cog.listener()
    async def on_message(self, message):
        if (not self.settings.enabled or message.guild is None or message.author.bot
                or message.webhook_id is not None or message.is_system()
                or len(''.join(message.content.split())) < self.settings.text_min_chars):
            return
        self.store.award_text(message.guild.id, message.author.id, time.time(),
                              self.settings.text_xp, self.settings.text_cooldown_seconds,
                              self.settings.text_daily_xp_limit)

    def update_voice(self, guild):
        if not self.settings.enabled or guild.unavailable:
            self.tracker.clear(guild.id)
            return
        eligible = eligible_voice_members(guild, self.settings.voice_min_members)
        awards = self.tracker.update(guild.id, eligible, time.monotonic(),
                                     self.settings.voice_xp_per_minute)
        if awards:
            self.store.award_voice(awards, daily_limit=self.settings.voice_daily_xp_limit, now=time.time())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if self.bot.is_ready():
            self.update_voice(member.guild)

    @commands.Cog.listener()
    async def on_disconnect(self):
        self.tracker.clear()

    @commands.Cog.listener()
    async def on_guild_unavailable(self, guild):
        self.tracker.clear(guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self.tracker.clear(guild.id)

    @tasks.loop(seconds=15)
    async def voice_tick(self):
        if not self.bot.is_ready():
            self.tracker.clear()
            return
        for guild in self.bot.guilds:
            try:
                self.update_voice(guild)
            except Exception:
                logging.exception('RPG voice XP update failed for guild %s', guild.id)
                self.tracker.clear(guild.id)

    @voice_tick.before_loop
    async def before_voice_tick(self):
        await self.bot.wait_until_ready()


    @app_commands.command(name='冒險', description='開啟安安大冒險：角色、裝備、技能、背包、商店與轉職')
    @app_commands.guild_only()
    async def adventure(self, interaction: discord.Interaction):
        view = AdventureView(self, interaction)
        self.menu_views.add(view)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    def character_embed(self, guild_id, member):
        xp = self.store.xp(guild_id, member.id)
        level = level_for(xp)
        state = self.characters.snapshot(guild_id, member.id)
        embed = discord.Embed(title=f'Lv.{level}・{state["title"]}', color=0x8B5CF6)
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        if level == MAX_LEVEL:
            embed.add_field(name='升級進度', value=f'{"▰" * 10}\n已達最高等級 Lv.{MAX_LEVEL}', inline=False)
        else:
            progress = xp - level_floor(level)
            required = level_floor(level + 1) - level_floor(level)
            filled = min(10, progress * 10 // required)
            embed.add_field(name='升級進度', value=f'{"▰" * filled}{"▱" * (10 - filled)}\n{progress:,} / {required:,} XP', inline=False)
            embed.add_field(name='距離下一級', value=f'{required - progress:,} XP')
        embed.add_field(name='累積經驗', value=f'{xp:,} XP')
        embed.add_field(name='金幣', value=f'{self.store.gold(guild_id, member.id):,} 金幣')
        now = time.time()
        embed.add_field(name='今日聊天經驗（台灣時間）', value=
                        f'文字：{self.store.daily_xp(guild_id, member.id, "text", now):,} / {self.settings.text_daily_xp_limit:,} XP\n'
                        f'語音：{self.store.daily_xp(guild_id, member.id, "voice", now):,} / {self.settings.voice_daily_xp_limit:,} XP\n'
                        '每日 00:00 重置；討伐經驗不計入。', inline=False)
        embed.add_field(name='基礎能力＋飾品加成', value='\n'.join(
            f'{name}：{total}（{base} + {bonus}）' for name, total, base, bonus in
            zip(STAT_NAMES, state['total'], state['base'], state['bonus'])), inline=False)
        embed.add_field(name='戰鬥能力', value='｜'.join(
            f'{name} {value}{"%" if name.endswith("率") else ""}'
            for name, value in state['combat'].items()), inline=False)
        embed.add_field(name='武器／套裝直接加成', value='、'.join(
            f'{name} +{value}' for name, value in state['combat_bonus'].items() if value) or '無', inline=False)
        embed.add_field(name='武器穩定度', value=(f'{state["stability"][0]}–{state["stability"][1]}% 傷害'
                        if '武器' in state['equipped'] else '未裝備武器，無法造成傷害'))
        embed.add_field(name=f'裝備欄・飾品 {state["capacity"]} 格', value='\n'.join(
            f'{slot}：{ITEMS[state["equipped"][slot]].name if slot in state["equipped"] else "空"}'
            for slot in state['slots']), inline=False)
        if state['job'] == '民兵':
            embed.description = 'Lv.10 起可使用 `/冒險 → 轉職` 選擇職業。'
        elif state['stage'] < 3:
            embed.description = f'下次晉升：Lv.{stage_level(state["stage"] + 1, self.settings)}；進階裝備可從 `/冒險 → 商店` 購買取得。'
        embed.set_footer(text='透過文字聊天與語音參與累積經驗' if self.settings.enabled else '目前暫停取得經驗值')
        return embed

    @app_commands.command(name='排行榜', description='查看本伺服器經驗值前十名')
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = self.store.leaders(interaction.guild_id)
        lines = [f'{index}. <@{user_id}> — Lv.{level_for(xp)} · {xp:,} XP'
                 for index, (user_id, xp) in enumerate(rows, 1)]
        embed = discord.Embed(title='安安大冒險｜冒險者排行榜', description='\n'.join(lines) or '還沒有冒險者，開始聊天來獲得經驗吧！', color=0xF59E0B)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='討伐通知', description='向安安領取或取消討伐通知身分組')
    @app_commands.guild_only()
    @app_commands.rename(action='操作')
    @app_commands.choices(action=[app_commands.Choice(name='領取', value='subscribe'),
                                 app_commands.Choice(name='取消', value='unsubscribe')])
    async def raid_notifications(self, interaction: discord.Interaction, action: str = 'subscribe'):
        if action not in ('subscribe', 'unsubscribe'):
            await interaction.response.send_message('請選擇領取或取消。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.raids.notifications.subscribe(interaction.guild, interaction.user, action == 'subscribe')
        except CharacterError as exc:
            message = str(exc)
        except discord.Forbidden:
            message = '身分組操作失敗，請確認安安有「管理身分組」權限，且位置高於討伐通知身分組。'
        except discord.HTTPException:
            message = 'Discord 暫時無法更新身分組，請稍後再試。'
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


    @app_commands.command(name='生成討伐', description='管理員立即在目前的討伐頻道生成魔物，五分鐘後開戰')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(kind='類型', name='名稱', strength='強度', victory_xp='經驗',
                         victory_gold='金幣', drop_percent='掉落率')
    @app_commands.describe(kind='不填則隨機抽取怪物類型', name='這場怪物的名稱，最多20字',
        strength='血量、攻擊、防禦倍率，0.1–10；會再乘頻道動態難度', victory_xp='勝利每人最終經驗，覆蓋類型預設獎勵',
        victory_gold='勝利每人最終金幣，覆蓋類型預設獎勵', drop_percent='專屬物品掉落百分比，0–100；史萊姆群固定不掉落')
    @app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in ('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像', '荊棘妖樹')])
    async def spawn_raid(self, interaction: discord.Interaction, kind: str = None,
                         name: app_commands.Range[str, 1, 20] = None,
                         strength: app_commands.Range[float, 0.1, 10.0] = 1.0,
                         victory_xp: app_commands.Range[int, 0, 100000] = None,
                         victory_gold: app_commands.Range[int, 0, 1000000] = None,
                         drop_percent: app_commands.Range[int, 0, 100] = None):
        if interaction.guild is None or not interaction.permissions.administrator:
            await interaction.response.send_message('只有伺服器管理員可以生成討伐。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.raids.spawn(interaction.channel, kind=kind, name=name, strength=strength,
                                            victory_xp=victory_xp, victory_gold=victory_gold, drop_percent=drop_percent)
        except CharacterError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logging.exception('Administrator raid spawn failed')
            await interaction.followup.send('生成討伐失敗，請確認機器人的頻道權限後再試。', ephemeral=True)
            return
        await interaction.followup.send(f'討伐已發布，五分鐘後開戰：{message.jump_url}', ephemeral=True)

    def skills_embed(self, guild, user):
        state = self.characters.snapshot(guild, user)
        embed = discord.Embed(title=f'{state["title"]}・自動技能', color=0x8B5CF6,
                              description='每回合由優先 1 開始檢查，施放第一個符合條件且冷卻結束的技能；否則普攻。\n'
                              '固定三格；Lv.20 後各職業解鎖兩個新技能，按「更換技能」配置。')
        for rule in self.tactics.rules(guild, user, state['job']):
            skill = rule_skill(state['job'], rule)
            embed.add_field(name=f'優先 {rule.priority}｜槽 {rule.slot}：{skill.name}｜{"開" if rule.enabled else "關"}',
                            value=f'{skill.description}\n冷卻 {skill.cooldown} 回合｜{CONDITIONS[rule.condition]}｜目標：{FIXED_TARGETS.get(skill.effect, TARGETS[rule.target])}', inline=False)
        embed.set_footer(text='在 /冒險 → 技能 面板調整。冷卻 2 表示完整等待兩回合。自身技能作用於自己；護衛作用全隊；範圍攻擊作用全體敵人，皆忽略目標選項。')
        return embed


async def setup(bot):
    await bot.add_cog(RPG(bot))
