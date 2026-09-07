"""Validate equipment-instance migration against a temporary SQLite backup."""
import argparse
from pathlib import Path
import sqlite3
import tempfile

from core.rpg import RPGStore
from core.rpg_character import Characters, ITEMS
from core.settings import RPGSettings


def is_equipment(item_id):
    item = ITEMS.get(item_id)
    return bool(item and item.slot in ('武器', '套裝', '飾品'))


def validate(path):
    source_path = Path(path).resolve()
    with tempfile.TemporaryDirectory(prefix='ourbot-rpg-migration-') as directory:
        copy_path = Path(directory) / 'rpg.db'
        source = sqlite3.connect(f'file:{source_path.as_posix()}?mode=ro', uri=True)
        target = sqlite3.connect(copy_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        probe = sqlite3.connect(copy_path)
        columns = {row[1] for row in probe.execute('PRAGMA table_info(rpg_equipment)')}
        legacy_units = sum(quantity for item_id, quantity in probe.execute(
            'SELECT item_id,quantity FROM rpg_inventory') if is_equipment(item_id))
        old_loadouts = probe.execute('SELECT COUNT(*) FROM rpg_equipment').fetchone()[0]
        existing_instances = probe.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                                           "AND name='rpg_equipment_instances'").fetchone()[0]
        existing_instances = (probe.execute('SELECT COUNT(*) FROM rpg_equipment_instances').fetchone()[0]
                              if existing_instances else 0)
        probe.close()

        store = RPGStore(copy_path)
        try:
            Characters(store, RPGSettings())
            remaining = sum(quantity for item_id, quantity in store.db.execute(
                'SELECT item_id,quantity FROM rpg_inventory') if is_equipment(item_id))
            instances = store.db.execute('SELECT COUNT(*) FROM rpg_equipment_instances').fetchone()[0]
            loadouts = store.db.execute('SELECT COUNT(*) FROM rpg_equipment').fetchone()[0]
            invalid_loadouts = store.db.execute('''SELECT COUNT(*) FROM rpg_equipment e
                LEFT JOIN rpg_equipment_instances i ON i.instance_id=e.instance_id
                WHERE i.instance_id IS NULL OR i.guild_id<>e.guild_id OR i.user_id<>e.user_id''').fetchone()[0]
            migration = store.db.execute("SELECT 1 FROM rpg_schema_migrations "
                                         "WHERE name='equipment_instances_v1'").fetchone()
            if remaining or invalid_loadouts or not migration:
                raise RuntimeError('遷移後資料完整性檢查失敗。')
            if 'item_id' in columns and loadouts != old_loadouts:
                raise RuntimeError('遷移後穿戴欄數量不一致。')
            if instances < existing_instances + legacy_units:
                raise RuntimeError('遷移後裝備實例數量不足。')
            return dict(legacy_units=legacy_units, instances=instances, loadouts=loadouts)
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', nargs='?', default='data/rpg.db')
    result = validate(parser.parse_args().database)
    print(f'遷移驗證成功：舊裝備 {result["legacy_units"]} 件，'
          f'新實例 {result["instances"]} 件，穿戴欄 {result["loadouts"]} 筆。')


if __name__ == '__main__':
    main()
