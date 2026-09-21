"""Houshou Mag's persistent, server-local tarot divinations."""
from dataclasses import dataclass
import json
import random
import time

from core.rpg_character import CharacterError
from core.rpg import record_gold
from core.rpg_mag_affinity import (affinity_status as mag_affinity_status,
                                   award_resonance, award_selection,
                                   initialize_mag_affinity)


@dataclass(frozen=True)
class TarotCard:
    name: str
    omen: str
    effect: str
    category: str
    weight: int


CARDS = {
    'fool': TarotCard('愚者', '「往前走吧。反正你也不知道會掉到哪裡。」', '討伐時隨機兩項能力 +10%、一項能力 −10%', '戰鬥', 4),
    'magician': TarotCard('魔術師', '「手法被看穿以前，魔法都是真的。」', '討伐技能冷卻時間 −1 回合', '戰鬥', 3),
    'high_priestess': TarotCard('女祭司', '「她掀開帷幕的一角……那邊的東西，好像也看見你了。」', '可發起一次最高階討伐；牌效期間所有討伐個人 XP +15%', '特殊', 2),
    'empress': TarotCard('女帝', '「生命會自己尋找延續的方法。」', '占卜期間開始種植的作物，收穫量 +1', '生活', 5),
    'emperor': TarotCard('皇帝', '「躲在規則後面，也是一種生存方式。」', '討伐防禦 +8%', '戰鬥', 8),
    'hierophant': TarotCard('教皇', '「答案未必正確，但人們總需要一個答案。」', '占卜期間取得的討伐、垂釣、農耕與烹飪 XP +5%', '特殊', 5),
    'lovers': TarotCard('戀人', '「把命交給陌生人，聽起來很浪漫吧？」', '討伐時與隨機一名隊友連結，平均分攤受到的傷害', '戰鬥', 4),
    'chariot': TarotCard('戰車', '「跑快一點吧。至於方向對不對……呵呵。」', '討伐速度 +8、命中值 +3', '戰鬥', 8),
    'strength': TarotCard('力量', '「至少今天，你的拳頭比腦袋可靠。」', '討伐攻擊 +8%', '戰鬥', 8),
    'hermit': TarotCard('隱者', '「孤身一人時，影子反而會保護你。」', '占卜期間派出的人偶遠征，獎勵 +20%', '生活', 4),
    'wheel': TarotCard('命運之輪', '「它開始轉了。你猜會停在哪裡？」', '討伐的個人裝備掉落率提高 40%', '特殊', 3),
    'justice': TarotCard('正義', '「得到多少，就該付出多少。很公平吧？」', '討伐造成傷害 +8%，受到傷害 +5%', '戰鬥', 6),
    'hanged_man': TarotCard('吊人', '「偶爾停下來，才能看見別人漏掉的東西。」', '討伐速度 −10、防禦 +12%', '戰鬥', 6),
    'death': TarotCard('死神', '「別怕，它只是想分走一點生命。」', '討伐直接傷害獲得 6% 吸血', '戰鬥', 4),
    'temperance': TarotCard('節制', '「細細品味，餘香便不會散去。」', '占卜期間完成的料理，烹飪 XP +25%', '生活', 5),
    'devil': TarotCard('惡魔', '「力量就在這裡。代價嘛……之後再說。」', '討伐攻擊與治療量 +12%，受到的治療 −20%', '戰鬥', 4),
    'tower': TarotCard('高塔', '「放心，倒下以前的景色通常都很好看。」', '討伐最大 HP −10%，攻擊 +15%、暴擊率 +5 個百分點', '戰鬥', 4),
    'star': TarotCard('星星', '「別誤會，那道光未必是在等你。」', '占卜期間開始的釣魚，稀有魚權重提高 50%', '生活', 6),
    'moon': TarotCard('月亮', '「看不清楚，對你反而比較有利。」', '占卜期間開始的釣魚，魚竿額外收穫機率 +10 個百分點', '生活', 7),
    'sun': TarotCard('太陽', '「今天的你，似乎不容易失手呢。」', '占卜期間取得的垂釣、農耕與烹飪 XP +12%', '生活', 4),
    'judgement': TarotCard('審判', '「結果已經寫好了，只是還沒輪到你倒下。」', '每場討伐首次受到致死傷害時保留 1 HP', '戰鬥', 3),
    'world': TarotCard('世界', '「今天，世界難得願意站在你這邊。」', '占卜期間取得的討伐與生活 XP +12%', '特殊', 1),
}


def card_rarity(card):
    if card.weight >= 7:
        return '常見'
    if card.weight >= 5:
        return '少見'
    if card.weight >= 3:
        return '稀有'
    return '傳說'


class Divinations:
    BASE_PRICE = 300
    DURATION_SECONDS = 6 * 3600

    def __init__(self, store, rng=None):
        self.store, self.db = store, store.db
        self.rng = rng or random.SystemRandom()
        initialize_mag_affinity(self.db)
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_divinations (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                day TEXT NOT NULL, draws INTEGER NOT NULL DEFAULT 0,
                card TEXT, bound_raid_id TEXT, summon_raid_id TEXT,
                offer_day TEXT, offered_cards TEXT, expires_at REAL,
                selected_at REAL, resonated INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id,user_id))''')
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(rpg_divinations)')}
            for name, definition in (('offer_day', 'TEXT'), ('offered_cards', 'TEXT'),
                                     ('expires_at', 'REAL'), ('selected_at', 'REAL'),
                                     ('resonated', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE rpg_divinations ADD COLUMN {name} {definition}')
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_divination_mastery (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, card TEXT NOT NULL,
                resonance INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id,user_id,card))''')
            migration_now = time.time()
            self.db.execute('''UPDATE rpg_divinations SET expires_at=?,selected_at=?,
                resonated=0,bound_raid_id=NULL WHERE card IS NOT NULL AND expires_at IS NULL''',
                (migration_now + self.DURATION_SECONDS, migration_now))
            self.db.execute("UPDATE rpg_divinations SET summon_raid_id=NULL WHERE summon_raid_id=':reserved:'")

    def status(self, guild, user, now=None):
        now = time.time() if now is None else now
        day = self.store.day_key(now)
        row = self.db.execute('''SELECT day,draws,card,bound_raid_id,summon_raid_id,
            offer_day,offered_cards,expires_at,selected_at,resonated
            FROM rpg_divinations WHERE guild_id=? AND user_id=?''', (guild, user)).fetchone()
        if not row:
            return dict(day=day, draws=0, card=None, bound_raid_id=None, summon_raid_id=None,
                        offer=(), expires_at=None, selected_at=None, resonated=False, next_price=0)
        saved_day, draws, card, bound, summoned, offer_day, raw_offer, expires_at, selected_at, resonated = row
        draws = draws if saved_day == day else 0
        active_card = card if card in CARDS and expires_at is not None and expires_at > now else None
        offer = tuple(json.loads(raw_offer)) if offer_day == day and raw_offer else ()
        return dict(day=day, draws=draws, card=active_card,
                    bound_raid_id=bound if active_card else None,
                    summon_raid_id=summoned if active_card else None, offer=offer,
                    expires_at=expires_at if active_card else None,
                    selected_at=selected_at if active_card else None,
                    resonated=bool(resonated) if active_card else False,
                    next_price=self.BASE_PRICE * draws)

    def _three_cards(self):
        population = list(CARDS)
        weights = [CARDS[key].weight for key in population]
        result = []
        for _ in range(3):
            card = self.rng.choices(population, weights=weights, k=1)[0]
            if card not in population:
                card = population[0]
            index = population.index(card)
            result.append(card)
            population.pop(index)
            weights.pop(index)
        return tuple(result)

    def reveal(self, guild, user, now=None):
        now = time.time() if now is None else now
        day = self.store.day_key(now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            status = self.status(guild, user, now)
            if status['summon_raid_id'] == ':reserved:':
                raise CharacterError('女祭司正在揭開帷幕，請等待討伐發布完成。')
            if status['offer']:
                raise CharacterError('請先從目前揭示的三張牌中選擇一張。')
            cost = status['next_price']
            if cost:
                paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                    WHERE guild_id=? AND user_id=? AND gold>=?''', (cost, guild, user, cost))
                if not paid.rowcount:
                    raise CharacterError(f'金幣不足，這次占卜需要 {cost:,} 金幣。')
                record_gold(self.db, guild, user, -cost, 'divination')
            offer = self._three_cards()
            self.db.execute('''INSERT INTO rpg_divinations
                (guild_id,user_id,day,draws,offer_day,offered_cards)
                VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET
                day=excluded.day,draws=excluded.draws,offer_day=excluded.offer_day,
                offered_cards=excluded.offered_cards''',
                (guild, user, day, status['draws'] + 1, day,
                 json.dumps(offer, ensure_ascii=False, separators=(',', ':'))))
        return offer, cost

    def choose(self, guild, user, card, now=None):
        now = time.time() if now is None else now
        status = self.status(guild, user, now)
        if card not in status['offer']:
            raise CharacterError('這張牌不在目前揭示的牌陣中。')
        encoded_offer = json.dumps(status['offer'], ensure_ascii=False, separators=(',', ':'))
        duration = self.DURATION_SECONDS
        with self.db:
            changed = self.db.execute('''UPDATE rpg_divinations SET card=?,expires_at=?,selected_at=?,
                resonated=0,bound_raid_id=NULL,summon_raid_id=NULL,offer_day=NULL,offered_cards=NULL
                WHERE guild_id=? AND user_id=? AND offer_day=? AND offered_cards=?''',
                (card, now + duration, now, guild, user, status['day'], encoded_offer))
            if not changed.rowcount:
                raise CharacterError('牌陣已經改變，請重新整理。')
            self.db.execute('''INSERT OR IGNORE INTO rpg_divination_mastery
                (guild_id,user_id,card,resonance) VALUES (?,?,?,0)''', (guild, user, card))
            award_selection(self.db, self.store, guild, user, now)
        return card, now + duration

    def draw(self, guild, user, now=None):
        """Compatibility helper: reveal three cards and select the first."""
        offer, cost = self.reveal(guild, user, now)
        self.choose(guild, user, offer[0], now)
        return offer[0], cost

    def xp_bonus_percent(self, guild, user, source, now=None):
        card = self.status(guild, user, now)['card']
        if card == 'world' and source in ('fishing', 'farming', 'cooking'):
            return 12
        if card == 'hierophant':
            return 5
        if card == 'sun' and source in ('fishing', 'farming', 'cooking'):
            return 12
        if card == 'temperance' and source == 'cooking':
            return 25
        return 0

    def resonate(self, guild, user, card, now=None, activation=None, award_now=None):
        now = time.time() if now is None else now
        award_now = now if award_now is None else award_now
        if not self.db.in_transaction:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                return self.resonate(guild, user, card, now, activation, award_now)
        changed = self.db.execute('''UPDATE rpg_divinations SET resonated=1
            WHERE guild_id=? AND user_id=? AND card=? AND expires_at>? AND resonated=0
            AND (? IS NULL OR selected_at=?)''',
            (guild, user, card, now, activation, activation))
        if changed.rowcount:
            self.db.execute('''INSERT INTO rpg_divination_mastery VALUES (?,?,?,1)
                ON CONFLICT(guild_id,user_id,card) DO UPDATE SET resonance=resonance+1''',
                (guild, user, card))
            award_resonance(self.db, self.store, guild, user, award_now)
        return bool(changed.rowcount)

    def mastery(self, guild, user):
        return dict(self.db.execute('''SELECT card,resonance FROM rpg_divination_mastery
            WHERE guild_id=? AND user_id=?''', (guild, user)))

    def affinity_status(self, guild, user, now=None):
        return mag_affinity_status(self.db, self.store, guild, user, now)

    def prepare_for_raid(self, raid_id, guild, users, now=None):
        result = {}
        for user in users:
            status = self.status(guild, user, now)
            card = status['card']
            activation = status['selected_at']
            if card:
                xp_percent = 12 if card == 'world' else 5 if card == 'hierophant' else 0
                if card == 'high_priestess':
                    xp_percent = 15
                result[user] = dict(id=card, name=CARDS[card].name,
                                    activation=activation,
                                    xp_percent=xp_percent)
        return result

    def clear_raid(self, raid_id):
        """Timed fortunes are not consumed when a raid ends."""

    def reserve_summon(self, guild, user):
        now = time.time()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            changed = self.db.execute('''UPDATE rpg_divinations SET summon_raid_id=':reserved:'
                WHERE guild_id=? AND user_id=? AND card='high_priestess' AND expires_at>?
                AND summon_raid_id IS NULL''', (guild, user, now))
            if not changed.rowcount:
                raise CharacterError('目前沒有可使用的「女祭司」，或這張牌已經發起過討伐。')

    def finish_summon(self, guild, user, raid_id):
        status = self.status(guild, user)
        with self.db:
            changed = self.db.execute('''UPDATE rpg_divinations SET summon_raid_id=?
                WHERE guild_id=? AND user_id=? AND card='high_priestess'
                AND summon_raid_id=':reserved:' ''', (raid_id, guild, user))
            if not changed.rowcount:
                raise CharacterError('女祭司的召喚狀態已經改變。')
        self.resonate(guild, user, 'high_priestess', now=status['selected_at'],
                      activation=status['selected_at'], award_now=time.time())

    def release_summon(self, guild, user):
        with self.db:
            self.db.execute('''UPDATE rpg_divinations SET summon_raid_id=NULL
                WHERE guild_id=? AND user_id=? AND summon_raid_id=':reserved:' ''', (guild, user))
