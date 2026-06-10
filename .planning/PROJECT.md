# codex_finder

## What This Is

一个跨 SSH 的 Codex CLI 凭证盘点工具,用于在多台远程服务器上找回被遗忘的 Codex/OpenAI 凭证。读取 `servers.yaml`(格式与 autoresearch 的 `servers` schema 兼容,采用 **D-03 Bootstrap-then-Key** 模式),登录每台目标,遍历所有用户、home 目录、Docker 容器、npm/pip 全局安装、shell history,定位与 Codex 相关的凭证残留。

## Core Value

**唯一目标**:列出所有可能存在 Codex 凭证的远程位置(文件路径 + 脱敏后的环境变量值),不导出明文凭证,让用户能 follow-up 决定如何处理。

## Why This Exists

工作目录在多台远程服务器之间切换过,曾用 root 在这些机器上跑过 Codex CLI,但具体路径、用户、容器都记不清了。需要一个工具能系统性地盘点这些机器上所有"可能藏着 Codex 凭证"的位置,而不是一台台手动 `find`。

跨网段扫描时通常没有直连,要从 192.168.13.154 跳板到其他子网(192.168.9.x、192.168.13.x)。每台机器的认证方式不同:多数已部署 ed25519 key,少数需要 bootstrap 一次性密码。

## Constraints

- **只读**:所有命令是 `ls` / `grep` / `test` / `getent` / `env` / `docker inspect` / `docker exec <只读>`,绝不修改远程状态
- **脱敏**:报告里所有凭证值保留首 4 + 末 2 字符
- **认证流程** (D-03 Bootstrap-then-Key):
  1. 先 `identity_file` 公钥(同时启用 SSH agent)
  2. 失败回落到 `bootstrap_password_secret` 密码
  3. 都没有时试 agent + `~/.ssh/` 默认 key
- **网络/协议错误立即抛出**:与认证失败分开,方便诊断
- **servers.yaml 含明文凭据**:必须 `.gitignore`,轮换机制由用户负责
- **离线工作**:脚本跑在用户本机,远程命令不依赖外网

## Key Decisions

| 决策 | 理由 |
|------|------|
| YAML 而非 .env | servers 列表是结构化数据(每台独立的 identity_file + 密码),YAML 比 .env 强;格式与 autoresearch 兼容可复用 |
| 走 paramiko + ProxyCommand 跳板 | 复用 OpenSSH 自带能力,无需在远程装任何东西 |
| 进程环境可覆盖 YAML | 临时调整并行度等不需要改文件;CI/脚本友好 |
| Bootstrap-then-Key 显式分两步 | 避免 paramiko 内部 "key 失败后自动尝试密码" 导致行为不可控;失败原因明确 |
| 单文件而非模块化 | 部署简单(可 `scp` 到跳板机),后续修改一个文件搞定 |
| Docker running + stopped 分开 | running 容器可直接 exec 扫描;stopped 容器只能列 volume 挂载点供人工 follow-up |
| 报告脱敏而非加密 | 工具是一次性盘点,后续用 `scp`/`cat` 单独拉取具体凭证更安全 |

## Context

### Environment
- 主机:macOS (darwin),位于中国,需代理 `127.0.0.1:7890` 才能访问外网
- 远程服务器清单(用户实际提供):
  - A2-AK-225 / 192.168.9.225 (root, ed25519, bootstrap_pwd)
  - A3-AX-153 / 192.168.13.153 (root, ed25519, bootstrap_pwd)
  - A3-AK-182 / 192.168.13.182 (root, ed25519, bootstrap_pwd)
  - A3-AX-176 / 192.168.13.176 (root, ed25519, bootstrap_pwd)
  - A2-AK-176 / 192.168.9.102 (admin123, ed25519, Huawei 默认密码)
- 跳板机候选:192.168.13.154 (root,本机有 key)
- Python:系统 Python 3 + 项目本地 venv

### Codex 凭证可能藏在哪(用户提供的清单)
1. 用户 home 目录 `~/.codex/`(含 `auth.json` 等)
2. 用户 shell 启动文件里的环境变量:`OPENAI_API_KEY` / `OPENAI_KEY` / `CODEX_API_KEY`
3. npm/pip 全局安装的 codex 包
4. Docker 容器内的用户 + 容器内环境变量
5. 用户 shell history 里的 codex/openai 调用痕迹

## Requirements

### Validated
(无 — 尚未在真实服务器上跑过)

### Active
- [ ] **CFG-01**:从 `servers.yaml` 解析 `servers[]` 列表
- [ ] **CFG-02**:支持 `servers.yaml.example` 模板
- [ ] **CFG-03**:YAML 语法错误时给出清晰错误信息
- [ ] **CFG-04**:进程环境变量可覆盖 YAML 中的可调项
- [ ] **AUTH-01**:Bootstrap-then-Key:先 `identity_file` 公钥
- [ ] **AUTH-02**:key 失败回落到 `bootstrap_password_secret` 密码
- [ ] **AUTH-03**:都未配置时试 SSH agent + `~/.ssh/` 默认 key
- [ ] **AUTH-04**:网络/协议错误(非认证失败)立即抛出,不与认证失败混淆
- [ ] **CONN-01**:支持 `jump.host` 走 SSH ProxyCommand 跳板
- [ ] **CONN-02**:支持 `--jump HOST` 覆盖 YAML 中的跳板
- [ ] **CONN-03**:支持 `--host H[:P]` 单台覆盖
- [ ] **SCAN-01**:枚举所有用户(带 home 目录)
- [ ] **SCAN-02**:对每个用户并行扫描
- [ ] **SCAN-03**:检测 `~/.codex/` 目录及 `auth.json` 元数据
- [ ] **SCAN-04**:在 shell 启动文件中 grep `OPENAI_*` / `CODEX_*` 环境变量
- [ ] **SCAN-05**:在 shell history 中 grep codex/openai/sk- 痕迹
- [ ] **SCAN-06**:在 `~/.npmrc` 中查 `_authToken`
- [ ] **SCAN-07**:扫描 `/etc/environment` / `/etc/profile*` 系统级配置
- [ ] **SCAN-08**:查全局 npm 包 `codex` / `@openai/codex`
- [ ] **SCAN-09**:查 pip 包 `codex`
- [ ] **SCAN-10**:查 `command -v codex` 可执行文件
- [ ] **SCAN-11**:扫描所有运行中 Docker 容器(`.codex/` + env + 全局 npm)
- [ ] **SCAN-12**:列出停止中 Docker 容器的 volume 挂载点
- [ ] **RPT-01**:终端分级报告(高/中/低/信息)
- [ ] **RPT-02**:JSON 报告落盘(`output: findings.json`)
- [ ] **RPT-03**:敏感值脱敏(首 4 + 末 2)
- [ ] **RPT-04**:扫描进度写到 stderr,报告写到 stdout
- [ ] **RPT-05**:报告里显示 `server_name` (人类可读) 而非仅 `host:port`
- [ ] **RPT-06**:JSON 里包含 `auth_method` (key/password/agent) 字段
- [ ] **UX-01**:连通性预检 `--check` 不执行扫描
- [ ] **UX-02**:列出解析到的服务器 `--list-servers`,带认证方式摘要
- [ ] **UX-03**:生成 `servers.yaml` 模板 `--init` / `--init-to PATH`
- [ ] **ERR-01**:单台主机失败不中断整体扫描
- [ ] **ERR-02**:单用户扫描失败不影响其他用户
- [ ] **ERR-03**:沉默 paramiko 内部 traceback,只显示用户级错误
- [ ] **ERR-04**:单条 server 配置错误时跳过该条,继续处理其他条

### Out of Scope
- **写/改远程文件** — 工具只读
- **导出明文凭证** — 用户自己 follow-up
- **Windows 远程主机** — 仅 POSIX
- **审计日志/合规报告** — 是一次性盘点工具,不是持续监控
- **Web UI** — CLI 足够
- **K8s / systemd EnvironmentFile 扫描** — v2 暂缓

## Evolution

本文档在以下时机更新:
- 完成真实服务器测试后,把 `Validated` 需求逐条搬入
- 出现新的扫描位置(如 k8s config、systemd EnvironmentFile)时,追加到 `Active`
- 决定增加写操作时,把 Out of Scope 的对应条目移除
