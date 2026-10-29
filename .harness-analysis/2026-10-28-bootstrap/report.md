# 实施与验证记录

分析 ID：ANALYSIS_20261028_MEMORY_BOOTSTRAP。输入类型 mixed（技术方案、官方源码、当前实现和运行证据）。范围：P0/P1 文字基础切片 + DSH 开发/自测技能文档更新；环境为 Windows x64、Python 3.14.4、Node 24.14.1。未执行 Linux、DSH Loader/Host、模型、云迁移或生产数据测试。

## 实际结果

- 36 项 pytest 全部通过，`pytest.xml` 与 `pytest.log` 保存本次结果。唯一 warning 是当前 Starlette 对 httpx TestClient 的弃用提示，未影响本次测试；没有将 warning 伪装成业务失败或隐去。
- Ruff 检查和格式检查通过。
- 真实 loopback HTTP 通过启动、写入、后台 FTS、at_least、checkpoint、强制停止后的进程恢复、幂等、删除和中文空格路径。测试进程与临时目录已清理。
- 6 份 DTO JSON Schema 和 OpenAPI 已导出；OpenAPI 包含 ConnectorCredential bearer scheme。
- `fusion_memory-0.1.0-py3-none-any.whl` 构建成功，校验入口与包内容；SHA-256 见 `package.json`。包未发布，实际烟测使用当前安装的源码版本。
- 两份 DSH Skill 结构校验通过；29 个相关本地引用存在。8 个原文件归档至 `skills-before.zip`。首次 validator 受 Windows GBK 默认编码影响，使用 `-X utf8` 后通过。

## EIR：主流程、节点与交接

| 节点 / 组件 | 输入 → 输出 | 主职责与状态归属 | 异常出口与恢复 | 证据 |
| --- | --- | --- | --- | --- |
| NODE_0001 / SRC_0001 门面 | Bearer + event → Scope + DTO | 固定配置身份映射；不接收客户端权威字段 | 401/403/422，不提交 | EVIDENCE_0001、0004 |
| NODE_0002 / SRC_0002 提交 | Scope + event → receipt + change | 权威事件、修订、head、tombstone、连续日志位置 | 事务回滚、幂等重试、CAS/序号冲突拒绝 | EVIDENCE_0002、0004 |
| NODE_0003 / SRC_0003 索引 | 连续变更 + 指定源版本 → FTS + 水位 | 召回拥有检索投影；权威不可改写 | 幂等重放、失败不跨水位、退避/五次停止 | EVIDENCE_0003、0005 |
| NODE_0004 / SRC_0004 搜索 | 查询 + Scope + 预算 → 候选/覆盖 | read-only index + MemoryReadPort；来源 RRF | 缺能力 pending、补查超限 partial | EVIDENCE_0003、0005 |
| NODE_0005 / SRC_0002 核验 | 候选/来源 → 放行 ID 与核验版本 | 当前权威快照判断；模型内容与事实分离 | 核验不可用 503；期间删除剔除 | EVIDENCE_0002、0005 |
| NODE_0006 / SRC_0005 恢复 | CAS/幂等 checkpoint → 状态与版本 | 独立执行状态，不写用户画像 | 冲突 409、工作流不兼容拒绝、重启读回 | EVIDENCE_0006、0007 |

控制流 FLOW_CONTROL_0001：认证 → 原始提交 → 回执；FLOW_CONTROL_0002：已提交 ChangeFeed → 后台投影 → 连续覆盖；FLOW_CONTROL_0003：候选 → 首次核验 → 装配 → 最终核验 → 放行。索引与查询不在权威提交事务内。

状态流 FLOW_STATE_0001：memory.sqlite 追加事件/变更并更新 head/tombstone；FLOW_STATE_0002：retrieval.sqlite 保存可重建投影；FLOW_STATE_0003：execution.sqlite 保存工作流状态。权限流 FLOW_PERMISSION_0001 由启动配置绑定 tenant/project，源 origin 与 actor 必须一致，查询不授予写入能力。动态策略撤销未实现，因此不宣称完整 ACL epoch 保证。

矩阵映射 MAP_0001：NODE_0002–0005 对应已存在的 `COG_MEMORY` / `TOP_CHAIN` / `CELL_MEMORY_CHAIN`，用于描述持久提交、异步投影、核验放行的链式交接。最小充分基线为单服务确定性代码、SQLite 事务和持续保留的日志；无需引入多 Agent、学习式路由或新的全局 pattern。没有向全局 Harness 技能注册表写入内容。

## 证据索引

| ID | 类型 | 定位与判断 |
| --- | --- | --- |
| EVIDENCE_0001 | source_code | `../../src/fusion_memory/app.py`：认证、错误、等待、路由；`config.py`：配置绑定 |
| EVIDENCE_0002 | source_code | `../../src/fusion_memory/memory_management/service.py`：事务、顺序、Outbox、权威核验 |
| EVIDENCE_0003 | source_code | `../../src/fusion_memory/memory_retrieval/`：索引、worker、只读检索与两次核验 |
| EVIDENCE_0004 | test | `../../tests/test_memory.py` + `pytest.xml`：隔离、并发幂等、回滚与修订 |
| EVIDENCE_0005 | test | `../../tests/test_retrieval.py` + `pytest.xml`：水位、重放、重建、失败、删除、预算 |
| EVIDENCE_0006 | test | `../../tests/test_execution_and_boundaries.py`：checkpoint、OS 锁、依赖边界 |
| EVIDENCE_0007 | runtime_record | `http-smoke.log` + `../../scripts/smoke.py`：真实进程与 HTTP 恢复 |
| EVIDENCE_0008 | official_doc/source_code | `upstream/comparison.json`、固定 release/迁移文档与包 exports；外部官方来源，不作为运行成功证据 |
| EVIDENCE_0009 | protocol | `source-plan.json`：原方案绝对位置、版本、SHA-256；`../../contracts/`：当前实现子集 |
| EVIDENCE_0010 | test | 两份 Skill validation log、`skill-links.json`：结构与本地引用通过 |

## 缺口、优先级与验收

| 状态 | 缺口/交接 | 优先级与责任 | 下一步与可观察验收 |
| --- | --- | --- | --- |
| adequate（本切片） | 权威与索引独立、删除核验 | 已验证；记忆/召回 | 保留上述回归，新增能力不能改变权威边界 |
| confirmed_gap | 媒体、事实、向量、完整 P0 契约 | P1；存储/记忆/模型 | 完成资产所有权、事实状态、真实 embedding 与逐能力状态；用真实样本验证 |
| confirmed_gap | 独立 worker lease、durable jobs、配额 | P1；存储/运维 | worker 崩溃/失去租约后旧结果拒绝；死信不跨覆盖；磁盘拒绝不丢已确认数据 |
| unknown | 新 DSH 适配器兼容 | P2；适配/QA | 按新技能构建隔离组合，真实加载/卸载/Native/PTC/附件核验，不用文档核对代替 |
| confirmed_gap | 动态 ACL、迁移、云、图、训练 | P3–P5；对应负责人 | 完整阶段标准逐项通过，见实施状态表；目前不开放相应 API |
| not_applicable（本切片） | 模型成本、外部部署、训练回滚 | 后续范围 | 本轮没有相关调用或部署，不生成成功结论 |

正确性结论：当前文字切片具备可复现证据。覆盖结论：P0/P1 均部分完成；M01–M12 的具体部分覆盖见 `../../docs/implementation-status.md`，无全技术完成声明。性能/语言质量/线上安全性尚无充分证据。治理：本地授权范围内新增源码、测试与文档；原方案和 DSH 用户配置未改；新建 Skill 时仍须位于项目 `./skills` 并提供双语。
