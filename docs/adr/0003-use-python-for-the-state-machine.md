# 使用 Python 实现同步状态机

`data-infra-sync-skill` 使用 Python 标准库实现 CLI、Git 子进程编排、结构化输出、原子状态文件和进程锁，DataInfra 项目适配器继续调用仓库原有 Bash 构建入口和已验证的系统检查命令。该边界保留项目原生构建能力，并使复杂同步判定、JSON 契约和临时 Git fixture 可以使用标准库独立测试，避免在 Bash 中继续扩展状态机逻辑。
