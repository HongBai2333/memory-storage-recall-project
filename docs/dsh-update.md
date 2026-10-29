# DSH 接入兼容性基线

核对日期：2026-10-28（Asia/Shanghai）。从 [官网](https://www.deepseek.com/harness/) 跟随官方仓库及开发文档入口，核对 [v0.2.2-alpha.2 发布](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-alpha.2)，提交 `f3803c96974ad4a94195fb97e5c637e9c2a3afe3`，发布日期 2026-10-28 10:50:54。

本页保留上述日期的文档与源码核对结果，用于后续适配器设计；不表示本项目已经安装或验证该宿主版本。

## 与记忆适配器直接相关的变化

| 变化 | 实施影响 |
| --- | --- |
| Node >=24.9.0 | 目标宿主和构建环境需满足此要求，并分别记录实际运行版本 |
| 工具 executor 拆包 | 移除 tools.mode/maxParallelSubCalls 与 DSH_TOOLS_MODE；组合显式挂载 Native/PTC executor |
| 条件工具按步准入 | `system-prompt/assemble` 不再携带 tools；使用 when/toolSet，声明与执行一致 |
| PTC script/retainsState/ABORTED | 适配程序格式与状态保存；区分调用取消和普通失败，保留历史错误兼容 |
| 异步 projection handles | await register；通过拥有者 API 读取；卸载清理正确处理 callable thenable |
| Session 发布后才能写 | session/created 不写入；setup/agent-created 与失败恢复顺序需真实验证 |
| SDK-minimal checkpoint | 持久化失败会阻止模型和工具；不能把此前缓冲行为当现行语义 |
| 完整附件引用 | 不再校验图片 Session membership；记忆核心必须独立核验资产项目所有权 |
| 技能目录完整性 | incomplete snapshot 不作为权威空目录缓存，失效后重新读取 |
| 第三方插件更新 | 有热更新失败后的恢复尝试，不能声称原子回滚 |

核对来源为上方固定版本发布及其提交。正式接入时，应分别记录文档基线、项目 SDK 依赖、运行中宿主版本和实际测试兼容范围。

当前 DSH Loader/Host/Native/PTC 集成仍为 `not_run`。接入验收需在隔离宿主中覆盖加载、工具调用、取消、卸载重挂、附件权限及恢复流程；文档核对不替代这些运行验证。
