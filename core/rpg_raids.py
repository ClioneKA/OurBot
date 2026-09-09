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
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks

from core.rpg_battle import raid_battle, dump_battle, load_battle
from core.rpg_character import CharacterError, ITEMS
from core.rpg_raid_store import (DROP_TABLES, HIGH_RAID_MIN_LEVEL, MID_RAID_MIN_LEVEL,
                                 RAID_PROOFS_BY_POOL, RaidStore, raid_min_level)
from core.rpg_notifications import RaidNotifications
from core.rpg_monsters import prepare_monster, monster_name
from core.settings import daily_periods


logger = logging.getLogger(__name__)
REGULAR_KINDS = ('巨獸', '毒蛛', '史萊姆群', '鐵殼魔像', '荊棘妖樹', '哥布林戰團', '月影妖狐', '血翼蝠王')
MID_KINDS = ('深淵鐘龍', '王城傀儡師', '瘟疫縫合獸', '赤雷與蒼炎', '吞城鯨')
HIGH_RAID_KINDS = ('熔爐鎧獸', '迷霧菌后', '星蝕巨神', '逆潮聖骸')
SPECIAL_KIND = '城崎諾亞'
PAINT_COLOR_NAMES = {'red': '紅色', 'yellow': '黃色', 'blue': '藍色'}


def channel_ids(raw, label='RPG_RAID_CHANNEL_IDS'):
    try:
        result = {int(part.strip()) for part in raw.split(',') if part.strip()}
        if any(value <= 0 for value in result):
            raise ValueError
        return result
    except ValueError as exc:
        raise ValueError(f'{label} 必須是逗號分隔的正整數頻道 ID') from exc


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
            if not self.service.cog.store.has_player(interaction.guild_id, interaction.user.id):
                raise CharacterError('請先接受邀請函，正式成為冒險者。')
            settings = self.service.settings_for_channel(interaction.channel_id)
            if not settings.enabled or interaction.channel_id not in self.service.all_channels:
                raise CharacterError('本頻道的討伐活動已停用。')
            raid = self.service.repo.get(self.raid_id)
            if not raid or interaction.channel_id != raid['channel_id']:
                raise CharacterError('無效的討伐頻道。')
            raid = self.service.repo.join(self.raid_id, interaction.guild_id, interaction.user.id,
                                          time.time(), settings.max_participants, leave=leave)
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
        self.environment_channels = channel_ids(os.getenv('RPG_RAID_CHANNEL_IDS', ''))
        self.mid_settings = cog.settings.mid_raid
        self.environment_mid_channels = channel_ids(os.getenv('RPG_MID_RAID_CHANNEL_IDS', ''), 'RPG_MID_RAID_CHANNEL_IDS')
        self.high_settings = cog.settings.high_raid
        self.environment_high_channels = channel_ids(os.getenv('RPG_HIGH_RAID_CHANNEL_IDS', ''), 'RPG_HIGH_RAID_CHANNEL_IDS')
        self.refresh_channels()
        self.repo = RaidStore(cog.store)
        self.notifications = RaidNotifications(cog.store)
        self.views = {}
        self.rosters = {}
        self.client = None
        self.spawning = set()
        self.spawning_guilds = set()
        self.spawn_tasks = set()

    def refresh_channels(self):
        self.environment_special_channels = channel_ids(
            os.getenv('RPG_SPECIAL_RAID_CHANNEL_IDS', ''), 'RPG_SPECIAL_RAID_CHANNEL_IDS')
        self.special_channels = set(self.environment_special_channels)
        self.channels = set(self.environment_channels)
        self.mid_channels = set(self.environment_mid_channels)
        self.high_channels = set(self.environment_high_channels)
        spaces = getattr(getattr(self.cog, 'spaces', None), 'store', None)
        for space in spaces.all() if spaces else ():
            if getattr(space, 'special_channel_id', None):
                self.special_channels.add(space.special_channel_id)
            if space.regular_channel_id:
                self.channels.add(space.regular_channel_id)
            if space.mid_channel_id:
                self.mid_channels.add(space.mid_channel_id)
            if space.high_channel_id:
                self.high_channels.add(space.high_channel_id)
        if self.channels & self.mid_channels or self.channels & self.high_channels or self.mid_channels & self.high_channels:
            raise ValueError('一般、中階與高階討伐頻道不可重複')
        self.all_channels = self.channels | self.mid_channels | self.high_channels
        if self.special_channels & self.all_channels:
            raise ValueError('特殊討伐頻道不可與一般、中階或高階討伐頻道重複')

    def start(self):
        for raid in self.repo.pending():
            if raid['status'] == 'posting':
                # A crash during send can leave an orphan message; its buttons stay closed.
                raid.update(status='cancelled', delivered=True)
                self.repo.save(raid)
                self.repo.refund_payment(raid['id'])
                if raid.get('bag_draw'):
                    if raid.get('pool') == 'high':
                        self.repo.return_high_kind(raid['channel_id'], raid['monster']['kind'])
                    elif raid.get('pool') == 'mid':
                        self.repo.return_mid_kind(raid['channel_id'], raid['monster']['kind'])
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
        settings = self.settings_for_channel(channel)
        minimum = settings.min_interval_minutes * 60
        maximum = settings.max_interval_minutes * 60
        local_now = datetime.fromtimestamp(
            now, timezone(timedelta(hours=settings.schedule_timezone_offset_hours)))
        minute = local_now.hour * 60 + local_now.minute
        for start, end in daily_periods(settings.half_interval_periods):
            in_period = start <= minute < end if start < end else minute >= start or minute < end
            if in_period:
                minimum //= 2
                maximum //= 2
                break
        delay = random.randint(minimum, maximum)
        self.repo.schedule(channel, now + delay)

    def settings_for_channel(self, channel):
        if channel in self.high_channels:
            return self.high_settings
        return self.mid_settings if channel in self.mid_channels else self.settings

    async def imagine(self, kind=None):
        kind = kind or random.choices(REGULAR_KINDS, weights=(20, 20, 5, 11, 11, 11, 11, 11), k=1)[0]
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
        if kind == '深淵鐘龍':
            monster.update(name='深淵鐘龍', description='安安：「鐘聲從深淵裡傳上來了！快在牠敲響終末之前擊碎鐘甲！」')
        if kind == '王城傀儡師':
            monster.update(name='王城傀儡師', description='安安：「廢棄王城的絲線動起來了！先拆掉護主的兩具傀儡！」')
        if kind == '瘟疫縫合獸':
            monster.update(name='瘟疫縫合獸', description='安安：「那頭縫合怪物正在散播腐敗！記得先淨化再治療！」')
        if kind == '赤雷與蒼炎':
            monster.update(name='赤雷與蒼炎', description='安安：「兩頭雙生獸同時現身了！必須在再生共鳴完成前一起擊倒！」')
        if kind == '吞城鯨':
            monster.update(name='吞城鯨', description='安安：「遮住天空的巨鯨正在引發漲潮！快用強力的單體攻擊擊破鯨脂！」')
        if kind == '熔爐鎧獸':
            monster.update(name='熔爐鎧獸', description='安安：「熔爐裡的重甲怪獸醒來了！擊碎爐甲，別讓爐心震爆完成！」')
        if kind == '迷霧菌后':
            monster.update(name='迷霧菌后', description='安安：「整座森林都在冒出孢子！膨脹的孢子要先處理掉！」')
        if kind == '星蝕巨神':
            monster.update(name='星蝕巨神', description='安安：「巨神正把星光吸進核心！快摧毀星核，阻止星蝕墜落！」')
        if kind == '逆潮聖骸':
            monster.update(name='逆潮聖骸', description='安安：「沉沒神殿的聖骸召來逆潮！法陣和身上的溺印都不能放著不管！」')
        ai_settings = (self.high_settings if kind in HIGH_RAID_KINDS else
                       self.mid_settings if kind in MID_KINDS else self.settings)
        if not ai_settings.ai_monsters or not os.getenv('OPENAI_API_KEY'):
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
                                     '史萊姆群': '一群史萊姆共用血量，每回合連續三次彈跳撞擊。名稱須包含史萊姆群。',
                                     '深淵鐘龍': '以可被多次直接命中擊碎的鐘甲蓄力全體終末鐘聲，免疫暈眩。名稱須包含鐘龍。',
                                     '王城傀儡師': '與劍傀儡、咒傀儡共同作戰，會修復或吸收傀儡。名稱須包含傀儡師。',
                                     '瘟疫縫合獸': '疊加可淨化的腐敗，三層會爆裂；治療腐敗者會使怪物回血。名稱須包含縫合獸。',
                                     '赤雷與蒼炎': '赤雷與蒼炎輪流攻擊，任一倒下時另一隻會嘗試將牠復活。名稱須包含赤雷與蒼炎。',
                                     '吞城鯨': '擁有需以高傷單體攻擊擊破的鯨脂護盾，水位會永久上升。名稱須包含吞城鯨。',
                                     '熔爐鎧獸': '三層可擊破爐甲保護本體，週期性蓄力全體爐心震爆。名稱須包含鎧獸。',
                                     '迷霧菌后': '菌后與兩隻孢子共同作戰，孢子膨脹後會爆炸並附加中毒。名稱須包含菌后。',
                                     '星蝕巨神': '星核保護巨神並週期性協助蓄力全體星蝕墜落，摧毀星核可打斷。名稱須包含巨神。',
                                     '逆潮聖骸': '攻擊疊加可淨化溺印，週期性召喚必須擊倒的逆潮法陣。名稱須包含聖骸。'}[kind],
                text={'format': schema}, max_output_tokens=800, store=False)
            data = json.loads(response.output_text)
            if not all(isinstance(data.get(k), str) and data[k].strip() for k in ('name', 'description')):
                raise ValueError('Invalid monster text')
            monster.update(name=data['name'][:20], description=data['description'][:120])
        except Exception as exc:
            logger.warning('Raid monster generation fallback: %s', type(exc).__name__)
        return monster

    def lobby_embed(self, raid):
        channel_settings = self.settings_for_channel(raid['channel_id']) if hasattr(self, 'settings_for_channel') else self.settings
        policy = SimpleNamespace(**raid['reward_policy']) if raid.get('reward_policy') else channel_settings
        traits = {'巨獸': '每三回合對全隊橫掃', '毒蛛': '攻擊附帶中毒，可用淨化解除',
                  '月影妖狐': '每三回合月影斬：150% 單體攻擊，閃避率 +15% 至下一回合結束',
                  '血翼蝠王': '每兩回合汲血撕咬：150% 單體攻擊，回復實際扣血量的 30%',
                  '哥布林戰團': '隊長與兩名打手獨立血量；隊長每三回合鼓舞全團，攻擊 +25% 至下一回合結束，當回合不普攻',
                  '荊棘妖樹': '高血量、速度慢；每三回合回血 5%，隨機暈眩存活玩家的 33%（向下取整），跳過下一次行動，可淨化；不受挑釁反擊影響',
                  '鐵殼魔像': '高防禦、速度慢；第 3、6、9…回合蓄力，下一回合 250% 重拳，受挑釁反擊影響',
                  '史萊姆群': '稀有史萊姆群，共用血量，每回合三次 45% 倍率撞擊；單體攻擊受挑釁反擊影響',
                  '深淵鐘龍': '蓄力時獲得鐘甲，直接命中可削減層數；鐘甲未破將釋放最高 300% 全體傷害；免疫暈眩',
                  '王城傀儡師': '劍傀儡護主、咒傀儡替本體減傷；每三回合修復，半血後吸收存活傀儡',
                  '瘟疫縫合獸': '攻擊疊加腐敗，三層在行動前爆裂；腐敗者受到技能治療會使怪物回血，可淨化',
                  '赤雷與蒼炎': '赤雷在奇數回合單體攻擊，蒼炎在偶數回合全體攻擊；第 4、8、12…回合改用雷炎吐息；倒下一隻後須在再生共鳴完成前擊倒另一隻',
                  '吞城鯨': '城塞鯨脂提供 35% 減傷，需高傷單體攻擊逐層擊破；水位每回合永久上升，100 時蓄力吞城',
                  '熔爐鎧獸': '三層爐甲提供減傷；每四回合補滿爐甲並蓄力全體爐心震爆，可破甲或盾擊打斷',
                  '迷霧菌后': '菌后與兩隻孢子獨立血量；孢子每三回合膨脹，下一回合爆炸並附加中毒',
                  '星蝕巨神': '星核存活時保護巨神；巨神每四回合蓄力星蝕墜落，擊倒星核可取消並使本體破甲',
                  '逆潮聖骸': '攻擊疊加溺印；每三回合召喚逆潮法陣，未及時擊倒會全體攻擊並治療本體',
                  '城崎諾亞': '免疫暈眩但可中毒；70% HP 前固定使用公告抽中的單色顏料；之後依紅 → 黃 → 藍輪替並累積未完成構圖，三色完成後蓄力未完成稿，可用盾擊打斷'}[raid['monster']['kind']]
        if raid['monster'].get('profile'):
            traits = {'巨獸': '血量與攻擊很高、防禦偏低、速度慢；每三回合以橫掃攻擊全隊。護衛與全隊治療能穩住橫掃後的血線',
                      '月影妖狐': '血量偏低但速度與閃避很高；每三回合施放 150% 單體月影斬，並使自身閃避 +15% 到下一回合結束。高命中攻擊較能穩定壓制',
                      '血翼蝠王': '速度與閃避偏高；每兩回合施放 150% 汲血撕咬，回復實際扣血量的 30%。撕咬受挑釁反擊、閃避與減傷影響，降低承傷也會壓低回血量',
                      '哥布林戰團': '隊長與兩名打手各有獨立血量，可用群攻同時削減；隊長每三回合放棄普攻並鼓舞存活成員，攻擊 +25% 到下一回合結束。優先擊倒或暈眩隊長可阻止鼓舞',
                      '毒蛛': '血量與防禦偏低，但速度、閃避與暴擊較高；每次攻擊命中都會附加中毒。提高命中能加速擊殺，淨化可在毒傷結算前解除中毒',
                      '史萊姆群': '三隻史萊姆各有獨立血量，每隻每回合施放一次 45% 彈跳撞擊；倒下的個體會停止行動。群體攻擊可同時削血，逐隻擊倒則能快速降低來襲次數',
                      '鐵殼魔像': '防禦極高、速度慢；第 3、6、9…回合蓄力，下一回合施放 250% 單體重拳。蓄力時可用盾擊打斷，破甲與高倍率單體攻擊適合突破鐵殼',
                      '荊棘妖樹': '血量高、速度慢；每三回合放棄普攻，回復最大 HP 5% 並隨機暈眩存活玩家的 33%（向下取整）。暈眩可淨化，爆發輸出可減少再生帶來的拖延',
                      '深淵鐘龍': '免疫暈眩；蓄力時取得鐘甲，每次直接命中可削減一層。鐘甲未破會對全隊造成最高 300% 傷害，半血後鐘甲更多、蓄力更頻繁，適合用多段攻擊集中拆甲',
                      '王城傀儡師': '本體、劍傀儡與咒傀儡各有獨立血量；劍傀儡護主，咒傀儡替本體減傷。每三回合會修復傀儡；讓兩具傀儡同時倒下可使修復失敗並令本體破甲',
                      '瘟疫縫合獸': '攻擊會疊加腐敗，三層會在玩家行動前爆裂並波及全隊；腐敗者接受技能治療時怪物也會回血。先淨化再治療可避免爆裂與共享血肉，半血後兩者威脅都會提高',
                      '赤雷與蒼炎': '兩隻各持一半總 HP；赤雷只在奇數回合單體攻擊，蒼炎只在偶數回合全體攻擊，且第 4、8、12…回合改用雷炎吐息。任一倒下後兩回合會復活，須同步壓低血量；盾擊可將復活延後一次',
                      '吞城鯨': '城塞鯨脂提供 35% 減傷，需至少 150% 倍率的高傷單體攻擊或暴擊才能逐層擊破；最後一層破裂時會短暫破甲。水位每回合永久上升，達 100 後蓄力 200% 全體吞城，可用盾擊打斷',
                      '熔爐鎧獸': '三層爐甲各提供 8% 減傷；高倍率單體攻擊或暴擊可逐層擊破。每四回合補滿爐甲並蓄力爐心震爆，可在爆發前破甲或以盾擊打斷',
                      '迷霧菌后': '菌后持有 60% 總 HP，兩隻孢子各 20%。孢子每三回合膨脹，下一回合爆炸並中毒全隊；可優先擊倒或以盾擊打斷',
                      '星蝕巨神': '星核存活時使巨神減傷 20%；巨神每四回合蓄力 200% 全體星蝕墜落。擊倒星核會取消蓄力並使本體破甲，之後仍會重建',
                      '逆潮聖骸': '攻擊附加可淨化的溺印，每層在回合結束造成 2% 最大 HP 傷害。每三回合召喚逆潮法陣，擊倒法陣可阻止全體傷害與治療並使本體破甲',
                      '城崎諾亞': '免疫暈眩但可正常中毒；70% HP 前固定使用公告抽中的單色顏料，70% 以下依紅→黃→藍調色。三色構圖完成後蓄力全體未完成稿；盾擊可打斷構圖並造成破甲'}[raid['monster']['kind']]
        embed = discord.Embed(title='魔物出現｜' + safe_text(monster_name(raid['monster']), 32),
                              description=safe_text(raid['monster']['description'], 120), color=0xB565D9)
        if raid.get('source') == 'bounty':
            embed.title = '酒館懸賞｜' + safe_text(monster_name(raid['monster']), 32)
            embed.description += '\n\n本場不發金幣、不調整頻道動態難度，也不重排正常討伐時間。'
        if raid.get('source') == 'fishing':
            embed.title = '釣魚特殊討伐｜' + safe_text(monster_name(raid['monster']), 32)
            embed.description += f'\n\n發現者：<@{raid["discoverer_id"]}>｜本場採固定階級，不調整頻道動態難度。'
        embed.add_field(name='報名倒數', value=f'<t:{int(raid["deadline"])}:R> 開戰（報名 5 分鐘）', inline=False)
        minimum = raid_min_level(raid)
        requirement = f'｜需 Lv.{minimum}' if minimum > 1 else ''
        embed.add_field(name=f'參與者 {len(raid["members"])}/{channel_settings.max_participants}{requirement}',
                        value=' '.join(f'<@{uid}>' for uid in raid['members']) or '等待冒險者加入', inline=False)
        if raid['monster']['kind'] == SPECIAL_KIND:
            primary = PAINT_COLOR_NAMES[raid['monster']['primary_color']]
            embed.add_field(name='階級／起始顏料', value=f'四階｜{primary}', inline=False)
        balance_version = raid['monster'].get('balance_version', 1)
        v2 = balance_version >= 2
        scaling_text = ('；特殊討伐採固定四階強度，不套用品質或頻道動態難度，人數只調整血量。'
                        if raid['monster']['kind'] == SPECIAL_KIND else
                        '；以階級與品質的固定內容等級為基準，人數只調整血量。'
                        if balance_version >= 3 else
                        '；以內容階級為基準，隊伍平均等級主要調整血量並小幅調整攻擊。'
                        if v2 else '；開戰時按隊伍人數及等級決定強度。')
        embed.add_field(name='魔物特性', value=traits + scaling_text, inline=False)
        if raid.get('difficulty'):
            d = raid['difficulty']
            if balance_version >= 3:
                difficulty_text = (f'{d["current"]:.3f}（HP／攻擊／防禦套用 100%／40%／10% 幅度）\n'
                                   '勝利依回合數上升 4%／2%／1%，平手／戰敗大幅下降；普通／精英／首領／傳說調整權重為 100%／50%／25%／10%；範圍 1–2.5。')
            elif v2:
                difficulty_text = (f'{d["current"]:.3f}（HP 完整套用、攻擊套用 40% 幅度、防禦不變）\n'
                                   '依完成回合與削減 HP 微調；精英半額，首領／傳說不調整；範圍 0.9–1.1。')
            else:
                difficulty_text = (f'{d["current"]:.3f} × 本場設定 {d["base_strength"]:g}\n'
                                   '勝利後 ×1.1，戰敗／回合上限後 ×0.9；動態難度範圍 0.5–3 倍。')
            embed.add_field(name='頻道動態難度', value=difficulty_text, inline=False)
        pool = raid.get('drop_pool', DROP_TABLES.get(raid['monster']['kind'], ()))
        category = '討伐飾品' if pool and all(ITEMS[key].slot == '飾品' for key in pool) else '專屬裝備'
        loot_text = ('不掉落飾品或其他裝備' if not pool or raid['monster']['kind'] == '史萊姆群'
                     else f'{policy.drop_chance * 100:g}% 機率取得{category}（可能重複）')
        if raid['monster']['kind'] in {'熔爐鎧獸', '迷霧菌后', '星蝕巨神', '逆潮聖骸'} and pool:
            loot_text += '；掉落池：' + '、'.join(ITEMS[key].name for key in pool)
        if raid['monster']['kind'] == SPECIAL_KIND:
            chance_drop = raid.get('chance_drop')
            loot_text += '；裝備有 50% 機率符合自身職業，皆可使用噴漆染色'
            if chance_drop:
                loot_text += (f'；勝利時全隊抽 {chance_drop.get("rolls", 1)} 次噴漆罐，'
                              f'每次有 {chance_drop["chance"] * 100:g}% 機率掉落 1 個隨機顏色，'
                              '每罐隨機給一名參戰者')
            loot_text += '；另有 2% 機率取得未完成的魔女畫作'
        if raid.get('fixed_drop'):
            if raid.get('fixed_drop_mode', 'per_participant') == 'single_random':
                loot_text += f'；勝利時全隊固定掉落 1 個 {ITEMS[raid["fixed_drop"]].name}，隨機給一名參戰者'
            else:
                loot_text += f'；勝利每人固定取得 {ITEMS[raid["fixed_drop"]].name}'
        food_drop = raid.get('food_drop')
        if raid.get('fishing_reward'):
            reward = raid['fishing_reward']
            loot_text += (f'；勝利每人取得 {ITEMS[reward["item"]].name} ×{reward["quantity"]}'
                          f'，發現者實際參戰再多 {reward["discoverer_bonus"]} 份（幸運・餘韻・盛宴）')
        if food_drop:
            loot_text += (f'；另獨立以 {food_drop["chance"] * 100:g}% 判定料理食材，'
                          f'成功時 95% 為 {ITEMS[food_drop["meat"]].name}、'
                          f'5% 為 {ITEMS[food_drop["seasoning"]].name}')
        proof_count = (RAID_PROOFS_BY_POOL.get(raid.get('pool', 'regular'), 0)
                       if raid.get('source') != 'admin' and raid.get('pool') != 'special' else 0)
        if proof_count:
            loot_text += f'；勝利每人取得 {proof_count} 個討伐之證'
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
        if raid['monster']['kind'] == '赤雷與蒼炎':
            revive_job = battle.mechanics.get('twin_revive_job')
            state = ('共鳴穩定' if not revive_job else
                     f'{revive_job} 將在 {max(0, battle.mechanics.get("twin_revive_round", battle.round) - battle.round)} 回合後復活'
                     + ('（已延後）' if battle.mechanics.get('twin_revive_delayed') else ''))
            embed.add_field(name='雙生共鳴', value=state, inline=False)
        elif raid['monster']['kind'] == '吞城鯨':
            state = (f'城塞鯨脂 {battle.mechanics.get("whale_shield", 0)} 層｜'
                     f'水位 {battle.mechanics.get("whale_tide", 0)}/100')
            if battle.mechanics.get('whale_swallow_charging'):
                state += '｜正在蓄力吞城'
            embed.add_field(name='潮汐狀態', value=state, inline=False)
        elif raid['monster']['kind'] == '熔爐鎧獸':
            state = f'爐甲 {battle.mechanics.get("furnace_armor", 0)} 層'
            if battle.mechanics.get('furnace_charging'):
                state += '｜正在蓄力爐心震爆'
            embed.add_field(name='熔爐狀態', value=state, inline=False)
        elif raid['monster']['kind'] == '迷霧菌后':
            swelling = [f.name for f in enemies if f.hp > 0 and f.has('spore_swelling', battle.round)]
            embed.add_field(name='孢子狀態', value=('膨脹中：' + '、'.join(swelling)) if swelling else '目前沒有膨脹孢子', inline=False)
        elif raid['monster']['kind'] == '星蝕巨神':
            core = next((f for f in enemies if f.job == '蝕光星核' and f.hp > 0), None)
            state = f'蝕光星核：{"存活" if core else "休眠"}'
            if battle.mechanics.get('star_charging'):
                state += '｜正在蓄力星蝕墜落'
            embed.add_field(name='星蝕狀態', value=state, inline=False)
        elif raid['monster']['kind'] == '逆潮聖骸':
            circle = next((f for f in enemies if f.job == '逆潮法陣' and f.hp > 0), None)
            marks = sum(f.status_stacks.get('drowning_mark', 0)
                        for f in battle.fighters if f.team == 0 and f.hp > 0)
            embed.add_field(name='逆潮狀態', value=f'法陣：{"蓄力中" if circle else "無"}｜全隊溺印 {marks} 層', inline=False)
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
            lines = [f'<@{r["id"]}>：+{r["xp"]} XP、+{r.get("gold", 0)} 金幣'
                     + (f'、{ITEMS[r["item"]].name}' if r['item'] else '')
                     + (f'、{ITEMS[r["fixed_item"]].name}' if r.get('fixed_item') else '')
                     + (f'、{ITEMS[r["chance_item"]].name}' if r.get('chance_item') else '')
                     + ''.join(f'、{ITEMS[item].name}' for item in r.get('chance_items', ()))
                     + (f'、{ITEMS[r["extra_item"]].name}' if r.get('extra_item') else '')
                     + (f'、{ITEMS[r["food_item"]].name}' if r.get('food_item') else '')
                     + (f'、{ITEMS[r["fishing_item"]].name} ×{r["fishing_quantity"]}'
                        if r.get('fishing_item') else '') for r in raid['rewards']]
            lines = [line + (f'、討伐之證 ×{reward["raid_proofs"]}'
                             if reward.get('raid_proofs') else '')
                     for line, reward in zip(lines, raid['rewards'])]
            embed.add_field(name='獎勵已入帳', value='\n'.join(lines)[:1024], inline=False)
            players = [fighter for fighter in battle.fighters if fighter.team == 0]
            players.sort(key=lambda fighter: (fighter.combat_stats['direct_damage']
                                              + fighter.combat_stats['support_damage']), reverse=True)
            summary = []
            for fighter in players:
                stats = fighter.combat_stats
                label = f'<@{fighter.user_id}>' if fighter.user_id else safe_text(fighter.name, 24)
                summary.append(f'{label}：實際傷害 {stats["direct_damage"]:,}｜輔助傷害 {stats["support_damage"]:,}｜'
                               f'治療 {stats["healing_done"]:,}｜'
                               f'承傷 {stats["damage_taken"]:,}｜命中 {stats["hits"]}/{stats["attacks"]}')
            embed.add_field(name='戰鬥結算｜依實際＋輔助傷害排序',
                            value='\n'.join(summary)[:1024] or '沒有戰鬥數據', inline=False)
        embed.set_footer(text='準備型增益優先，其餘依速度排序；每回合每人一次行動。完整戰報於結束後附上。')
        return embed

    async def advance(self, raid, channel, now):
        message = channel.get_partial_message(raid['message_id'])
        settings = self.settings_for_channel(raid['channel_id'])
        if raid['status'] == 'lobby':
            expected_channels = (self.special_channels if raid.get('source') == 'fishing' else
                                 self.high_channels if raid.get('pool') == 'high' else
                                 self.mid_channels if raid.get('pool') in ('mid', 'special') else self.channels)
            if raid['channel_id'] not in expected_channels or not settings.enabled:
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
                    if state['level'] < raid_min_level(raid):
                        continue
                    passive = self.cog.tactics.passive(raid['guild_id'], uid, state['job'])
                    participants.append(dict(id=uid, name=safe_text(member.display_name, 16), state=state,
                                             rules=[asdict(r) for r in self.cog.tactics.rules(raid['guild_id'], uid, state['job'])],
                                             basic_target=self.cog.tactics.basic_target(raid['guild_id'], uid, state['job']),
                                             passive_id=passive.id if passive else None))
                divinations = getattr(self.cog, 'divinations', None)
                fortunes = (divinations.prepare_for_raid(
                    raid['id'], raid['guild_id'], [participant['id'] for participant in participants])
                    if participants and divinations is not None else {})
                for participant in participants:
                    if participant['id'] in fortunes:
                        participant['fortune'] = fortunes[participant['id']]
                tavern = getattr(self.cog, 'tavern', None)
                drinks = (tavern.store.prepare_for_raid(
                    raid['id'], raid['guild_id'], [participant['id'] for participant in participants])
                    if participants and tavern is not None else {})
                for participant in participants:
                    if participant['id'] in drinks:
                        participant['tavern'] = drinks[participant['id']]
                provisions = getattr(self.cog, 'provisions', None)
                if participants and provisions is not None:
                    prepared = provisions.prepare_for_raid(
                        raid['id'], raid['guild_id'], [participant['id'] for participant in participants],
                        preserve_users=[participant['id'] for participant in participants
                                        if participant.get('fortune', {}).get('id') == 'temperance'])
                    for participant in participants:
                        if participant['id'] in prepared:
                            participant['meal'] = prepared[participant['id']]
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
                raid = self.repo.settle(raid['id'], raid['battle'], settings)
            else:
                self.repo.save(raid)
                await message.edit(embed=self.battle_embed(raid, battle), view=None, allowed_mentions=discord.AllowedMentions.none())
                return
        if raid['status'] == 'completed':
            battle = load_battle(raid['battle'])
            report = '\n'.join(battle.log) + '\n\n戰鬥結算：\n' + '\n'.join(
                f'{f.user_id or f.name}: 實際傷害 {f.combat_stats["direct_damage"]}, '
                f'輔助傷害 {f.combat_stats["support_damage"]}, 治療 {f.combat_stats["healing_done"]}, '
                f'承傷 {f.combat_stats["damage_taken"]}, 命中 {f.combat_stats["hits"]}/{f.combat_stats["attacks"]}, '
                f'暴擊 {f.combat_stats["critical_hits"]}, 技能 {json.dumps(f.combat_stats["skills_used"], ensure_ascii=False)}'
                for f in battle.fighters if f.team == 0) + '\n\n獎勵：\n' + '\n'.join(
                f'{r["id"]}: {r["xp"]} XP, {r.get("gold", 0)} 金幣, '
                f'{ITEMS[r["item"]].name if r["item"] else "無隨機掉落"}'
                f'{", " + ITEMS[r["fixed_item"]].name if r.get("fixed_item") else ""}'
                f'{", " + ITEMS[r["chance_item"]].name if r.get("chance_item") else ""}'
                f'{"".join(", " + ITEMS[item].name for item in r.get("chance_items", ()))}'
                f'{", " + ITEMS[r["extra_item"]].name if r.get("extra_item") else ""}'
                f'{", " + ITEMS[r["food_item"]].name if r.get("food_item") else ""}'
                f'{", " + ITEMS[r["fishing_item"]].name + " ×" + str(r["fishing_quantity"]) if r.get("fishing_item") else ""}'
                f'{", 討伐之證 ×" + str(r["raid_proofs"]) if r.get("raid_proofs") else ""}'
                for r in raid['rewards'])
            await message.edit(embed=self.battle_embed(raid, battle), view=None,
                               attachments=[discord.File(io.BytesIO(report.encode('utf-8')), filename='討伐戰報.txt')],
                               allowed_mentions=discord.AllowedMentions.none())
        elif raid['status'] == 'cancelled':
            await message.edit(content=raid.get('reason', '本次討伐取消。'), embed=None, view=None)
        raid['delivered'] = True
        self.repo.save(raid)
        self.rosters.pop(raid['id'], None)
        if not raid.get('preserve_schedule'):
            self.next_spawn(raid['channel_id'], now)

    async def spawn(self, channel, *, kind=None, name=None, strength=1.0,
                    victory_xp=None, victory_gold=None, drop_percent=None,
                    initial_member=None, return_raid=False, preserve_schedule=False,
                    use_dynamic=True, source=None, payment_user=None, payment_gold=0,
                    use_shuffle_bag=True):
        if kind is not None and kind not in REGULAR_KINDS + MID_KINDS + HIGH_RAID_KINDS:
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
        if not isinstance(channel, discord.TextChannel) or channel.id not in self.all_channels:
            raise CharacterError('請在已設定的討伐文字頻道使用此指令。')
        pool = 'high' if channel.id in self.high_channels else 'mid' if channel.id in self.mid_channels else 'regular'
        settings = self.settings_for_channel(channel.id)
        allowed = HIGH_RAID_KINDS if pool == 'high' else MID_KINDS if pool == 'mid' else REGULAR_KINDS
        if kind is not None and kind not in allowed:
            label = {'regular': '一般', 'mid': '中階', 'high': '高階'}[pool]
            raise CharacterError(f'這個頻道只能生成{label}討伐怪物。')
        if not settings.enabled:
            label = {'regular': '一般', 'mid': '中階', 'high': '高階'}[pool]
            raise CharacterError(f'{label}討伐活動目前已停用。')
        if channel.guild.unavailable:
            raise CharacterError('伺服器暫時無法使用，請稍後再試。')
        pending = self.repo.pending()
        if (channel.id in self.spawning or channel.guild.id in self.spawning_guilds
                or any(r['guild_id'] == channel.guild.id for r in pending)):
            raise CharacterError('本伺服器已有討伐或結果待送出，請等待完成。')
        # Reserve the guild before the AI call so no two raid channels can publish together.
        self.spawning.add(channel.id)
        self.spawning_guilds.add(channel.guild.id)
        task = asyncio.current_task()
        self.spawn_tasks.add(task)
        if not preserve_schedule:
            self.next_spawn(channel.id, time.time())
        raid = None
        bag_draw = False
        try:
            if kind is None and pool == 'mid' and use_shuffle_bag:
                kind = self.repo.next_mid_kind(channel.id, MID_KINDS)
                bag_draw = True
            elif kind is None and pool == 'high' and use_shuffle_bag:
                kind = self.repo.next_high_kind(channel.id, HIGH_RAID_KINDS)
                bag_draw = True
            elif kind is None and pool == 'mid':
                kind = random.choice(MID_KINDS)
            elif kind is None and pool == 'high':
                kind = random.choice(HIGH_RAID_KINDS)
            monster = dict(await self.imagine(kind) if kind is not None else await self.imagine())
            monster['strength'] = strength
            if name is not None:
                monster['name'] = name.strip()
            monster = prepare_monster(monster)
            raid = self.repo.create(channel.guild.id, channel.id, monster, time.time(), asdict(settings), overrides,
                                    pool=pool, use_dynamic=use_dynamic,
                                    payment_user=payment_user, payment_gold=payment_gold)
            raid['source'] = source
            raid['preserve_schedule'] = preserve_schedule
            if initial_member is not None:
                raid['members'] = [initial_member]
            raid['bag_draw'] = bag_draw
            self.repo.save(raid)
            role = None
            try:
                role = await self.notifications.ensure(channel.guild, pool)
            except (CharacterError, discord.HTTPException) as exc:
                logger.warning('Raid notification role unavailable for guild %s: %s', channel.guild.id, exc)
            message = await channel.send(content=role.mention if role else None,
                                         embed=self.lobby_embed(raid), view=self.signup(raid),
                                         allowed_mentions=discord.AllowedMentions(everyone=False, users=False,
                                                                                 roles=[role] if role else [], replied_user=False))
            raid.update(message_id=message.id, status='lobby', deadline=time.time() + 300)
            self.repo.save(raid)
            return (message, raid) if return_raid else message
        except (Exception, asyncio.CancelledError):
            if raid is not None:
                raid.update(status='cancelled', delivered=True)
                self.repo.save(raid)
                self.repo.refund_payment(raid['id'])
                view = self.views.pop(raid['id'], None)
                if view:
                    view.stop()
            if bag_draw:
                if pool == 'high':
                    self.repo.return_high_kind(channel.id, kind)
                else:
                    self.repo.return_mid_kind(channel.id, kind)
            raise
        finally:
            self.spawning.discard(channel.id)
            self.spawning_guilds.discard(channel.guild.id)
            self.spawn_tasks.discard(task)

    async def publish_fishing_encounters(self, guild_id=None):
        from core.rpg_fishing_raids import publish_fishing_encounters
        await publish_fishing_encounters(self, guild_id)

    async def summon_bounty(self, guild, user, pool, price):
        """Publish an extra paid raid without moving the normal channel schedule."""
        if user.bot:
            raise CharacterError('機器人不能張貼懸賞。')
        if pool not in ('regular', 'mid', 'high'):
            raise CharacterError('無效的懸賞類型。')
        state = self.cog.characters.snapshot(guild.id, user.id)
        minimum = HIGH_RAID_MIN_LEVEL if pool == 'high' else MID_RAID_MIN_LEVEL if pool == 'mid' else 1
        if state['level'] < minimum:
            label = {'mid': '中階', 'high': '高階'}[pool]
            raise CharacterError(f'張貼{label}懸賞需達 Lv.{minimum}。')
        channel_ids = self.high_channels if pool == 'high' else self.mid_channels if pool == 'mid' else self.channels
        channels = sorted((self.bot.get_channel(channel_id) for channel_id in channel_ids),
                          key=lambda channel: channel.id if channel else 0)
        channels = [channel for channel in channels if isinstance(channel, discord.TextChannel)
                    and channel.guild.id == guild.id and self.settings_for_channel(channel.id).enabled]
        if not channels:
            label = {'regular': '一般', 'mid': '中階', 'high': '高階'}[pool]
            raise CharacterError(f'這個伺服器尚未設定已啟用的{label}討伐文字頻道。')
        if guild.unavailable:
            raise CharacterError('伺服器暫時無法使用，請稍後再試。')
        if any(raid['status'] in ('lobby', 'running') and user.id in raid.get('members', ())
               for raid in self.repo.pending()):
            raise CharacterError('你已參與另一場討伐，請先完成或退出。')
        occupied = {raid['channel_id'] for raid in self.repo.pending()
                    if raid['status'] in ('posting', 'lobby', 'running')}
        channel = next((item for item in channels
                        if item.id not in occupied and item.id not in self.spawning), None)
        if channel is None:
            label = {'regular': '一般', 'mid': '中階', 'high': '高階'}[pool]
            raise CharacterError(f'目前所有可用的{label}討伐頻道都有活動，請稍後再試。')
        message, raid = await self.spawn(
            channel, victory_gold=0, initial_member=user.id, return_raid=True,
            preserve_schedule=True, use_dynamic=False, source='bounty',
            payment_user=user.id, payment_gold=price, use_shuffle_bag=False)
        return channel, message, raid

    async def summon_divination(self, guild, user):
        """Use the High Priestess to publish and join the highest eligible raid pool."""
        if user.bot:
            raise CharacterError('機器人不能使用占卜發起討伐。')
        state = self.cog.characters.snapshot(guild.id, user.id)
        pool = ('high' if state['level'] >= HIGH_RAID_MIN_LEVEL else
                'mid' if state['level'] >= MID_RAID_MIN_LEVEL else 'regular')
        channel_ids = self.high_channels if pool == 'high' else self.mid_channels if pool == 'mid' else self.channels
        channels = sorted((self.bot.get_channel(channel_id) for channel_id in channel_ids),
                          key=lambda channel: channel.id if channel else 0)
        channels = [channel for channel in channels if isinstance(channel, discord.TextChannel)
                    and channel.guild.id == guild.id and self.settings_for_channel(channel.id).enabled]
        if not channels:
            label = {'regular': '一般', 'mid': '中階', 'high': '高階'}[pool]
            raise CharacterError(f'這個伺服器尚未設定已啟用的{label}討伐文字頻道。')
        if guild.unavailable:
            raise CharacterError('伺服器暫時無法使用，請稍後再試。')
        if any(raid['status'] in ('lobby', 'running') and user.id in raid.get('members', ())
               for raid in self.repo.pending()):
            raise CharacterError('你已參與另一場討伐，請先完成或退出。')
        occupied = {raid['channel_id'] for raid in self.repo.pending()
                    if raid['status'] in ('posting', 'lobby', 'running')}
        channel = next((item for item in channels
                        if item.id not in occupied and item.id not in self.spawning), None)
        if channel is None:
            label = {'regular': '一般', 'mid': '中階', 'high': '高階'}[pool]
            raise CharacterError(f'目前所有可用的{label}討伐頻道都有活動，請稍後再試。')

        self.cog.divinations.reserve_summon(guild.id, user.id)
        try:
            _, raid = await self.spawn(channel, initial_member=user.id, return_raid=True)
            self.cog.divinations.finish_summon(guild.id, user.id, raid['id'])
            return channel, raid
        except (Exception, asyncio.CancelledError):
            self.cog.divinations.release_summon(guild.id, user.id)
            raise

    async def summon_noah(self, guild, user):
        """Consume a paint set and publish the fixed special raid in this guild's mid channel."""
        if user.bot:
            raise CharacterError('機器人不能召喚特殊討伐。')
        state = self.cog.characters.snapshot(guild.id, user.id)
        if state['level'] < MID_RAID_MIN_LEVEL:
            raise CharacterError(f'使用噴漆罐套組需達 Lv.{MID_RAID_MIN_LEVEL}。')
        channels = sorted((self.bot.get_channel(cid) for cid in self.mid_channels),
                          key=lambda channel: channel.id if channel else 0)
        channels = [channel for channel in channels if isinstance(channel, discord.TextChannel)
                    and channel.guild.id == guild.id]
        if not channels:
            raise CharacterError('這個伺服器尚未設定中階討伐文字頻道。')
        active = [raid for raid in self.repo.pending() if raid['guild_id'] == guild.id]
        if active or guild.id in self.spawning_guilds:
            raise CharacterError('目前已有討伐或結果待送出，暫時不能使用噴漆罐套組。')
        if any(raid['status'] in ('lobby', 'running') and user.id in raid.get('members', ())
               for raid in self.repo.pending()):
            raise CharacterError('你已參與另一場討伐，請先完成或退出。')
        channel = next((item for item in channels if self.settings_for_channel(item.id).enabled), None)
        if channel is None:
            raise CharacterError('這個伺服器的中階討伐活動目前已停用。')
        if guild.unavailable:
            raise CharacterError('伺服器暫時無法使用，請稍後再試。')

        self.spawning.add(channel.id)
        self.spawning_guilds.add(guild.id)
        task = asyncio.current_task()
        self.spawn_tasks.add(task)
        raid = None
        consumed = False
        try:
            primary_color = random.choice(('red', 'yellow', 'blue'))
            monster = prepare_monster(dict(
                kind=SPECIAL_KIND, name=SPECIAL_KIND, strength=1.0, primary_color=primary_color,
                description='她把王城牆面當成畫布，單色顏料逐漸交疊成一幅危險的未完成構圖。'), quality='普通')
            settings = self.settings_for_channel(channel.id)
            raid = self.repo.create(guild.id, channel.id, monster, time.time(), asdict(settings),
                                    pool='special', use_dynamic=False)
            raid['preserve_schedule'] = True
            raid['members'] = [user.id]
            self.repo.save(raid)
            self.cog.characters.consume_item(guild.id, user.id, 'paint:set')
            consumed = True
            role = None
            try:
                role = await self.notifications.ensure(guild, 'mid')
            except (CharacterError, discord.HTTPException) as exc:
                logger.warning('Raid notification role unavailable for guild %s: %s', guild.id, exc)
            message = await channel.send(content=role.mention if role else None,
                                         embed=self.lobby_embed(raid), view=self.signup(raid),
                                         allowed_mentions=discord.AllowedMentions(everyone=False, users=False,
                                                                                 roles=[role] if role else [], replied_user=False))
            raid.update(message_id=message.id, status='lobby', deadline=time.time() + 300)
            self.repo.save(raid)
            return channel, message, raid
        except (Exception, asyncio.CancelledError):
            if consumed:
                self.cog.characters.grant_item(guild.id, user.id, 'paint:set')
            if raid is not None:
                raid.update(status='cancelled', delivered=True)
                self.repo.save(raid)
                view = self.views.pop(raid['id'], None)
                if view:
                    view.stop()
            raise
        finally:
            self.spawning.discard(channel.id)
            self.spawning_guilds.discard(channel.guild.id)
            self.spawn_tasks.discard(task)

    @tasks.loop(seconds=5)
    async def tick(self):
        if not self.bot.is_ready():
            return
        now = time.time()
        pending = self.repo.pending()
        for raid in pending:
            if raid['status'] == 'posting':
                continue
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
                    divinations = getattr(self.cog, 'divinations', None)
                    if divinations is not None:
                        divinations.clear_raid(raid['id'])
                raid['delivered'] = True
                self.repo.save(raid)
                if not raid.get('preserve_schedule'):
                    self.next_spawn(raid['channel_id'], now)
                view = self.views.pop(raid['id'], None)
                if view:
                    view.stop()
            except Exception:
                logger.exception('Raid update failed: %s', raid['id'])
        if not self.settings.enabled and not self.mid_settings.enabled and not self.high_settings.enabled:
            return
        await self.publish_fishing_encounters()
        current = self.repo.pending()
        occupied = {r['channel_id'] for r in current}
        busy_guilds = {r['guild_id'] for r in current} | self.spawning_guilds
        due_channels = []
        for channel_id in self.all_channels - occupied:
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.guild.unavailable:
                continue
            if not self.settings_for_channel(channel_id).enabled:
                continue
            due = self.repo.next_at(channel_id)
            if due is None:
                self.next_spawn(channel_id, now)
                continue
            if now < due:
                continue
            due_channels.append((due, channel_id, channel))
        # Keep colliding deadlines overdue. Once the current raid is delivered,
        # the oldest waiting channel is published on the next scheduler tick.
        for _, channel_id, channel in sorted(due_channels):
            if channel.guild.id in busy_guilds:
                continue
            try:
                await self.spawn(channel)
                busy_guilds.add(channel.guild.id)
            except CharacterError:
                continue  # A manual spawn may already be generating this channel's monster.
            except Exception:
                logger.exception('Raid spawn failed in channel %s', channel_id)

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
