# 来源契约

分类响应含 category_id、catalog_version、items[{product_id,detail_url}]；详情含 product_id、sku_id、edition、assets[{asset_id,url,digest?}]。名称不是身份键，资源摘要可能延后提供。源站按分类发布版本，不承诺全站原子换代。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 sku_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
