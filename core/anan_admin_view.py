"""Private, owner-bound administration for Anan."""
import logging
from dataclasses import asdict

import discord

logger = logging.getLogger(__name__)
SECTIONS = {'member': '成員概況', 'personal': '個人記憶', 'guild': '伺服器記憶', 'voice': '語音控制'}


async def require_admin(interaction, ai, owner_id=None, guild_id=None):
    if (interaction.guild_id is None
            or (guild_id is not None and interaction.guild_id != guild_id)
            or (owner_id is not None and interaction.user.id != owner_id)
            or not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator):
        await interaction.response.send_message('只有開啟面板的伺服器管理員可以操作。', ephemeral=True)
        return False
    if not ai._guild_is_allowed(interaction.guild_id):
        await interaction.response.send_message('這個伺服器尚未開放安安管理。', ephemeral=True)
        return False
    return True


class AnanAdminView(discord.ui.View):
    def __init__(self, ai, owner_id, guild_id):
        super().__init__(timeout=600)
        self.ai, self.owner_id, self.guild_id = ai, owner_id, guild_id
        self.member = None
        self.section = 'member'
        self.page = 0
        self.message = None
        self.closed = False

    def stop(self):
        self.closed = True
        super().stop()

    async def interaction_check(self, interaction):
        if not await require_admin(interaction, self.ai, self.owner_id, self.guild_id):
            return False
        if self.closed or self.is_finished():
            await interaction.response.send_message('面板已過期，請重新使用 /安安管理。', ephemeral=True)
            return False
        return True

    def memories(self, section=None, member_id=None):
        section = section or self.section
        if section == 'guild':
            return [asdict(item) for item in self.ai.memory.list_guild_memories(self.guild_id, 100)]
        member_id = member_id or (self.member.id if self.member else None)
        if section == 'personal' and member_id is not None:
            return self.ai.memory.list_personal_memories(self.guild_id, member_id)
        return []

    def button(self, label, callback, *, disabled=False, style=discord.ButtonStyle.secondary, row=3):
        button = discord.ui.Button(label=label, disabled=disabled, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    def render(self):
        self.clear_items()
        users = discord.ui.UserSelect(placeholder='選擇要管理的成員', row=0)

        async def choose_user(interaction):
            if not await self.interaction_check(interaction):
                return
            member = users.values[0]
            if not isinstance(member, discord.Member) or member.guild.id != self.guild_id:
                await interaction.response.send_message('請選擇本伺服器的成員。', ephemeral=True)
                return
            self.member, self.page = member, 0
            await self.refresh(interaction)

        users.callback = choose_user
        self.add_item(users)
        sections = discord.ui.Select(placeholder='選擇管理功能', row=1, options=[
            discord.SelectOption(label=label, value=key, default=key == self.section)
            for key, label in SECTIONS.items()
        ])

        async def choose_section(interaction):
            if not await self.interaction_check(interaction):
                return
            self.section, self.page = sections.values[0], 0
            await self.refresh(interaction)

        sections.callback = choose_section
        self.add_item(sections)
        embed = discord.Embed(title=f'安安管理｜{SECTIONS[self.section]}', color=0x9182C4)
        target = self.member.display_name if self.member else '尚未選擇'
        embed.description = f'目前管理成員：**{discord.utils.escape_markdown(target)}**'
        if self.section == 'member' and self.member:
            score = self.ai.memory.get_affinity(self.guild_id, self.member.id)
            level, _ = self.ai._affinity_profile(score)
            impression = self.ai.memory.get_impression(self.guild_id, self.member.id)
            embed.add_field(name='整體印象', value=impression or '尚未形成印象。', inline=False)
            embed.add_field(name='好感度', value=f'{score}／100（{level}）')
            self.button('調整好感度', self.edit_affinity, style=discord.ButtonStyle.primary)
        elif self.section in {'personal', 'guild'}:
            items = self.memories()
            self.page = max(0, min(self.page, (len(items) - 1) // 5))
            visible = items[self.page * 5:self.page * 5 + 5]
            for item in visible:
                basis = {'explicit': '明確提及', 'inferred': '暫時推測'}.get(item.get('basis'), '')
                expiry = f"\n有效至 {item['expires_at']} UTC" if item.get('expires_at') else ''
                embed.add_field(name=f"#{item['id']} {basis}", value=(
                    f"{item['content']}\n更新：{item['updated_at']} UTC{expiry}"
                )[:1024], inline=False)
            if not visible:
                embed.add_field(name='記憶', value='請先選擇成員。' if self.section == 'personal' and not self.member else '目前沒有記憶。')
            if visible:
                # Capture the displayed owner and records; later member switches cannot retarget deletion.
                scope = self.section
                member_id = self.member.id if self.member and scope == 'personal' else None
                snapshots = {str(item['id']): dict(item) for item in visible}
                picker = discord.ui.Select(placeholder='選擇要刪除的記憶（下一步確認）', row=2, options=[
                    discord.SelectOption(label=f"#{item['id']} {item['content']}"[:100], value=str(item['id']))
                    for item in visible
                ])

                async def select_memory(interaction):
                    if not await self.interaction_check(interaction):
                        return
                    snapshot = snapshots[picker.values[0]]
                    confirm = DeleteMemoryView(self, scope, member_id, snapshot)
                    await interaction.response.send_message(
                        embed=discord.Embed(title='確認刪除記憶', description=(
                            f"{'伺服器共同記憶' if scope == 'guild' else f'成員 ID：{member_id}'}\n"
                            f"#{snapshot['id']}\n{snapshot['content']}\n\n刪除後無法復原。"
                        )), view=confirm, ephemeral=True,
                    )

                picker.callback = select_memory
                self.add_item(picker)
            self.button('上一頁', self.previous, disabled=self.page == 0)
            self.button('下一頁', self.next, disabled=(self.page + 1) * 5 >= len(items))
            embed.set_footer(text=f'第 {self.page + 1} 頁 · 共 {len(items)} 條 · 面板閒置十分鐘後過期')
        elif self.section == 'voice':
            guild = self.ai.bot.get_guild(self.guild_id)
            voice = guild.voice_client if guild else None
            embed.add_field(name='語音狀態', value=f'位於 {voice.channel.name}' if voice else '尚未加入語音頻道。', inline=False)
            embed.add_field(name='操作方式', value='上線會加入操作者所在頻道；朗讀文字會在該頻道播放。', inline=False)
            self.button('上線', self.join_voice, style=discord.ButtonStyle.success)
            self.button('離開語音', self.leave_voice)
            self.button('朗讀文字', self.speak)
        else:
            embed.add_field(name='成員管理', value='先從上方選擇成員，即可查看印象與調整好感度。')
        self.button('重新整理', self.refresh, row=4)
        self.button('關閉', self.close, row=4)
        return embed

    async def refresh(self, interaction):
        if await self.interaction_check(interaction):
            await interaction.response.edit_message(embed=self.render(), view=self)

    async def previous(self, interaction):
        if await self.interaction_check(interaction):
            self.page = max(0, self.page - 1)
            await self.refresh(interaction)

    async def next(self, interaction):
        if await self.interaction_check(interaction):
            self.page += 1
            await self.refresh(interaction)

    async def edit_affinity(self, interaction):
        if await self.interaction_check(interaction) and self.member:
            await interaction.response.send_modal(AffinityModal(self, self.member))

    async def join_voice(self, interaction):
        await self.voice_action(interaction, 'join')

    async def leave_voice(self, interaction):
        await self.voice_action(interaction, 'leave')

    async def voice_action(self, interaction, action, text=None, language=None):
        if not await self.interaction_check(interaction):
            return
        anan = self.ai.bot.get_cog('Anan')
        if anan is None:
            await interaction.response.send_message('語音功能尚未載入。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await anan.admin_voice_action(interaction, action, text, language)
        await interaction.followup.send(result, ephemeral=True)

    async def speak(self, interaction):
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(SpeechModal(self))

    async def close(self, interaction):
        if await self.interaction_check(interaction):
            self.stop()
            await interaction.response.edit_message(content='管理面板已關閉。', embed=None, view=None)

    async def on_timeout(self):
        self.closed = True
        if self.message:
            try:
                await self.message.edit(content='管理面板已過期，請重新使用 /安安管理。', embed=None, view=None)
            except discord.HTTPException:
                pass


class DeleteMemoryView(discord.ui.View):
    def __init__(self, panel, scope, member_id, snapshot):
        super().__init__(timeout=60)
        self.panel, self.scope, self.member_id, self.snapshot = panel, scope, member_id, snapshot
        self.closed = False

    def stop(self):
        self.closed = True
        super().stop()

    async def interaction_check(self, interaction):
        if not await self.panel.interaction_check(interaction):
            return False
        if self.closed or self.is_finished():
            await interaction.response.send_message('確認已失效，請重新選擇記憶。', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='確認刪除', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        current = next((item for item in self.panel.memories(self.scope, self.member_id)
                        if item['id'] == self.snapshot['id']), None)
        if current != self.snapshot:
            result = '這條記憶已更新、過期或刪除，請重新整理面板後再選擇。'
        else:
            store = self.panel.ai.memory
            if self.scope == 'guild':
                removed = store.forget_guild_memory(self.panel.guild_id, self.snapshot['id'])
            else:
                removed = store.forget_personal_memory(self.panel.guild_id, self.member_id, self.snapshot['id'])
            result = '記憶已刪除，可回到面板重新整理。' if removed else '記憶已不存在。'
            logger.info('管理員刪除記憶 guild=%s admin=%s scope=%s target=%s memory=%s removed=%s',
                        self.panel.guild_id, self.panel.owner_id, self.scope, self.member_id, self.snapshot['id'], removed)
        self.stop()
        await interaction.response.edit_message(content=result, embed=None, view=None)

    @discord.ui.button(label='取消')
    async def cancel(self, interaction, button):
        if await self.interaction_check(interaction):
            self.stop()
            await interaction.response.edit_message(content='已取消刪除。', embed=None, view=None)


class AffinityModal(discord.ui.Modal):
    def __init__(self, panel, member):
        super().__init__(title=f'調整好感度｜{member.display_name}'[:45], timeout=180)
        self.panel, self.member_id = panel, member.id
        self.score = discord.ui.TextInput(label='好感度（-100 到 100）', max_length=4,
            default=str(panel.ai.memory.get_affinity(panel.guild_id, member.id)))
        self.add_item(self.score)

    async def on_submit(self, interaction):
        if not await self.panel.interaction_check(interaction):
            return
        try:
            score = int(self.score.value)
            if not -100 <= score <= 100:
                raise ValueError
        except ValueError:
            await interaction.response.send_message('請輸入 -100 到 100 的整數。', ephemeral=True)
            return
        self.panel.ai.memory.set_affinity(self.panel.guild_id, self.member_id, score)
        logger.info('管理員好感度操作 guild=%s admin=%s target=%s score=%s',
                    self.panel.guild_id, self.panel.owner_id, self.member_id, score)
        await interaction.response.send_message(f'已將指定成員的好感度設為 {score}，可回到面板重新整理。', ephemeral=True)


class SpeechModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(title='讓安安朗讀', timeout=180)
        self.panel = panel
        self.text = discord.ui.TextInput(label='朗讀文字（最多 50 字）', max_length=50)
        self.language = discord.ui.TextInput(label='語言：自動、日文、中文、英文', default='自動', max_length=4)
        self.add_item(self.text)
        self.add_item(self.language)

    async def on_submit(self, interaction):
        if not await self.panel.interaction_check(interaction):
            return
        languages = {'自動': 'auto', '日文': 'Japanese', '中文': 'Chinese', '英文': 'English'}
        language = languages.get(self.language.value.strip())
        if not language or not self.text.value.strip():
            await interaction.response.send_message('請填寫文字，並選擇自動、日文、中文或英文。', ephemeral=True)
            return
        await self.panel.voice_action(interaction, 'speak', self.text.value, language)
