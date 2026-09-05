"""Scheduled channel raids, persistent signups, resumable combat and result delivery."""
import asyncio
from dataclasses import asdict
import io
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import random
import time

import discord
from discord.ext import tasks

from core.rpg_battle import raid_battle, dump_battle, load_battle
from core.rpg_character import CharacterError, ITEMS
from core.rpg_raid_store import RaidStore, DROP_TABLES
from core.rpg_notifications import RaidNotifications
from core.rpg_monsters import prepare_monster, monster_name


logger = logging.getLogger(__name__)


def channel_ids(raw):
    try:
        result = {int(part.strip()) for part in raw.split(',') if part.strip()}
        if any(value <= 0 for value in result):
            raise ValueError
        return result
    except ValueError as exc:
        raise ValueError('RPG_RAID_CHANNEL_IDS 必須是逗號分隔的正整數頻道 ID') from exc


def safe_text(value, length):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(' '.join(str(value).split())[:length]))


class RaidSignup(discord.ui.View):
    def __init__(self, service, raid_id):
        super().__init__(timeout=None)
        self.service, self.raid_id = service, raid_id
        self.join_button.custom_id = f'raid:join:{raid_id}'
        self.leave_button.custom_id = f'raid:leave:{raid_id}'

    async def respond(self, interaction, leave):
        if interaction.guild_id is None or interaction.user.bot:
            await interaction.response.send_message('只有伺服器成員可參加。', ephemeral=True)
            return
        try:
            if not self.service.settings.enabled or interaction.channel_id not in self.service.channels:
                raise CharacterError('本頻道的討伐活動已停用。')
            raid = self.service.repo.get(self.raid_id)
            if not raid or interaction.channel_id != raid['channel_id']:
                raise CharacterError('無效的討伐頻道。')
            raid = self.service.repo.join(self.raid_id, interaction.guild_id, interaction.user.id,
                                          time.time(), self.service.settings.max_participants, leave=leave)
            text = '已退出討伐。' if leave else '報名成功！截止時會使用你當時的裝備與技能設定自動戰鬥。'
        except CharacterError as exc:
            text = str(exc)
        await interaction.response.send_message(text, ephemeral=True)
        # The scheduler refreshes the roster, avoiding concurrent edits from button clicks.

    @discord.ui.button(label='參與討伐', style=discord.ButtonStyle.success, custom_id='raid:join')
    async def join_button(self, interaction, button):
        await self.respond(interaction, False)

    @discord.ui.button(label='退出報名', style=discord.ButtonStyle.secondary, custom_id='raid:leave')
    async def leave_button(self, interaction, button):
        await self.respond(interaction, True)


class RaidService:
    def __init__(self, cog):
        self.cog, self.bot = cog, cog.bot
        self.settings = cog.settings.raid
        self.channels = channel_ids(os.getenv('RPG_RAID_CHANNEL_IDS', ''))
        self.repo = RaidStore(cog.store)
        self.notifications = RaidNotifications(cog.store)
        self.views = {}
        self.rosters = {}
        self.client = None
        self.spawning = set()
        self.spawn_tasks = set()

    def start(self):
        for raid in self.repo.pending():
            if raid['status'] == 'posting':
                # A crash during send can leave an orphan message; its buttons stay closed.
                raid.update(status='cancelled', delivered=True)
                self.repo.save(raid)
            elif raid['status'] == 'lobby' and raid['message_id']:
                view = self.signup(raid)
                self.bot.add_view(view, message_id=raid['message_id'])
        self.tick.start()

    async def close(self):
        self.tick.cancel()
        pending = list(self.spawn_tasks)
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        task = self.tick.get_task()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for view in self.views.values():
            view.stop()
        if self.client:
            await self.client.close()

    def signup(self, raid):
        if raid['id'] not in self.views:
            self.views[raid['id']] = RaidSignup(self, raid['id'])
        return self.views[raid['id']]

    def next_spawn(self, channel, now):
        delay = random.randint(self.settings.min_interval_minutes * 60, self.settings.max_interval_minutes * 60)
        self.repo.schedule(channel, now + delay)

    async def imagine(self, kind=None):
        kind = kind or random.choices(('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像', '荊棘妖樹', '哥布林戰團', '月影妖狐', '血翼蝠王'), weights=(20, 20, 5, 11, 11, 11, 11, 11), k=1)[0]
        monster = dict(name=random.choice(('吞月棉花獸', '夜光茶壺怪', '迷霧糖霜蛛')),
                       description='安安：「吾輩剛剛想到的怪物跑出來了！有誰願意一起對付牠？」', kind=kind)
        if kind == '月影妖狐':
            monster.update(name='月影妖狐', description='安安：「月光裡那條尾巴晃得吾輩眼花了！小心牠的月影斬！」')
        if kind == '血翼蝠王':
            monster.update(name='血翼蝠王', description='安安：「洞窟裡飛出一隻大蝙蝠！別讓牠咬到，牠會吸血恢復體力！」')
        if kind == '哥布林戰團':
            monster.update(name='哥布林戰團', description='安安：「吾輩的點心被三個哥布林搶走了！隊長還在替打手加油！」')
        if kind == '荊棘妖樹':
            monster.update(name='荊棘妖樹', description='安安：「吾輩種的小樹開始揮舞藤蔓了！小心被纏住！」')
        if kind == '鐵殼魔像':
            monster.update(name='鐵殼魔像', description='安安：「吾輩做的鐵皮玩偶站起來了！當心牠蓄力後的重拳！」')
        if kind == '史萊姆群':
            monster.update(name='蹦跳果凍史萊姆群', description='安安：「吾輩的果凍變成一大群史萊姆了！快來幫忙收拾牠們！」')
        if not self.settings.ai_monsters or not os.getenv('OPENAI_API_KEY'):
            return monster
        try:
            if self.client is None:
                from openai import AsyncOpenAI
                self.client = AsyncOpenAI(timeout=20, max_retries=0)
            persona_path = Path(__file__).resolve().parent.parent / 'config/persona.txt'
            persona = persona_path.read_text(encoding='utf-8') if persona_path.exists() else '你叫安安。'
            schema = dict(type='json_schema', name='raid_monster', strict=True,
                          schema={'type': 'object', 'properties': {'name': {'type': 'string'},
                                  'description': {'type': 'string'}},
                                  'required': ['name', 'description'], 'additionalProperties': False})
            response = await self.client.responses.create(
                model=self.cog.ai_model, instructions=persona + '\n為 Discord 合作 RPG 構思一隻原創奇幻怪物。使用繁體中文，名稱最多20字、出場描述最多120字，以安安口吻邀請大家討伐。不要寫數值、獎勵、@提及或連結。',
                input='怪物特色：' + {'巨獸': '每三回合對全隊橫掃。', '毒蛛': '普通攻擊附帶中毒。',
                                     '月影妖狐': '高速高閃避，每三回合月影斬攻擊並短暫提高閃避。名稱須包含妖狐。',
                                     '血翼蝠王': '每兩回合汲血撕咬，攻擊並按實際傷害吸血。名稱須包含蝠王。',
                                     '哥布林戰團': '一名隊長與兩名打手，各自獨立血量；隊長每三回合鼓舞全團。名稱須包含哥布林戰團。',
                                     '荊棘妖樹': '每三回合再生回血並纏繞暈眩部分玩家。名稱須包含妖樹。',
                                     '鐵殼魔像': '高防禦魔像，每三回合蓄力，下一回合單體重拳。名稱須包含魔像。',
                                     '史萊姆群': '一群史萊姆共用血量，每回合連續三次彈跳撞擊。名稱須包含史萊姆群。'}[kind],
                text={'format': schema}, max_output_tokens=800, store=False)
            data = json.loads(response.output_text)
            if not all(isinstance(data.get(k), str) and data[k].strip() for k in ('name', 'description')):
                raise ValueError('Invalid monster text')
            monster.update(name=data['name'][:20], description=data['description'][:120])
        except Exception as exc:
            logger.warning('Raid monster generation fallback: %s', type(exc).__name__)
        return monster

    def lobby_embed(self, raid):
        policy = SimpleNamespace(**raid['reward_policy']) if raid.get('reward_policy') else self.settings
        traits = {'巨獸': '每三回合對全隊橫掃', '毒蛛': '攻擊附帶中毒，可用淨化解除',
                  '月影妖狐': '每三回合月影斬：150% 單體攻擊，閃避率 +15% 至下一回合結束',
                  '血翼蝠王': '每兩回合汲血撕咬：150% 單體攻擊，回復實際扣血量的 30%',
                  '哥布林戰團': '隊長與兩名打手獨立血量；隊長每三回合鼓舞全團，攻擊 +25% 至下一回合結束，當回合不普攻',
                  '荊棘妖樹': '每三回合回血 5%，隨機暈眩存活玩家的 33%（向下取整），跳過下一次行動，可淨化；不受嘲諷影響',
                  '鐵殼魔像': '血量 1.2 倍、防禦 2 倍；第 3、6、9…回合蓄力，下一回合 250% 重拳，受嘲諷影響',
                  '史萊姆群': '稀有史萊姆群，共用血量，每回合三次 45% 倍率撞擊；單體攻擊受嘲諷影響'}[raid['monster']['kind']]
        if raid['monster'].get('profile'):
            traits = {'巨獸': '血厚、攻擊高、防禦偏低、速度慢；每三回合對全隊橫掃',
                      '月影妖狐': '血薄、速度快、閃避高；每三回合月影斬：150% 單體攻擊，閃避率 +15% 至下一回合結束；精準射擊必中',
                      '血翼蝠王': '速度快；每兩回合汲血撕咬：150% 單體攻擊，回復實際扣血量的 30%；受嘲諷、閃避及減傷影響',
                      '哥布林戰團': '隊長與兩名打手獨立血量，可用群攻；隊長每三回合鼓舞全團，攻擊 +25% 至下一回合結束，取代普攻；隊長倒下後停止鼓舞',
                      '毒蛛': '血薄、防禦低、速度快、閃避高；攻擊附帶中毒，可淨化',
                      '史萊姆群': '三隻獨立血量的史萊姆，每隻每回合一次 45% 撞擊；倒下後停止攻擊，可用群攻對付',
                      '鐵殼魔像': '高防禦、速度慢；第 3、6、9…回合蓄力，下一回合 250% 單體重拳',
                      '荊棘妖樹': '高血量、低攻擊；每三回合回血 5%，隨機暈眩存活玩家的 33%（向下取整），可淨化'}[raid['monster']['kind']]
        embed = discord.Embed(title='魔物出現｜' + safe_text(monster_name(raid['monster']), 32),
                              description=safe_text(raid['monster']['description'], 120), color=0xB565D9)
        embed.add_field(name='報名倒數', value=f'<t:{int(raid["deadline"])}:R> 開戰（報名 5 分鐘）', inline=False)
        embed.add_field(name=f'參與者 {len(raid["members"])}/{self.settings.max_participants}',
                        value=' '.join(f'<@{uid}>' for uid in raid['members']) or '等待冒險者加入', inline=False)
        embed.add_field(name='魔物特性', value=traits + '；開戰時按隊伍人數及等級決定強度。', inline=False)
        embed.add_field(name='強度倍率', value=f'{raid["monster"].get("strength", 1):g} 倍（血量、攻擊、防禦）')
        if raid.get('difficulty'):
            d = raid['difficulty']
            embed.add_field(name='頻道動態難度', value=f'{d["current"]:.3f} × 本場設定 {d["base_strength"]:g}\n'
                            '勝利後 ×1.1，戰敗／回合上限後 ×0.9；動態難度範圍 0.5–3 倍。', inline=False)
        pool = raid.get('drop_pool', DROP_TABLES.get(raid['monster']['kind'], ()))
        category = '討伐飾品' if pool and all(ITEMS[key].slot == '飾品' for key in pool) else '專屬裝備'
        loot_text = ('不掉落飾品或其他裝備' if not pool or raid['monster']['kind'] == '史萊姆群'
                     else f'{policy.drop_chance * 100:g}% 機率取得{category}（可能重複）')
        scaling = raid.get('reward_scaling')
        if scaling:
            labels = {'victory_xp': '勝利經驗', 'victory_gold': '勝利金幣'}
            scaled = '、'.join(labels[key] for key in scaling['fields'])
            embed.add_field(name='難度報酬', value=(f'{scaled}已套用 {scaling["multiplier"]:.3f} 倍；下列為每人最終獎勵。'
                            if scaled else '經驗與金幣採管理員指定的最終數額。'), inline=False)
        embed.add_field(name='獎勵', value=f'成功：每人 {policy.victory_xp} XP、{getattr(policy, "victory_gold", 0)} 金幣，{loot_text}。\n'
                        '失敗或回合上限：依怪物已削減 HP 比例發放上述經驗與金幣，無條件捨去，無掉落。'
                        '多隻怪物合計血量，以結束時剩餘 HP 計算；倒下者仍依隊伍結果領獎。', inline=False)
        embed.set_footer(text='可先用 /冒險 → 裝備／能力 與 /冒險 → 技能 調整策略。沒有 NPC 隊友；至少一人即可開戰。')
        return embed

    def battle_embed(self, raid, battle):
        embed = discord.Embed(title=f'{safe_text(monster_name(raid["monster"]), 32)}｜第 {battle.round} 回合｜{battle.result or "自動戰鬥中"}',
                              description='\n'.join(battle.log[-12:])[-3000:] or '戰鬥即將開始', color=0xE09B37)
        enemies = [f for f in battle.fighters if f.team == 1]
        embed.add_field(name='魔物 HP', value='\n'.join(f'{f.name}：{f.hp:,}/{f.stats["HP"]:,}' for f in enemies)[:1024])
        roster = '\n'.join(f'{f.name}：{f.hp}/{f.stats["HP"]}' for f in battle.fighters if f.team == 0)
        embed.add_field(name='討伐隊伍', value=roster[:1024], inline=False)
        if raid.get('difficulty_change'):
            change = raid['difficulty_change']
            embed.add_field(name='下次討伐動態難度', value=f'{change["before"]:.3f} → {change["after"]:.3f} 倍', inline=False)
        if raid.get('rewards'):
            if raid.get('failure_progress'):
                progress = raid['failure_progress']
                maximum, remaining = progress['max_hp'], progress['remaining_hp']
                percent = (maximum - remaining) * 100 / maximum if maximum else 0
                embed.add_field(name='失敗獎勵比例', value=f'怪物剩餘 HP：{remaining:,}/{maximum:,}；'
                                f'依已削減血量 {percent:.2f}% 發放經驗與金幣（無條件捨去），無掉落。', inline=False)
            lines = [f'<@{r["id"]}>：+{r["xp"]} XP、+{r.get("gold", 0)} 金幣' + (f'、{ITEMS[r["item"]].name}' if r['item'] else '') for r in raid['rewards']]
            embed.add_field(name='獎勵已入帳', value='\n'.join(lines)[:1024], inline=False)
            players = [fighter for fighter in battle.fighters if fighter.team == 0]
            players.sort(key=lambda fighter: fighter.combat_stats['damage_dealt'], reverse=True)
            summary = []
            for fighter in players:
                stats = fighter.combat_stats
                label = f'<@{fighter.user_id}>' if fighter.user_id else safe_text(fighter.name, 24)
                summary.append(f'{label}：傷害 {stats["damage_dealt"]:,}｜治療 {stats["healing_done"]:,}｜'
                               f'承傷 {stats["damage_taken"]:,}｜命中 {stats["hits"]}/{stats["attacks"]}')
            embed.add_field(name='戰鬥結算｜依傷害排序', value='\n'.join(summary)[:1024] or '沒有戰鬥數據', inline=False)
        embed.set_footer(text='靈巧決定順序；每回合每人一次行動。完整戰報於結束後附上。')
        return embed

    async def advance(self, raid, channel, now):
        message = channel.get_partial_message(raid['message_id'])
        if raid['status'] == 'lobby':
            if raid['channel_id'] not in self.channels or not self.settings.enabled:
                raid.update(status='cancelled', reason='討伐活動已停用。')
                self.repo.save(raid)
            elif now < raid['deadline']:
                roster = tuple(raid['members'])
                if self.rosters.get(raid['id']) != roster:
                    await message.edit(embed=self.lobby_embed(raid), view=self.signup(raid), allowed_mentions=discord.AllowedMentions.none())
                    self.rosters[raid['id']] = roster
                return
            else:
                participants = []
                for uid in raid['members']:
                    member = channel.guild.get_member(uid)
                    if not member or member.bot:
                        continue
                    state = self.cog.characters.snapshot(raid['guild_id'], uid)
                    participants.append(dict(id=uid, name=safe_text(member.display_name, 16), state=state,
                                             rules=[asdict(r) for r in self.cog.tactics.rules(raid['guild_id'], uid, state['job'])]))
                provisions = getattr(self.cog, 'provisions', None)
                if participants and provisions is not None:
                    prepared = provisions.prepare_for_raid(
                        raid['id'], raid['guild_id'], [participant['id'] for participant in participants])
                    for participant in participants:
                        participant['provisions'] = prepared.get(participant['id'], {})
                raid['participants'] = participants
                if not participants:
                    raid.update(status='cancelled', reason='沒有人參與，魔物離開了。')
                else:
                    battle = raid_battle(participants, raid['monster'], raid['seed'])
                    raid.update(status='running', battle=dump_battle(battle))
                self.repo.save(raid)
            view = self.views.pop(raid['id'], None)
            if view:
                view.stop()
        if raid['status'] == 'running':
            battle = load_battle(raid['battle'])
            battle.step()
            raid['battle'] = dump_battle(battle)
            if battle.result:
                raid = self.repo.settle(raid['id'], raid['battle'], self.settings)
            else:
                self.repo.save(raid)
                await message.edit(embed=self.battle_embed(raid, battle), view=None, allowed_mentions=discord.AllowedMentions.none())
                return
        if raid['status'] == 'completed':
            battle = load_battle(raid['battle'])
            report = '\n'.join(battle.log) + '\n\n戰鬥結算：\n' + '\n'.join(
                f'{f.user_id or f.name}: 傷害 {f.combat_stats["damage_dealt"]}, 治療 {f.combat_stats["healing_done"]}, '
                f'承傷 {f.combat_stats["damage_taken"]}, 命中 {f.combat_stats["hits"]}/{f.combat_stats["attacks"]}, '
                f'暴擊 {f.combat_stats["critical_hits"]}, 技能 {json.dumps(f.combat_stats["skills_used"], ensure_ascii=False)}'
                for f in battle.fighters if f.team == 0) + '\n\n獎勵：\n' + '\n'.join(
                f'{r["id"]}: {r["xp"]} XP, {r.get("gold", 0)} 金幣, {ITEMS[r["item"]].name if r["item"] else "無掉落"}' for r in raid['rewards'])
            await message.edit(embed=self.battle_embed(raid, battle), view=None,
                               attachments=[discord.File(io.BytesIO(report.encode('utf-8')), filename='討伐戰報.txt')],
                               allowed_mentions=discord.AllowedMentions.none())
        elif raid['status'] == 'cancelled':
            await message.edit(content=raid.get('reason', '本次討伐取消。'), embed=None, view=None)
        raid['delivered'] = True
        self.repo.save(raid)
        self.rosters.pop(raid['id'], None)
        self.next_spawn(raid['channel_id'], now)

    async def spawn(self, channel, *, kind=None, name=None, strength=1.0,
                    victory_xp=None, victory_gold=None, drop_percent=None):
        if kind is not None and kind not in ('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像', '荊棘妖樹', '哥布林戰團', '月影妖狐', '血翼蝠王'):
            raise CharacterError('無效的怪物類型。')
        if not 0.1 <= strength <= 10:
            raise CharacterError('強度倍率必須介於 0.1–10。')
        if name is not None and not 1 <= len(name.strip()) <= 20:
            raise CharacterError('怪物名稱需為 1–20 個字元。')
        overrides = {}
        for key, value, limit in (('victory_xp', victory_xp, 100000),
                                  ('victory_gold', victory_gold, 1000000), ('drop_chance', drop_percent, 100)):
            if value is not None:
                if not isinstance(value, int) or not 0 <= value <= limit:
                    raise CharacterError(f'{key} 必須介於 0–{limit}。')
                overrides[key] = value / 100 if key == 'drop_chance' else value
        if not self.settings.enabled:
            raise CharacterError('討伐活動目前已停用。')
        if not isinstance(channel, discord.TextChannel) or channel.id not in self.channels:
            raise CharacterError('請在已設定的討伐文字頻道使用此指令。')
        if channel.guild.unavailable:
            raise CharacterError('伺服器暫時無法使用，請稍後再試。')
        if channel.id in self.spawning or any(r['channel_id'] == channel.id for r in self.repo.pending()):
            raise CharacterError('本頻道已有討伐或結果待送出，請等待完成。')
        # Reserve before the AI call so manual and scheduled spawns cannot overlap.
        self.spawning.add(channel.id)
        task = asyncio.current_task()
        self.spawn_tasks.add(task)
        self.next_spawn(channel.id, time.time())
        raid = None
        try:
            monster = dict(await self.imagine(kind) if kind is not None else await self.imagine())
            monster['strength'] = strength
            if name is not None:
                monster['name'] = name.strip()
            monster = prepare_monster(monster)
            raid = self.repo.create(channel.guild.id, channel.id, monster, time.time(), asdict(self.settings), overrides)
            role = None
            try:
                role = await self.notifications.ensure(channel.guild)
            except (CharacterError, discord.HTTPException) as exc:
                logger.warning('Raid notification role unavailable for guild %s: %s', channel.guild.id, exc)
            message = await channel.send(content=role.mention if role else None,
                                         embed=self.lobby_embed(raid), view=self.signup(raid),
                                         allowed_mentions=discord.AllowedMentions(everyone=False, users=False,
                                                                                 roles=[role] if role else [], replied_user=False))
            raid.update(message_id=message.id, status='lobby', deadline=time.time() + 300)
            self.repo.save(raid)
            return message
        except (Exception, asyncio.CancelledError):
            if raid is not None:
                raid.update(status='cancelled', delivered=True)
                self.repo.save(raid)
                view = self.views.pop(raid['id'], None)
                if view:
                    view.stop()
            raise
        finally:
            self.spawning.discard(channel.id)
            self.spawn_tasks.discard(task)

    @tasks.loop(seconds=5)
    async def tick(self):
        if not self.bot.is_ready():
            return
        now = time.time()
        pending = self.repo.pending()
        for raid in pending:
            channel = self.bot.get_channel(raid['channel_id'])
            if channel is None or channel.guild.unavailable:
                continue
            try:
                await self.advance(raid, channel, now)
            except discord.NotFound:
                # Deleted announcement: no unfinished fight rewards; settled rewards stay credited.
                raid = self.repo.get(raid['id'])
                if raid['status'] != 'completed':
                    raid['status'] = 'cancelled'
                raid['delivered'] = True
                self.repo.save(raid)
                self.next_spawn(raid['channel_id'], now)
                view = self.views.pop(raid['id'], None)
                if view:
                    view.stop()
            except Exception:
                logger.exception('Raid update failed: %s', raid['id'])
        if not self.settings.enabled:
            return
        occupied = {r['channel_id'] for r in self.repo.pending()}
        for channel_id in self.channels - occupied:
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.guild.unavailable:
                continue
            due = self.repo.next_at(channel_id)
            if due is None:
                self.next_spawn(channel_id, now)
                continue
            if now < due:
                continue
            try:
                await self.spawn(channel)
            except CharacterError:
                continue  # A manual spawn may already be generating this channel's monster.
            except Exception:
                logger.exception('Raid spawn failed in channel %s', channel_id)

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
