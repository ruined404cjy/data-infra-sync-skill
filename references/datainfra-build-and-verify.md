# DataInfra 构建与安装核验

源码同步完成、`verify install` 返回 `build_required` 或 `deployment_mismatch` 时读取本文件。所有路径均相对于当前 DataInfra checkout 根目录。

## 权威入口

每次构建前读取当前 checkout 的以下内容：

1. `README.md`：环境准备、全量构建、增量构建和测试说明。
2. `AGENTS.md`：当前构建顺序、构建风格、补丁和工作区约束。
3. `build/build-component.sh --help`：当前组件 build/install/verify 命令。
4. `build/build-all.sh`：DataInfra 全量构建入口。
5. `test/run_all.sh`：母仓测试 wrapper。

仓库当前文件与脚本帮助定义构建契约。`data-infra-sync` 只核验身份，不执行构建或测试。

## 组件规则

`build/build-component.sh` 提供 `opengauss`、`bridge`、`fdw`、`catalog`、`delta` 和 `all` 等入口。`bridge`、`fdw`、`catalog`、`delta` 会完成对应组件的 build 与 install；`*-build` 只生成产物，`*-install` 只安装现有产物。`assemble` 安装已有 Bridge、FDW、Catalog 和 Delta 产物。

根据当前源码变化与 DataInfra 文档确定受影响组件。全部需要的组件必须完成 build 和 install。依赖范围不明确时执行当前文档规定的全量入口：

```bash
./build/build-all.sh
```

DataInfra 原生验证可使用组件脚本的 verify 入口。任务要求回归测试时通过母仓 wrapper 运行：

```bash
./test/run_all.sh <当前文档要求的参数>
```

## Manifest 与完成判据

安装身份覆盖关键仓库 HEAD、Bridge 的构建/Catalog 依赖/安装副本、Catalog/FDW/Delta 的构建与安装副本、extension control/SQL，以及关联 `gaussdb` 的共享库映射。

完成全部 build/install 后运行：

```bash
data-infra-sync --format json verify install --record
data-infra-sync --format json verify install
```

`--record` 跳过旧 manifest 比较，但仍要求当前源码、全部产物副本、扩展文件和进程映射内部一致；检查通过后才原子覆盖 manifest。失败时保留旧 manifest。随后普通核验必须返回 `deployment_consistent` 且退出 0，才表示部署完成。

`build_required` 表示 manifest 缺失或源码 HEAD 偏离记录。`deployment_mismatch` 表示产物副本、manifest 或运行进程映射不一致。两者均返回退出 2，并进入构建安装流程。
