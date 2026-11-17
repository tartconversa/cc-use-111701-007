"""来源响应的条目提取与结构化解析。

名称不是身份键，解析器只承认来源显式声明的身份字段
（product_id / sku_id / asset_id / category_id）。
"""
import base64
import hashlib
import json
from dataclasses import dataclass, field


def extract(response, key):
    """旧流程：从 records 数组中按指定键取条目，空数组即零条目。"""
    if response.status != 200:
        raise ValueError('来源未返回成功实体')
    value = json.loads(response.body)
    if not isinstance(value, dict) or not isinstance(value.get('records'), list):
        raise ValueError('缺少 records 数组')
    found = []
    for row in value['records']:
        if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key]:
            raise ValueError('条目身份无效')
        found.append({'key': row[key], 'value': row})
    return found


# ---------------------------------------------------------------- 结构化文档

KIND_CATEGORY = 'category'
KIND_DETAIL = 'detail'
KIND_PACKAGING = 'packaging'
KIND_ASSET = 'asset'
KIND_UNKNOWN = 'unknown'

_RESERVED = {'product_id', 'sku_id', 'edition', 'name', 'assets'}


def _require_str(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError('字段 %s 缺失或不是非空字符串' % key)
    return value


def _optional_str(row, key):
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError('字段 %s 必须是非空字符串' % key)
    return value


@dataclass(frozen=True)
class CategoryPage:
    category_id: str
    catalog_version: str
    items: tuple  # ({product_id, detail_url}, ...)
    page: object
    total_pages: object
    final_page: bool
    raw: dict


@dataclass(frozen=True)
class Detail:
    product_id: str
    sku_id: str
    edition: str
    name: object
    assets: tuple  # ({asset_id, url, digest}, ...)
    attributes: dict
    raw: dict


@dataclass(frozen=True)
class PackagingManifest:
    product_id: object
    sku_id: object
    edition: object
    assets: tuple  # ({asset_id, url, digest}, ...)
    raw: dict


@dataclass(frozen=True)
class Parsed:
    kind: str
    document: object = None


def _parse_assets(rows):
    assets = []
    if not isinstance(rows, list):
        raise ValueError('assets 必须是数组')
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('资产条目必须是对象')
        assets.append({
            'asset_id': _require_str(row, 'asset_id'),
            'url': _require_str(row, 'url'),
            'digest': _optional_str(row, 'digest'),
        })
    return tuple(assets)


def _parse_category(value):
    if not isinstance(value.get('items'), list):
        raise ValueError('分类响应缺少 items 数组')
    items = []
    for row in value['items']:
        if not isinstance(row, dict):
            raise ValueError('分类条目必须是对象')
        items.append({
            'product_id': _require_str(row, 'product_id'),
            'detail_url': _require_str(row, 'detail_url'),
        })
    page = value.get('page', 1)
    total_pages = value.get('total_pages', 1)
    final_page = value.get('final_page')
    if not isinstance(final_page, bool):
        final_page = page == total_pages
    return CategoryPage(
        category_id=_require_str(value, 'category_id'),
        catalog_version=_require_str(value, 'catalog_version'),
        items=tuple(items),
        page=page,
        total_pages=total_pages,
        final_page=final_page,
        raw=value,
    )


def _parse_detail(value):
    assets = _parse_assets(value.get('assets', []))
    attributes = {
        k: v for k, v in value.items()
        if k not in _RESERVED and isinstance(v, (str, int, float, bool))
    }
    name = value.get('name')
    if name is not None and not isinstance(name, str):
        raise ValueError('name 必须是字符串')
    return Detail(
        product_id=_require_str(value, 'product_id'),
        sku_id=_require_str(value, 'sku_id'),
        edition=_require_str(value, 'edition'),
        name=name,
        assets=assets,
        attributes=attributes,
        raw=value,
    )


def _parse_packaging(value):
    if 'assets' in value:
        assets = _parse_assets(value['assets'])
    else:
        assets = ()
    return PackagingManifest(
        product_id=_optional_str(value, 'product_id'),
        sku_id=_optional_str(value, 'sku_id'),
        edition=_optional_str(value, 'edition'),
        assets=assets,
        raw=value,
    )


def classify(value):
    """按来源声明的字段判定文档类型，不依赖名称匹配。"""
    if not isinstance(value, dict):
        return KIND_UNKNOWN
    # 包装清单以 packaging_id 为显式标记，优先于详情的字段组合判定
    if 'packaging_id' in value:
        return KIND_PACKAGING
    if 'category_id' in value and 'catalog_version' in value and 'items' in value:
        return KIND_CATEGORY
    if all(k in value for k in ('product_id', 'sku_id', 'edition')) and 'assets' in value:
        return KIND_DETAIL
    if 'assets' in value and not all(k in value for k in ('sku_id', 'edition')):
        return KIND_PACKAGING
    return KIND_UNKNOWN


def parse(response):
    """把成功响应解析成来源声明的结构化文档。

    解析失败（结构不符或身份字段缺失）抛 ValueError，由台账层记为
    source_unparseable 人工项，不会静默丢数据。
    """
    if response.status != 200:
        raise ValueError('来源未返回成功实体')
    value = json.loads(response.body)
    kind = classify(value)
    if kind == KIND_CATEGORY:
        return Parsed(kind, _parse_category(value))
    if kind == KIND_DETAIL:
        return Parsed(kind, _parse_detail(value))
    if kind == KIND_PACKAGING:
        return Parsed(kind, _parse_packaging(value))
    raise ValueError('无法辨认的来源文档结构')


# ---------------------------------------------------------------- 包装文件原件

def digest_bytes(body, algorithm='sha256'):
    """对下载到的包装文件原件计算摘要，用于和来源声明的 digest 核对。"""
    if algorithm != 'sha256':
        raise ValueError('仅支持 sha256 摘要')
    return hashlib.sha256(body).hexdigest()


def decode_asset_packet(packet):
    """读取资产下载包 {asset_id?, url, body_base64}，返回 (meta, 原件字节)。"""
    if not isinstance(packet, dict):
        raise ValueError('资产包必须是对象')
    asset_id = packet.get('asset_id')
    if asset_id is not None and (not isinstance(asset_id, str) or not asset_id):
        raise ValueError('asset_id 必须是非空字符串')
    url = packet.get('url')
    if not isinstance(url, str) or not url:
        raise ValueError('资产包缺少 url')
    try:
        body = base64.b64decode(packet['body_base64'], validate=True)
    except KeyError:
        body = b''
    except Exception as exc:
        raise ValueError('资产实体编码无效') from exc
    return {'asset_id': asset_id, 'url': url}, body
