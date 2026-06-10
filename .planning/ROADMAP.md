# Roadmap

## Phase 1: MVP 扫描工具(当前)

**Goal**:实现 `codex_finder.py` 单文件 CLI,从 `servers.yaml` 读取服务器清单(每台独立配置 identity_file + bootstrap_password_secret),扫描 Codex 凭证残留,输出分级报告 + JSON。

**Mode**:mvp(端到端,先能用)

**Success Criteria**:
1. `.venv/bin/python codex_finder.py --init-to servers.yaml` 生成符合 autoresearch schema 的模板
2. `--list-servers` 列出所有 server 条目及其 `auth_summary`(key/pwd 是否有)
3. `--check` 在可达主机上输出 `[auth=...]` + `uname -a`,不可达主机输出根因错误(网络超时与认证失败分开)
4. 真实场景下 SSH 走 Bootstrap-then-Key 流程:先 key,失败回落密码,都成功
5. `--jump HOST` 走 SSH ProxyCommand 跳板
6. `--host H` 覆盖 YAML 中的 SERVERS
7. 全量扫描对单台主机:
   - 列出所有用户并并行扫描
   - 找到 `~/.codex/` 目录时输出 high 级别条目
   - 找到 `OPENAI_API_KEY=...` 时输出 high 级别条目(脱敏)
   - 找到 `codex` 全局 npm/pip 包时输出 medium 级别条目
   - 扫描所有 running Docker 容器并 exec 进容器内查 `.codex/` + env
   - 列出 stopped 容器的 volume 挂载点
8. `output: findings.json` 落盘结构化 JSON(含 `auth_method` 字段)
9. 单台主机/单用户失败不影响其他扫描
10. YAML 语法错误 / 单条 server 配置错误 / 缺 host 等情况有清晰错误信息

**Requirements 覆盖**:CFG-01~04, AUTH-01~05, CONN-01~04, SCAN-01~12, RPT-01~08, UX-01~03, ERR-01~05

**Status**:✅ 已实现(待真实环境验证)

**实现说明**:
- `RemoteExecutor.connect()` 实现 Bootstrap-then-Key 三步
  - step 1: `key_filename=identity_file` + `allow_agent=True` + `look_for_keys=True`
  - step 2(仅在 step 1 报 `AuthenticationException` 后):`password=bootstrap_password_secret`,禁用 agent
  - step 3(仅当 identity_file 和 password 都为空):`allow_agent=True` + `look_for_keys=True`
  - 网络错误(`NoValidConnectionsError` / `socket.timeout` 等)在任何阶段都自然抛出,不被包装为认证失败

---

## Phase 2: 真实环境验证(后续)

**Goal**:在用户的 5 台机器上实际跑一次,补全遗漏的扫描位置,修复实际遇到的问题。

**Success Criteria**:
1. 用真实 `servers.yaml` 跑通 `--check` 和全量扫描
2. 报告中的所有 finding 用户确认过路径有效
3. 发现的真实凭证位置被用户人工清理
4. 脚本中任何实际遇到的 bug 修复
5. 清理所有机器的 `bootstrap_password_secret`(替换为正式 key + 弱密码轮换)

**触发条件**:用户决定首次运行

---

## Phase 3: 增强(暂缓)

- 扫描 k8s pod 配置
- 扫描 systemd `EnvironmentFile=`
- 多主机并行(目前串行,单台内部并行)
- Markdown 报告
- `--diff <baseline.json>` 模式:对比两次扫描的差异
- 从 `~/.bash_history` 里把 `codex login` / `codex auth` 这类关键命令抓出来单独高亮
