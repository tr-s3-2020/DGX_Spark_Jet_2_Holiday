# 协议版本

新对接使用 **task2/**：英文枚举、扁平 TriageOutput，包含 safety 和 metadata.degraded。

本目录根部的七份 JSON Schema 均为 **legacy / 仅旧接口兼容，不用于新对接**。
它们描述中文枚举和旧版 TriageResult/TriageResponse；保留是为了兼容旧调用方。
旧 integration.to_digest_record 只投影健康字段，会丢失阻断、降级和关联信息。

新版导出：`python scripts/export_task2_schemas.py`；校验加 `--check`。
旧版导出：`python scripts/export_schemas.py`，只维护兼容资产。
