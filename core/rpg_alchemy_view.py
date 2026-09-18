"""Private management panel for alchemy dolls."""
from collections import Counter
import asyncio
import time

import discord

from core.rpg_alchemy import (COMBAT_SKILLS, CORE_ITEM, LIFE_SKILLS, RARITIES,
                              STAT_NAMES, material_profile, parse_stone)
from core.rpg_character import CharacterError, ITEMS, item_sellable
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import navigate


class RenameDollModal(discord.ui.Modal, title='替煉金人偶命名'):
    name = discord.ui.TextInput(label='人偶名稱', max_length=16)

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.name.default = panel.cog.alchemy.state(
            panel.guild_id, panel.owner.id)['name']

    async def on_submit(self, interaction):
        try:
            self.panel.cog.alchemy.rename(
                self.panel.guild_id, self.panel.owner.id, str(self.name))
            self.panel.rebuild()
            await interaction.response.edit_message(
                embed=self.panel.embed('人偶名稱已更新。'), view=self.panel)
        except CharacterError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


class AlchemyView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=300)
        self.cog, self.origin = cog, interaction
        self.owner, self.guild_id = interaction.user, interaction.guild_id
        self.page = 'overview'
        self.materials = []
        self.core_id = None
        self.slot = None
        self.item_id = None
        self.confirm_fuel = False
        self.trigger_skill = 'cooking'
        self.exchange_domain = 'combat'
        self.exchange_skill = next(iter(COMBAT_SKILLS))
        self.exchange_rarity = '普通'
        self.closed = False
        self.lock = asyncio.Lock()
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                '請使用 /冒險 → 生活 → 煉金人偶開啟自己的面板。', ephemeral=True)
            return False
        return True

    def button(self, label, action, row, *, disabled=False, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label, row=row, disabled=disabled, style=style)
        async def callback(interaction):
            await self.handle(interaction, action)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        self.clear_items()
        state = self.cog.alchemy.state(self.guild_id, self.owner.id)
        inventory = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
        if self.page == 'overview':
            for label, action in (('素體', 'body'), ('思考核心', 'cores'),
                                  ('技能石轉蛋', 'gacha'), ('燃料', 'fuel')):
                self.button(label, action, 0)
            self.button('命名', 'rename', 0)
            self.button('取消補位' if state['registered'] else '登錄討伐補位',
                        'toggle_register', 1, style=discord.ButtonStyle.primary)
            self.button('分享人偶配置', 'share', 1)
            fish = state['config'].get('fishing', {})
            farm = state['config'].get('farming', {})
            self.button(f'自律釣魚：{"開" if fish.get("enabled") else "關"}',
                        'toggle_life:fishing:enabled', 2)
            self.button(f'釣魚再出發：{"開" if fish.get("repeat") else "關"}',
                        'toggle_life:fishing:repeat', 2)
            self.button(f'自律農耕：{"開" if farm.get("enabled") else "關"}',
                        'toggle_life:farming:enabled', 2)
            self.button(f'農耕再種植：{"開" if farm.get("repeat") else "關"}',
                        'toggle_life:farming:repeat', 2)
            signup = state['config'].get('raid_signup', {})
            self.button(f'討伐響應：{"開" if signup.get("enabled") else "關"}',
                        'toggle_life:raid_signup:enabled', 1)
            cooking = state['config'].get('cooking', {})
            self.button(f'自動備餐：{"開" if cooking.get("enabled") else "關"}',
                        'toggle_life:cooking:enabled', 1)
            self.button('編輯觸發條件', 'triggers', 1)
            presets = [preset for preset in self.cog.provisions.presets(
                self.guild_id, self.owner.id) if preset['ingredients']]
            self.add_item(PanelSelect('cooking_preset', row=3, placeholder='選擇自動備餐配方',
                disabled=not presets, options=[discord.SelectOption(label=preset['name'][:100],
                    value=str(preset['slot']), default=preset['slot'] == cooking.get('preset_slot'))
                    for preset in presets[:25]] or [discord.SelectOption(label='尚無已保存配方', value='empty')]))
        elif self.page == 'body':
            options = [discord.SelectOption(label=ITEMS[key].name[:100], value=key,
                       description=f'持有 {quantity}｜素材 T{material_profile(key)[0]}')
                       for key, quantity in inventory.items() if quantity and material_profile(key)]
            self.add_item(PanelSelect('add_material', row=0, placeholder='每次加入一份素材',
                disabled=not options or len(self.materials) >= 10,
                options=options[:25] or [discord.SelectOption(label='沒有可用素材', value='empty')]))
            self.button('移除最後一份', 'remove_material', 1, disabled=not self.materials)
            self.button('開始製作', 'start_body', 1, disabled=len(self.materials) != 10,
                        style=discord.ButtonStyle.success)
            craft = state['crafting']
            self.button('完成素體', 'finish_body', 1,
                        disabled=not craft or time.time() < craft['ready_at'])
            self.button('安裝候選素體', 'install_body', 2, disabled=not state['candidate_body'])
            self.button('拆除候選素體', 'discard_body', 2, disabled=not state['candidate_body'],
                        style=discord.ButtonStyle.danger)
        elif self.page == 'cores':
            cores = self.cog.alchemy.cores(self.guild_id, self.owner.id)
            visible_cores = cores[:25]
            if visible_cores and self.core_id not in {core['id'] for core in visible_cores}:
                self.core_id = visible_cores[0]['id']
            self.add_item(PanelSelect('core', row=0, placeholder='選擇思考核心', disabled=not cores,
                options=[discord.SelectOption(label=f'#{core["id"]} Lv.{core["level"]} {core["orientation"]}',
                    value=str(core['id']), description='已裝備' if core['equipped'] else '未裝備',
                    default=core['id'] == self.core_id) for core in visible_cores]
                    or [discord.SelectOption(label='尚無已定向核心', value='empty')]))
            self.button('證章購買 Lv.1', 'buy_core', 1)
            for level in (1, 2, 3):
                self.button(f'Lv.{level} 定向戰鬥', f'orient:{level}:戰鬥', 1 + level // 3,
                            disabled=not inventory.get(CORE_ITEM[level]))
                self.button(f'Lv.{level} 定向生活', f'orient:{level}:生活', 1 + level // 3,
                            disabled=not inventory.get(CORE_ITEM[level]))
            chosen = next((core for core in cores if core['id'] == self.core_id), None)
            slots = []
            if chosen:
                for domain, skills in chosen['skills'].items():
                    for index, saved in enumerate(skills, 1):
                        label = (COMBAT_SKILLS if domain == 'combat' else LIFE_SKILLS).get(
                            saved['key'], '空白') if saved else '空白'
                        slots.append(discord.SelectOption(
                            label=f'{"戰鬥" if domain == "combat" else "生活"} {index}｜{label}',
                            value=f'{domain}:{index}', default=f'{domain}:{index}' == self.slot))
            self.add_item(PanelSelect('slot', row=3, placeholder='選擇刻印格', disabled=not slots,
                options=slots or [discord.SelectOption(label='沒有刻印格', value='empty')]))
            domain = self.slot.split(':')[0] if self.slot else None
            stones = [(key, parse_stone(key)) for key, quantity in inventory.items()
                      if quantity and parse_stone(key) and parse_stone(key)[0] == domain]
            self.add_item(PanelSelect('stone', row=4, placeholder='選擇技能石', disabled=not stones,
                options=[discord.SelectOption(label=ITEMS[key].name[:100], value=key,
                    description=f'持有 {inventory[key]}', default=key == self.item_id)
                    for key, _ in stones[:25]] or [discord.SelectOption(label='沒有相符技能石', value='empty')]))
            self.button('裝備核心', 'equip_core', 2, disabled=not chosen)
            self.button('刻入技能石', 'engrave', 2, disabled=not chosen or not self.slot or not self.item_id)
            self.button('分解技能石', 'decompose', 2, disabled=not self.item_id)
        elif self.page == 'gacha':
            for index, domain in enumerate(('combat', 'life')):
                label = '戰鬥' if domain == 'combat' else '生活'
                self.button(f'{label}單抽・500', f'draw:{domain}:1', index)
                self.button(f'{label}十連・5,000', f'draw:{domain}:10', index,
                            style=discord.ButtonStyle.primary)
            self.button('鍊金粉塵兌換', 'powder', 2)
        elif self.page == 'powder':
            self.add_item(PanelSelect('exchange_domain', row=0, placeholder='選擇技能領域', options=[
                discord.SelectOption(label='戰鬥', value='combat', default=self.exchange_domain == 'combat'),
                discord.SelectOption(label='生活', value='life', default=self.exchange_domain == 'life')]))
            pool = COMBAT_SKILLS if self.exchange_domain == 'combat' else LIFE_SKILLS
            if self.exchange_skill not in pool:
                self.exchange_skill = next(iter(pool))
            self.add_item(PanelSelect('exchange_skill', row=1, placeholder='選擇指定技能', options=[
                discord.SelectOption(label=name, value=key, default=key == self.exchange_skill)
                for key, name in pool.items()]))
            self.add_item(PanelSelect('exchange_rarity', row=2, placeholder='選擇稀有度', options=[
                discord.SelectOption(label=f'{rarity}｜{cost} 粉塵', value=rarity,
                                     default=rarity == self.exchange_rarity)
                for rarity, cost in (('普通', 10), ('稀有', 30), ('史詩', 100))]))
            self.button('確認兌換', 'exchange', 3, style=discord.ButtonStyle.primary)
        elif self.page == 'fuel':
            options = [discord.SelectOption(label=ITEMS[key].name[:100], value=key,
                       description=f'持有 {quantity}｜轉換 1 個')
                       for key, quantity in inventory.items() if quantity and key in ITEMS
                       and not key.startswith('alchemy:')
                       and ITEMS[key].category not in ('裝備', '釣竿')
                       and item_sellable(ITEMS[key])]
            self.add_item(PanelSelect('fuel_item', row=0, placeholder='選擇轉換一份素材',
                disabled=not options, options=options[:25] or [
                    discord.SelectOption(label='沒有可轉換素材', value='empty')]))
            label = '確認不可逆轉換' if self.confirm_fuel else '轉換一份'
            self.button(label, 'convert_fuel', 1, disabled=not self.item_id,
                        style=discord.ButtonStyle.danger if self.confirm_fuel else discord.ButtonStyle.secondary)
        elif self.page == 'triggers':
            self.add_item(PanelSelect('trigger_skill', row=0, placeholder='選擇要設定的技能', options=[
                discord.SelectOption(label='自動備餐', value='cooking',
                                     default=self.trigger_skill == 'cooking'),
                discord.SelectOption(label='討伐響應', value='raid_signup',
                                     default=self.trigger_skill == 'raid_signup')]))
            config = state['config'].get(self.trigger_skill, {})
            pools = config.get('pools', ('regular', 'mid', 'high'))
            for label, value in (('一般', 'regular'), ('中階', 'mid'), ('高階', 'high')):
                self.button(f'{"✓" if value in pools else "×"} {label}', f'trigger_pool:{value}', 1)
            qualities = config.get('qualities', ('普通', '精英', '首領', '傳說'))
            for value in ('普通', '精英', '首領', '傳說'):
                self.button(f'{"✓" if value in qualities else "×"} {value}',
                            f'trigger_quality:{value}', 2)
            self.button(f'{"✓" if config.get("scheduled", True) else "×"} 正常排程',
                        'trigger_source:scheduled', 3)
            self.button(f'{"✓" if config.get("bounty", True) else "×"} 酒館懸賞',
                        'trigger_source:bounty', 3)
            if self.trigger_skill == 'cooking':
                self.button(f'{"✓" if config.get("require_no_effect", True) else "×"} 自己無料理效果才開桌',
                            'trigger_no_effect', 3)
        if self.page != 'overview':
            self.button('返回人偶', 'overview', 4)
        self.button('返回生活', 'life', 4)
        self.button('重新整理', 'refresh', 4)
        self.button('關閉', 'close', 4)

    def embed(self, notice=None):
        state = self.cog.alchemy.state(self.guild_id, self.owner.id)
        body = state['active_body']
        core = state['core']
        if self.page == 'overview':
            body_text = ('尚未安裝' if not body else f'T{body["tier"]}｜' +
                         '、'.join(f'{name} {value}' for name, value in zip(STAT_NAMES, body['stats'])))
            core_text = ('尚未裝備' if not core else
                         f'Lv.{core["level"]} {core["orientation"]}｜核心 #{core["id"]}')
            embed = discord.Embed(title=f'煉金人偶｜{state["name"]}', color=0xB8864B,
                description=f'**素體**　{body_text}\n**思考核心**　{core_text}\n'
                            f'**燃料**　{state["fuel"]:,}\n**討伐補位**　'
                            f'{"已登錄" if state["registered"] else "未登錄"}')
        elif self.page == 'body':
            counts = Counter(self.materials)
            selection = '、'.join(f'{ITEMS[key].name}×{amount}' for key, amount in counts.items()) or '尚未選擇'
            craft = state['crafting']
            status = (f'T{craft["tier"]}，<t:{int(craft["ready_at"])}:R>完成' if craft else
                      '有候選素體等待處理' if state['candidate_body'] else '目前閒置')
            embed = discord.Embed(title='煉金人偶｜素體製作', color=0xB8864B,
                description=f'選擇固定 10 份、最多 5 種素材。\n**已選 {len(self.materials)}/10**　{selection}\n'
                            f'**工坊狀態**　{status}')
        elif self.page == 'cores':
            lines = []
            for saved in self.cog.alchemy.cores(self.guild_id, self.owner.id):
                skills = [entry for domain in saved['skills'].values() for entry in domain if entry]
                lines.append(f'#{saved["id"]}｜Lv.{saved["level"]} {saved["orientation"]}'
                             f'{"【已裝備】" if saved["equipped"] else ""}｜已刻印 {len(skills)} 格')
            embed = discord.Embed(title='煉金人偶｜思考核心', color=0xB8864B,
                description='\n'.join(lines) or '尚無已定向核心。Lv.1 胚可用 15 枚討伐之證購買。')
        elif self.page == 'gacha':
            embed = discord.Embed(title='煉金人偶｜技能石轉蛋', color=0xB8864B,
                description='普通 70%｜稀有 24%｜史詩 5%｜傳說 1%\n'
                            '十連至少一顆稀有；50 抽保底史詩、100 抽保底傳說。')
        elif self.page == 'powder':
            powder = self.cog.characters.inventory_counts(
                self.guild_id, self.owner.id).get('alchemy:powder', 0)
            embed = discord.Embed(title='煉金人偶｜鍊金粉塵兌換', color=0xB8864B,
                description=f'目前持有 **{powder}** 粉塵。可指定兌換普通、稀有或史詩技能石；傳說不開放兌換。')
        elif self.page == 'triggers':
            label = LIFE_SKILLS[self.trigger_skill]
            embed = discord.Embed(title=f'煉金人偶｜{label}觸發條件', color=0xB8864B,
                description='勾選討伐池、品質與來源。條件全部符合時才會嘗試操作；'
                            '檢查失敗不消耗燃料。')
        else:
            embed = discord.Embed(title='煉金人偶｜燃料', color=0xB8864B,
                description=f'目前燃料：**{state["fuel"]:,}**\n可將具有出售價值的素材轉換成燃料。')
        if notice:
            embed.add_field(name='操作結果', value=notice, inline=False)
        embed.set_footer(text='刻印會消耗技能石；覆蓋舊技能不會返還。')
        return embed

    def public_embed(self):
        state = self.cog.alchemy.state(self.guild_id, self.owner.id)
        body, core = state['active_body'], state['core']
        embed = discord.Embed(title=f'煉金人偶｜{state["name"]}', color=0xB8864B,
                              description=f'擁有者：{self.owner.mention}')
        if body:
            embed.add_field(name=f'素體 T{body["tier"]}', value='｜'.join(
                f'{name} {value}' for name, value in zip(STAT_NAMES, body['stats'])), inline=False)
        if core:
            for domain, label, pool in (('combat', '戰鬥迴路', COMBAT_SKILLS),
                                        ('life', '生活迴路', LIFE_SKILLS)):
                lines = [f'{index}. {entry["rarity"]}・{pool[entry["key"]]}' if entry else f'{index}. 空白'
                         for index, entry in enumerate(core['skills'][domain], 1)]
                embed.add_field(name=f'Lv.{core["level"]} {core["orientation"]}｜{label}',
                                value='\n'.join(lines), inline=False)
        history = self.cog.store.db.execute('''SELECT p.direct_damage,p.damage_taken,p.healing_done,
            p.support_taken FROM rpg_battle_participants p JOIN rpg_battle_results r
            ON r.raid_id=p.raid_id WHERE r.guild_id=? AND p.user_id=?
            ORDER BY r.completed_at DESC LIMIT 5''', (self.guild_id, -self.owner.id)).fetchall()
        if history:
            totals = tuple(sum(row[index] for row in history) for index in range(4))
            embed.add_field(name=f'最近 {len(history)} 場',
                            value=(f'傷害 {totals[0]:,}｜承傷 {totals[1]:,}｜'
                                   f'治療 {totals[2]:,}｜輔助承傷 {totals[3]:,}'), inline=False)
        embed.set_footer(text='討伐補位：' + ('已開啟' if state['registered'] else '未開啟'))
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            try:
                if action in ('overview', 'body', 'cores', 'gacha', 'powder', 'fuel', 'triggers'):
                    self.page = action
                elif action == 'share':
                    await interaction.response.defer()
                    await interaction.channel.send(embed=self.public_embed(),
                        allowed_mentions=discord.AllowedMentions.none())
                    self.rebuild()
                    await interaction.edit_original_response(embed=self.embed('已分享至目前頻道。'), view=self)
                    return
                elif action == 'rename':
                    await interaction.response.send_modal(RenameDollModal(self))
                    return
                elif action == 'life':
                    await navigate(self, interaction, 'life')
                    return
                elif action == 'close':
                    await interaction.response.edit_message(content='煉金人偶面板已關閉。', embed=None, view=None)
                    self.closed = True
                    self.stop()
                    return
                elif action == 'add_material' and value != 'empty':
                    if self.materials.count(value) >= self.cog.characters.inventory_counts(
                            self.guild_id, self.owner.id).get(value, 0):
                        raise CharacterError('背包中沒有更多這種素材。')
                    if value not in self.materials and len(set(self.materials)) >= 5:
                        raise CharacterError('素體最多只能混合五種素材。')
                    self.materials.append(value)
                elif action == 'remove_material':
                    self.materials.pop()
                elif action == 'start_body':
                    craft = self.cog.alchemy.start_body(self.guild_id, self.owner.id, self.materials)
                    self.materials = []
                    notice = f'T{craft["tier"]} 素體開始製作。'
                elif action == 'finish_body':
                    body = self.cog.alchemy.finish_body(self.guild_id, self.owner.id)
                    notice = f'T{body["tier"]} 素體已完成，請確認是否安裝。'
                elif action == 'install_body':
                    self.cog.alchemy.install_candidate(self.guild_id, self.owner.id)
                    notice = '已安裝候選素體。'
                elif action == 'discard_body':
                    self.cog.alchemy.discard_candidate(self.guild_id, self.owner.id)
                    notice = '已拆除候選素體；材料不會返還。'
                elif action == 'buy_core':
                    self.cog.alchemy.buy_core1(self.guild_id, self.owner.id)
                    notice = '已購入 Lv.1 思考核心胚。'
                elif action.startswith('orient:'):
                    _, level, orientation = action.split(':')
                    self.core_id = self.cog.alchemy.orient_core(
                        self.guild_id, self.owner.id, int(level), orientation)
                    notice = f'核心已定向為{orientation}面向。'
                elif action == 'core' and value != 'empty':
                    self.core_id, self.slot, self.item_id = int(value), None, None
                elif action == 'slot' and value != 'empty':
                    self.slot, self.item_id = value, None
                elif action in ('stone', 'fuel_item') and value != 'empty':
                    self.item_id = value
                    self.confirm_fuel = False
                elif action == 'equip_core':
                    self.cog.alchemy.equip_core(self.guild_id, self.owner.id, self.core_id)
                    notice = '已裝備思考核心。'
                elif action == 'engrave':
                    domain, slot = self.slot.split(':')
                    self.cog.alchemy.engrave(self.guild_id, self.owner.id, self.core_id,
                                             domain, int(slot), self.item_id)
                    self.item_id = None
                    notice = '技能石已刻入。'
                elif action == 'decompose':
                    powder = self.cog.alchemy.decompose(
                        self.guild_id, self.owner.id, self.item_id)
                    self.item_id = None
                    notice = f'技能石已分解為 {powder} 份鍊金粉塵。'
                elif action.startswith('draw:'):
                    _, domain, count = action.split(':')
                    results = self.cog.alchemy.draw(self.guild_id, self.owner.id, domain, int(count))
                    notice = '、'.join(f'{entry["rarity"]}・{(COMBAT_SKILLS if domain == "combat" else LIFE_SKILLS)[entry["skill"]]}'
                                      for entry in results)
                elif action == 'exchange_domain':
                    self.exchange_domain = value
                    pool = COMBAT_SKILLS if value == 'combat' else LIFE_SKILLS
                    self.exchange_skill = next(iter(pool))
                elif action == 'exchange_skill':
                    self.exchange_skill = value
                elif action == 'exchange_rarity':
                    self.exchange_rarity = value
                elif action == 'exchange':
                    item_id = self.cog.alchemy.exchange_stone(
                        self.guild_id, self.owner.id, self.exchange_domain,
                        self.exchange_skill, self.exchange_rarity)
                    notice = f'已兌換 {ITEMS[item_id].name}。'
                elif action == 'convert_fuel':
                    if not self.confirm_fuel:
                        self.confirm_fuel = True
                        warning = ('；這是常用的幸運／盛宴食材' if any(
                            word in ITEMS[self.item_id].description for word in ('幸運', '盛宴')) else '')
                        notice = f'將消耗 1 個 {ITEMS[self.item_id].name}{warning}。此操作不可逆，請再次確認。'
                    else:
                        fuel = self.cog.alchemy.convert_fuel(
                            self.guild_id, self.owner.id, self.item_id, 1)
                        self.confirm_fuel = False
                        notice = f'已轉換 {fuel} 燃料。'
                elif action == 'toggle_register':
                    enabled = not self.cog.alchemy.state(
                        self.guild_id, self.owner.id)['registered']
                    self.cog.alchemy.set_registered(self.guild_id, self.owner.id, enabled)
                    notice = '已登錄討伐補位。' if enabled else '已取消討伐補位。'
                elif action.startswith('toggle_life:'):
                    _, key, field = action.split(':')
                    current = self.cog.alchemy.state(
                        self.guild_id, self.owner.id)['config'].get(key, {}).get(field, False)
                    self.cog.alchemy.configure_life(
                        self.guild_id, self.owner.id, key, **{field: not current})
                    notice = f'{LIFE_SKILLS[key]}的設定已更新。'
                elif action == 'cooking_preset' and value != 'empty':
                    self.cog.alchemy.configure_life(
                        self.guild_id, self.owner.id, 'cooking', preset_slot=int(value))
                    notice = '已更新自動備餐使用的保存配方。'
                elif action == 'trigger_skill':
                    self.trigger_skill = value
                elif action.startswith('trigger_pool:'):
                    value = action.split(':', 1)[1]
                    config = self.cog.alchemy.state(self.guild_id, self.owner.id)[
                        'config'].get(self.trigger_skill, {})
                    selected = set(config.get('pools', ('regular', 'mid', 'high')))
                    selected.symmetric_difference_update((value,))
                    self.cog.alchemy.configure_life(
                        self.guild_id, self.owner.id, self.trigger_skill, pools=selected)
                elif action.startswith('trigger_quality:'):
                    value = action.split(':', 1)[1]
                    config = self.cog.alchemy.state(self.guild_id, self.owner.id)[
                        'config'].get(self.trigger_skill, {})
                    selected = set(config.get('qualities', ('普通', '精英', '首領', '傳說')))
                    selected.symmetric_difference_update((value,))
                    self.cog.alchemy.configure_life(
                        self.guild_id, self.owner.id, self.trigger_skill, qualities=selected)
                elif action.startswith('trigger_source:'):
                    field = action.split(':', 1)[1]
                    config = self.cog.alchemy.state(self.guild_id, self.owner.id)[
                        'config'].get(self.trigger_skill, {})
                    self.cog.alchemy.configure_life(
                        self.guild_id, self.owner.id, self.trigger_skill,
                        **{field: not config.get(field, True)})
                elif action == 'trigger_no_effect':
                    config = self.cog.alchemy.state(self.guild_id, self.owner.id)[
                        'config'].get('cooking', {})
                    self.cog.alchemy.configure_life(
                        self.guild_id, self.owner.id, 'cooking',
                        require_no_effect=not config.get('require_no_effect', True))
                else:
                    notice = None
                self.rebuild()
                await interaction.response.edit_message(embed=self.embed(locals().get('notice')), view=self)
            except CharacterError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
