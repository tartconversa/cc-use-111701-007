# 来源契约

## 来源声明的身份字段

分类响应含 `category_id`、`catalog_version`、`items[{product_id,detail_url}]`，可带
`page`、`total_pages`、`final_page` 声明分页进度；详情含 `product_id`、`sku_id`、
`edition`、`assets[{asset_id,url,digest?}]`；包装清单以 `packaging_id` 为显式标记，
可附 `product_id`、`sku_id`、`edition` 与 `assets[]`。名称不是身份键，资源摘要可能
延后提供。源站按分类发布版本，不承诺全站原子换代。

## 台账如何解释这些字段

身份只认来源显式声明的 `product_id` / `sku_id` / `asset_id`：

- 分类页（`app/extract.py` 的 `KIND_CATEGORY`）建立“分类版本 → 商品 → 详情入口”，
  并以 `(category_id, catalog_version)` 为单位记录分页扫描进度；
- 详情页（`KIND_DETAIL`）把 `sku_id` 绑定到 `product_id`，`edition` 建立产品版本，
  名称仅作为版本的历史属性，改名追加 `renamed` 事件，不覆盖旧名称；
- 包装清单（`KIND_PACKAGING`）与包装文件原件只允许通过 `asset_id` 关联，
  同一 `asset_id` 的摘要变更解释为包装换版（追加 `digest_changed`），
  URL 变更则是身份冲突，必须人工确认；
- 结构无法辨认的响应不做猜测，整体隔离为 `source_unparseable` 人工项，原件留存。

## 暂存、核实与对外目录

新证据一律先进 `staged`，只有同时满足以下条件的产品版本才进入对外目录
（`CatalogStore.public_catalog`，未经核实的资料不会替换对外条目）：

1. 商品出现在某分类**最新且已完成**的扫描中（在架）；
2. 版本拥有详情原件，且该 SKU 没有未解决的人工级冲突；
3. 版本引用的每个包装文件都有声明摘要，且下载原件的 sha256 与声明一致。

包装文件状态机：`declared_only`（仅声明）→ `pending`（摘要/CDN 文件未齐，info 级，
可自动恢复）→ `verified`（摘要相符）或 `conflict`（摘要不符，manual 级）。
已发布版本若核实条件失效，转为 `unpublished` 撤回对外目录，历史完整保留。

## 扫描完成、中断恢复与旧版详情

- 扫描进度按分类版本持久化：已见页码、是否见末页、完成时间、已声明条目。
  中断后重发任意页都按响应 sha256 幂等处理；无分页标记的来源可用
  `complete_scan` / 驱动层显式收口。
- 新一版完成扫描时缺席的商品标记 `delisted`（历史入口不删除，重新挂出可 relist）。
- 详情原件记录采集时所处的分类版本上下文；分类换代后未在新版本下重抓的详情
  会被 `stale_detail` 人工项标出，重抓（即使原件字节相同）即消解。
- 详情先于分类页到达时挂 `product_pending_category`（info），分类到达后自动补登。

## 幂等与重复请求

- 每份响应按原件 sha256 登记一次；重复提交返回 `duplicate=true` 且不产生 effects，
  绝不新增 SKU（SKU 主键为 `sku_id`）。
- 重复的详情响应可在新分类版本下刷新上下文，但不创建任何新条目或新资产。

## 人工确认队列

`severity=manual` 的项目只能由人工通过 `resolve_review` 关闭，包括：来源无法辨认、
product/sku 归属冲突、详情 URL 身份冲突、asset_id 复用、摘要不符、包装孤儿、
孤儿文件、旧版详情。`severity=info`（摘要延迟、同名提示、缺详情/缺分类）在后续
证据到达时自动消解。`trace_sku` 输出中的 `manual_confirmation_required`
直接表明该产品身份有哪些项只能靠人工确认。

## 运行时边界

来源配置与真实凭据由运行时提供，仓库内样例与夹具均为合成数据。响应原件与下载的
包装文件可通过 `archive_dir` 归档，台账状态以单个 JSON 文件持久化（`save`/`load`）。
