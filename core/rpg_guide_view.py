"""Interactive raid and monster guide opened by /攻略."""
import asyncio

import discord

from core.rpg_fishing_bosses import FISHING_BOSSES
from core.rpg_monsters import MONSTER_GUIDES, PROFILES
from core.rpg_painted_maze import COLOR_CONTRACTS, ENTRY_ROUTES, MAX_PARTICIPANTS, MIN_LEVEL, PAINTING_STAGES
from core.rpg_raids import HIGH_RAID_KINDS, MID_KINDS, REGULAR_KINDS, SPECIAL_KIND
from core.rpg_witch_battle import SPELL_DESCRIPTIONS, WITCH_TRAITS
from core.rpg_witch_catalog import PROFILE
from core.rpg_witch_rest import ENTRY_PROOFS, MAX_ENRAGE, MIN_LEVEL as WITCH_MIN_LEVEL, WITCHES


CATEGORIES = {
    'regular': ('一般討伐', REGULAR_KINDS),
    'mid': ('中階討伐', MID_KINDS),
    'high': ('高階討伐', HIGH_RAID_KINDS),
    'special': ('特殊討伐', (SPECIAL_KIND, *tuple(boss.name for boss in FISHING_BOSSES.values()))),
    'maze': ('繪境迷宮', ('模式總覽', '契約與路線')),
    'witch': ('魔女模式', ('魔女試煉', *tuple(row[1] for row in PROFILE.values()),
                           *tuple(f'安息・{witch.name}' for witch in WITCHES.values()))),
}

MODE_TEXT = {
    'regular': ('由一般討伐頻道定時出現，五分鐘報名後自動戰鬥；無最低等級。'
                '怪物為 0～2 階，品質與頻道動態難度會影響強度及獎勵。'),
    'mid': ('由中階討伐頻道出現，需 Lv.30；五分鐘報名後自動戰鬥。'
            '怪物為 3～4 階，機制比一般討伐更要求集火、淨化、打斷或同步控血。'),
    'high': ('由高階討伐頻道出現，需 Lv.50；五分鐘報名後自動戰鬥。'
             '怪物為 5～6 階，具有必須處理的護甲、召喚物或蓄力機制。'),
    'special': ('包含道具召喚的城崎諾亞與釣魚觸發的特殊 Boss。'
                '它們在特殊討伐規則下使用固定階級，不調整頻道動態難度。'),
    'maze': (f'繪境迷宮需 Lv.{MIN_LEVEL}，最多 {MAX_PARTICIPANTS} 人，以畫作道具開啟。'
             '隊伍連闖三幕，每幕選擇畫中敵人並取得契約；契約同時強化隊伍與最終敵人。'),
    'witch': (f'魔女試煉是三名隨機魔女組成的逐回合手動戰鬥。魔女安息儀式需 Lv.{WITCH_MIN_LEVEL}，'
              f'正式挑戰每人消耗討伐之證 ×{ENTRY_PROOFS}；魔女化 0～99% 為 1～6 人自動戰鬥，'
              f'100～{MAX_ENRAGE:,}% 為 3～6 人手動戰鬥。'),
}

REST_GUIDES = {
    '櫻羽艾瑪': ('普攻會施加有罪狀態，需安排淨化；有罪會強化後續處刑。留意判決護盾與最終階段，'
              '把爆發技能保留給護盾解除或本體破綻，避免高層數有罪同時爆發。'),
    '二階堂希羅': ('核心是公開歷史與死亡回溯。預告「看穿」後避免重複上一輪的行動，否則會遭追擊；'
               '第一次擊倒可能回溯復活，保留資源處理回溯後的收尾階段。'),
}


def _monster_embed(category, name, kind=None):
    kind = kind or name
    tier, _, _, _, speed, hit, dodge, crit = PROFILES[kind]
    count = 3 if kind in ('史萊姆群', '哥布林戰團', '王城傀儡師', '迷霧菌后') else 2 if kind in ('赤雷與蒼炎', '星蝕巨神') else 1
    embed = discord.Embed(title=f'討伐攻略｜{name}', color=0xB565D9,
                          description=MONSTER_GUIDES[kind])
    embed.add_field(name='戰鬥資料', value=(f'{tier} 階｜{count} 個戰鬥單位｜速度 {speed}\n'
                                           f'命中 {hit}｜迴避 {dodge}｜暴擊 {crit}%'), inline=False)
    embed.add_field(name='所屬模式', value=MODE_TEXT[category], inline=False)
    return embed


def guide_embed(category, entry='overview'):
    label, entries = CATEGORIES[category]
    if entry == 'overview':
        names = '、'.join(entries)
        embed = discord.Embed(title=f'討伐攻略｜{label}', color=0x8B5CF6,
                              description=MODE_TEXT[category])
        embed.add_field(name='可查閱內容', value=names, inline=False)
        return embed

    if category in ('regular', 'mid', 'high'):
        return _monster_embed(category, entry)
    if category == 'special':
        if entry == SPECIAL_KIND:
            return _monster_embed(category, entry)
        boss = next(boss for boss in FISHING_BOSSES.values() if boss.name == entry)
        embed = _monster_embed(category, boss.name, boss.kind)
        embed.insert_field_at(0, name='觸發與獎勵', value=(
            f'釣魚每次實際捕獲有 0.1% 機率遭遇，每趟最多一隻；戰鬥機制沿用「{boss.kind}」。\n'
            f'勝利取得品質 {boss.quality} 食材「{boss.ingredient_name}」。'), inline=False)
        return embed
    if category == 'maze':
        embed = discord.Embed(title=f'討伐攻略｜繪境迷宮・{entry}', color=0x7C3AED,
                              description=MODE_TEXT['maze'])
        if entry == '模式總覽':
            routes = '、'.join(ENTRY_ROUTES)
            stages = ['第 ' + str(index) + ' 幕：' + '、'.join(p['name'] for p in stage)
                      for index, stage in enumerate(PAINTING_STAGES, 1)]
            embed.add_field(name='入場畫作', value=routes, inline=False)
            embed.add_field(name='三幕敵人', value='\n'.join(stages), inline=False)
            embed.add_field(name='建議', value='先確認隊伍缺少輸出、生存或速度，再投票契約；最終敵人會吃到每份契約的反噬。', inline=False)
        else:
            base = [contract for key, contract in COLOR_CONTRACTS.items() if ':' not in key]
            embed.add_field(name='契約效果', value='\n'.join(
                f'**{c["name"]}**：{c["party"]}\n反噬：{c["backlash"]}' for c in base), inline=False)
        return embed

    if entry == '魔女試煉':
        embed = discord.Embed(title='討伐攻略｜魔女試煉', color=0xDC2626,
                              description=MODE_TEXT['witch'])
        embed.add_field(name='戰鬥重點', value=(
            '每回合先讀取三名魔女的行動預告，再選擇普攻、技能或防禦；所有人確認後結算。'
            '集火、拆除召喚物、淨化與防禦都要依預告調整，不能只用固定輪轉。'), inline=False)
        embed.add_field(name='魔法強度', value='一段／二段／三段魔女化會逐步升級每名魔女的法術。', inline=False)
        return embed
    if entry.startswith('安息・'):
        name = entry.removeprefix('安息・')
        embed = discord.Embed(title=f'討伐攻略｜魔女安息・{name}', color=0xA855F7,
                              description=REST_GUIDES[name])
        embed.add_field(name='模式規則', value=MODE_TEXT['witch'], inline=False)
        return embed

    witch_id = next(key for key, row in PROFILE.items() if row[1] == entry)
    embed = discord.Embed(title=f'討伐攻略｜魔女試煉・{entry}', color=0xDC2626,
                          description=WITCH_TRAITS[witch_id])
    embed.add_field(name='各階段法術', value='\n'.join(
        f'{index + 1} 段：{text}' for index, text in enumerate(SPELL_DESCRIPTIONS[witch_id])), inline=False)
    embed.add_field(name='應對原則', value='依當回合預告決定集火、淨化、拆物件或防禦；需要打斷時優先處理該魔女。', inline=False)
    return embed


class GuideSelect(discord.ui.Select):
    def __init__(self, kind, options, placeholder, row):
        super().__init__(placeholder=placeholder, options=options, row=row)
        self.kind = kind

    async def callback(self, interaction):
        await self.view.handle(interaction, self.kind, self.values[0])


class RaidGuideView(discord.ui.View):
    def __init__(self, interaction):
        super().__init__(timeout=180)
        self.origin = interaction
        self.owner_id = interaction.user.id
        self.category, self.entry = 'regular', 'overview'
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        self.add_item(GuideSelect('category', [
            discord.SelectOption(label=label, value=key, default=key == self.category)
            for key, (label, _) in CATEGORIES.items()], '選擇討伐分類', 0))
        self.add_item(GuideSelect('entry', [discord.SelectOption(
            label='模式總覽' if self.category != 'maze' else '分類總覽', value='overview',
            default=self.entry == 'overview'), *[
                discord.SelectOption(label=name, value=name, default=name == self.entry)
                for name in CATEGORIES[self.category][1]]], '選擇模式或怪物', 1))
        close = discord.ui.Button(label='關閉', row=2)
        close.callback = lambda interaction: self.handle(interaction, 'close')
        self.add_item(close)

    def embed(self):
        embed = guide_embed(self.category, self.entry)
        embed.set_footer(text='先選討伐分類，再選模式或怪物；僅自己可操作，閒置 3 分鐘後關閉。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('請使用 /攻略 開啟自己的攻略面板。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('攻略面板已關閉，請重新使用 /攻略。', ephemeral=True)
                return
            if action == 'category' and value in CATEGORIES:
                self.category, self.entry = value, 'overview'
            elif action == 'entry' and (value == 'overview' or value in CATEGORIES[self.category][1]):
                self.entry = value
            elif action == 'close':
                await interaction.response.edit_message(content='攻略面板已關閉。', embed=None, view=None)
                self.closed = True
                self.stop()
                return
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        if self.closed:
            return
        self.closed = True
        for child in self.children:
            child.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass

