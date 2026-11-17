"""版本化商品台账：把分类页、详情页、包装资料按来源声明的身份关联起来。

设计原则：
- 身份只认来源显式声明的 product_id / sku_id / asset_id，名称只作历史属性；
- 新证据先进暂存（staged），跨来源核实通过才进入对外目录（verified）；
- 改名、下架、包装替换全部以追加事件留痕，不覆盖历史；
- 响应按原件 sha256 去重，SKU 以 sku_id 为唯一主键，重复请求不产生新 SKU；
- 来源矛盾进人工队列，CDN 延迟等可自动恢复的问题单独标记。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# 审核项级别：manual 只能人工确认；info 可在后续证据到达时自动消解。
SEVERITY_MANUAL = 'manual'
SEVERITY_INFO = 'info'

# 审核项代码
REV_UNPARSEABLE = 'source_unparseable'        # 来源改版/结构无法辨认，人工
REV_DETAIL_CONFLICT = 'product_detail_conflict'  # product_id 与 sku_id 既有归属冲突
REV_DIGEST_CONFLICT = 'digest_conflict'       # 包装文件摘要与声明不符，人工
REV_URL_CONFLICT = 'detail_url_conflict'      # 同一详情 URL 被声明给不同商品
REV_ASSET_ID_REUSE = 'asset_identity_conflict'  # asset_id 被复用到不同 URL
REV_PACKAGING_ORPHAN = 'packaging_orphan'     # 包装资料指向台账中不存在的身份
REV_ASSET_ORPHAN = 'asset_orphan'             # 下载的文件没有任何来源声明引用
REV_STALE_DETAIL = 'stale_detail'             # 详情仍属旧分类版本
REV_PENDING_DIGEST = 'pending_digest'         # 摘要延后/文件未同步，可自动恢复
REV_NAME_AMBIGUITY = 'name_ambiguity'         # 同名覆盖多个 SKU，提示名称不可作键
REV_PRODUCT_NO_DETAIL = 'product_without_detail'
REV_PRODUCT_NO_CATEGORY = 'product_pending_category'


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _version_older(a, b):
    """版本先后比较：按点分数字段逐段比，非数字段退化为字符串比较。"""
    def parts(v):
        out = []
        for segment in str(v).split('.'):
            out.append((0, int(segment)) if segment.isdigit() else (1, segment))
        return out
    pa, pb = parts(a), parts(b)
    return pa < pb


class CatalogStore:
    def __init__(self, state=None, *, now=None, archive_dir=None):
        self.now = now or _utcnow
        self.archive_dir = Path(archive_dir) if archive_dir else None
        self.state = state or self.empty_state()
        if self.state.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('台账版本不受支持')

    # ------------------------------------------------------------ 持久化
    @staticmethod
    def empty_state():
        return {
            'schema_version': SCHEMA_VERSION,
            'responses': {},       # sha256 -> 响应元数据
            'categories': {},      # category_id -> 版本与扫描进度
            'products': {},        # product_id -> 商品身份与历史
            'skus': {},            # sku_id -> SKU 与版本（主键，永不因重复请求新增）
            'assets': {},          # asset_id -> 包装文件声明与核实状态
            'name_index': {},      # name -> {sku_id: 最近见到时间}
            'reviews': [],         # 人工/自动审核队列
            'catalog_snapshots': [],
        }

    def save(self, path):
        Path(path).write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')

    @classmethod
    def load(cls, path, **kwargs):
        state = json.loads(Path(path).read_text(encoding='utf-8'))
        return cls(state, **kwargs)

    # ------------------------------------------------------------ 基础工具
    def _archive(self, response):
        if not self.archive_dir:
            return None
        digest = response.sha256
        target = self.archive_dir / digest[:2] / digest
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.body)
        return str(target)

    def _record_response(self, response, kind):
        """登记响应原件；返回 (sha, duplicate)。"""
        digest = response.sha256
        seen = digest in self.state['responses']
        if not seen:
            self.state['responses'][digest] = {
                'source_id': response.source_id,
                'url': response.url,
                'status': response.status,
                'kind': kind,
                'observed_at': self.now(),
                'body_sha256': digest,
                'archive_path': self._archive(response),
            }
        return digest, seen

    def _open_review(self, severity, code, subject, summary, *, response_sha=None, data=None):
        """打开审核项；同 (code, subject) 已存在未解决项时不重复开单。"""
        for review in self.state['reviews']:
            if review['status'] == 'open' and review['code'] == code \
                    and review['subject'] == subject:
                return review
        review = {
            'review_id': 'rv-%d' % (len(self.state['reviews']) + 1),
            'severity': severity,
            'code': code,
            'subject': subject,
            'summary': summary,
            'response_sha256': response_sha,
            'data': data or {},
            'opened_at': self.now(),
            'status': 'open',
            'resolved_at': None,
            'resolution': None,
        }
        self.state['reviews'].append(review)
        return review

    def _resolve(self, code, subject, resolution='auto'):
        for review in self.state['reviews']:
            if review['status'] == 'open' and review['code'] == code \
                    and review['subject'] == subject:
                review['status'] = 'resolved'
                review['resolved_at'] = self.now()
                review['resolution'] = resolution

    def open_reviews(self, severity=None):
        out = [r for r in self.state['reviews'] if r['status'] == 'open']
        if severity:
            out = [r for r in out if r['severity'] == severity]
        return out

    def resolve_review(self, review_id, resolution, *, actor='operator'):
        for review in self.state['reviews']:
            if review['review_id'] == review_id:
                if review['status'] != 'open':
                    raise ValueError('审核项已关闭')
                review['status'] = 'resolved'
                review['resolved_at'] = self.now()
                review['resolution'] = '%s: %s' % (actor, resolution)
                self._recompute()
                return review
        raise ValueError('审核项不存在')

    # ------------------------------------------------------------ 分类页
    def ingest_category(self, response, page):
        sha, duplicate = self._record_response(response, 'category')
        effects = []
        if duplicate:
            return self._receipt(sha, 'category', True, effects)

        cid = page.category_id
        ver = page.catalog_version
        category = self.state['categories'].setdefault(cid, {
            'category_id': cid, 'versions': {}, 'latest_version': None})
        version = category['versions'].setdefault(ver, {
            'catalog_version': ver, 'pages': [], 'final_page_seen': False,
            'completed_at': None, 'items': {}, 'response_sha256': []})
        version['response_sha256'].append(sha)
        if page.page not in version['pages']:
            version['pages'].append(page.page)
        if page.final_page:
            version['final_page_seen'] = True

        for entry in page.items:
            pid = entry['product_id']
            url = entry['detail_url']
            existing = version['items'].get(pid)
            if existing and existing['detail_url'] != url:
                self._open_review(
                    SEVERITY_MANUAL, REV_URL_CONFLICT, '%s@%s/%s' % (pid, cid, ver),
                    '分类页对同一商品声明了不同详情 URL：%s / %s' % (existing['detail_url'], url),
                    response_sha=sha)
            # 同一 URL 指向不同商品：来源身份冲突，必须人工
            for other_pid, other in version['items'].items():
                if other_pid != pid and other['detail_url'] == url:
                    self._open_review(
                        SEVERITY_MANUAL, REV_URL_CONFLICT, '%s->%s' % (url, pid),
                        '详情 URL %s 同时被声明给 %s 与 %s' % (url, other_pid, pid),
                        response_sha=sha)
            version['items'].setdefault(pid, {
                'product_id': pid, 'detail_url': url,
                'first_response': sha, 'observed_at': self.now()})
            effects.append(('category_entry_declared', cid, ver, pid, url))

            product = self._product(pid)
            if product['listing']['status'] == 'unknown':
                product['listing'] = {'status': 'listed', 'reason': 'seen_in_category',
                                      'at': self.now(), 'category_id': cid}
            product['memberships'].append({
                'category_id': cid, 'catalog_version': ver,
                'detail_url': url, 'response_sha256': sha, 'at': self.now()})
            self._resolve(REV_PRODUCT_NO_CATEGORY, pid)
            # 仅在商品首次进入分类体系时补登先到的详情原件；
            # 分类换代不做回填——旧版详情由完成扫描的新鲜度检查辨认
            if not any(m['catalog_version'] != ver
                       for m in product['memberships'][:-1]):
                self._backfill_detail_for_version(product, cid, ver)
            # 新分类版本重新挂出商品：解除此前的下架状态
            if product['listing']['status'] == 'delisted' \
                    and product['listing'].get('category_id') == cid:
                product['listing'] = {'status': 'listed', 'reason': 'relisted',
                                      'at': self.now(), 'category_id': cid}
                effects.append(('product_relisted', pid, cid, ver))

        category['latest_version'] = ver
        if version['final_page_seen'] and version['completed_at'] is None:
            self._complete_scan(cid, ver, sha, effects)
        self._recompute()
        return self._receipt(sha, 'category', False, effects)

    def complete_scan(self, category_id, catalog_version):
        """驱动程序确认分类已扫完（无分页标记时的显式收口）。"""
        try:
            version = self.state['categories'][category_id]['versions'][catalog_version]
        except KeyError:
            raise ValueError('分类版本不存在：%s %s' % (category_id, catalog_version))
        effects = []
        if version['completed_at'] is None:
            version['final_page_seen'] = True
            self._complete_scan(category_id, catalog_version, None, effects)
            self._recompute()
        return {'category_id': category_id, 'catalog_version': catalog_version,
                'completed_at': version['completed_at'], 'effects': effects}

    def _complete_scan(self, cid, ver, sha, effects):
        category = self.state['categories'][cid]
        version = category['versions'][ver]
        version['completed_at'] = self.now()
        effects.append(('scan_completed', cid, ver, sorted(version['pages'])))

        new_items = version['items']
        # 上一版在收的商品，本版缺席 => 下架（历史保留）
        previous = self._previous_version(cid, ver)
        if previous is not None:
            for pid in previous['items']:
                if pid not in new_items:
                    product = self._product(pid)
                    product['listing'] = {
                        'status': 'delisted', 'reason': 'absent_from_completed_scan',
                        'at': self.now(), 'category_id': cid,
                        'catalog_version': ver, 'scan_response': sha}
                    for sku_id in product.get('sku_ids', []):
                        sku = self.state['skus'].get(sku_id)
                        if not sku or sku['product_id'] != pid:
                            continue
                        for edition in sku['editions'].values():
                            if edition['status'] != 'delisted':
                                edition['status'] = 'delisted'
                                edition['delisted_at'] = self.now()
                                edition['history'].append({
                                    'event': 'delisted', 'at': self.now(),
                                    'response_sha256': sha,
                                    'detail': '分类 %s 版本 %s 完成扫描后缺席' % (cid, ver)})
                    effects.append(('product_delisted', pid, cid, ver))

        # 详情新鲜度：本版挂出的商品若没有在本版上下文下抓到的详情 => 旧版详情
        for pid, entry in new_items.items():
            product = self._product(pid)
            url = entry['detail_url']
            contexts = [
                ctx for seen in product['detail_seen'].values()
                for ctx in seen['contexts']
                if ctx['category_id'] == cid and ctx['catalog_version'] == ver]
            subject = '%s@%s/%s' % (pid, cid, ver)
            if contexts:
                self._resolve(REV_STALE_DETAIL, subject)
            else:
                self._open_review(
                    SEVERITY_MANUAL, REV_STALE_DETAIL, subject,
                    '商品 %s 在分类 %s 版本 %s 下尚无对应详情，现有详情属旧版或缺失' % (pid, cid, ver),
                    response_sha=sha)
            if not product.get('sku_ids'):
                self._open_review(
                    SEVERITY_INFO, REV_PRODUCT_NO_DETAIL, pid,
                    '分类已挂出商品 %s，但尚未收到详情页' % pid, response_sha=sha)

    def _previous_version(self, cid, current_ver):
        """取严格早于当前版本的最新一个分类版本。"""
        previous = None
        for ver, version in self.state['categories'][cid]['versions'].items():
            if ver == current_ver or not _version_older(ver, current_ver):
                continue
            if previous is None or _version_older(previous[0], ver):
                previous = (ver, version)
        return previous[1] if previous else None

    def scan_status(self):
        """报告哪些分类已扫完、哪些仍在进行、哪些详情属旧版。"""
        report = []
        for cid, category in sorted(self.state['categories'].items()):
            for ver, version in category['versions'].items():
                stale = [r['subject'] for r in self.open_reviews()
                         if r['code'] == REV_STALE_DETAIL
                         and r['subject'].endswith('@%s/%s' % (cid, ver))]
                report.append({
                    'category_id': cid,
                    'catalog_version': ver,
                    'pages_seen': sorted(version['pages']),
                    'final_page_seen': version['final_page_seen'],
                    'completed': version['completed_at'] is not None,
                    'completed_at': version['completed_at'],
                    'items_declared': len(version['items']),
                    'stale_detail_subjects': stale,
                })
        return report

    # ------------------------------------------------------------ 详情页
    def ingest_detail(self, response, detail):
        sha, duplicate = self._record_response(response, 'detail')
        effects = []
        if duplicate:
            # 原件重复不产生新条目；但同一详情在新分类版本下重抓可刷新上下文，
            # 让该分类版本的 stale_detail 审核项得以消解
            sku = self.state['skus'].get(detail.sku_id)
            if sku:
                product = self.state['products'].get(sku['product_id'])
                if product and sku['product_id'] == detail.product_id:
                    self._register_detail_context(product, response.url, sha)
            return self._receipt(sha, 'detail', True, effects)

        pid = detail.product_id
        sku_id = detail.sku_id
        product = self._product(pid)

        # 身份一致性：sku_id 已归属其他商品时不得静默合并
        existing_sku = self.state['skus'].get(sku_id)
        if existing_sku and existing_sku['product_id'] not in (pid, 'UNCONFIRMED'):
            self._open_review(
                SEVERITY_MANUAL, REV_DETAIL_CONFLICT, sku_id,
                'SKU %s 原属商品 %s，新响应声明为 %s' % (
                    sku_id, existing_sku['product_id'], pid),
                response_sha=sha, data={'existing_product': existing_sku['product_id'],
                                        'claimed_product': pid})
        product.setdefault('sku_ids', [])
        if sku_id not in product['sku_ids']:
            product['sku_ids'].append(sku_id)

        sku = existing_sku or self._new_sku(sku_id, pid)
        if sku.get('unconfirmed'):
            # 详情页补齐了包装资料无法单独确立的身份
            sku['product_id'] = pid
            sku['unconfirmed'] = False
            self._resolve(REV_PACKAGING_ORPHAN, sku_id)
        edition = sku['editions'].get(detail.edition)
        if edition is None:
            edition = self._new_edition(detail)
            sku['editions'][detail.edition] = edition
            effects.append(('edition_declared', sku_id, detail.edition))
        else:
            if detail.name and edition['name'] and detail.name != edition['name']:
                edition['history'].append({
                    'event': 'renamed', 'at': self.now(),
                    'response_sha256': sha,
                    'from': edition['name'], 'to': detail.name})
                effects.append(('edition_renamed', sku_id, detail.edition,
                                edition['name'], detail.name))
            if detail.name:
                edition['name'] = detail.name
            edition['attributes'].update(detail.attributes)
            edition['observed_at'] = self.now()

        edition['detail_responses'].append(sha)
        if detail.name:
            self._note_name(detail.name, sku_id, sha)
        if not product['memberships']:
            self._open_review(
                SEVERITY_INFO, REV_PRODUCT_NO_CATEGORY, pid,
                '详情先于分类页到达：商品 %s 尚无分类入口' % pid, response_sha=sha)
        self._resolve(REV_PRODUCT_NO_DETAIL, pid)

        # 记录详情采集时所处的分类版本上下文（用于旧版详情辨认）
        self._register_detail_context(product, response.url, sha)

        for asset in detail.assets:
            self._upsert_asset(asset, sku_id, detail.edition, sha, effects,
                               via='detail')
        self._recompute()
        return self._receipt(sha, 'detail', False, effects)

    # ------------------------------------------------------------ 包装资料
    def ingest_packaging(self, response, manifest):
        sha, duplicate = self._record_response(response, 'packaging')
        effects = []
        if duplicate:
            return self._receipt(sha, 'packaging', True, effects)

        sku_id = manifest.sku_id
        pid = manifest.product_id
        edition_id = manifest.edition

        sku = self.state['skus'].get(sku_id) if sku_id else None
        if sku is None:
            # 包装资料不能单独确立商品身份：挂人工，数据留存不丢弃
            subject = sku_id or response.url
            self._open_review(
                SEVERITY_MANUAL, REV_PACKAGING_ORPHAN, subject,
                '包装资料引用的 SKU %s 在台账中不存在，身份只能人工确认' % sku_id,
                response_sha=sha, data={'product_id': pid, 'edition': edition_id})
            if sku_id:
                sku = self._new_sku(sku_id, pid or 'UNCONFIRMED')
                sku['unconfirmed'] = True
            else:
                for asset in manifest.assets:
                    self._upsert_asset(asset, None, edition_id, sha, effects,
                                       via='packaging')
                    self._open_review(
                        SEVERITY_MANUAL, REV_PACKAGING_ORPHAN, asset['asset_id'],
                        '包装清单未声明 sku_id，资产 %s 无法归属' % asset['asset_id'],
                        response_sha=sha)
                self._recompute()
                return self._receipt(sha, 'packaging', False, effects)

        if pid and sku['product_id'] not in (pid, 'UNCONFIRMED'):
            self._open_review(
                SEVERITY_MANUAL, REV_DETAIL_CONFLICT, sku_id,
                '包装清单把 SKU %s 声明给商品 %s，台账归属为 %s' % (
                    sku_id, pid, sku['product_id']), response_sha=sha)
        if sku.get('unconfirmed') and pid:
            sku['product_id'] = pid
            sku['unconfirmed'] = False
            self._resolve(REV_PACKAGING_ORPHAN, sku_id)

        target_edition = edition_id
        if target_edition not in sku['editions']:
            # 先建占位版本，保持 staged，等详情身份补齐
            sku['editions'][target_edition or 'UNCONFIRMED'] = self._new_edition(
                manifest, placeholder=not edition_id)
            target_edition = target_edition or 'UNCONFIRMED'
        edition = sku['editions'][target_edition]
        edition['packaging_responses'].append(sha)
        edition['history'].append({
            'event': 'packaging_attached', 'at': self.now(),
            'response_sha256': sha,
            'asset_ids': [a['asset_id'] for a in manifest.assets]})
        effects.append(('packaging_attached', sku_id, target_edition))

        for asset in manifest.assets:
            self._upsert_asset(asset, sku_id, target_edition, sha, effects,
                               via='packaging')
        self._recompute()
        return self._receipt(sha, 'packaging', False, effects)

    # ------------------------------------------------------------ 包装文件原件
    def ingest_asset_file(self, *, url, body, asset_id=None, source_response_sha=None):
        """登记下载到的包装文件原件，按 URL/asset_id 与来源声明核对摘要。"""
        from .extract import digest_bytes
        actual = digest_bytes(body)
        at = self.now()
        linked = None
        if asset_id:
            linked = self.state['assets'].get(asset_id)
        if linked is None:
            for aid, asset in self.state['assets'].items():
                if asset['url'] == url:
                    linked = asset
                    asset_id = aid
                    break
        if linked is None:
            review_id = self._open_review(
                SEVERITY_MANUAL, REV_ASSET_ORPHAN, asset_id or url,
                '下载的文件没有任何分类/详情/包装来源声明引用',
                response_sha=source_response_sha,
                data={'url': url, 'actual_digest': actual})
            return {'url': url, 'actual_digest': actual, 'matched': False,
                    'review_id': review_id['review_id']}

        linked['actual_digest'] = actual
        linked['observed_at'] = at
        declared = linked.get('declared_digest')
        if not declared:
            # 来源尚未给摘要（CDN/清单延迟）：保持待核实，可自动恢复
            linked['observed_status'] = 'pending'
            self._open_review(
                SEVERITY_INFO, REV_PENDING_DIGEST, linked['asset_id'],
                '文件已下载但来源尚未提供声明摘要', data={'url': url})
        elif declared == actual:
            linked['observed_status'] = 'verified'
            # CDN 重新同步出正确文件时，此前的冲突自动消解
            self._resolve(REV_PENDING_DIGEST, linked['asset_id'])
            self._resolve(REV_DIGEST_CONFLICT, linked['asset_id'])
        else:
            linked['observed_status'] = 'conflict'
            self._open_review(
                SEVERITY_MANUAL, REV_DIGEST_CONFLICT, linked['asset_id'],
                '文件摘要与来源声明不符：声明 %s，实测 %s' % (declared, actual),
                response_sha=source_response_sha,
                data={'declared': declared, 'actual': actual})
        if self.archive_dir:
            target = self.archive_dir / 'assets' / actual
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            linked['archive_path'] = str(target)
        self._recompute()
        return {'url': url, 'asset_id': linked['asset_id'],
                'actual_digest': actual, 'matched': declared == actual,
                'observed_status': linked['observed_status'],
                'archive_path': linked.get('archive_path')}

    # ------------------------------------------------------------ 无法辨认的响应
    def ingest_unknown(self, response):
        sha, _ = self._record_response(response, 'unknown')
        review = self._open_review(
            SEVERITY_MANUAL, REV_UNPARSEABLE, response.url,
            '来源响应结构无法辨认（疑似改版），原件已留存待人工核对',
            response_sha=sha)
        return self._receipt(sha, 'unknown', False,
                             [('unparseable_response_quarantined', response.url)],
                             review_id=review['review_id'])

    # ------------------------------------------------------------ 内部建模
    def _register_detail_context(self, product, url, sha):
        """把一份详情原件登记到该商品当前最新的分类版本上下文。

        详情响应的实际 URL 可能与分类声明的 detail_url 存在 CDN/域名差异，
        因此按商品身份关联；历史版本是否新鲜已在各自完成扫描时判定，
        新抓取的详情只能为“当前版本”提供新鲜证据。
        """
        seen = product['detail_seen'].setdefault(url, {'contexts': []})
        seen.setdefault('first_response_sha256', sha)
        latest = {}
        for membership in product['memberships']:
            current = latest.get(membership['category_id'])
            if current is None or _version_older(current['catalog_version'],
                                                 membership['catalog_version']):
                latest[membership['category_id']] = membership
        for membership in latest.values():
            ctx_key = (membership['category_id'], membership['catalog_version'])
            if not any((c['category_id'], c['catalog_version']) == ctx_key
                       for c in seen['contexts']):
                seen['contexts'].append({
                    'category_id': membership['category_id'],
                    'catalog_version': membership['catalog_version'],
                    'response_sha256': sha, 'at': self.now()})
            self._resolve(
                REV_STALE_DETAIL,
                '%s@%s/%s' % (product['product_id'], membership['category_id'],
                              membership['catalog_version']))

    def _backfill_detail_for_version(self, product, cid, ver):
        """分类页晚于详情页到达：把已留存的详情原件补登到指定分类版本。

        只补登当前这一个版本；历史详情不会被自动算作新版本的证据，
        新版本是否仍属旧版由完成扫描时的新鲜度检查判定。
        """
        for seen in product['detail_seen'].values():
            if any(ctx['category_id'] == cid and ctx['catalog_version'] == ver
                   for ctx in seen['contexts']):
                continue
            seen['contexts'].append({
                'category_id': cid, 'catalog_version': ver,
                'response_sha256': seen.get('first_response_sha256'),
                'at': self.now(), 'backfilled': True, 'pre_category': True})

    def _product(self, pid):
        return self.state['products'].setdefault(pid, {
            'product_id': pid,
            'memberships': [],          # 分类入口历史（追加）
            'detail_seen': {},         # detail_url -> 采集上下文
            'name_history': [],
            'sku_ids': [],
            'listing': {'status': 'unknown', 'at': self.now()}})

    def _new_sku(self, sku_id, pid):
        sku = {'sku_id': sku_id, 'product_id': pid,
               'editions': {}, 'name_history': [], 'created_at': self.now()}
        self.state['skus'][sku_id] = sku
        return sku

    def _new_edition(self, doc, *, placeholder=False):
        return {
            'edition': getattr(doc, 'edition', None),
            'name': getattr(doc, 'name', None),
            'attributes': dict(getattr(doc, 'attributes', {}) or {}),
            'assets': {},
            'detail_responses': [],
            'packaging_responses': [],
            'history': [{'event': 'created', 'at': self.now(),
                         'placeholder': placeholder}],
            'status': 'staged',
            'published_at': None,
            'delisted_at': None,
            'placeholder': placeholder}

    def _note_name(self, name, sku_id, sha):
        index = self.state['name_index'].setdefault(name, {})
        index[sku_id] = self.now()
        sku = self.state['skus'][sku_id]
        sku.setdefault('name_history', [])
        if not sku['name_history'] or sku['name_history'][-1]['name'] != name:
            sku['name_history'].append(
                {'name': name, 'response_sha256': sha, 'at': self.now()})
        if len(index) > 1:
            self._open_review(
                SEVERITY_INFO, REV_NAME_AMBIGUITY, name,
                '名称 %s 被 %d 个不同 SKU 共用，名称不可作为身份键：%s'
                % (name, len(index), ', '.join(sorted(index))),
                data={'sku_ids': sorted(index)})
        else:
            self._resolve(REV_NAME_AMBIGUITY, name)

    def _upsert_asset(self, declaration, sku_id, edition_id, sha, effects, *, via):
        aid = declaration['asset_id']
        asset = self.state['assets'].get(aid)
        if asset is None:
            asset = {'asset_id': aid, 'url': declaration['url'],
                     'declared_digest': declaration['digest'],
                     'actual_digest': None, 'observed_status': 'declared_only',
                     'referenced_by': [], 'history': [],
                     'first_response': sha, 'observed_at': self.now()}
            self.state['assets'][aid] = asset
            effects.append(('asset_declared', aid, via))
        else:
            if asset['url'] != declaration['url']:
                self._open_review(
                    SEVERITY_MANUAL, REV_ASSET_ID_REUSE, aid,
                    'asset_id %s 被复用到不同 URL：%s / %s'
                    % (aid, asset['url'], declaration['url']), response_sha=sha)
                asset['history'].append({'event': 'url_changed', 'at': self.now(),
                                         'from': asset['url'],
                                         'to': declaration['url'],
                                         'response_sha256': sha})
                asset['url'] = declaration['url']
            if declaration['digest'] and asset['declared_digest'] \
                    and declaration['digest'] != asset['declared_digest']:
                # 同一包装位换版：声明摘要变更，旧核实结论失效，新文件重新核实
                asset['history'].append({
                    'event': 'digest_changed', 'at': self.now(),
                    'from': asset['declared_digest'],
                    'to': declaration['digest'], 'response_sha256': sha})
                asset['declared_digest'] = declaration['digest']
                asset['observed_status'] = 'pending'
                asset['actual_digest'] = None
                self._open_review(
                    SEVERITY_INFO, REV_PENDING_DIGEST, aid,
                    '包装声明已换版，等待新文件经 CDN 同步后核对',
                    data={'url': asset['url']})
            elif declaration['digest'] and not asset['declared_digest']:
                asset['declared_digest'] = declaration['digest']
                self._resolve(REV_PENDING_DIGEST, aid)
        ref = {'sku_id': sku_id, 'edition': edition_id, 'via': via,
               'response_sha256': sha, 'at': self.now()}
        if ref not in asset['referenced_by']:
            asset['referenced_by'].append(ref)
        if sku_id and edition_id in self.state['skus'].get(sku_id, {}).get('editions', {}):
            edition = self.state['skus'][sku_id]['editions'][edition_id]
            edition['assets'].setdefault(aid, {
                'asset_id': aid, 'url': declaration['url'],
                'declared_digest': declaration['digest'],
                'actual_digest': None, 'observed_status': 'declared_only',
                'archive_path': None,
                'first_response': sha, 'observed_at': self.now()})
        # 声明摘要/文件到达后的状态推进
        if asset['observed_status'] == 'declared_only' and asset['declared_digest']:
            asset['observed_status'] = 'pending'
            self._open_review(
                SEVERITY_INFO, REV_PENDING_DIGEST, aid,
                '已声明摘要，等待 CDN 文件同步核对', data={'url': asset['url']})

    # ------------------------------------------------------------ 核实与对外目录
    def _recompute(self):
        """根据全部已留存证据重算每个版本的核实状态（纯函数式推导，不丢历史）。"""
        self._sync_edition_assets()
        for sku in self.state['skus'].values():
            for edition in sku['editions'].values():
                if edition['status'] == 'delisted':
                    continue
                if edition.get('placeholder') or sku.get('unconfirmed'):
                    self._stage(edition)
                    continue
                if self._has_open_manual(sku['sku_id']):
                    self._stage(edition)
                    continue
                product = self.state['products'].get(sku['product_id'])
                listed = bool(product) and self._product_currently_listed(product)
                assets_ready = bool(edition['assets']) and all(
                    self.state['assets'][aid]['observed_status'] == 'verified'
                    for aid in edition['assets'] if aid in self.state['assets'])
                if listed and assets_ready and edition['detail_responses']:
                    if edition['status'] != 'verified':
                        edition['status'] = 'verified'
                        edition['published_at'] = self.now()
                        edition['history'].append(
                            {'event': 'verified', 'at': self.now()})
                else:
                    self._stage(edition)

    @staticmethod
    def _stage(edition):
        if edition['status'] == 'verified':
            # 曾经发布过的证据失效：撤回对外目录但保留历史，不静默替换
            edition['status'] = 'staged'
            edition['history'].append(
                {'event': 'unpublished', 'at': _utcnow(),
                 'detail': '核实条件不再满足，撤回对外目录'})
        elif edition['status'] not in ('delisted',):
            edition['status'] = 'staged'

    def _sync_edition_assets(self):
        """把资产主记录的核实结论与归档路径同步到各版本下的引用副本。"""
        for sku in self.state['skus'].values():
            for edition in sku['editions'].values():
                for aid, ref in edition['assets'].items():
                    master = self.state['assets'].get(aid)
                    if not master:
                        continue
                    ref['url'] = master['url']
                    ref['declared_digest'] = master['declared_digest']
                    ref['actual_digest'] = master['actual_digest']
                    ref['observed_status'] = master['observed_status']
                    if master.get('archive_path'):
                        ref['archive_path'] = master['archive_path']

    def _has_open_manual(self, sku_id):
        for review in self.open_reviews(SEVERITY_MANUAL):
            if review['subject'] == sku_id:
                return True
            data = review.get('data') or {}
            if data.get('sku_id') == sku_id:
                return True
        return False

    def _product_currently_listed(self, product):
        """商品在任一分类的最新已完成扫描中挂出，才算当前在架。"""
        for membership in product['memberships']:
            category = self.state['categories'].get(membership['category_id'])
            if not category or category['latest_version'] != membership['catalog_version']:
                continue
            version = category['versions'][membership['catalog_version']]
            if version['completed_at'] is not None \
                    and product['product_id'] in version['items']:
                return True
        return False

    def public_catalog(self):
        """对外目录投影：只含已核实版本；未经核实的资料不会出现在这里。"""
        entries = []
        for sku_id in sorted(self.state['skus']):
            sku = self.state['skus'][sku_id]
            for edition_id in sorted(sku['editions']):
                edition = sku['editions'][edition_id]
                if edition['status'] != 'verified':
                    continue
                entries.append({
                    'product_id': sku['product_id'],
                    'sku_id': sku_id,
                    'edition': edition_id,
                    'name': edition['name'],
                    'attributes': edition['attributes'],
                    'assets': [self._asset_view(aid) for aid in sorted(edition['assets'])],
                    'published_at': edition['published_at'],
                })
        return {'generated_at': self.now(), 'entry_count': len(entries),
                'entries': entries}

    def publish_snapshot(self):
        snapshot = self.public_catalog()
        snapshot['snapshot_id'] = 'cat-%d' % (len(self.state['catalog_snapshots']) + 1)
        self.state['catalog_snapshots'].append(snapshot)
        return snapshot

    def _asset_view(self, aid):
        asset = self.state['assets'][aid]
        return {'asset_id': aid, 'url': asset['url'],
                'declared_digest': asset['declared_digest'],
                'actual_digest': asset['actual_digest'],
                'observed_status': asset['observed_status'],
                'archive_path': asset.get('archive_path')}

    # ------------------------------------------------------------ 追溯
    def trace_sku(self, sku_id):
        """从一个产品版本追到分类入口、详情原文与包装文件。"""
        sku = self.state['skus'].get(sku_id)
        if sku is None:
            raise ValueError('SKU 不存在：%s' % sku_id)
        product = self.state['products'].get(sku['product_id'])
        category_entries = []
        if product:
            for membership in product['memberships']:
                category = self.state['categories'].get(membership['category_id'])
                version = category['versions'][membership['catalog_version']] \
                    if category else None
                category_entries.append({
                    'category_id': membership['category_id'],
                    'catalog_version': membership['catalog_version'],
                    'detail_url': membership['detail_url'],
                    'scan_completed': bool(version and version['completed_at']),
                    'response_sha256': membership['response_sha256'],
                    'response_url': self._response_url(membership['response_sha256']),
                    'archive_path': self._response_archive(membership['response_sha256']),
                })
        editions = {}
        for eid, edition in sku['editions'].items():
            editions[eid] = {
                'status': edition['status'],
                'name': edition['name'],
                'attributes': edition['attributes'],
                'history': edition['history'],
                'detail_sources': [self._response_view(s)
                                   for s in edition['detail_responses']],
                'packaging_sources': [self._response_view(s)
                                      for s in edition['packaging_responses']],
                'assets': [self._asset_view(aid) for aid in sorted(edition['assets'])],
            }
        blocking = [{'review_id': r['review_id'], 'code': r['code'],
                     'subject': r['subject'], 'summary': r['summary']}
                    for r in self.open_reviews(SEVERITY_MANUAL)
                    if r['subject'] == sku_id
                    or (r.get('data') or {}).get('sku_id') == sku_id]
        return {
            'sku_id': sku_id,
            'product_id': sku['product_id'],
            'identity_unconfirmed': bool(sku.get('unconfirmed')),
            'listing': product['listing'] if product else None,
            'name_history': sku.get('name_history', []),
            'category_entries': category_entries,
            'editions': editions,
            'manual_confirmation_required': blocking,
            'in_public_catalog': any(
                e['status'] == 'verified' for e in sku['editions'].values()),
        }

    def _response_view(self, sha):
        meta = self.state['responses'].get(sha, {})
        return {'response_sha256': sha, 'source_id': meta.get('source_id'),
                'url': meta.get('url'), 'kind': meta.get('kind'),
                'observed_at': meta.get('observed_at'),
                'archive_path': meta.get('archive_path')}

    def _response_url(self, sha):
        return self.state['responses'].get(sha, {}).get('url')

    def _response_archive(self, sha):
        return self.state['responses'].get(sha, {}).get('archive_path')

    # ------------------------------------------------------------ 收据
    @staticmethod
    def _receipt(sha, kind, duplicate, effects, *, review_id=None):
        return {'response_sha256': sha, 'kind': kind,
                'duplicate': duplicate, 'effects': effects,
                'review_id': review_id}
