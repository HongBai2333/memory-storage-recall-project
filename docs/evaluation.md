# Fusion Memory Eval 实施与使用

实施日期：2026-10-29（Asia/Shanghai）；评测协议 `eval-0.1.0`，产品核心仍为 `fusion-memory 0.1.0`。

依据 `../融合召回方案/Agent记忆中间件回测系统完整技术方案.md`（工作区外的同级方案目录）开始执行。本次交付 P0/P1 的本地文字回测基础闭环，以及部分 P2 规则诊断、P3 离散事件仿真。**尚未达到方案的“基础回测可用”完整验收级别，更未完成 P0—P6 或 28 项技术验收。**

## 运行

当前虚拟环境已安装 `fm-eval` 命令。最方便的入口：

```powershell
.\scripts\evaluate.ps1
```

每次创建新的 `artifacts/evaluation/pilot-<随机标识>`，不接入本机既有记忆数据或凭据。也可显式执行：

```powershell
.\.venv\Scripts\fm-eval.exe validate --dataset evaluation\datasets\text-foundation-v1
.\.venv\Scripts\fm-eval.exe run `
  --dataset evaluation\datasets\text-foundation-v1 `
  --experiment evaluation\datasets\text-foundation-v1\experiment.json `
  --output artifacts\evaluation\my-first-run
.\.venv\Scripts\fm-eval.exe verify --run-dir artifacts\evaluation\my-first-run
```

`--output` 必须是新目录，防止覆盖既有实验。安装后尚无新命令时运行 `python -m pip install --no-deps -e .`；也可直接使用 `python -m fusion_memory.evaluation.cli`。运行环境沿用项目锁定依赖，无新增运行时依赖。

中断后用原实验恢复：

```powershell
.\.venv\Scripts\fm-eval.exe resume --run-dir artifacts\evaluation\my-first-run
```

恢复会核对源码、依赖、Python/平台、实验和数据摘要。已封存的 SUT 响应直接评分，不因低分重跑；中断中的单题在新临时库重建，旧尝试保留为 `interrupted`。这是本地无外部副作用的逐题恢复，尚不是方案的 episode 游标、远程工具副作用核对或分布式恢复。

## 当前能力与数据

- 严格 Pydantic/JSON Schema：公开 fixture、问题、身份与预算；评测侧证据单元、Gold 和实验清单分开。未知字段拒绝。
- 数据准入：文件白名单及哈希、唯一 ID、收件时间与顺序、修订/CAS/重复、完整知识前缀、删除/作用域、字符区间和充分证据组合检查。当前不接受预期拒绝写入的故障脚本。
- 真实 SDK 适配：直接调用现有 `MemoryService`、`IndexWorker`、`RetrievalService`，不重写检索算法。不是 HTTP、MCP 或 DSH 宿主验收。
- 每题独立 Python 进程、临时数据根目录、初始空状态检查、公开输入管道、临时目录清理；不把回答写回核心。
- capability：仅重建允许前缀，再推进每个作用域的连续处理水位。
- operational：冻结到达时间，按每个作用域 FIFO 加工耗时推进。查询不等待未到期任务。`available_at_simulated` 为模拟时间；核心 `received_at` 仍为实际墙钟时间，不伪造历史获知时间。
- SQLite 任务表、单运行 OS 文件锁、租约/fencing、不可覆盖的尝试结果、封存哈希、失败/超时/未判定保留完整分母。
- 最终上下文的完整证据命中、多组替代证据、答案点支持、禁止断言、预算和边界规则；按 episode 簇配对 bootstrap，模式分栏。
- Markdown、CSV、JSON/JSONL、G0—G5 和 T01—T28 覆盖矩阵、数据/源码/依赖/产物摘要。

开发集为 **19 题、5 个 episode**，包括源修订、重复投递、租户/项目/会话隔离、删除、中文/日文/型号、多证据、字符块、未来探针，以及尚未支持的事实/历史能力。两种模式 × 两种方案共 76 个调度单元。重复与不同方案不会增加独立统计簇数。

其中 `business-new-message-update` 保留方案中“通过新消息改变预算”的场景。原始事件都合法，但旧值与新值一起进入摘录回答时应判失败；不能把“召回到新值”误认为“当前状态已经正确”。这个失败定位到最终上下文/摘录回答中的旧值，尚不能仅凭该轨迹区分事实层与真实宿主的责任。

这些是项目生成的合成数据，**不是方案要求的 150 点人工复核 pilot，也不是 400 题正式集**。固定宿主 `extractive-context-v1` 仅原样摘录上下文；`literal-extractive-v1` 只检验明确字符串答案与引用支持，不替代自然语言 Judge、数值归一化、实际 LLM 问答或任务执行质量。

原文、Gold、结果同属当前用户可访问文件。SUT 使用受信任本地代码，公开 DTO 不携带 Gold；**尚无 OS ACL/容器强隔离**，不适合运行不受信任的 SUT 或挂载敏感业务数据。G0 始终明确保留这一缺口。

## 指标口径

| 指标 | 分子 / 分母 | 缺失策略 |
| --- | --- | --- |
| 严格成功 | 已确认成功 / 全部应测题 | SUT 失败与超时为失败；依赖未就绪及不可判定保留在分母，并另报未判定 |
| 判定覆盖 | 已判定 / 全部应测题 | 未判定不声称错误或通过 |
| 完整证据 | 命中任一充分组合 / 全部可回答题 | 未执行/失败不从分母剔除；另报证据判定覆盖 |
| 单题召回 | 各充分组合覆盖比例的最大值 | 无答案题为 N/A |
| 答案点覆盖 | 内容正确且有充分引用支持的点 / 必需点 | 单靠相同 source ID 不计分 |
| 配对差 | 候选严格成功减基线严格成功，题目加权 | 未判定配对列明，阻止正式区间与提升结论 |
| 簇 bootstrap | 同簇所有题/重复一起抽样，冻结种子与次数 | 少于 2 簇不给区间；少量开发簇不声称提升成立 |

金额/模型调用：此版模型调用为 0；计算与存储成本未计量。`query_elapsed_ms` 不含进程启动、数据重建；`case_elapsed_ms` 含这些开销。未提供生产 P95/SLA 结论。前置检索候选、重排、OCR/embedding、模型输入 token 尚无观测，不能输出伪造的阶段归因。

## 产物与模块边界

`resolved-manifest.json` 冻结实验和实施版本；`dataset/` 保存输入与 Gold；`tasks.sqlite` 持久化任务和尝试；`results.jsonl` 保存实际回执、请求、最终上下文、答案与评分；`attempts.jsonl` 保存包括中断在内的尝试；`summary.json`、`results.csv`、`report.md` 提供汇总和诊断；`artifact-manifest.json` 校验 12 项产物。哈希用于检测变化，不是抵御同账户恶意重签的安全签名。

实现集中在 `src/fusion_memory/evaluation/`。产品服务不导入评测代码；评测器通过现有业务服务契约访问核心。每个单题结束后关闭 Runtime 并清理临时库；父进程强制终止子进程超时后也清理其临时根。运行结束保留评测产物，自动 TTL、真实删除请求跨 Gold/trace 清理尚未实现。

## 后续依赖与验收

| 优先级 | 节点 / 责任 | 剩余工作与验收 |
| --- | --- | --- |
| P0 | 数据 / 业务标注、QA | 150 点独立人工复核、歧义裁决、许可与切分记录；不为当前成绩调整 Gold |
| P0 | 隔离 / 平台 | 独立账户/容器和挂载白名单，证明 SUT 无法访问 Gold、报告和其他 variant |
| P0 | 回放 / 核心 | 双时间事实、测试时钟、历史投影、动态撤权与真实删除链；完成预算迟报/纠错例 |
| P0 | 宿主 / 平台、模型 | 选定固定宿主与模型版本、采集实际序列化输入；数值/语义 Judge 校准与人工复核 |
| P1 | 调度 / 评测 | 控制面 HTTP、取消/恢复点、跨进程故障注入、真实节奏回放、累计成本预算与 TTL |
| P1 | 观测 / 检索 | 分路召回→融合→重排→上下文→宿主输入的完整轨迹，关联 token、费用与超时 |
| P1 | 通用能力 / 核心、平台 | Wiki/Graph/CAG、多模态、云 Provider、迁移、目录切换、真实宿主接入 |
| P2 | 研究 / 模型 | T23—T27 真实模型/权重/策略产物、回退和全矩阵联合验收；T28 完整工作流恢复 |

方案内引用的主技术方案摘要与目前主方案文件不同；实施记录冻结两个实际文件的摘要，不覆盖原方案和旧依据。Harness 节点记录与验证证据见 `.harness-analysis/ANALYSIS_20261029_MEMORY_EVAL/`。
