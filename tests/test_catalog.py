"""版本化台账的行为测试。

合成数据，不接触真实企业系统。覆盖：来源身份关联、改名/下架/换版历史、
未经核实不入对外目录、扫描中断恢复、旧版详情辨认、重复请求幂等、
包装替换与 CDN 延迟、来源冲突的人工队列、端到端追溯。
"""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.catalog import (CatalogStore, SEVERITY_MANUAL, SEVERITY_INFO,
                         REV_STALE_DETAIL, REV_DIGEST_CONFLICT,
                         REV_DETAIL_CONFLICT, REV_PENDING_DIGEST,
                         REV_UNPARSEABLE, REV_NAME_AMBIGUITY,
                         REV_PACKAGING_ORPHAN, REV_ASSET_ID_REUSE)
from app.service import ingest_packet, ingest_asset_packet, inspect
from app.source import from_packet


# ---------------------------------------------------------------- 构造工具
def packet(source_id, url, body_obj, status=200):
    body = json.dumps(body_obj, ensure_ascii=False).encode('utf-8')
    return {'source_id': source_id, 'url': url, 'status': status,
            'headers': [['Content-Type', 'application/json']],
            'body_base64': base64.b64encode(body).decode('ascii')}


def category_packet(cid, ver, items, *, page=1, total_pages=1, final_page=None,
                    source_id='src-cat', url=None):
    return packet(source_id, url or 'https://x.test/cat/%s/%s' % (cid, ver), {
        'category_id': cid, 'catalog_version': ver,
        'page': page, 'total_pages': total_pages,
        **({'final_page': final_page} if final_page is not None else {}),
        'items': items})


def detail_packet(pid, sku_id, edition, name, assets, *, url=None,
                  source_id='src-detail', extra=None):
    body = {'product_id': pid, 'sku_id': sku_id, 'edition': edition,
            'name': name, 'assets': assets}
    if extra:
        body.update(extra)
    return packet(source_id, url or 'https://x.test/detail/%s' % pid, body)


def packaging_packet(assets, *, pid=None, sku_id=None, edition=None,
                     packaging_id='pkg-1', url=None):
    body = {'packaging_id': packaging_id, 'assets': assets}
    if pid is not None:
        body['product_id'] = pid
    if sku_id is not None:
        body['sku_id'] = sku_id
    if edition is not None:
        body['edition'] = edition
    return packet('src-pkg', url or 'https://x.test/pkg/%s' % packaging_id, body)


def asset_decl(aid, url, digest=None):
    row = {'asset_id': aid, 'url': url}
    if digest:
        row['digest'] = digest
    return row


def asset_file_packet(url, content, asset_id=None):
    return {'url': url,
            'asset_id': asset_id,
            'body_base64': base64.b64encode(content).decode('ascii')}


class FixedClock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        self.t += 1
        return '2026-11-18T00:00:%02dZ' % self.t


def make_store(tmpdir=None):
    return CatalogStore(now=FixedClock(), archive_dir=tmpdir)


# ---------------------------------------------------------------- 测试
class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def store(self):
        return make_store(self.tmp.name)

    def test_identity_linked_across_three_sources_and_verified(self):
        """古20：分类入口、详情、包装文件按来源身份贯通后才进对外目录。"""
        store = self.store()
        cat = category_packet('baijiu', '2026-v1',
                              [{'product_id': 'g20', 'detail_url': 'https://x.test/d/g20'}])
        r1 = ingest_packet(store, cat)
        self.assertFalse(r1['duplicate'])
        # 分类已扫完但详情未到：对外目录为空，旧版/缺失详情进人工队列
        self.assertEqual(store.public_catalog()['entry_count'], 0)
        self.assertTrue(any(r['code'] == REV_STALE_DETAIL for r in store.open_reviews()))

        body = b'g20-box-v1'
        digest = hashlib.sha256(body).hexdigest()
        det = detail_packet('g20', 'g20-52-500', '2026-a', '古20',
                            [asset_decl('a-g20-box', 'https://cdn.x.test/g20/box', digest)])
        ingest_packet(store, det)
        # 详情已到但 CDN 文件未同步：仍只是暂存
        self.assertEqual(store.public_catalog()['entry_count'], 0)
        pending = [r for r in store.open_reviews() if r['code'] == REV_PENDING_DIGEST]
        self.assertEqual(pending[0]['severity'], SEVERITY_INFO)

        ingest_asset_packet(
            store, asset_file_packet('https://cdn.x.test/g20/box', body, 'a-g20-box'))
        catalog = store.public_catalog()
        self.assertEqual(catalog['entry_count'], 1)
        entry = catalog['entries'][0]
        self.assertEqual(entry['sku_id'], 'g20-52-500')
        self.assertEqual(entry['assets'][0]['observed_status'], 'verified')
        # 暂存转发布后，旧版详情审核项已自动消解
        self.assertFalse(any(r['code'] == REV_STALE_DETAIL for r in store.open_reviews()))

    def test_repeated_requests_never_create_new_sku(self):
        """同一响应重复提交与重复抓取均幂等，SKU 数不增长。"""
        store = self.store()
        cat = category_packet('c', 'v1',
                              [{'product_id': 'g5', 'detail_url': 'https://x.test/d/g5'}])
        det = detail_packet('g5', 'g5-45-500', '2026-a', '古5', [])
        for _ in range(3):
            ingest_packet(store, cat)
        for _ in range(3):
            ingest_packet(store, det)
        self.assertEqual(len(store.state['skus']), 1)
        self.assertEqual(len(store.state['responses']), 2)
        receipt = ingest_packet(store, det)
        self.assertTrue(receipt['duplicate'])
        self.assertEqual(receipt['effects'], [])

    def test_interrupted_scan_is_resumable_and_stale_details_visible(self):
        """分页扫描中断后能辨认扫到哪页；完成前不产生下架结论。"""
        store = self.store()
        p1 = category_packet('c', 'v2',
                             [{'product_id': 'g8', 'detail_url': 'https://x.test/d/g8'}],
                             page=1, total_pages=2, final_page=False)
        ingest_packet(store, p1)
        status = store.scan_status()[0]
        self.assertFalse(status['completed'])
        self.assertEqual(status['pages_seen'], [1])

        # 中断恢复：重发第 1 页（幂等）再发第 2 页
        ingest_packet(store, p1)
        p2 = category_packet('c', 'v2',
                             [{'product_id': 'g16', 'detail_url': 'https://x.test/d/g16'}],
                             page=2, total_pages=2, final_page=True)
        ingest_packet(store, p2)
        status = store.scan_status()[0]
        self.assertTrue(status['completed'])
        subjects = {r['subject'] for r in store.open_reviews()
                    if r['code'] == REV_STALE_DETAIL}
        self.assertIn('g8@c/v2', subjects)
        self.assertIn('g16@c/v2', subjects)

        # 无分页标记的来源可用显式收口
        store2 = self.store()
        single = category_packet('c2', 'v1',
                                  [{'product_id': 'g5', 'detail_url': 'https://x.test/d/g5'}])
        ingest_packet(store2, single)  # 默认 page==total_pages，自动收口
        self.assertTrue(store2.scan_status()[0]['completed'])

    def test_rename_history_kept_and_name_is_not_identity(self):
        """改名只追加历史；同名跨 SKU 触发提示但不影响身份。"""
        store = self.store()
        ingest_packet(store, category_packet('c', 'v1',
                     [{'product_id': 'g20', 'detail_url': 'https://x.test/d/g20'}]))
        ingest_packet(store, detail_packet('g20', 'g20-52-500', '2026-a', '古20', []))
        ingest_packet(store, detail_packet('g20', 'g20-52-500', '2026-a',
                                           '古20 新版', [],
                                           url='https://x.test/d/g20'))
        sku = store.state['skus']['g20-52-500']
        history = sku['editions']['2026-a']['history']
        self.assertIn({'event': 'renamed',
                       'from': '古20', 'to': '古20 新版'},
                      [{k: h.get(k) for k in ('event', 'from', 'to')} for h in history])
        self.assertEqual([h['name'] for h in sku['name_history']],
                         ['古20', '古20 新版'])

        # 另一个 SKU 同名：名称不可作键，只开 info 提示，不合并身份
        ingest_packet(store, category_packet('c', 'v1',
                     [{'product_id': 'g8', 'detail_url': 'https://x.test/d/g8'}]))
        ingest_packet(store, detail_packet('g8', 'g8-50-500', '2026-b', '古20', []))
        self.assertEqual(len(store.state['skus']), 2)
        note = next(r for r in store.open_reviews() if r['code'] == REV_NAME_AMBIGUITY)
        self.assertEqual(note['severity'], SEVERITY_INFO)

    def test_delisting_preserved_as_history(self):
        """新一版完成扫描后缺席的商品标记下架，历史不删除。"""
        store = self.store()
        ingest_packet(store, category_packet('c', 'v1',
                     [{'product_id': 'g16', 'detail_url': 'https://x.test/d/g16'}]))
        ingest_packet(store, detail_packet('g16', 'g16-52-500', '2025-b', '古16', []))
        self.assertEqual(store.state['products']['g16']['listing']['status'], 'listed')

        ingest_packet(store, category_packet('c', 'v2', []))  # 空页 + final
        product = store.state['products']['g16']
        self.assertEqual(product['listing']['status'], 'delisted')
        self.assertEqual(len(product['memberships']), 1)  # 历史入口仍在
        edition = store.state['skus']['g16-52-500']['editions']['2025-b']
        self.assertEqual(edition['status'], 'delisted')
        self.assertTrue(any(h['event'] == 'delisted' for h in edition['history']))
        self.assertEqual(store.public_catalog()['entry_count'], 0)

        # 再挂出：relist，状态恢复
        ingest_packet(store, category_packet('c', 'v3',
                     [{'product_id': 'g16', 'detail_url': 'https://x.test/d/g16'}]))
        self.assertEqual(store.state['products']['g16']['listing']['status'], 'listed')

    def test_packaging_replacement_history_and_digest_delay(self):
        """包装版次替换留痕；CDN 延迟期间旧目录不被替换。"""
        store = self.store()
        ingest_packet(store, category_packet('c', 'v1',
                     [{'product_id': 'g20', 'detail_url': 'https://x.test/d/g20'}]))
        old_body = b'box-old'
        new_body = b'box-new'
        old_digest = hashlib.sha256(old_body).hexdigest()
        new_digest = hashlib.sha256(new_body).hexdigest()

        # 第一版包装核实通过、进入对外目录
        ingest_packet(store, detail_packet(
            'g20', 'g20-52-500', '2026-a', '古20',
            [asset_decl('box', 'https://cdn.x.test/box', old_digest)]))
        ingest_asset_packet(store, asset_file_packet(
            'https://cdn.x.test/box', old_body, 'box'))
        self.assertEqual(store.public_catalog()['entry_count'], 1)

        # 包装替换：新清单先声明新摘要，CDN 还在同步旧文件
        ingest_packet(store, packaging_packet(
            [asset_decl('box', 'https://cdn.x.test/box', new_digest)],
            pid='g20', sku_id='g20-52-500', edition='2026-a', packaging_id='pkg-2'))
        asset = store.state['assets']['box']
        self.assertEqual(asset['declared_digest'], new_digest)
        self.assertTrue(any(h['event'] == 'packaging_attached'
                            for h in store.state['skus']['g20-52-500']
                            ['editions']['2026-a']['history']))
        # 新文件未核实：版本撤回对外目录，未经核实资料不替换旧条目
        self.assertEqual(store.public_catalog()['entry_count'], 0)

        # CDN 同步完成，摘要相符，重新发布
        result = ingest_asset_packet(store, asset_file_packet(
            'https://cdn.x.test/box', new_body, 'box'))
        self.assertTrue(result['matched'])
        self.assertEqual(store.public_catalog()['entry_count'], 1)
        self.assertFalse(any(r['code'] == REV_PENDING_DIGEST
                             for r in store.open_reviews()))

    def test_digest_conflict_requires_manual_confirmation(self):
        """文件摘要与声明不符：人工项，版本保持暂存。"""
        store = self.store()
        ingest_packet(store, category_packet('c', 'v1',
                     [{'product_id': 'g5', 'detail_url': 'https://x.test/d/g5'}]))
        digest = hashlib.sha256(b'declared').hexdigest()
        ingest_packet(store, detail_packet(
            'g5', 'g5-45-500', '2026-a', '古5',
            [asset_decl('box', 'https://cdn.x.test/box', digest)]))
        ingest_asset_packet(store, asset_file_packet(
            'https://cdn.x.test/box', b'different-content', 'box'))
        conflict = next(r for r in store.open_reviews()
                        if r['code'] == REV_DIGEST_CONFLICT)
        self.assertEqual(conflict['severity'], SEVERITY_MANUAL)
        self.assertEqual(store.public_catalog()['entry_count'], 0)
        # 人工处理后重算（仍冲突则维持暂存；这里演示关闭审核项的流程）
        store.resolve_review(conflict['review_id'], '来源侧文件有误，等待重发')
        self.assertEqual(store.open_reviews(SEVERITY_MANUAL), [])

    def test_sku_identity_conflict_quarantined(self):
        """同一 sku_id 被声明给不同 product_id：不静默合并，进人工。"""
        store = self.store()
        ingest_packet(store, detail_packet('g5', 'shared-sku', 'e1', '古5', []))
        ingest_packet(store, detail_packet('g8', 'shared-sku', 'e1', '古8', []))
        conflict = next(r for r in store.open_reviews()
                        if r['code'] == REV_DETAIL_CONFLICT)
        self.assertEqual(conflict['severity'], SEVERITY_MANUAL)
        # 两个 product 各自留存，SKU 不新增
        self.assertEqual(len(store.state['skus']), 1)
        self.assertEqual(set(store.state['products']), {'g5', 'g8'})

    def test_packaging_without_detail_is_orphan_until_identity_arrives(self):
        """包装资料先到且无详情：身份只能人工/后续证据确认。"""
        store = self.store()
        ingest_packet(store, packaging_packet(
            [asset_decl('box', 'https://cdn.x.test/box')],
            sku_id='g20-52-500', edition='2026-a'))
        orphan = next(r for r in store.open_reviews()
                      if r['code'] == REV_PACKAGING_ORPHAN)
        self.assertEqual(orphan['severity'], SEVERITY_MANUAL)
        self.assertTrue(store.state['skus']['g20-52-500']['unconfirmed'])
        trace = store.trace_sku('g20-52-500')
        self.assertTrue(trace['identity_unconfirmed'])
        self.assertTrue(trace['manual_confirmation_required'])

        # 详情到达补齐身份，孤儿项自动消解
        ingest_packet(store, category_packet('c', 'v1',
                     [{'product_id': 'g20', 'detail_url': 'https://x.test/d/g20'}]))
        ingest_packet(store, detail_packet('g20', 'g20-52-500', '2026-a', '古20', []))
        self.assertFalse(any(r['code'] == REV_PACKAGING_ORPHAN
                             for r in store.open_reviews()))

    def test_asset_id_reuse_is_manual_conflict(self):
        """asset_id 被复用到不同 URL：身份冲突留痕并人工确认。"""
        store = self.store()
        ingest_packet(store, detail_packet(
            'g5', 'g5-45-500', 'e1', '古5',
            [asset_decl('shared-asset', 'https://cdn.x.test/a')]))
        ingest_packet(store, detail_packet(
            'g8', 'g8-50-500', 'e1', '古8',
            [asset_decl('shared-asset', 'https://cdn.x.test/b')]))
        review = next(r for r in store.open_reviews()
                      if r['code'] == REV_ASSET_ID_REUSE)
        self.assertEqual(review['severity'], SEVERITY_MANUAL)
        self.assertTrue(any(h['event'] == 'url_changed'
                            for h in store.state['assets']['shared-asset']['history']))

    def test_unparseable_response_quarantined_with_body_kept(self):
        """来源改版导致结构无法辨认：原件留存，不中断整轮采集。"""
        store = self.store()
        bad = packet('src', 'https://x.test/mystery', {'unexpected': {'nested': 1}})
        receipt = ingest_packet(store, bad)
        self.assertEqual(receipt['kind'], 'unknown')
        review = next(r for r in store.open_reviews()
                      if r['code'] == REV_UNPARSEABLE)
        self.assertEqual(review['response_sha256'], receipt['response_sha256'])
        # 后续正常响应仍可摄入
        ingest_packet(store, detail_packet('g5', 'g5-45-500', 'e1', '古5', []))
        self.assertIn('g5-45-500', store.state['skus'])

    def test_trace_walks_back_to_category_detail_and_packaging(self):
        """从产品版本追到分类入口、详情原文和包装文件。"""
        store = self.store()
        cat = category_packet('baijiu', '2026-v1',
                              [{'product_id': 'g20', 'detail_url': 'https://x.test/d/g20'}])
        body = b'box'
        digest = hashlib.sha256(body).hexdigest()
        det = detail_packet('g20', 'g20-52-500', '2026-a', '古20',
                            [asset_decl('box', 'https://cdn.x.test/box', digest)])
        pkg = packaging_packet([asset_decl('box', 'https://cdn.x.test/box', digest)],
                               pid='g20', sku_id='g20-52-500', edition='2026-a')
        for p in (cat, det, pkg):
            ingest_packet(store, p)
        ingest_asset_packet(store, asset_file_packet(
            'https://cdn.x.test/box', body, 'box'))

        trace = store.trace_sku('g20-52-500')
        self.assertEqual(trace['product_id'], 'g20')
        entry = trace['category_entries'][0]
        self.assertEqual(entry['category_id'], 'baijiu')
        self.assertTrue(entry['scan_completed'])
        self.assertTrue(Path(entry['archive_path']).exists())  # 分类原件已归档
        edition = trace['editions']['2026-a']
        self.assertEqual(len(edition['detail_sources']), 1)
        self.assertEqual(len(edition['packaging_sources']), 1)
        self.assertEqual(edition['assets'][0]['observed_status'], 'verified')
        self.assertTrue(Path(edition['assets'][0]['archive_path']).exists())
        self.assertEqual(
            edition['assets'][0]['actual_digest'],
            hashlib.sha256(body).hexdigest())
        self.assertEqual(trace['manual_confirmation_required'], [])
        self.assertTrue(trace['in_public_catalog'])

    def test_detail_leading_category_and_stale_after_version_bump(self):
        """详情先于分类到达可挂账；分类换代后未刷新的详情被辨认为旧版。"""
        store = self.store()
        # 详情先到
        ingest_packet(store, detail_packet(
            'g8', 'g8-50-500', 'e1', '古8', [], url='https://x.test/d/g8'))
        pending_cat = next(r for r in store.open_reviews()
                           if r['code'] == 'product_pending_category')
        self.assertEqual(pending_cat['severity'], SEVERITY_INFO)

        # 分类 v1 到达，引用同一 URL，stale 消解
        ingest_packet(store, category_packet('c', 'v1',
                     [{'product_id': 'g8', 'detail_url': 'https://x.test/d/g8'}]))
        self.assertFalse(any(r['code'] == 'product_pending_category'
                             for r in store.open_reviews()))
        self.assertFalse(any(r['code'] == REV_STALE_DETAIL for r in store.open_reviews()))

        # 分类升级 v2，详情未在 v2 上下文重抓：详情属旧版
        ingest_packet(store, category_packet('c', 'v2',
                     [{'product_id': 'g8', 'detail_url': 'https://x.test/d/g8'}]))
        stale = next(r for r in store.open_reviews() if r['code'] == REV_STALE_DETAIL)
        self.assertEqual(stale['subject'], 'g8@c/v2')

        # 在 v2 上下文重抓详情后消解
        ingest_packet(store, detail_packet(
            'g8', 'g8-50-500', 'e1', '古8', [], url='https://x.test/d/g8'))
        self.assertFalse(any(r['code'] == REV_STALE_DETAIL for r in store.open_reviews()))

    def test_state_persistence_roundtrip_and_snapshot(self):
        """台账可落盘恢复，发布快照只含已核实条目。"""
        path = Path(self.tmp.name) / 'state.json'
        store = self.store()
        ingest_packet(store, detail_packet('g5', 'g5-45-500', 'e1', '古5', []))
        store.save(path)

        restored = CatalogStore.load(path, now=FixedClock())
        self.assertEqual(restored.state['skus'].keys(), store.state['skus'].keys())
        snapshot = restored.publish_snapshot()
        self.assertEqual(snapshot['snapshot_id'], 'cat-1')
        self.assertEqual(snapshot['entry_count'], 0)  # 无分类入口，未核实

    def test_stale_reviews_scoped_per_category_version(self):
        """一个分类版本的详情重抓不会消解另一个分类版本的旧版详情审核项。"""
        store = self.store()
        ingest_packet(store, category_packet('c1', 'v1',
                     [{'product_id': 'g5', 'detail_url': 'https://x.test/d/g5'}]))
        ingest_packet(store, category_packet('c2', 'v1',
                     [{'product_id': 'g5', 'detail_url': 'https://x.test/d/g5'}]))
        subjects = {r['subject'] for r in store.open_reviews()
                    if r['code'] == REV_STALE_DETAIL}
        self.assertEqual(subjects, {'g5@c1/v1', 'g5@c2/v1'})
        # 详情到达：只消解该商品在架版本，两个分类的审核项按精确 subject 各自消解
        ingest_packet(store, detail_packet('g5', 'g5-45-500', 'e1', '古5', [],
                                           url='https://x.test/d/g5'))
        self.assertFalse(any(r['code'] == REV_STALE_DETAIL
                             for r in store.open_reviews()))

    def test_digest_recovers_after_bad_cdn_file(self):
        """CDN 先同步了错误文件（冲突），正确文件到达后自动恢复核实。"""
        store = self.store()
        ingest_packet(store, category_packet('c', 'v1',
                     [{'product_id': 'g5', 'detail_url': 'https://x.test/d/g5'}]))
        good = b'correct-box'
        digest = hashlib.sha256(good).hexdigest()
        ingest_packet(store, detail_packet(
            'g5', 'g5-45-500', 'e1', '古5',
            [asset_decl('box', 'https://cdn.x.test/box', digest)]))
        first = ingest_asset_packet(store, asset_file_packet(
            'https://cdn.x.test/box', b'wrong-cdn-content', 'box'))
        self.assertFalse(first['matched'])
        self.assertEqual(store.public_catalog()['entry_count'], 0)
        # 正确文件到达（无需人工关闭冲突项即可重新核实）
        second = ingest_asset_packet(store, asset_file_packet(
            'https://cdn.x.test/box', good, 'box'))
        self.assertTrue(second['matched'])
        self.assertEqual(store.state['assets']['box']['observed_status'], 'verified')
        conflict = next(r for r in store.state['reviews']
                        if r['code'] == REV_DIGEST_CONFLICT)
        self.assertEqual(conflict['status'], 'resolved')
        self.assertEqual(store.public_catalog()['entry_count'], 1)

    def test_orphan_download_quarantined(self):
        """没有任何来源声明引用的下载文件进入人工队列，不建立 SKU 或资产身份。"""
        store = self.store()
        result = ingest_asset_packet(store, asset_file_packet(
            'https://cdn.x.test/nobody', b'lonely'))
        self.assertFalse(result['matched'])
        review = next(r for r in store.open_reviews()
                      if r['code'] == 'asset_orphan')
        self.assertEqual(review['severity'], SEVERITY_MANUAL)
        self.assertEqual(store.state['skus'], {})

    def test_legacy_flow_unchanged(self):
        """旧的单响应 inspect 流程与收据格式保持兼容。"""
        sample = json.loads(
            (Path(__file__).resolve().parents[1] / 'fixtures' / 'sample.json')
            .read_text(encoding='utf-8'))
        result = inspect(sample)
        self.assertEqual(result['source_id'], 'source-a')
        self.assertEqual(result['items'][0]['key'], 'g20-52-500')
        self.assertEqual(len(result['response_sha256']), 64)
        # Response 校验仍生效
        with self.assertRaises(ValueError):
            from_packet({**sample, 'body_base64': '?'})


if __name__ == '__main__':
    unittest.main()
