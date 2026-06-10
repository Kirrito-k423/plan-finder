# v1 需求

## 配置 (CFG)

- [ ] **CFG-01**:从 `servers.yaml` 解析 `servers[]` 列表(每项含 name/host/port/user/identity_file/bootstrap_password_secret)
- [ ] **CFG-02**:支持 `servers.yaml.example` 模板
- [ ] **CFG-03**:YAML 语法错误时给出清晰错误信息并退出码 2
- [ ] **CFG-04**:进程环境变量可覆盖 YAML 中的可调项(PARALLEL/CMD_TIMEOUT/VERBOSE/SCAN_DOCKER/SCAN_HISTORY/SCAN_NPM_PIP/OUTPUT)

## 认证 (AUTH)

- [ ] **AUTH-01**:Bootstrap-then-Key 步骤 1 — 尝试 `identity_file` 公钥认证(同时启用 SSH agent + look_for_keys)
- [ ] **AUTH-02**:Bootstrap-then-Key 步骤 2 — key 认证失败时回落到 `bootstrap_password_secret` 密码
- [ ] **AUTH-03**:Bootstrap-then-Key 步骤 3 — 都未配置时试 SSH agent + `~/.ssh/` 默认 key
- [ ] **AUTH-04**:网络/协议错误(`NoValidConnectionsError` / `socket.timeout` 等)立即抛出,不被包装为认证失败
- [ ] **AUTH-05**:报告里显示实际用上的认证方式(`auth_method: key | password | agent`)

## 连接 (CONN)

- [ ] **CONN-01**:支持 `jump.host` 走 SSH ProxyCommand 跳板
- [ ] **CONN-02**:支持 `--jump HOST` 覆盖 YAML 中的跳板
- [ ] **CONN-03**:支持 `--host H[:P]` 单台覆盖(YAML 中无该条时用占位 Server,无认证配置)
- [ ] **CONN-04**:连通性预检 `--check` 显示每台机器的 `auth_method` + `uname -a`

## 扫描 (SCAN)

- [ ] **SCAN-01**:枚举所有带 home 目录的用户(`getent passwd`)
- [ ] **SCAN-02**:对每个用户并行扫描(线程池,默认 4)
- [ ] **SCAN-03**:检测 `~/.codex/` 目录存在性及 `auth.json` 大小
- [ ] **SCAN-04**:在用户 shell 启动文件(`.bashrc` `.zshrc` `.profile` 等)中 grep `OPENAI_*` / `CODEX_*` 环境变量
- [ ] **SCAN-05**:在 shell history(`.bash_history` `.zsh_history`)中 grep `codex` / `openai` / `sk-` 痕迹
- [ ] **SCAN-06**:在 `~/.npmrc` 中查 `_authToken`
- [ ] **SCAN-07**:扫描 `/etc/environment` / `/etc/profile` / `/etc/profile.d/*` 系统级配置
- [ ] **SCAN-08**:查全局 npm 包 `codex` / `@openai/codex` / `@openai/codex-cli`
- [ ] **SCAN-09**:查 pip 包 `codex`
- [ ] **SCAN-10**:查 `command -v codex` 可执行文件
- [ ] **SCAN-11**:扫描所有运行中 Docker 容器(`.codex/` + env + 全局 npm)
- [ ] **SCAN-12**:列出停止中 Docker 容器的 volume 挂载点

## 报告 (RPT)

- [ ] **RPT-01**:终端分级报告(高/中/低/信息)
- [ ] **RPT-02**:JSON 报告落盘(`output: findings.json`)
- [ ] **RPT-03**:敏感值脱敏(首 4 + 末 2)
- [ ] **RPT-04**:扫描进度写到 stderr,报告写到 stdout
- [ ] **RPT-05**:按 severity 倒序展示
- [ ] **RPT-06**:报告里显示 `server_name` (人类可读) 而非仅 `host:port`
- [ ] **RPT-07**:JSON 里包含 `auth_method` (key/password/agent) 字段
- [ ] **RPT-08**:每台机器进度行显示 `[auth=...]` 摘要

## 用户体验 (UX)

- [ ] **UX-01**:连通性预检 `--check` 不执行扫描
- [ ] **UX-02**:列出解析到的服务器 `--list-servers`,带认证方式摘要
- [ ] **UX-03**:生成 `servers.yaml` 模板 `--init` / `--init-to PATH`

## 错误处理 (ERR)

- [ ] **ERR-01**:单台主机失败不中断整体扫描(继续扫下一台)
- [ ] **ERR-02**:单用户扫描失败不影响其他用户
- [ ] **ERR-03**:沉默 paramiko 内部 traceback,只显示用户级错误
- [ ] **ERR-04**:单条 server 配置错误(缺 host / 非法 port)时跳过该条,继续处理其他条
- [ ] **ERR-05**:YAML 语法错误时给出文件名 + 行号 + 列号

## v2(暂缓)

- 扫描 k8s config / pod 环境变量
- 扫描 systemd `EnvironmentFile=`
- 扫描 `~/.aws/credentials` 关联的 OpenAI 凭据(可能性低)
- 多主机并行(目前串行,够用)
- 输出 Markdown 报告
- `--diff <baseline.json>` 模式:对比两次扫描的差异

## Out of Scope

- **写/改远程文件** — 工具只读
- **导出明文凭证** — 用户自己 follow-up
- **Windows 远程主机** — 仅 POSIX
- **持续监控 / 定时任务** — 一次性盘点
- **Web UI** — CLI 足够
- **凭据自动轮换** — bootstrap 是一次性人工流程

## Traceability

| Phase | 覆盖需求 |
|-------|---------|
| Phase 1(MVP 扫描工具) | CFG-01~04, AUTH-01~05, CONN-01~04, SCAN-01~12, RPT-01~08, UX-01~03, ERR-01~05 |
