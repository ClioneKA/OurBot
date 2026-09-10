"""Permanent per-player slot upgrades with atomic, escalating purchases."""

EXPANSIONS = {
    'expansion:loadout': ('出戰配置擴充', '討伐之證', (20, 40, 70, 110, 160)),
    'expansion:recipe': ('料理配方擴充', '金幣', (2000, 4000, 7000, 11000, 16000)),
}


class SlotExpansions:
    def __init__(self, store):
        self.db = store.db
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS rpg_slot_expansions (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
                purchased INTEGER NOT NULL CHECK(purchased BETWEEN 0 AND 5),
                PRIMARY KEY(guild_id,user_id,kind))''')

    def status(self, guild, user, kind):
        name, currency, prices = EXPANSIONS[kind]
        row = self.db.execute('''SELECT purchased FROM rpg_slot_expansions
            WHERE guild_id=? AND user_id=? AND kind=?''', (guild, user, kind)).fetchone()
        purchased = row[0] if row else 0
        return dict(name=name, currency=currency, purchased=purchased,
                    limit=len(prices), price=prices[purchased] if purchased < len(prices) else None)

    def capacity(self, guild, user, kind, base=3):
        return base + self.status(guild, user, kind)['purchased']

    def buy(self, guild, user, kind, *, expected_purchased):
        from core.rpg_character import CharacterError

        if kind not in EXPANSIONS:
            raise CharacterError('這項擴充不在商店販售。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            status = self.status(guild, user, kind)
            price = status['price']
            if price is None:
                raise CharacterError('這項擴充已購買 5 次，已達上限。')
            if status['purchased'] != expected_purchased:
                raise CharacterError('擴充價格已變更，請確認新價格後再購買。')
            if kind == 'expansion:loadout':
                paid = self.db.execute('''UPDATE rpg_inventory SET quantity=quantity-?
                    WHERE guild_id=? AND user_id=? AND item_id='proof:raid' AND quantity>=?''',
                    (price, guild, user, price))
            else:
                paid = self.db.execute('''UPDATE rpg_wallets SET gold=gold-?
                    WHERE guild_id=? AND user_id=? AND gold>=?''', (price, guild, user, price))
            if not paid.rowcount:
                raise CharacterError(f'{status["currency"]}不足，需要 {price:,}。')
            self.db.execute('''INSERT INTO rpg_slot_expansions VALUES (?,?,?,1)
                ON CONFLICT(guild_id,user_id,kind) DO UPDATE SET purchased=purchased+1''',
                (guild, user, kind))
        return status
