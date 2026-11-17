# 年份原浆产品目录的版本化抓取

按来源声明的商品身份（`product_id` / `sku_id` / `asset_id`）把分类页、详情页和
包装资料关联到同一份版本化台账。名称不是身份键：古5、古8、古16、古20 的瓶型、
度数、包装版次差异都以 SKU 与版本事件留痕，改名、下架、包装替换不覆盖历史。

## 模块

- `source.py`：单响应读取、状态/头/原始字节校验与 sha256（保持不变）。
- `extract.py`：旧的 `records` 提取（兼容），新增分类页/详情页/包装清单解析器、
  文档分类器与包装文件摘要计算。
- `catalog.py`：`CatalogStore` 台账——身份关联、暂存/核实状态机、扫描进度与
  中断恢复、幂等收据、人工审核队列、对外目录投影与 SKU 全链路追溯。
- `ledger.py`：旧的单响应收据（保持不变）。
- `service.py`：统一入口，旧 `inspect` 流程与新的版本化摄入并存。

## 快速开始

    # 旧流程（单响应收据，行为不变）
    python -m app.service inspect fixtures/sample.json

    # 版本化摄入；--archive 留存响应原件与包装文件
    python -m app.service ingest packet.json --state catalog_state.json --archive archive/
    python -m app.service asset  asset_packet.json --state catalog_state.json

    # 扫描进度（哪些分类已扫完、哪些详情仍属旧版）
    python -m app.service scan --state catalog_state.json

    # 从一个产品版本追到分类入口、详情原文与包装文件
    python -m app.service trace g20-52-500 --state catalog_state.json

    # 对外目录（只含已核实条目）；--publish 留存发布快照
    python -m app.service catalog --state catalog_state.json [--publish]

    # 人工确认队列；resolve 关闭指定审核项
    python -m app.service reviews --state catalog_state.json
    python -m app.service resolve rv-3 "来源侧文件有误，等待重发" --state catalog_state.json

摄入的 packet 与旧格式一致（`source_id/url/status/headers/body_base64`）；
包装文件包为 `{url, asset_id?, body_base64}`。无法辨认的响应自动隔离入人工队列，
不中断整轮采集。

## 保证与边界

- 未经跨来源核实的资料保持 `staged`，不会出现在对外目录中；已发布条目失效时
  撤回而非被静默替换。
- 响应按 sha256 幂等，重复请求不产生新 SKU；扫描中断后可凭持久化状态恢复。
- 只能人工确认的身份问题在 `trace_sku` 与 `reviews` 中显式标出；CDN 延迟等
  可自动恢复的问题为 info 级，证据到达后自行消解。
- 仅使用 Python 标准库，不接触真实企业系统；夹具为合成数据。

## 测试

    python -m unittest discover -s tests -v
    python -m compileall -q app tests
