# Fusion Memory

Agent 记忆中间件，开始实施于 2026-10-28。当前 `0.1.0` 是 **P0/P1 的文字存储与召回基础切片**，不是完整方案或完整 P1 验收版。

已实现：事件持久化、修订与幂等、源删除 tombstone、独立后台 FTS5 索引、中日文基础分词和型号查询、近期原文补查、双次证据核验、上下文预算、checkpoint、租户/项目凭据隔离。默认本地运行，不依赖模型或云服务。向量、事实抽取和媒体能力明确报告未实现。

## 启动

当前工作区的 `.venv` 已安装并验证。直接运行：

```powershell
.\scripts\start.ps1
```

默认监听 `http://127.0.0.1:8765`，接口文档位于 `/docs`。默认数据目录为 Windows `%LOCALAPPDATA%\fusion-memory`；首次启动生成 `config\local.json` 和本地随机凭据。文件中的 token 仅供本机客户端读取，日志不输出它。启动脚本前台运行，Ctrl+C 停止。

自定义目录（支持中文和空格）：

```powershell
.\scripts\start.ps1 -DataRoot 'D:\AgentMemory\本地记忆' -Port 8765
```

已存在配置的 `data_root` 必须与命令参数一致。换目录会使用另一份数据，不是迁移；本版没有自动搬库。服务持有 OS 文件锁，一个数据根目录只允许一个服务进程。

从干净环境安装（当前验证组合为 Windows x64 / Python 3.14.4；Linux 尚未运行）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

## 写入和召回

服务启动后，在另一个 PowerShell 中执行：

```powershell
$memoryRoot = Join-Path $env:LOCALAPPDATA 'fusion-memory'
$memoryConfig = Get-Content -LiteralPath (Join-Path $memoryRoot 'config\local.json') -Raw | ConvertFrom-Json
$memoryHeaders = @{ Authorization = 'Bearer ' + $memoryConfig.credentials[0].token }
$memoryEvent = Get-Content -LiteralPath '.\examples\event.json' -Raw -Encoding UTF8
$memoryReceipt = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/v1/events' -Method Post `
  -Headers $memoryHeaders -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($memoryEvent))
$memoryQuery = @{
  project_id = 'default'; query = '灰色 A-123'
  log_id = $memoryReceipt.log_id; min_commit_seq = $memoryReceipt.commit_seq
  consistency_mode = 'at_least'; wait_timeout_ms = 1000
} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/v1/memory/search' -Method Post `
  -Headers $memoryHeaders -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($memoryQuery))
```

相同源事件重复提交返回 `duplicate`；同修订不同正文返回 409。修改消息需新 delivery/revision 和更大的 source_sequence，或提供 expected_previous_revision。源版本是字符串，不按字典序判断新旧。完整契约见 [contracts](contracts/openapi.json)；尚不支持的媒体、policy、历史查询等字段会被明确拒绝，不能静默丢弃。

## 已实现的 API

| 路径 | 用途 |
| --- | --- |
| POST `/v1/events` | 单事件提交；事件 1.1 / 回执 1.2；删除使用相应 delete 事件 |
| GET `/v1/sources/{id}` | 读取当前有效原文；已过期修订/已删除/其他租户返回 404 |
| POST `/v1/memory/search` | 关键词召回、近期补查与来源证据 |
| POST `/v1/context/assemble` | 同一检索结果的有预算上下文，不调用生成模型 |
| GET `/v1/processing-status/{id}` | 提交及逐能力处理进度 |
| POST / GET `/v1/checkpoints[/{id}]` | 幂等保存、版本 CAS、恢复时校验工作流版本 |
| GET `/v1/capabilities` | 实际能力与 worker 状态 |
| GET `/health/live`、`/health/ready` | 本地基础服务健康 |

`committed` 仅表示原始数据已落盘。`best_effort` 可带 `partial`；`at_least` 对必需能力有界等待，未达到返回 202/pending。vector/facts 当前为 unavailable，不能作为已完成能力。不存在的完整方案 API 尚未实现。

## 验证与后续

已开始实施独立回测系统（2026-10-29）。运行 `.\scripts\evaluate.ps1` 可执行真实本地核心的前缀回放、索引延迟仿真和证据评分，并生成 Markdown/CSV/JSON 报告。当前为 19 题合成开发集与确定性摘录宿主，完整验收仍为 incomplete；使用与剩余工作见 [回测实施说明](docs/evaluation.md)。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -X utf8 scripts\smoke.py
.\.venv\Scripts\python.exe -X utf8 scripts\export_contracts.py
```

测试使用临时库。HTTP 冒烟会启动两个自己创建的测试进程，检验强制停止后的恢复，然后清理；不访问用户 DSH profile。

下一阶段和范围差异见 [实施状态](docs/implementation-status.md)，模块与失败出口见 [架构契约](docs/architecture.md)，DSH 官方变更及本地更新见 [DSH 核对记录](docs/dsh-update.md)。本版仅适合本地开发验证：尚无动态 ACL、凭据热轮换、云部署、物理擦除、目录迁移、媒体上传或语义向量检索。
