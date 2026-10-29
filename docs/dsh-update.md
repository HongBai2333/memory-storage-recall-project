# DSH 官方更新核对

核对日期：2026-10-28（Asia/Shanghai）。从 [官网](https://www.deepseek.com/harness/) 跟随官方仓库及开发文档入口，核对 [v0.2.2-alpha.2 发布](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-alpha.2)，提交 `f3803c96974ad4a94195fb97e5c637e9c2a3afe3`，发布日期 2026-10-28 10:50:54。

在既有父项目 `skills` 中原地更新：

- `deepseek-harness-plugin/SKILL.md` 与 sources、version-updates、packaging-and-validation、tools-and-extensions、plugin-contracts 六个文件。
- `dsh-functional-self-test/SKILL.md` 与 test-matrix 两个文件。

共八个文件，保持中英双语元信息、使用、流程与约束；未新建技能，因此没有复制出两份同名 Skill。改前文件已归档至本项目 `.harness-analysis/2026-10-28-bootstrap/skills-before.zip`。

## 与记忆适配器直接相关的变化

| 变化 | 实施影响 |
| --- | --- |
| Node >=24.9.0 | 本机 Node 24.14.1 满足；这不表示用户运行中的 DSH 已升级 |
| 工具 executor 拆包 | 移除 tools.mode/maxParallelSubCalls 与 DSH_TOOLS_MODE；组合显式挂载 Native/PTC executor |
| 条件工具按步准入 | `system-prompt/assemble` 不再携带 tools；使用 when/toolSet，声明与执行一致 |
| PTC script/retainsState/ABORTED | 适配程序格式与状态保存；区分调用取消和普通失败，保留历史错误兼容 |
| 异步 projection handles | await register；通过拥有者 API 读取；卸载清理正确处理 callable thenable |
| Session 发布后才能写 | session/created 不写入；setup/agent-created 与失败恢复顺序需真实验证 |
| SDK-minimal checkpoint | 持久化失败会阻止模型和工具；不能把此前缓冲行为当现行语义 |
| 完整附件引用 | 不再校验图片 Session membership；记忆核心必须独立核验资产项目所有权 |
| 技能目录完整性 | incomplete snapshot 不作为权威空目录缓存，失效后重新读取 |
| 第三方插件更新 | 有热更新失败后的恢复尝试，不能声称原子回滚 |

上述每项固定源码/迁移链接保存在 [本地迁移参考](../../skills/deepseek-harness-plugin/references/version-updates.md)。官方源码与对比信息缓存在本项目 `.harness-analysis/2026-10-28-bootstrap/upstream` 目录；没有修改用户 DSH 安装、日常 profile 或旁边写作项目的依赖。

只读抽查到 `writing-plugin/node_modules/@deepseek-ai/dsh-agent` 为 0.2.1-alpha.4。文档基线、该目录的 SDK、运行中宿主和实际测试兼容范围是四个不同事实。本次只完成前者更新，DSH Loader/Host/Native/PTC 集成仍为 not_run。

验证：两份技能通过 quick_validate（Windows 使用 `python -X utf8`）；引用文件路径检查和固定来源检查另见实施报告。第一次按系统 GBK 默认编码运行 validator 失败，改为 UTF-8 后通过，未修改技能校验器本身。
