"""Houshou Mag's persistent, server-local tarot divinations."""
from dataclasses import dataclass
import random
import time

from core.rpg_character import CharacterError


@dataclass(frozen=True)
class TarotCard:
    name: str
    omen: str
    effect: str
    weight: int


CARDS = {
    'strength': TarotCard('力量', '「至少今天，你的拳頭比腦袋可靠。」', '攻擊 +6%', 10),
    'emperor': TarotCard('皇帝', '「躲在規則後面，也是一種生存方式。」', '防禦 +6%', 10),
    'empress': TarotCard('女帝', '「生命會自己尋找延續的方法。」', '最大 HP +6%', 10),
    'star': TarotCard('星星', '「別誤會，那道光未必是在等你。」', '治療量 +8%、受到的治療 +5%', 10),
    'chariot': TarotCard('戰車', '「跑快一點吧。至於方向對不對……呵呵。」', '速度 +8、命中值 +3', 10),
    'moon': TarotCard('月亮', '「看不清楚，對你反而比較有利。」', '閃避值 +4', 10),
    'sun': TarotCard('太陽', '「今天的你，似乎不容易失手呢。」', '武器穩定度下限 +8 個百分點', 10),
    'justice': TarotCard('正義', '「得到多少，就該付出多少。很公平吧？」', '造成傷害 +6%，受到傷害 +4%', 7),
    'temperance': TarotCard('節制', '「細細品味，餘香便不會散去。」', '本場料理效果不消耗場次', 5),
    'hanged_man': TarotCard('吊人', '「偶爾停下來，才能看見別人漏掉的東西。」', '速度 −10、防禦 +10%', 7),
    'devil': TarotCard('惡魔', '「力量就在這裡。代價嘛……之後再說。」', '攻擊與治療量 +10%，受到的治療 −20%', 5),
    'tower': TarotCard('高塔', '「放心，倒下以前的景色通常都很好看。」', '最大 HP −10%，攻擊 +12%、暴擊率 +5 個百分點', 5),
    'wheel': TarotCard('命運之輪', '「它開始轉了。你猜會停在哪裡？」', '個人裝備掉落率提高 40%', 3),
    'hermit': TarotCard('隱者', '「孤身一人時，影子反而會保護你。」', '首次降至 40% HP 以下時，獲得一回合 25% 直接傷害減免', 4),
    'death': TarotCard('死神', '「別怕，它只是想分走一點生命。」', '直接傷害獲得 5% 吸血', 4),
    'judgement': TarotCard('審判', '「結果已經寫好了，只是還沒輪到你倒下。」', '首次受到致死傷害時保留 1 HP', 3),
    'lovers': TarotCard('戀人', '「把命交給陌生人，聽起來很浪漫吧？」', '與隨機一名隊友連結，平均分攤受到的傷害', 4),
    'fool': TarotCard('愚者', '「往前走吧。反正你也不知道會掉到哪裡。」', '隨機兩項能力 +10%、一項能力 −10%', 4),
    'magician': TarotCard('魔術師', '「手法被看穿以前，魔法都是真的。」', '所有技能冷卻時間 −1 回合', 3),
    'world': TarotCard('世界', '「今天，世界難得願意站在你這邊。」', '全部戰鬥能力 +10%', 1),
    'high_priestess': TarotCard('女祭司', '「她掀開帷幕的一角……那邊的東西，好像也看見你了。」',
                                '可發起目前能參加的最高階討伐', 2),
}


class Divinations:
    BASE_PRICE = 300

    def __init__(self, store, rng=None):
        self.store, self.db = store, store.db
        self.rng = rng or random.SystemRandom()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_divinations (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                day TEXT NOT NULL, draws INTEGER NOT NULL DEFAULT 0,
                card TEXT, bound_raid_id TEXT, summon_raid_id TEXT,
                PRIMARY KEY(guild_id,user_id))''')
            # A process can stop after reserving the card but before publishing.
            # Active/lobby membership still prevents duplicate summons if the
            # announcement was actually sent before the stop.
            self.db.execute("UPDATE rpg_divinations SET summon_raid_id=NULL WHERE summon_raid_id=':reserved:'")

    def status(self, guild, user, now=None):
        day = self.store.day_key(time.time() if now is None else now)
        row = self.db.execute('''SELECT day,draws,card,bound_raid_id,summon_raid_id
            FROM rpg_divinations WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone()
        if not row:
            return dict(day=day, draws=0, card=None, bound_raid_id=None, summon_raid_id=None,
                        next_price=self.BASE_PRICE)
        saved_day, draws, card, bound, summoned = row
        draws = draws if saved_day == day else 0
        return dict(day=day, draws=draws, card=card, bound_raid_id=bound,
                    summon_raid_id=summoned, next_price=self.BASE_PRICE * (draws + 1))

    def draw(self, guild, user, now=None):
        now = time.time() if now is None else now
        day = self.store.day_key(now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('''SELECT day,draws,bound_raid_id,summon_raid_id FROM rpg_divinations
                WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone()
            if row and row[2]:
                raise CharacterError('占卜效果已綁定正在進行的討伐，請等待討伐結束。')
            if row and row[3] == ':reserved:':
                raise CharacterError('女祭司正在揭開帷幕，請等待討伐發布完成。')
            draws = row[1] if row and row[0] == day else 0
            cost = self.BASE_PRICE * (draws + 1)
            paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                WHERE guild_id=? AND user_id=? AND gold>=?''', (cost, guild, user, cost))
            if not paid.rowcount:
                raise CharacterError(f'金幣不足，這次占卜需要 {cost:,} 金幣。')
            keys = tuple(CARDS)
            card = self.rng.choices(keys, weights=[CARDS[key].weight for key in keys], k=1)[0]
            self.db.execute('''INSERT INTO rpg_divinations
                (guild_id,user_id,day,draws,card,bound_raid_id,summon_raid_id)
                VALUES (?,?,?,?,?,NULL,NULL)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET
                day=excluded.day,draws=excluded.draws,card=excluded.card,
                bound_raid_id=NULL,summon_raid_id=NULL''', (guild, user, day, draws + 1, card))
        return card, cost

    def prepare_for_raid(self, raid_id, guild, users):
        """Bind each active card to this raid and return an idempotent snapshot."""
        result = {}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for user in users:
                row = self.db.execute('''SELECT card,bound_raid_id FROM rpg_divinations
                    WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone()
                if not row or not row[0] or row[1] not in (None, raid_id):
                    continue
                card = row[0]
                self.db.execute('''UPDATE rpg_divinations SET bound_raid_id=?
                    WHERE guild_id=? AND user_id=?''', (raid_id, guild, user))
                result[user] = dict(id=card, name=CARDS[card].name, xp_percent=10)
        return result

    def clear_raid(self, raid_id):
        with self.db:
            self.db.execute('''UPDATE rpg_divinations
                SET card=NULL,bound_raid_id=NULL,summon_raid_id=NULL
                WHERE bound_raid_id=?''', (raid_id,))

    def reserve_summon(self, guild, user):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            changed = self.db.execute('''UPDATE rpg_divinations SET summon_raid_id=':reserved:'
                WHERE guild_id=? AND user_id=? AND card='high_priestess'
                AND bound_raid_id IS NULL AND summon_raid_id IS NULL''', (guild, user))
            if not changed.rowcount:
                raise CharacterError('目前沒有可使用的「女祭司」，或這張牌已經發起過討伐。')

    def finish_summon(self, guild, user, raid_id):
        with self.db:
            changed = self.db.execute('''UPDATE rpg_divinations SET summon_raid_id=?
                WHERE guild_id=? AND user_id=? AND card='high_priestess'
                AND summon_raid_id=':reserved:' AND bound_raid_id IS NULL''', (raid_id, guild, user))
            if not changed.rowcount:
                raise CharacterError('女祭司的召喚狀態已經改變。')

    def release_summon(self, guild, user):
        with self.db:
            self.db.execute('''UPDATE rpg_divinations SET summon_raid_id=NULL
                WHERE guild_id=? AND user_id=? AND summon_raid_id=':reserved:' ''', (guild, user))
