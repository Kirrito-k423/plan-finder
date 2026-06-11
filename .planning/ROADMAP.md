# Roadmap

## Phase 1: MVP 扫描工具 ✅

**Goal**:从 `servers.yaml` 读取服务器列表(每台独立配置 identity_file + bootstrap_password_secret),扫描 Codex 凭证残留,输出分级报告 + JSON + 可复现测试脚本。

**Mode**:mvp(端到端,先能用)

**实现内容**:
- **连接层**: `servers.yaml` 配置 + Bootstrap-then-Key (key→password) + 跳板机 ProxyCommand + 网络/认证错误分离
- **扫描层**: `~/.codex/` (含 auth/ 子目录,真实文件名) / shell rc / system env / npm / pip / Docker (running+stopped 可启停)
- **报告层**: 去重 / URGENT 标记 / time-stamped result_dir (mode 0600/0700)
- **测试层**: 21+ 个 OpenAI 兼容中转站 (DEFAULT_PROVIDERS + 用户 YAML + ~/.codex/config.toml + 持久化 discovered_providers.json) 并行测,返回 {provider: True/False/None} 矩阵
- **可复现层**: 每 key 一个 `test_scripts/test_<hash>.py` self-contained 脚本 + `test_results.json` 报告
- **access_plan 层**: `access_plan.json` (per-host 汇总) + `access_plan/access_plan_<key_hash>.json` (per-key 详情) 记录真实可用的 (key × provider) 组合,含 body_preview
- **响应体捕获**: `_call` 返 `{valid, status, body_preview, issue}`,`_check_body_for_issue` 扫 quota/expired/rate_limit 等假成功
- **失败模式**: body 200 + `{"error":{"code":"insufficient_quota"}}` 标 `⚠` 而不是 `✓`

**Status**:✅ 已实现 + 推到 https://github.com/Kirrito-k423/plan-finder

**已内置 providers (21)**:
- 通用: openai, deepseek, zhipu, moonshot, dashscope, openrouter, anthropic, gemini, mistral, groq, perplexity, siliconflow, yi, baichuan, stepfun, minimax
- 中转站: **bobdong, tokenshop, yunwu, xcode, taijiai**

---

## Phase 2: 真实环境验证(后续)

**Goal**:在用户的 5 台机器上实际跑一次,补全遗漏的扫描位置,修复实际遇到的问题。

**Success Criteria**:
1. 用真实 `servers.yaml` 跑通 `--check` 和全量扫描
2. 报告中的所有 finding 用户确认过路径有效
3. 发现的真实凭证位置被用户人工清理
4. 脚本中任何实际遇到的 bug 修复
5. 清理所有机器的 `bootstrap_password_secret`(替换为正式 key + 弱密码轮换)
6. 用 `test_scripts/test_<hash>.py` 复跑关键 key,确认 OpenAI 控制台 revoke 后脚本输出 invalid

**触发条件**:用户决定首次运行

---

## Phase 3: 增强(暂缓)

- 扫描 k8s pod 配置
- 扫描 systemd `EnvironmentFile=`
- 多主机并行(目前串行,单台内部并行)
- Markdown 报告
- `--diff <baseline.json>` 模式:对比两次扫描的差异
- 从 `~/.bash_history` 里把 `codex login` / `codex auth` 这类关键命令抓出来单独高亮
- `discovered_providers.json` 加 TTL 机制(过期自动淘汰)
