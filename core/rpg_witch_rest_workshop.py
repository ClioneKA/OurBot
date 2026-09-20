"""Small Discord panel for Witch Rest equipment processing."""
import asyncio

import discord

from core.rpg_character import CharacterError, ITEMS
from core.rpg_menu import add_back
from core.rpg_witch_rest import JOBS, affix_kinds, reroll_cost


AFFIX_NAMES = {
    'vitality': '生命', 'assault': '攻勢', 'fortitude': '堅韌', 'prayer': '祈願',
    'precision': '精準', 'haste': '迅捷', 'critical': '會心', 'prowess': '威能',
    'stability': '穩定', 'drain': '汲取', 'evasion': '閃身', 'revival': '回生',
    'guard': '堅守', 'corrosion': '抗蝕', 'unyielding': '不屈',
}
ROMAN = ('', 'I', 'II', 'III', 'IV')


def affix_effect_text(kind, value):
    """Describe what an affix value changes instead of showing a bare number."""
    effects = {
        'vitality': f'HP {value:+}',
        'assault': f'攻擊 {value:+}',
        'fortitude': f'防禦 {value:+}',
        'prayer': f'治療量 {value:+}',
        'precision': f'命中值 {value:+}',
        'haste': f'速度 {value:+}',
        'critical': f'暴擊率 +{value} 個百分點',
        'prowess': f'暴擊傷害 +{value}%',
        'stability': f'武器穩定度下限 +{value} 個百分點',
        'drain': f'直接傷害吸血 +{value}%',
        'evasion': f'閃避值 {value:+}',
        'revival': f'受到治療量 +{value}%',
        'guard': f'受到的直接傷害 -{value}%',
        'corrosion': f'受到的持續傷害 -{value}%',
        'unyielding': f'HP 低於 35% 時受到傷害 -{value}%',
    }
    return effects[kind]


def affix_roll_text(kind, grade, value):
    return f'{AFFIX_NAMES[kind]} {ROMAN[int(grade)]}（{affix_effect_text(kind, value)}）'


def affix_text(row):
    _, affix_id, value = row
    kind, grade = affix_id.split(':')[1:]
    return affix_roll_text(kind, grade, value)


def improve_text(row):
    grade = int(row[1].rsplit(':', 1)[1])
    if grade >= 4:
        return '已達上限'
    chance, gold = {1: (100, 1_000), 2: (80, 4_000), 3: (50, 10_000)}[grade]
    return f'{chance}%／{gold:,}G'


class EquipmentSelect(discord.ui.Select):
    def __init__(self, view, equipment):
        super().__init__(placeholder='選擇魔女裝備實例', row=0, options=[
            discord.SelectOption(
                label=f'#{item["instance_id"]} {ITEMS[item["item_id"]].name}'[:100],
                value=str(item['instance_id']), default=item['instance_id'] == view.selected)
            for item in equipment[:25]])

    async def callback(self, interaction):
        view = self.view
        view.selected = int(self.values[0])
        view.rebuild()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class DirectAffixModal(discord.ui.Modal, title='使用定向詞條記憶'):
    kind = discord.ui.TextInput(label='目標詞條名稱', placeholder='例如：迅捷', max_length=12)

    def __init__(self, panel, affix_index):
        super().__init__()
        self.panel, self.affix_index = panel, affix_index

    async def on_submit(self, interaction):
        reverse = {name: key for key, name in AFFIX_NAMES.items()}
        kind = reverse.get(str(self.kind).strip(), str(self.kind).strip().lower())
        try:
            result = self.panel.cog.witch_rest.direct_affix(
                f'direct:{interaction.id}', self.panel.guild_id, self.panel.owner.id,
                self.panel.selected, self.affix_index, kind)
            notice = f'定向完成：{affix_roll_text(result["kind"], result["grade"], result["value"])}。'
        except CharacterError as exc:
            notice = str(exc)
        self.panel.rebuild()
        await interaction.response.edit_message(embed=self.panel.embed(notice), view=self.panel)


class RerollDecisionView(discord.ui.View):
    def __init__(self, panel, request_id, offer):
        super().__init__(timeout=180)
        self.panel, self.request_id, self.offer = panel, request_id, offer

    async def decide(self, interaction, accept):
        if interaction.user.id != self.panel.owner.id:
            await interaction.response.send_message('這不是你的重鑄結果。', ephemeral=True)
            return
        result = self.panel.cog.witch_rest.resolve_reroll(self.request_id, accept)
        self.stop()
        self.panel.rebuild()
        notice = ('已接受新詞條。' if result['accepted'] else
                  '已保留舊詞條；本次材料與重鑄次數不退回。')
        await interaction.response.edit_message(embed=self.panel.embed(notice), view=self.panel)

    @discord.ui.button(label='接受新詞條', style=discord.ButtonStyle.success)
    async def accept(self, interaction, _button):
        await self.decide(interaction, True)

    @discord.ui.button(label='保留舊詞條', style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, _button):
        await self.decide(interaction, False)


class DismantleConfirmView(discord.ui.View):
    def __init__(self, panel):
        super().__init__(timeout=60)
        self.panel = panel

    @discord.ui.button(label='確認分解', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _button):
        if interaction.user.id != self.panel.owner.id:
            await interaction.response.send_message('這不是你的裝備。', ephemeral=True)
            return
        try:
            result = self.panel.cog.witch_rest.dismantle(
                f'dismantle:{interaction.id}', self.panel.guild_id,
                self.panel.owner.id, self.panel.selected)
            notice = f'已分解：魔力核心 ×{result["cores"]}、記憶殘頁 ×{result["pages"]}。'
        except CharacterError as exc:
            notice = str(exc)
        self.stop()
        self.panel.selected = None
        self.panel.rebuild()
        await interaction.response.edit_message(embed=self.panel.embed(notice), view=self.panel)

    @discord.ui.button(label='取消', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _button):
        self.stop()
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class WitchRestWorkshopView(discord.ui.View):
    def __init__(self, cog, owner, guild_id, *, origin):
        super().__init__(timeout=180)
        self.cog, self.owner, self.guild_id, self.origin = cog, owner, guild_id, origin
        self.selected = None
        self.page = 0
        self.lock = asyncio.Lock()
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner.id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message('這不是你的魔女裝備工坊。', ephemeral=True)
        return False

    def item(self):
        return next((item for item in self.equipment if item['instance_id'] == self.selected), None)

    def button(self, label, action, row, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, style=style, disabled=self.selected is None)
        button.callback = lambda interaction: self.handle(interaction, action)
        self.add_item(button)

    def rebuild(self):
        self.equipment = self.cog.witch_rest.equipment(self.guild_id, self.owner.id)
        pages = max(1, (len(self.equipment) + 24) // 25)
        self.page %= pages
        visible = self.equipment[self.page * 25:(self.page + 1) * 25]
        if self.selected not in {item['instance_id'] for item in visible}:
            self.selected = visible[0]['instance_id'] if visible else None
        self.clear_items()
        if visible:
            self.add_item(EquipmentSelect(self, visible))
        self.button('重鑄前綴', 'reroll:0', 1)
        self.button('重鑄後綴', 'reroll:1', 1)
        self.button('記憶強化前綴', 'improve:0', 1)
        self.button('記憶強化後綴', 'improve:1', 1)
        self.button('定向前綴', 'direct:0', 2)
        self.button('定向後綴', 'direct:1', 2)
        self.button('昇階 T90', 'upgrade', 2, discord.ButtonStyle.primary)
        self.button('分解', 'dismantle', 2, discord.ButtonStyle.danger)
        for label, action in (('合成魔女結晶', 'crystal'), ('合成高級記憶', 'memory'),
                              ('關閉', 'close')):
            button = discord.ui.Button(label=label, row=3)
            button.callback = lambda interaction, action=action: self.handle(interaction, action)
            self.add_item(button)
        add_back(self, 3, 'tailor:dye', '返回漢娜的裁縫所')
        if pages > 1:
            for label, action in (('裝備上一頁', 'previous'), ('裝備下一頁', 'next')):
                button = discord.ui.Button(label=label, row=4)
                button.callback = lambda interaction, action=action: self.handle(interaction, action)
                self.add_item(button)

    def embed(self, notice=None):
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        wallet = self.cog.store.db.execute('SELECT gold FROM rpg_wallets WHERE guild_id=? AND user_id=?',
                                          (self.guild_id, self.owner.id)).fetchone()
        description = (f'金幣：{(wallet[0] if wallet else 0):,}\n'
                       f'魔女殘片 ×{counts.get("witch_rest:fragment", 0)}｜'
                       f'重鑄粉塵 ×{counts.get("witch_rest:dust", 0)}\n'
                       f'高級詞條記憶 ×{counts.get("witch_rest:advanced_memory", 0)}｜'
                       f'殘頁 ×{counts.get("witch_rest:memory_page", 0)}')
        embed = discord.Embed(title='魔女安息儀式｜裝備工坊', description=description,
                              color=0xA855F7)
        item = self.item()
        if item:
            name = ITEMS[item['item_id']].name
            prefix, suffix = item['affixes']
            costs = [reroll_cost(value) for value in item['rerolls']]
            embed.add_field(name=f'#{item["instance_id"]} {name}', inline=False,
                            value=(f'前綴：{affix_text(prefix)}｜下次重鑄 '
                                   f'{costs[0]["fragment"]}/{costs[0]["dust"]}/{costs[0]["gold"]:,}G\n'
                                   f'　記憶強化：{improve_text(prefix)}\n'
                                   f'後綴：{affix_text(suffix)}｜下次重鑄 '
                                   f'{costs[1]["fragment"]}/{costs[1]["dust"]}/{costs[1]["gold"]:,}G\n'
                                   f'　記憶強化：{improve_text(suffix)}'))
            if ':t80:' in item['item_id']:
                embed.add_field(name='T90 昇階費用', inline=False,
                                value='同魔女魔力核心 ×12、凝聚魔女結晶 ×1、20,000G')
            _, _, _, slug, slot = item['item_id'].split(':')
            job = next(name for name, data in JOBS.items() if data[0] == slug)
            prefix = '、'.join(AFFIX_NAMES[key] for key, _ in affix_kinds(job, slot, 0))
            suffix = '、'.join(AFFIX_NAMES[key] for key, _ in affix_kinds(job, slot, 1))
            embed.add_field(name='定向詞條可輸入', inline=False,
                            value=f'前綴：{prefix}\n後綴：{suffix}')
        else:
            embed.add_field(name='沒有魔女裝備', value='仍可合成結晶或高級記憶。', inline=False)
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='重鑄預覽後可保留舊詞條，但消耗與次數不退回。')
        return embed

    async def handle(self, interaction, action):
        async with self.lock:
            if action in ('previous', 'next'):
                self.page += -1 if action == 'previous' else 1
                self.rebuild()
                await interaction.response.edit_message(embed=self.embed(), view=self)
                return
            if action == 'back':
                from core.rpg_tailor_view import TailorView
                view = TailorView(self.cog, self.origin)
                await interaction.response.edit_message(embed=view.embed(), view=view)
                self.stop()
                return
            if action == 'close':
                self.stop()
                await interaction.response.edit_message(content='工坊已關閉。', embed=None, view=None)
                return
            if action.startswith('direct:'):
                await interaction.response.send_modal(DirectAffixModal(self, int(action[-1])))
                return
            if action == 'dismantle':
                item = self.item()
                cores = 8 if ':t90:' in item['item_id'] else 4
                grades = [int(affix[1].rsplit(':', 1)[1]) for affix in item['affixes']]
                pages = sum(2 if grade == 4 else 1 if grade == 3 else 0 for grade in grades)
                await interaction.response.edit_message(
                    embed=self.embed(f'分解後無法復原；將取得魔力核心 ×{cores}、'
                                     f'記憶殘頁 ×{pages}。T90 不返還魔女結晶。'),
                    view=DismantleConfirmView(self))
                return
            try:
                if action.startswith('reroll:'):
                    index = int(action[-1])
                    request_id = f'reroll:{interaction.id}'
                    offer = self.cog.witch_rest.offer_reroll(
                        request_id, self.guild_id, self.owner.id, self.selected, index)
                    old, new = offer['old'], offer['new']
                    embed = self.embed(
                        f'舊：{affix_roll_text(*old)}\n'
                        f'新：{affix_roll_text(*new)}')
                    await interaction.response.edit_message(
                        embed=embed, view=RerollDecisionView(self, request_id, offer))
                    return
                if action.startswith('improve:'):
                    result = self.cog.witch_rest.improve_affix(
                        f'improve:{interaction.id}', self.guild_id, self.owner.id,
                        self.selected, int(action[-1]))
                    notice = (f'強化成功，詞條提升至 {ROMAN[result["grade"]]}。'
                              if result['success'] else
                              f'強化失敗（{result["chance"]}%）；只消耗 {result["gold"]:,} 金幣。')
                elif action == 'upgrade':
                    result = self.cog.witch_rest.upgrade(self.guild_id, self.owner.id, self.selected)
                    notice = f'已昇階為【{ITEMS[result["item_id"]].name}】。'
                elif action == 'crystal':
                    self.cog.witch_rest.craft_crystal(f'crystal:{interaction.id}', self.guild_id, self.owner.id)
                    notice = '已消耗 50 碎片與 5,000 金幣，合成凝聚魔女結晶。'
                elif action == 'memory':
                    self.cog.witch_rest.compose_memory(f'memory:{interaction.id}', self.guild_id, self.owner.id)
                    notice = '已消耗 2 張殘頁，合成高級詞條記憶。'
                else:
                    raise CharacterError('無效的工坊操作。')
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)
