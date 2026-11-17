"""采集入口：旧的单响应收据流程与版本化目录摄入并存。

    python -m app.service inspect <packet.json>          # 旧流程（保持不变）
    python -m app.service ingest  <packet.json> [--state state.json] [--archive dir]
    python -m app.service asset   <asset_packet.json> [--state ...]
    python -m app.service scan    [--state ...]                           # 扫描进度
    python -m app.service trace   <sku_id> [--state ...]                  # 身份追溯
    python -m app.service catalog [--state ...] [--publish]               # 对外目录
    python -m app.service reviews [--state ...]                           # 人工队列
"""
import json
import sys
from pathlib import Path

from . import extract
from .catalog import CatalogStore, SEVERITY_MANUAL
from .source import from_packet, fetch

KEY = 'sku_id'
DEFAULT_STATE = 'catalog_state.json'


# ------------------------------------------------------------ 旧流程（兼容）
def inspect(packet):
    response = from_packet(packet)
    from .ledger import receipt
    return receipt(response, extract.extract(response, KEY))


def collect_one(url, source_id):
    response = fetch(url, source_id)
    from .ledger import receipt
    return receipt(response, extract.extract(response, KEY))


# ------------------------------------------------------------ 版本化目录
def ingest_packet(store, packet):
    """按来源声明的字段自动辨认类型并摄入；结构无法辨认则隔离入人工队列。"""
    response = from_packet(packet)
    try:
        parsed = extract.parse(response)
    except (ValueError, json.JSONDecodeError):
        return store.ingest_unknown(response)
    if parsed.kind == extract.KIND_CATEGORY:
        return store.ingest_category(response, parsed.document)
    if parsed.kind == extract.KIND_DETAIL:
        return store.ingest_detail(response, parsed.document)
    if parsed.kind == extract.KIND_PACKAGING:
        return store.ingest_packaging(response, parsed.document)
    return store.ingest_unknown(response)


def ingest_asset_packet(store, packet):
    meta, body = extract.decode_asset_packet(packet)
    return store.ingest_asset_file(
        url=meta['url'], body=body, asset_id=meta['asset_id'])


def open_store(args, *, archive=None):
    state_path = Path(_option(args, '--state', DEFAULT_STATE))
    if state_path.exists():
        return CatalogStore.load(state_path, archive_dir=archive), state_path
    return CatalogStore(archive_dir=archive), state_path


def _option(args, flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    command = args[0]

    if command == 'inspect':
        packet = json.loads(Path(args[1]).read_text(encoding='utf-8'))
        print(json.dumps(inspect(packet), ensure_ascii=False, indent=2))
        return

    archive = _option(args, '--archive')
    store, state_path = open_store(args, archive=archive)

    if command == 'ingest':
        packet = json.loads(Path(args[1]).read_text(encoding='utf-8'))
        result = ingest_packet(store, packet)
    elif command == 'asset':
        packet = json.loads(Path(args[1]).read_text(encoding='utf-8'))
        result = ingest_asset_packet(store, packet)
    elif command == 'scan':
        result = store.scan_status()
    elif command == 'trace':
        result = store.trace_sku(args[1])
    elif command == 'catalog':
        result = store.publish_snapshot() if '--publish' in args \
            else store.public_catalog()
    elif command == 'reviews':
        result = {'open': store.open_reviews(),
                  'manual': store.open_reviews(SEVERITY_MANUAL)}
    elif command == 'resolve':
        result = store.resolve_review(args[1], args[2])
    else:
        raise SystemExit('未知命令：%s\n%s' % (command, __doc__))

    store.save(state_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
