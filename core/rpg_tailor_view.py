"""Tono Hanna's equipment dyeing and accessory embroidery shop."""
import asyncio

import discord

from core.rpg_character import (CharacterError, DYE_PRICE, EMBROIDERIES,
                                EMBROIDERY_PRICE, HANNA_BROOCH_ITEMS,
                                HANNA_BROOCH_THRESHOLDS, ITEMS,
                                LIFESTYLE_ACCESSORY_RECIPES, PAINT_ITEMS, PAINT_NAMES,
                                STAT_NAMES, inventory_entry_label, item_text)
from core.rpg_equipment_view import PanelSelect
from core.rpg_witch_embroideries import DESCRIPTIONS, REQUIREMENTS
from core.rpg_witch_catalog import PROFILE
from core.rpg_menu import navigate
from core.rpg_affinity import hanna_affinity, tailoring_price


PAGE_SIZE = 20


def paint_effect(color, slot):
    if slot not in ('武器', '套裝'):
        return {'red': '武器攻擊 +30%；套裝 HP +50%',
                'yellow': '武器命中值 +5；套裝速度 +15',
                'blue': '武器 5% 機率減傷 50%；套裝閃避值 +5'}[color]
    if color == 'red':
        return '武器攻擊 +30%' if slot == '武器' else '套裝 HP +50%'
    if color == 'yellow':
        return '武器命中值 +5' if slot == '武器' else '套裝速度 +15'
    return '武器 5% 機率減傷 50%' if slot == '武器' else '套裝閃避值 +5'


class TailorView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=180)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.mode, self.selected, self.option, self.page = 'dye', None, 'red', 0
        self.embroidery_slot, self.recipe = 0, 'fishing'
        self.closed, self.lock = False, asyncio.Lock()
        self.rebuild()

    def _button(self, label, action, row, *, disabled=False, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, disabled=disabled, style=style)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        self.unlocked = self.cog.characters.unlocked_witch_embroideries(self.guild_id, self.owner.id)
        self.lifestyle = self.cog.characters.lifestyle_accessory_status(self.guild_id, self.owner.id)
        self.brooch = self.cog.characters.hanna_brooch_status(self.guild_id, self.owner.id)
        entries = self.cog.characters.inventory_entries(self.guild_id, self.owner.id)
        self.equipped_ids = set(self.cog.characters.snapshot(
            self.guild_id, self.owner.id)['equipped_instances'].values())
        if self.mode == 'dye':
            entries = [entry for entry in entries if entry.instance_id and entry.item.socket_base]
        elif self.mode == 'embroidery':
            entries = [entry for entry in entries if entry.instance_id and entry.item.slot == '飾品'
                       and entry.item.embroidery_slots > 0]
        else:
            entries = []
        self.entries = {entry.reference: entry for entry in entries}
        references = list(self.entries)
        pages = max(1, (len(references) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        visible = references[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        if self.selected not in self.entries:
            self.selected = None
        self.clear_items()
        self._button('裝備染色', 'mode:dye', 0,
                     style=discord.ButtonStyle.primary if self.mode == 'dye' else discord.ButtonStyle.secondary)
        self._button('飾品刺繡', 'mode:embroidery', 0,
                     style=discord.ButtonStyle.primary if self.mode == 'embroidery' else discord.ButtonStyle.secondary)
        self._button('生活飾品', 'mode:craft', 0,
                     style=discord.ButtonStyle.primary if self.mode == 'craft' else discord.ButtonStyle.secondary)
        self._button('好感胸針', 'mode:affinity', 0,
                     style=discord.ButtonStyle.primary if self.mode == 'affinity' else discord.ButtonStyle.secondary)
        self._button('顏料結晶', 'crystals', 0)
        options = [discord.SelectOption(
            label=inventory_entry_label(self.entries[reference], self.equipped_ids),
            value=reference, description=item_text(self.entries[reference].item)[:100],
            default=reference == self.selected) for reference in visible]
        if self.mode in ('dye', 'embroidery'):
            self.add_item(PanelSelect('item', row=1,
                placeholder='選擇要加工的裝備' if options else '目前沒有可加工的裝備',
                disabled=not options, options=options or [discord.SelectOption(label='沒有可加工裝備', value='empty')]))
        elif self.mode == 'craft':
            self.add_item(PanelSelect('recipe', row=1, placeholder='選擇生活飾品', options=[
                discord.SelectOption(label=ITEMS[data['item_id']].name, value=skill,
                    description=(('已製作' if data['owned'] else
                        f'Lv.{data["level"]}｜{ITEMS[data["material_id"]].name} '
                        f'{data["materials"]}/{data["needed"]}'))[:100],
                    default=self.recipe == skill) for skill, data in self.lifestyle.items()]))
        if self.mode == 'dye':
            self.add_item(PanelSelect('option', row=2, placeholder='選擇染色顏料', options=[
                discord.SelectOption(label=f'{PAINT_NAMES[color]}染色', value=color,
                    description='消耗 1 罐噴漆｜' + paint_effect(color, '武器／套裝'),
                    default=self.option == color) for color in PAINT_ITEMS]))
        elif self.mode == 'embroidery':
            if self.option not in EMBROIDERIES:
                self.option = 'heart'
            self.add_item(PanelSelect('option', row=2, placeholder='選擇刺繡圖樣', options=[
                discord.SelectOption(label=('🔒 ' if key in REQUIREMENTS and key not in self.unlocked else '') + name, value=key,
                    description=(f'解鎖：通關包含{PROFILE[REQUIREMENTS[key]][1]}的魔女試煉'
                                 if key in REQUIREMENTS and key not in self.unlocked else DESCRIPTIONS[key] if key in DESCRIPTIONS else
                                 f'{STAT_NAMES[int(effect.split(":")[1])]} +{value}')[:100],
                    default=self.option == key)
                for key, (name, effect, value) in EMBROIDERIES.items()]))
        selected_item = self.entries[self.selected].item if self.selected in self.entries else None
        if self.mode == 'embroidery' and selected_item:
            self.embroidery_slot = min(self.embroidery_slot, selected_item.embroidery_slots - 1)
            for index in range(selected_item.embroidery_slots):
                self._button(f'格 {index + 1}', f'slot:{index}', 3,
                    style=discord.ButtonStyle.primary if index == self.embroidery_slot else discord.ButtonStyle.secondary)
        labels = {'dye': '確認染色', 'embroidery': '確認刺繡',
                  'craft': '確認製作', 'affinity': '領取／升級'}
        disabled = ((self.mode in ('dye', 'embroidery') and self.selected is None)
                    or (self.mode == 'embroidery' and self.option in REQUIREMENTS
                        and self.option not in self.unlocked)
                    or (self.mode == 'craft' and (self.lifestyle[self.recipe]['owned']
                        or self.lifestyle[self.recipe]['level'] < 60
                        or self.lifestyle[self.recipe]['materials'] < self.lifestyle[self.recipe]['needed']
                        or self.cog.store.gold(self.guild_id, self.owner.id) < self.lifestyle[self.recipe]['price']))
                    or (self.mode == 'affinity' and self.brooch['eligible_tier'] <= self.brooch['tier']))
        self._button(labels[self.mode], 'apply', 3, disabled=disabled, style=discord.ButtonStyle.success)
        self._button('上一頁', 'previous', 4,
                     disabled=self.page == 0 or self.mode not in ('dye', 'embroidery'))
        self._button('下一頁', 'next', 4,
                     disabled=self.page == pages - 1 or self.mode not in ('dye', 'embroidery'))
        self._button('返回移動', 'travel', 4)
        self._button('重新整理', 'refresh', 4)
        self._button('關閉', 'close', 4)

    def current_embroidery(self, reference):
        instance = self.cog.characters.get_instance(self.guild_id, self.owner.id, reference)
        if not instance:
            return None
        labels = []
        slots = ITEMS[instance.item_id].embroidery_slots
        for index in range(slots):
            affix = next((item for item in instance.affixes
                          if item[0] == index and item[1].startswith('embroidery:')), None)
            embroidery = EMBROIDERIES.get(affix[1].split(':', 1)[1]) if affix else None
            if slots == 1:
                return embroidery[0] if embroidery else None
            labels.append(f'{index + 1}：{embroidery[0] if embroidery else "空"}')
        return '｜'.join(labels)

    def embed(self, notice=None):
        dye_price = tailoring_price(self.cog.store.db, self.guild_id, self.owner.id, DYE_PRICE)
        embroidery_price = tailoring_price(self.cog.store.db, self.guild_id, self.owner.id, EMBROIDERY_PRICE)
        if self.mode == 'dye':
            description = (f'消耗對應噴漆罐並支付 **{dye_price:,} 金幣**，替諾亞武器或套裝染色。'
                           '再次染色會取代原顏色，舊顏料與費用不返還。')
        elif self.mode == 'embroidery':
            description = (f'支付 **{embroidery_price:,} 金幣**，在具有刺繡格的討伐飾品上縫製圖樣。'
                           '魔女刺繡需先通關對應試煉並消耗 3 個魔女繡線；'
                           '每件飾品最多一個魔女刺繡。')
        elif self.mode == 'craft':
            description = '生活技能達 Lv.60 後可用專屬材料與 **5,000 金幣**製作綁定飾品。'
        else:
            description = '漢娜好感度達 25／50／75／100 時，可領取或升級同一件胸針。'
        embed = discord.Embed(title='安安大冒險｜漢娜的裁縫所',
                              description=description, color=0xE85D75)
        if self.mode == 'embroidery' and self.option in REQUIREMENTS:
            name = PROFILE[REQUIREMENTS[self.option]][1]
            status = '已解鎖' if self.option in self.unlocked else f'未解鎖：請先通關包含{name}的魔女試煉'
            embed.add_field(name=EMBROIDERIES[self.option][0],
                            value=f'{DESCRIPTIONS[self.option]}\n{status}', inline=False)
        if self.mode == 'craft':
            data = self.lifestyle[self.recipe]
            item, material = ITEMS[data['item_id']], ITEMS[data['material_id']]
            ready = (data['level'] >= 60 and data['materials'] >= data['needed']
                     and self.cog.store.gold(self.guild_id, self.owner.id) >= data['price'])
            status = '已製作' if data['owned'] else '可製作' if ready else '尚未達成'
            skill_name = '釣魚' if self.recipe == 'fishing' else '農耕'
            embed.add_field(name=item.name, value=(f'{item_text(item)}\n{skill_name} Lv.{data["level"]}｜'
                f'{material.name} {data["materials"]}/{data["needed"]}｜{data["price"]:,} 金幣｜{status}'),
                inline=False)
        elif self.mode == 'affinity':
            lines = []
            for index, (threshold, item_id) in enumerate(zip(HANNA_BROOCH_THRESHOLDS, HANNA_BROOCH_ITEMS)):
                marker = '✅' if self.brooch['tier'] >= index else '🎁' if self.brooch['eligible_tier'] >= index else '🔒'
                lines.append(f'{marker} {threshold}：{ITEMS[item_id].name}｜{item_text(ITEMS[item_id])}')
            embed.add_field(name=f'漢娜好感度 {self.brooch["score"]}/100', value='\n'.join(lines), inline=False)
        entry = self.entries.get(self.selected)
        if entry:
            details = item_text(entry.item)
            if self.mode == 'dye':
                color = next((PAINT_NAMES[color] for color, key in PAINT_ITEMS.items()
                              if dict(self.cog.characters.get_instance(
                                  self.guild_id, self.owner.id, self.selected).sockets).get(0) == key), '尚未染色')
                details += f'\n目前顏色：{color}'
            else:
                details += f'\n目前刺繡：{self.current_embroidery(self.selected) or "無"}'
            embed.add_field(name=inventory_entry_label(entry, self.equipped_ids), value=details, inline=False)
        counts = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        embed.add_field(name='加工資源', value=(
            f'金幣：{self.cog.store.gold(self.guild_id, self.owner.id):,}\n'
            + '｜'.join(f'{ITEMS[key].name} ×{counts.get(key, 0)}' for key in PAINT_ITEMS.values())
            + f'\n魔女繡線 ×{counts.get("witch:thread", 0)}'
            + ''.join(f'\n{ITEMS[data["material_id"]].name} ×{data["materials"]}'
                      for data in self.lifestyle.values())), inline=False)
        score = hanna_affinity(self.cog.store.db, self.guild_id, self.owner.id)
        embed.add_field(name='漢娜好感度', value=f'{score}/100｜加工費減免 {score / 4:g}%（最高 25%）\n'
                        '報價已套用折扣，金幣無條件進位；材料數量不變。', inline=False)
        if notice:
            embed.add_field(name='加工結果', value=notice, inline=False)
        embed.set_footer(text='每件裝備以編號區分；顏料結晶的鑲嵌、替換、出售與轉交也在此處辦理。')
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message('請從自己的移動頁進入漢娜的裁縫所。', ephemeral=True)
            return False
        return True

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed or self.is_finished():
                await interaction.response.send_message('裁縫所面板已關閉，請從 /冒險 重新進入。', ephemeral=True)
                return
            if action == 'travel':
                await navigate(self, interaction, 'travel')
                return
            if action == 'crystals':
                await navigate(self, interaction, 'crystals')
                return
            if action == 'close':
                self.closed = True
                self.stop()
                await interaction.response.edit_message(content='你離開了漢娜的裁縫所。', embed=None, view=None)
                return
            notice = None
            try:
                if action.startswith('mode:'):
                    self.mode, self.selected, self.page = action.split(':', 1)[1], None, 0
                    self.option = 'red' if self.mode == 'dye' else 'heart'
                    self.embroidery_slot = 0
                elif action == 'item':
                    if value not in self.entries:
                        raise CharacterError('這件裝備目前不能加工，請重新選擇。')
                    self.selected = value
                    self.embroidery_slot = 0
                elif action == 'recipe':
                    if value not in LIFESTYLE_ACCESSORY_RECIPES:
                        raise CharacterError('無效的生活飾品配方。')
                    self.recipe = value
                elif action == 'option':
                    valid = PAINT_ITEMS if self.mode == 'dye' else EMBROIDERIES
                    if value not in valid:
                        raise CharacterError('無效的加工選項。')
                    self.option = value
                elif action.startswith('slot:'):
                    self.embroidery_slot = int(action.split(':', 1)[1])
                elif action in ('previous', 'next'):
                    self.page = max(0, self.page + (1 if action == 'next' else -1))
                    self.selected = None
                elif action == 'apply':
                    if self.mode in ('dye', 'embroidery') and not self.selected:
                        raise CharacterError('請先選擇要加工的裝備。')
                    if self.mode == 'dye':
                        price = tailoring_price(self.cog.store.db, self.guild_id, self.owner.id, DYE_PRICE)
                        self.cog.characters.dye_equipment(
                            self.guild_id, self.owner.id, self.selected, self.option)
                        notice = f'染色完成，已消耗 {ITEMS[PAINT_ITEMS[self.option]].name}與 {price:,} 金幣。'
                    elif self.mode == 'embroidery':
                        price = tailoring_price(self.cog.store.db, self.guild_id, self.owner.id, EMBROIDERY_PRICE)
                        self.cog.characters.embroider_accessory(
                            self.guild_id, self.owner.id, self.selected, self.option,
                            self.embroidery_slot)
                        notice = f'第 {self.embroidery_slot + 1} 格{EMBROIDERIES[self.option][0]}完成，已支付 {price:,} 金幣。'
                    elif self.mode == 'craft':
                        instance = self.cog.characters.craft_lifestyle_accessory(
                            self.guild_id, self.owner.id, self.recipe)
                        notice = f'已製作 {ITEMS[instance.item_id].name}，並放入背包。'
                    else:
                        instance = self.cog.characters.upgrade_hanna_brooch(
                            self.guild_id, self.owner.id)
                        notice = f'已獲得 {ITEMS[instance.item_id].name}；原有刺繡已保留。'
            except CharacterError as exc:
                notice = str(exc)
            self.rebuild()
            await interaction.response.edit_message(embed=self.embed(notice), view=self)

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop()
            try:
                await self.origin.edit_original_response(content='裁縫所面板已逾時，請從 /冒險 重新進入。', view=None)
            except discord.HTTPException:
                pass
