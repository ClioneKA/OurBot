"""Private management panel for alchemy dolls."""
from collections import Counter
import asyncio
import time

import discord

from core.rpg_alchemy import (COMBAT_RARITY_STATS, COMBAT_SKILLS, COMBAT_SKILL_DETAILS,
                              CORE_ITEM, LIFE_SKILLS,
                              LIFE_WORK_UNLOCKS, RARITIES,
                              STAT_NAMES, FUEL_CAPACITY, body_acceleration_cost,
                              fuel_discount, fuel_value, life_skill_unlocks, life_work,
                              operation_fuel_cost,
                              material_profile, parse_stone)
from core.rpg_character import CharacterError, ITEMS, item_sellable
from core.rpg_equipment_view import PanelSelect
from core.rpg_menu import navigate


def combat_stone_effect(skill_key, rarity, body):
    """Describe a combat stone after applying rarity and installed body stats."""
    rarity_stats = COMBAT_RARITY_STATS[rarity]
    multiplier = rarity_stats['multiplier']
    stats = body['stats'] if body else (0,) * 5
    structure, power, durability, _, spirit = stats
    attack, defense = power * 3, durability * 3
    healing, hp = spirit * 3, 50 + structure * 10
    percent = lambda base: round(base * multiplier)
    if skill_key == 'power_strike':
        return f'{percent(160)}% 攻擊（基礎 {int(attack * 1.6 * multiplier):,}）'
    if skill_key == 'armor_break':
        return f'{percent(100)}% 攻擊｜降低防禦 {percent(80)}%'
    if skill_key == 'sweep':
        return f'全體 {percent(120)}% 攻擊（基礎 {int(attack * 1.2 * multiplier):,}）'
    if skill_key == 'overload':
        return f'{percent(220)}% 攻擊（基礎 {int(attack * 2.2 * multiplier):,}）'
    if skill_key == 'counter':
        return f'指定隊友誘敵，反擊 {percent(100)}% 人偶攻擊'
    if skill_key == 'barrier':
        return f'全隊防禦 +{max(1, int(defense * multiplier)):,}'
    if skill_key == 'rally':
        return f'修復主人 {int((hp // 2) * multiplier):,} HP'
    if skill_key == 'repair':
        return f'恢復 {int(healing * multiplier):,} HP'
    if skill_key == 'group_repair':
        return f'全隊各恢復 {int((healing * 65 // 100) * multiplier):,} HP'
    if skill_key == 'amplify':
        return f'攻擊 +{percent(40)}%'
    if skill_key == 'cleanse':
        return f'移除 {rarity_stats["cleanse"]} 個負面狀態'
    if skill_key == 'interrupt':
        return f'{percent(120)}% 攻擊，命中後打斷'
    return f'效果 {multiplier:.0%}'


def life_work_state(skill_key, rarity, body):
    """Return work and unlock text for previews without mutating the doll."""
    work = life_work(body, skill_key, rarity)
    if work is None:
        return '無素體，無法計算'
    unlocks = '、'.join(life_skill_unlocks(skill_key, work)) or '尚無'
    return f'工作力 {work:,}｜解鎖：{unlocks}'


def duration_text(seconds):
    if not seconds:
        return '0 小時'
    if seconds % 86400 == 0:
        return f'{seconds // 86400} 天'
    if seconds >= 86400:
        days, remainder = divmod(seconds, 86400)
        return f'{days} 天 {remainder // 3600:g} 小時'
    if seconds % 3600 == 0:
        return f'{seconds // 3600} 小時'
    return f'{seconds / 3600:g} 小時'


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


class MultiPanelSelect(discord.ui.Select):
    def __init__(self, action, **kwargs):
        super().__init__(**kwargs)
        self.action = action

    async def callback(self, interaction):
        await self.view.handle(interaction, self.action, self.values)


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
        self.fuel_item_id = None
        self.fuel_quantity = 1
        self.decompose_items = []
        self.confirm_decompose = False
        self.confirm_core_dismantle = False
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
                                  ('技能石轉蛋', 'gacha'), ('燃料', 'fuel'),
                                  ('自動化設定', 'automation')):
                self.button(label, action, 0)
            self.button('命名', 'rename', 1)
            combat_enabled = state['config'].get('combat_support', {}).get('enabled', False)
            self.button(f'戰鬥支援：{"開" if combat_enabled else "關"}',
                        'toggle_combat_support', 1,
                        style=(discord.ButtonStyle.primary if combat_enabled
                               else discord.ButtonStyle.secondary))
            self.button('分享人偶配置', 'share', 1)
            self.button('能力說明', 'stats', 1)
        elif self.page == 'automation':
            fish = state['config'].get('fishing', {})
            farm = state['config'].get('farming', {})
            self.button(f'自律釣魚：{"開" if fish.get("enabled") else "關"}',
                        'toggle_life:fishing:enabled', 0)
            self.button(f'自律農耕：{"開" if farm.get("enabled") else "關"}',
                        'toggle_life:farming:enabled', 0)
            signup = state['config'].get('raid_signup', {})
            self.button(f'討伐響應：{"開" if signup.get("enabled") else "關"}',
                        'toggle_life:raid_signup:enabled', 0)
            cooking = state['config'].get('cooking', {})
            self.button(f'自動備餐：{"開" if cooking.get("enabled") else "關"}',
                        'toggle_life:cooking:enabled', 0)
            self.button('編輯觸發條件', 'triggers', 2)
            presets = [preset for preset in self.cog.provisions.presets(
                self.guild_id, self.owner.id) if preset['ingredients']]
            self.add_item(PanelSelect('cooking_preset', row=1, placeholder='選擇自動備餐配方',
                disabled=not presets, options=[discord.SelectOption(label=preset['name'][:100],
                    value=str(preset['slot']), default=preset['slot'] == cooking.get('preset_slot'))
                    for preset in presets[:25]] or [discord.SelectOption(label='尚無已保存配方', value='empty')]))
        elif self.page == 'body':
            materials = [(key, quantity, material_profile(key))
                         for key, quantity in inventory.items() if quantity and material_profile(key)]
            materials.sort(key=lambda row: (-row[2][0], ITEMS[row[0]].name, row[0]))
            options = [discord.SelectOption(label=ITEMS[key].name[:100], value=key,
                       description=(f'持有 {quantity}｜素材 T{profile[0]}｜傾向 '
                                    + '／'.join(STAT_NAMES[index] for index, weight
                                                 in enumerate(profile[1]) if weight)))
                       for key, quantity, profile in materials]
            self.add_item(PanelSelect('add_material', row=0, placeholder='每次加入一份素材',
                disabled=not options or len(self.materials) >= 10,
                options=options[:25] or [discord.SelectOption(label='沒有可用素材', value='empty')]))
            self.button('移除最後一份', 'remove_material', 1, disabled=not self.materials)
            self.button('開始製作', 'start_body', 1, disabled=len(self.materials) != 10,
                        style=discord.ButtonStyle.success)
            craft = state['crafting']
            self.button('完成素體', 'finish_body', 1,
                        disabled=not craft or time.time() < craft['ready_at'])
            acceleration_cost = body_acceleration_cost(craft)
            self.button(f'立即完成・{acceleration_cost:,} 金幣', 'accelerate_body', 1,
                        disabled=not acceleration_cost, style=discord.ButtonStyle.primary)
            self.button('安裝候選素體', 'install_body', 2, disabled=not state['candidate_body'])
            self.button('拆除候選素體', 'discard_body', 2, disabled=not state['candidate_body'],
                        style=discord.ButtonStyle.danger)
        elif self.page == 'cores':
            cores = self.cog.alchemy.cores(self.guild_id, self.owner.id)
            visible_cores = cores[:24]
            if self.core_id not in {core['id'] for core in visible_cores}:
                self.core_id = None
            self.add_item(PanelSelect('core', row=0, placeholder='選擇思考核心', options=[
                discord.SelectOption(label='新增／定向思考核心', value='new',
                                     default=self.core_id is None)] + [
                discord.SelectOption(label=f'#{core["id"]} Lv.{core["level"]} {core["orientation"]}',
                    value=str(core['id']), description='已裝備' if core['equipped'] else '未裝備',
                    default=core['id'] == self.core_id) for core in visible_cores]))
            chosen = next((core for core in cores if core['id'] == self.core_id), None)
            if chosen:
                same_stone = False
                if self.slot and self.item_id:
                    selected_domain, selected_index = self.slot.split(':')
                    selected = chosen['skills'][selected_domain][int(selected_index) - 1]
                    stone = parse_stone(self.item_id)
                    same_stone = bool(selected and stone and selected['key'] == stone[1]
                                      and selected['rarity'] == stone[2])
                self.button('裝備核心', 'equip_core', 1)
                self.button('刻入技能石', 'engrave', 1,
                            disabled=not self.slot or not self.item_id or same_stone)
                dismantle_label = ('確認拆解核心' if self.confirm_core_dismantle
                                   else '拆解核心')
                self.button(dismantle_label, 'dismantle_core', 1,
                            style=discord.ButtonStyle.danger)
                slots = []
                for domain, skills in chosen['skills'].items():
                    for index, saved in enumerate(skills, 1):
                        label = (COMBAT_SKILLS if domain == 'combat' else LIFE_SKILLS).get(
                            saved['key'], '空白') if saved else '空白'
                        if saved:
                            label = f'{saved["rarity"]}・{label}'
                        slots.append(discord.SelectOption(
                            label=f'{"戰鬥" if domain == "combat" else "生活"} {index}｜{label}',
                            value=f'{domain}:{index}', default=f'{domain}:{index}' == self.slot))
                self.add_item(PanelSelect('slot', row=2, placeholder='選擇刻印格', options=slots))
                domain = self.slot.split(':')[0] if self.slot else None
                stones = [(key, parse_stone(key)) for key, quantity in inventory.items()
                          if quantity and parse_stone(key) and parse_stone(key)[0] == domain]
                self.add_item(PanelSelect('stone', row=3, placeholder='選擇技能石', disabled=not stones,
                    options=[discord.SelectOption(label=ITEMS[key].name[:100], value=key,
                        description=f'持有 {inventory[key]}｜{ITEMS[key].description}'[:100],
                        default=key == self.item_id)
                        for key, _ in stones[:25]] or [
                            discord.SelectOption(label='沒有相符技能石', value='empty')]))
            else:
                self.button('證章購買 Lv.1', 'buy_core', 1)
                for level in (1, 2, 3):
                    self.button(f'Lv.{level} 定向戰鬥', f'orient:{level}:戰鬥', 1 + level // 3,
                                disabled=not inventory.get(CORE_ITEM[level]))
                    self.button(f'Lv.{level} 定向生活', f'orient:{level}:生活', 1 + level // 3,
                                disabled=not inventory.get(CORE_ITEM[level]))
        elif self.page == 'gacha':
            for index, domain in enumerate(('combat', 'life')):
                label = '戰鬥' if domain == 'combat' else '生活'
                self.button(f'{label}單抽・500', f'draw:{domain}:1', index)
                self.button(f'{label}十連・5,000', f'draw:{domain}:10', index,
                            style=discord.ButtonStyle.primary)
            self.button('鍊金粉塵兌換', 'powder', 2)
            self.button('批次分解技能石', 'decompose', 2)
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
        elif self.page == 'decompose':
            stones = [(key, quantity, parse_stone(key)) for key, quantity in inventory.items()
                      if quantity and parse_stone(key)]
            stones.sort(key=lambda row: (tuple(RARITIES).index(row[2][2]), ITEMS[row[0]].name))
            options = [discord.SelectOption(label=ITEMS[key].name[:100], value=key,
                       description=f'持有 {quantity}｜全數 {RARITIES[stone[2]][2] * quantity} 粉塵',
                       default=key in self.decompose_items)
                       for key, quantity, stone in stones[:25]]
            self.add_item(MultiPanelSelect('stone_batch', row=0,
                placeholder='可同時選擇多種技能石', disabled=not options,
                min_values=1, max_values=max(1, len(options)), options=options or [
                    discord.SelectOption(label='沒有可分解的技能石', value='empty')]))
            label = '確認分解所選全部' if self.confirm_decompose else '分解所選技能石'
            self.button(label, 'decompose_batch', 1, disabled=not self.decompose_items,
                        style=discord.ButtonStyle.danger if self.confirm_decompose else discord.ButtonStyle.secondary)
        elif self.page == 'fuel':
            options = [discord.SelectOption(label=ITEMS[key].name[:100], value=key,
                       description=f'持有 {quantity}｜每個 {fuel_value(ITEMS[key])} 燃料')
                       for key, quantity in inventory.items() if quantity and key in ITEMS
                       and not key.startswith('alchemy:')
                       and ITEMS[key].category not in ('裝備', '釣竿')
                       and item_sellable(ITEMS[key])]
            self.add_item(PanelSelect('fuel_item', row=0, placeholder='選擇要轉換的素材',
                disabled=not options, options=options[:25] or [
                    discord.SelectOption(label='沒有可轉換素材', value='empty')]))
            owned = inventory.get(self.fuel_item_id, 0)
            per_item = fuel_value(ITEMS[self.fuel_item_id]) if self.fuel_item_id in ITEMS else 0
            maximum = owned if per_item else 0
            remaining_capacity = max(0, FUEL_CAPACITY - state['fuel'])
            quantities = sorted({amount for amount in (1, 5, 10, maximum) if amount <= maximum})
            if self.fuel_quantity not in quantities:
                self.fuel_quantity = quantities[0] if quantities else 1
            self.add_item(PanelSelect('fuel_quantity', row=1, placeholder='選擇轉換數量',
                disabled=not quantities, options=[discord.SelectOption(
                    label=('全部持有數量' if amount == maximum else f'{amount} 個'),
                    value=str(amount), description=(
                        f'實際增加 {min(amount * per_item, remaining_capacity)} 燃料' +
                        (f'｜溢出 {amount * per_item - remaining_capacity}'
                         if amount * per_item > remaining_capacity else ''))[:100],
                    default=amount == self.fuel_quantity) for amount in quantities] or [
                        discord.SelectOption(label='無可轉換數量', value='empty')]))
            label = '確認不可逆轉換' if self.confirm_fuel else f'轉換 {self.fuel_quantity} 個'
            self.button(label, 'convert_fuel', 2,
                        disabled=(not self.fuel_item_id or not maximum or not remaining_capacity),
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
            combat_enabled = state['config'].get('combat_support', {}).get('enabled', False)
            combat_cost = operation_fuel_cost(body) if body else 100
            embed = discord.Embed(title=f'煉金人偶｜{state["name"]}', color=0xB8864B,
                description=f'**素體**　{body_text}\n**思考核心**　{core_text}\n'
                            f'**燃料**　{state["fuel"]:,}\n'
                            f'**戰鬥支援**　{"已開啟" if combat_enabled else "未開啟"}｜'
                            f'每場 {combat_cost} 燃料｜不占隊伍名額')
            if core:
                combat_lines = []
                for saved in core['skills']['combat']:
                    if saved:
                        result = (combat_stone_effect(saved['key'], saved['rarity'], body)
                                  if body else '需安裝素體')
                        uses = COMBAT_RARITY_STATS[saved['rarity']]['uses']
                        combat_lines.append(
                            f'{saved["rarity"]}・{COMBAT_SKILLS[saved["key"]]}｜'
                            f'{result}｜每場 {uses} 次')
                life_lines = []
                for saved in core['skills']['life']:
                    if saved:
                        skill = self.cog.alchemy.life_skill(
                            self.guild_id, self.owner.id, saved['key'])
                        if skill:
                            unlocks = life_skill_unlocks(saved['key'], skill['work'])
                            unlocked = '、'.join(unlocks) if unlocks else '尚無'
                            result = f'工作力 {skill["work"]:,}｜已解鎖：{unlocked}'
                        else:
                            result = '需安裝素體'
                        life_lines.append(
                            f'{saved["rarity"]}・{LIFE_SKILLS[saved["key"]]}｜{result}')
                if combat_lines:
                    embed.add_field(name='戰鬥技能石｜稀有度計算後',
                                    value='\n'.join(combat_lines), inline=False)
                if life_lines:
                    embed.add_field(name='生活技能石｜稀有度計算後',
                                    value='\n'.join(life_lines), inline=False)
            embed.add_field(name='使用流程',
                            value='製作並安裝素體 → 定向並裝備思考核心 → '
                                  '取得技能石並刻入對應迴路。\n'
                                  '生活技能還需在「自動化設定」開啟；'
                                  '戰鬥技能可在戰鬥中有限次數發動。', inline=False)
        elif self.page == 'body':
            counts = Counter(self.materials)
            selection = '、'.join(f'{ITEMS[key].name}×{amount}' for key, amount in counts.items()) or '尚未選擇'
            craft = state['crafting']
            status = (f'T{craft["tier"]}，<t:{int(craft["ready_at"])}:R>完成' if craft else
                      '有候選素體等待處理' if state['candidate_body'] else '目前閒置')
            embed = discord.Embed(title='煉金人偶｜素體製作', color=0xB8864B,
                description=f'選擇固定 10 份、最多 5 種素材。\n**已選 {len(self.materials)}/10**　{selection}\n'
                            f'**工坊狀態**　{status}')
            if body:
                embed.add_field(name=f'當前素體 T{body["tier"]}', value='｜'.join(
                    f'{name} {value}' for name, value in zip(STAT_NAMES, body['stats'])), inline=False)
            candidate = state['candidate_body']
            if candidate:
                values = []
                for index, (name, value) in enumerate(zip(STAT_NAMES, candidate['stats'])):
                    delta = value - body['stats'][index] if body else None
                    values.append(f'{name} {value}' + (f' ({delta:+d})' if delta is not None else ''))
                embed.add_field(name=f'候選素體 T{candidate["tier"]}',
                                value='｜'.join(values), inline=False)
                if core:
                    previews = []
                    for saved in core['skills']['life']:
                        if not saved:
                            continue
                        current = life_work_state(saved['key'], saved['rarity'], body)
                        after = life_work_state(saved['key'], saved['rarity'], candidate)
                        previews.append(
                            f'{saved["rarity"]}・{LIFE_SKILLS[saved["key"]]}\n'
                            f'目前　{current}\n安裝後　{after}')
                    if previews:
                        embed.add_field(name='安裝候選素體｜生活工作力預覽',
                                        value='\n\n'.join(previews), inline=False)
        elif self.page == 'cores':
            cores = self.cog.alchemy.cores(self.guild_id, self.owner.id)
            lines = []
            for saved in cores:
                skills = [entry for domain in saved['skills'].values() for entry in domain if entry]
                lines.append(f'#{saved["id"]}｜Lv.{saved["level"]} {saved["orientation"]}'
                             f'{"【已裝備】" if saved["equipped"] else ""}｜已刻印 {len(skills)} 格')
            embed = discord.Embed(title='煉金人偶｜思考核心', color=0xB8864B,
                description=('核心定向後會綁定，並決定戰鬥／生活刻印格數。'
                             '先選核心、再選刻印格與對應技能石；覆蓋不返還舊石。'
                             '拆解核心時，已刻印技能石會一併轉為鍊金粉塵。\n\n'
                             + ('\n'.join(lines) if lines else
                                '尚無已定向核心。Lv.1 胚可用 15 枚討伐之證購買。')))
            chosen = next((saved for saved in cores if saved['id'] == self.core_id), None)
            if chosen:
                for domain, label, pool in (('combat', '戰鬥迴路', COMBAT_SKILLS),
                                            ('life', '生活迴路', LIFE_SKILLS)):
                    details = []
                    for index, saved in enumerate(chosen['skills'][domain], 1):
                        if not saved:
                            details.append(f'{index}. 空白')
                        elif domain == 'combat':
                            effect = (combat_stone_effect(saved['key'], saved['rarity'], body)
                                      if body else COMBAT_SKILL_DETAILS[saved['key']])
                            uses = COMBAT_RARITY_STATS[saved['rarity']]['uses']
                            details.append(
                                f'{index}. {saved["rarity"]}・{pool[saved["key"]]}｜'
                                f'{effect}｜每場 {uses} 次')
                        else:
                            skill = self.cog.alchemy.life_skill(
                                self.guild_id, self.owner.id, saved['key'])
                            work = f'工作力 {skill["work"]:,}' if skill else '需安裝素體'
                            details.append(
                                f'{index}. {saved["rarity"]}・{pool[saved["key"]]}｜{work}')
                    embed.add_field(name=f'核心 #{chosen["id"]}｜{label}',
                                    value='\n'.join(details), inline=False)
            pending = parse_stone(self.item_id) if self.item_id else None
            if pending:
                domain, key, rarity = pending
                detail = (combat_stone_effect(key, rarity, body) if domain == 'combat' and body
                          else COMBAT_SKILL_DETAILS[key] if domain == 'combat'
                          else f'效果倍率 {RARITIES[rarity][1]:.0%}')
                if domain == 'combat':
                    detail += f'｜每場 {COMBAT_RARITY_STATS[rarity]["uses"]} 次'
                embed.add_field(name='待刻印技能石',
                                value=f'{rarity}・{(COMBAT_SKILLS if domain == "combat" else LIFE_SKILLS)[key]}\n{detail}',
                                inline=False)
                if chosen and domain == 'life' and self.slot:
                    slot_domain, slot_index = self.slot.split(':')
                    if slot_domain == 'life':
                        current = chosen['skills']['life'][int(slot_index) - 1]
                        before = ('空白' if not current else
                                  f'{current["rarity"]}・{LIFE_SKILLS[current["key"]]}｜'
                                  f'{life_work_state(current["key"], current["rarity"], body)}')
                        after = (f'{rarity}・{LIFE_SKILLS[key]}｜'
                                 f'{life_work_state(key, rarity, body)}')
                        embed.add_field(name='刻印後｜生活工作力預覽',
                                        value=f'目前　{before}\n刻印後　{after}', inline=False)
        elif self.page == 'gacha':
            gold = self.cog.store.gold(self.guild_id, self.owner.id)
            pity = self.cog.alchemy.pity(self.guild_id, self.owner.id)
            embed = discord.Embed(title='煉金人偶｜技能石轉蛋', color=0xB8864B,
                description=f'**目前金幣**　{gold:,}\n'
                            '**價格**　單抽 500｜十連 5,000\n\n'
                            '**基礎機率**　普通 70%｜稀有 24%｜史詩 5%｜傳說 1%\n'
                            '**戰鬥石**　普通 90%・1 次｜稀有 100%・1 次｜'
                            '史詩 110%・2 次｜傳說 120%・3 次\n'
                            '**生活石效果**　普通 70%｜稀有 80%｜史詩 90%｜傳說 100%\n\n'
                            '**保底**　十連至少一顆稀有；'
                            '50 抽未出史詩時保底史詩以上，100 抽未出傳說時保底傳說。\n'
                            f'**目前進度**　史詩以上保底剩 **{pity["epic_remaining"]}** 抽｜'
                            f'傳說保底剩 **{pity["legend_remaining"]}** 抽\n'
                            '戰鬥與生活卡池共用保底計數。')
        elif self.page == 'powder':
            powder = self.cog.characters.inventory_counts(
                self.guild_id, self.owner.id).get('alchemy:powder', 0)
            embed = discord.Embed(title='煉金人偶｜鍊金粉塵兌換', color=0xB8864B,
                description=f'目前持有 **{powder}** 粉塵。可指定兌換普通、稀有或史詩技能石；傳說不開放兌換。')
        elif self.page == 'decompose':
            inventory = self.cog.characters.inventory_counts(self.guild_id, self.owner.id)
            quantity = sum(inventory.get(key, 0) for key in self.decompose_items)
            powder = sum(inventory.get(key, 0) * RARITIES[parse_stone(key)[2]][2]
                         for key in self.decompose_items if parse_stone(key))
            embed = discord.Embed(title='煉金人偶｜批次分解技能石', color=0xB8864B,
                description=f'選取多種技能石後，會分解所選種類的全部庫存。\n'
                            f'**已選**　{len(self.decompose_items)} 種／{quantity} 顆\n'
                            f'**預計取得**　{powder} 鍊金粉塵')
        elif self.page == 'automation':
            cost = operation_fuel_cost(body) if body else 100
            discount = fuel_discount(body['stats'][2]) if body else 0
            embed = discord.Embed(title='煉金人偶｜自動化設定', color=0xB8864B,
                description='生活技能必須已刻入目前核心才會執行。釣魚與農耕會持續到燃料不足；'
                            '自動備餐與討伐響應另外使用討伐觸發條件。\n\n'
                            '每次「收竿＋再出發」、「收成＋重種」、備餐或討伐響應'
                            f'皆為基礎 100 燃料。目前耐久減免 {discount}%，'
                            f'每次實際消耗 **{cost}** 燃料。')
        elif self.page == 'stats':
            farming_thresholds = '｜'.join(
                f'{label} {threshold}' for threshold, label in LIFE_WORK_UNLOCKS['farming'])
            embed = discord.Embed(title='煉金人偶｜能力說明', color=0xB8864B,
                description=('**構造**　每點 +10 最大 HP；農耕、討伐響應的副能力。\n'
                             '**動力**　每點 +3 攻擊；自律農耕的主能力。\n'
                             '**耐久**　每點 +3 防禦；燃料減免 = 耐久÷（耐久＋100），'
                             '最高 70%；'
                             '自律釣魚的副能力。\n'
                             '**精密**　速度 = 35 + 精密÷10（上限 100）；釣魚、備餐與討伐響應的主能力。\n'
                             '**靈質**　每點 +3 治療量，並影響治療、護盾與輔助技能；備餐的副能力。\n\n'
                             '**戰鬥技能石**　普通 90%・1 次｜稀有 100%・1 次｜'
                             '史詩 110%・2 次｜傳說 120%・3 次；支援後間隔兩回合。\n'
                             '**生活技能石**　普通 70%｜稀有 80%｜史詩 90%｜傳說 100%。\n'
                             '生活工作力 = ⌊（主能力×2＋副能力）÷3×稀有度倍率⌋；'
                             '計算後數值會顯示在人偶主介面。\n'
                             f'**農耕工作力門檻**　{farming_thresholds}；'
                             '每塊田獨立檢查。\n'
                             '命中依素體 Tier 固定，不受精密影響；暴擊率 10%、閃避率 0%。'))
        elif self.page == 'triggers':
            label = LIFE_SKILLS[self.trigger_skill]
            embed = discord.Embed(title=f'煉金人偶｜{label}觸發條件', color=0xB8864B,
                description='勾選討伐池、品質與來源。條件全部符合時才會嘗試操作；'
                            '檢查失敗不消耗燃料。')
        else:
            cost = operation_fuel_cost(body) if body else 100
            discount = fuel_discount(body['stats'][2]) if body else 0
            cycles = state['fuel'] // cost
            endurance = '｜'.join(
                f'{label}約 {duration_text(cycles * seconds)}'
                for label, seconds in (('30 分鐘釣魚', 1800), ('2 小時釣魚', 7200),
                                       ('8 小時釣魚', 28800)))
            embed = discord.Embed(title='煉金人偶｜燃料', color=0xB8864B,
                description=f'目前燃料：**{state["fuel"]:,}／{FUEL_CAPACITY:,}**\n'
                            f'目前耐久減免：**{discount}%**｜每次自動操作：**{cost}** 燃料\n'
                            f'剩餘可執行：**{cycles} 次**（收竿、收成、備餐或討伐響應）\n'
                            f'{endurance}\n\n'
                            '每件素材取得等同該物品出售價的燃料，最低 1。')
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
        embed.set_footer(text='人偶戰鬥技能不占玩家原本的回合行動。')
        return embed

    async def handle(self, interaction, action, value=None):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            try:
                if action != 'dismantle_core':
                    self.confirm_core_dismantle = False
                if action in ('automation', 'triggers'):
                    state = self.cog.alchemy.state(self.guild_id, self.owner.id)
                    if not state['active_body'] or not state['core']:
                        raise CharacterError('請先安裝素體與思考核心，再設定自動化。')
                    self.page = action
                elif action in ('overview', 'body', 'cores', 'gacha', 'powder', 'fuel',
                                'decompose', 'stats'):
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
                elif action == 'accelerate_body':
                    cost = self.cog.alchemy.accelerate_body(self.guild_id, self.owner.id)
                    body = self.cog.alchemy.finish_body(self.guild_id, self.owner.id)
                    notice = f'已消耗 {cost:,} 金幣，T{body["tier"]} 素體已完成。'
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
                elif action == 'core':
                    self.core_id = None if value == 'new' else int(value)
                    self.slot, self.item_id = None, None
                    self.confirm_core_dismantle = False
                elif action == 'slot' and value != 'empty':
                    self.slot, self.item_id = value, None
                elif action == 'stone' and value != 'empty':
                    self.item_id = value
                elif action == 'fuel_item' and value != 'empty':
                    self.fuel_item_id = value
                    self.fuel_quantity = 1
                    self.confirm_fuel = False
                elif action == 'equip_core':
                    self.cog.alchemy.equip_core(self.guild_id, self.owner.id, self.core_id)
                    notice = '已裝備思考核心。'
                elif action == 'dismantle_core':
                    if not self.confirm_core_dismantle:
                        core = next(saved for saved in self.cog.alchemy.cores(
                            self.guild_id, self.owner.id) if saved['id'] == self.core_id)
                        stones = [stone for domain in core['skills'].values()
                                  for stone in domain if stone]
                        powder = sum(RARITIES[stone['rarity']][2] for stone in stones)
                        self.confirm_core_dismantle = True
                        notice = (f'將永久拆解核心 #{self.core_id}；其中 {len(stones)} 顆技能石'
                                  f'會一併分解為 {powder} 份鍊金粉塵。請再次確認。')
                    else:
                        result = self.cog.alchemy.dismantle_core(
                            self.guild_id, self.owner.id, self.core_id)
                        self.core_id = None
                        self.slot, self.item_id = None, None
                        self.confirm_core_dismantle = False
                        notice = (f'已拆解思考核心與 {result["stones"]} 顆技能石，'
                                  f'取得 {result["powder"]} 份鍊金粉塵。')
                elif action == 'engrave':
                    domain, slot = self.slot.split(':')
                    self.cog.alchemy.engrave(self.guild_id, self.owner.id, self.core_id,
                                             domain, int(slot), self.item_id)
                    self.item_id = None
                    notice = '技能石已刻入。'
                elif action == 'stone_batch':
                    self.decompose_items = [key for key in value if key != 'empty']
                    self.confirm_decompose = False
                elif action == 'decompose_batch':
                    if not self.confirm_decompose:
                        self.confirm_decompose = True
                        notice = '將分解所選種類的全部技能石；此操作不可逆，請再次確認。'
                    else:
                        quantity, powder = self.cog.alchemy.decompose_many(
                            self.guild_id, self.owner.id, self.decompose_items)
                        self.decompose_items = []
                        self.confirm_decompose = False
                        notice = f'已分解 {quantity} 顆技能石，取得 {powder} 份鍊金粉塵。'
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
                elif action == 'fuel_quantity' and value != 'empty':
                    self.fuel_quantity = int(value)
                    self.confirm_fuel = False
                elif action == 'convert_fuel':
                    raw_fuel = fuel_value(ITEMS[self.fuel_item_id]) * self.fuel_quantity
                    current_fuel = self.cog.alchemy.state(
                        self.guild_id, self.owner.id)['fuel']
                    actual_fuel = min(raw_fuel, max(0, FUEL_CAPACITY - current_fuel))
                    overflow = raw_fuel - actual_fuel
                    if not self.confirm_fuel:
                        self.confirm_fuel = True
                        warning = ('；這是常用的幸運／盛宴食材' if any(
                            word in ITEMS[self.fuel_item_id].description for word in ('幸運', '盛宴')) else '')
                        notice = (f'將消耗 {self.fuel_quantity} 個 {ITEMS[self.fuel_item_id].name}{warning}，'
                                  f'實際增加 {actual_fuel} 燃料並補至最多 {FUEL_CAPACITY:,}。'
                                  + (f'其中 {overflow} 燃料會溢出消失。' if overflow else '') +
                                  '此操作不可逆，請再次確認。')
                    else:
                        fuel = self.cog.alchemy.convert_fuel(
                            self.guild_id, self.owner.id, self.fuel_item_id, self.fuel_quantity)
                        self.confirm_fuel = False
                        notice = (f'已增加 {fuel} 燃料，目前最多為 {FUEL_CAPACITY:,}。' +
                                  (f'另有 {overflow} 燃料溢出。' if overflow else ''))
                elif action == 'toggle_combat_support':
                    current = self.cog.alchemy.state(
                        self.guild_id, self.owner.id)['config'].get(
                            'combat_support', {}).get('enabled', False)
                    enabled = self.cog.alchemy.set_combat_support(
                        self.guild_id, self.owner.id, not current)
                    notice = ('已開啟戰鬥支援；每場建立支援時會消耗一次燃料。'
                              if enabled else '已關閉戰鬥支援。')
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
