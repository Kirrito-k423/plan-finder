# codex_finder

跨 SSH 扫描 Codex CLI 凭证残留工具。在多台远程服务器上登录(每台可独立配置认证),遍历所有用户、home 目录、Docker 容器、npm/pip 全局安装、shell history,定位与 Codex / OpenAI 相关的凭证文件和环境变量。

> **定位**:一次性盘点工具,用于找回被遗忘在远程机器上的 Codex 凭证。请勿在生产环境长期挂载运行。扫描本身只读取元数据,报告中所有凭证值已脱敏。

## 功能

- **YAML 配置**:`servers.yaml` 列出每台机器的 `name` / `host` / `user` / `identity_file` / `bootstrap_password_secret`,格式与 autoresearch 的 `servers` schema 兼容 (D-03 Bootstrap-then-Key)
- **SSH 认证流程**:
  1. 先尝试 `identity_file` 公钥认证(同时启用 SSH agent)
  2. 失败回落到 `bootstrap_password_secret` 密码认证
  3. 都没有时尝试 agent + `~/.ssh/` 默认 key
- **跳板支持**:`jump.host` 或 `--jump HOST` 走 SSH ProxyCommand
- **覆盖范围**:
  - `~/.codex/` 目录及 `auth.json` / `config.toml` 元信息
  - 用户 shell 启动文件(`.bashrc` / `.zshrc` / `.profile` 等)中的 `OPENAI_API_KEY` 等,**定位到具体文件 + 行号**
  - `/etc/environment`、`/etc/profile`、`/etc/profile.d/*` 等系统级配置
  - `~/.bash_history` / `~/.zsh_history` 中的 codex / openai 痕迹,**`export *KEY` 和 `codex login` 自动升级到 HIGH + 标记 URGENT**
  - `~/.npmrc` 中的 `_authToken`
  - 全局安装的 `codex` / `@openai/codex` (npm) 和 `codex` (pip)
  - **Docker 容器内**:running 容器内的 `~/.codex/`、env、全局 npm;stopped 容器列出 volume 挂载点
- **去重**:同 `(location, path, kind)` 合并,`user` 字段用逗号列所有匹配用户(operator 和 root 都用 `/root` 不会再出两份)
- **复制到 result_dir**:`--result-dir PATH`,把工件按 host/category 分类落到本地,目录 0700 + 文件 0600,`auth.json` / 实际 env 值都落盘(本地权限保护)
- **URGENT 子目录**:`export X_API_KEY` / `codex login` 这类 history 单独存到 `result_dir/URGENT/`,便于快速 triage
- **测试 key 有效性**:`--test-credentials`,对每个候选 key 调 `GET /v1/models` 验(200=有效,401=失效),自动读 `HTTPS_PROXY`/`TEST_PROXY` 走 7890 代理
- **脱敏输出**:终端/JSON 报告中所有凭证值只保留首 4 + 末 2 字符
- **JSON 报告**:`output: findings.json` 自动落盘
- **分级**:high / medium / low / info 四档,按降序展示

## 安装

项目使用 Python 3.8+、paramiko、PyYAML。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

国内网络可加代理:

```bash
HTTPS_PROXY=http://127.0.0.1:7890 .venv/bin/pip install -r requirements.txt
```

## 快速开始

### 1. 生成 servers.yaml

```bash
.venv/bin/python codex_finder.py --init-to servers.yaml
```

### 2. 编辑 servers.yaml

每台服务器独立配置认证:

```yaml
servers:
  - name: A2-AK-225
    host: 192.168.9.225
    port: 22
    user: root
    identity_file: ~/.ssh/id_ed25519
    # 首次连接一次性密码 (bootstrap 阶段用, 部署完 key 后可删)
    bootstrap_password_secret: "<填入或留空>"

  - name: A2-AK-176
    host: 192.168.9.102
    port: 22
    user: admin123            # 非 root 用户也可
    identity_file: ~/.ssh/id_ed25519
    bootstrap_password_secret: "<填入或留空>"

# --- 跳板机 (可选) ---
jump:
  host: 192.168.13.154
  port: 22
  user: root
  identity_file: ~/.ssh/id_ed25519
  # password: ""  # 跳板用密码时填这里(需本机有 sshpass)

# --- 扫描行为 (可省,均有默认值) ---
scan:
  parallel: 4
  scan_docker: true
  scan_history: true
  scan_npm_pip: true
  verbose: false

# --- JSON 报告 (留空不输出) ---
output: findings.json
```

**重要**:`servers.yaml` 已在 `.gitignore` 中,不要 commit。`bootstrap_password_secret` 是 bootstrap 阶段的一次性密码,key 部署完后应删除该字段并轮换真实密码。

### 3. 连通性预检

```bash
.venv/bin/python codex_finder.py --check
```

逐台尝试 SSH,显示用 `key` / `password` / `agent` 哪种方式连上,打印 `uname -a`,不执行扫描。`--list-servers` 可以不连任何机器就列出所有目标。

### 4. 扫描

```bash
# 从本机直接连(走 SSH 密钥 / agent / bootstrap 密码)
.venv/bin/python codex_finder.py

# 通过跳板机(覆盖 YAML 中的 jump.host)
.venv/bin/python codex_finder.py --jump 192.168.13.154

# 只扫单台(覆盖 YAML)
.venv/bin/python codex_finder.py --host 10.0.0.5:2222

# 把工件复制到 ./findings,按 host/category 分类(mode 0700)
.venv/bin/python codex_finder.py --result-dir ./findings

# 对每个候选 key 测是否还有效(自动走 7890 代理)
export TEST_PROXY=http://127.0.0.1:7890
.venv/bin/python codex_finder.py --test-credentials

# 三个一起
.venv/bin/python codex_finder.py --result-dir ./findings --test-credentials

# 关闭彩色输出
.venv/bin/python codex_finder.py --no-color
```

报告示例(终端):

```
→ 扫描 A2-AK-225 (root@192.168.9.225:22)  [key✓(id_ed25519)+pwd✓]
  ✓ 连接成功 [auth=key]  uname: Linux server1 6.1.0...
  [用户] 发现 3 个账号 (并行度 4)
  [docker] 发现 5 个容器
══════════════════════════════════════════════
  Codex 凭证扫描报告
══════════════════════════════════════════════
  扫描主机: 5    失败: 0    命中: 2 高 / 1 中 / 0 低 / 8 信息

▸ A2-AK-225 (root@192.168.9.225:22)  (4 项)
    [HIGH  ] <host_home> root  /root/.codex
             .codex/ 目录存在 (4 项, auth.json=有(312B))
    [HIGH  ] <host_home> alice  <shell rc>
             OPENAI_API_KEY=sk-1T***8z
    [MEDIUM] <host_npm>  root  /usr/lib/node_modules/@openai/codex
             npm 全局包: @openai/codex
```

JSON 报告(由 `output: findings.json` 控制):

```json
{
  "generated": "2026-06-10T10:50:00",
  "results": [
    {
      "server": "A2-AK-225 (root@192.168.9.225:22)",
      "server_name": "A2-AK-225",
      "host": "192.168.9.225",
      "port": 22,
      "auth_method": "key",
      "findings": [
        {
          "server": "A2-AK-225 (root@192.168.9.225:22)",
          "location": "host_home",
          "user": "root",
          "path": "/root/.codex",
          "kind": "codex_dir",
          "severity": "high",
          "detail": ".codex/ 目录存在 (4 项, auth.json=有(312B))",
          "meta": { "auth_json_size": 312 }
        }
      ]
    }
  ],
  "errors": []
}
```

## 凭据测试 & 可复现脚本

`--test-credentials`(默认 ON)对每个 key 并行打 DEFAULT + YAML + config.toml + `~/.codex_finder/discovered_providers.json` 里的所有 OpenAI 兼容中转站。

**响应体默认打**:body 前 140 字符(无论 200/401/issue)都打,方便肉眼判断是不是 quota 假成功。用 `--no-show-body` 关闭。

**结果写三处**:
- `findings/<ts>/<host>/test_results.json` — per-host 完整测试结果(含 body_preview + issue)
- `findings/<ts>/<host>/test_scripts/test_<hash>.py` — per-key 独立脚本(mode 0600)
- `findings/<ts>/<host>/access_plan.json` + `access_plan/access_plan_<key_hash>.json` — **真实可用**的 (key × provider) 组合

**可复现脚本例子** (`test_3a4f9b2c8d1e.py`):

```bash
cd findings/20260610-110000/192.168.9.225-22/test_scripts
TEST_PROXY=http://127.0.0.1:7890 python3 test_3a4f9b2c8d1e.py
# stdout: {"results": {"openai": {"valid": true}, "zhipu": {"valid": false}, ...}, ...}
# exit 0: 全部测完(可能含 None = 网络问题)
# exit 2: 有 provider 确认失效(401/403)
# exit 3: 有 provider 200 但 body 有 issue (quota/expired)
```

**access_plan.json — 真实可用的 (key × provider) 组合**:

每个 host 生成:
- `access_plan.json` — 该 host 所有"真有效"组合(200 + body 无 issue)的汇总
- `access_plan/access_plan_<key_hash>.json` — per-key 详情,含可复跑 test_script 路径

```jsonc
// access_plan.json (per-host)
{
  "server": "A3-AK-182 (root@192.168.13.182:22)",
  "providers_count": 22,
  "valid_combinations_count": 3,
  "combinations": [
    {
      "key_hash": "7450481bef1a",
      "key_preview": "sk-1T***8z",
      "key_source_server": "A3-AK-182 (...)",
      "key_source_path": "/home/operator/.codex",
      "key_source_kind": "codex_dir",
      "key_source_user": "operator",
      "provider": "openai",
      "provider_url": "https://api.openai.com/v1",
      "status": 200,
      "body_preview": "{\"object\":\"list\",\"data\":[...]}",
    },
    // ...
  ],
}
```

```jsonc
// access_plan/access_plan_7450481bef1a.json (per-key)
{
  "key_hash": "7450481bef1a",
  "key_preview": "sk-1T***8z",
  "test_script": "test_scripts/test_7450481bef1a.py",
  "providers": {
    "openai": { "url": "...", "status": 200, "body_preview": "..." },
    "yunwu":  { "url": "...", "status": 200, "body_preview": "..." }
  }
}
```

用途:
- **路由决策**: 多个 key 多个 provider,这张表告诉你哪个 key 调哪个 endpoint
- **清理死 key**: `providers` 为空 → 这 key 在所有测过的 provider 上都失效了,去 OpenAI 控制台 revoke
- **轮换验证**: revoke 之后 `python3 test_<hash>.py` 复跑,access_plan 里的 `status` 应变 401

**持续发现新 provider**:
- 每次跑,`config.toml` 里 `model_providers` 解析出的新 `base_url` 自动加入
- 跑完写回 `~/.codex_finder/discovered_providers.json`(mode 0600)
- 下次跑自动加载 + 一起测

**已内置 21 个 provider**:
openai, deepseek, zhipu, moonshot, dashscope, openrouter, anthropic, gemini, mistral, groq, perplexity, siliconflow, yi, baichuan, stepfun, minimax, **bobdong, tokenshop, yunwu, xcode, taijiai**

要在 YAML 临时加:
```yaml
scan:
  providers:
    my_proxy: https://my-proxy.example.com/v1
```

## 配置项

### YAML 字段(servers.yaml)

| 字段 | 默认 | 说明 |
|------|------|------|
| `result_dir` | _(空)_ | 扫描完后把工件复制到此目录 |
| `test_credentials` | `false` | 对每个 key 调 `/v1/models` 测有效性 |

### result_dir 布局

```
findings/
└── 20260610-110000/                    # 每次扫描一个时间戳子目录
    ├── 192.168.9.225-22/                 # 每台主机一个目录
    │   ├── codex_dir/
    │   │   └── root_.codex/              # 递归拷,保留子目录和真实文件名
    │   │       ├── auth.json
    │   │       ├── auth (api).json
    │   │       ├── auth/
    │   │       │   └── api.json
    │   │       ├── config.toml
    │   │       └── _meta.json             # 总结(user/items/providers)
    │   ├── env_var/
    │   │   └── root_.bashrc_OPENAI_API_KEY_L42.txt
    │   ├── history/
    │   ├── npm_pkg/
    │   ├── pip_pkg/
    │   ├── binary/
    │   ├── test_scripts/                 # 每 key 一个可独立复跑的脚本
    │   │   ├── test_3a4f9b2c8d1e.py
    │   │   └── ...
    │   ├── test_results.json              # 完整测试结果 (per-host,含 body+issue)
    │   ├── access_plan.json               # 真实可用 (key×provider) 组合汇总 (per-host)
    │   └── access_plan/                  # per-key access_plan 详情
    │       ├── access_plan_3a4f9b2c8d1e.json
    │       └── ...
    ├── URGENT/                           # export *KEY / codex login
    └── findings.json                     # 全量结构化报告
```

所有文件 mode 0600,目录 mode 0700。`test_scripts/test_<hash>.py` 是 self-contained 的——轮换/重新生成 key 后可以 `python3 test_<hash>.py` 复跑,不需要再连远程。

### 命令行参数

| 参数 | 覆盖 YAML | 说明 |
|------|-----------|------|
| `--config PATH` | — | YAML 配置文件 |
| `--result-dir PATH` | `result_dir` | 复制工件到本地 |
| `--test-credentials` | `test_credentials: true` | 测 key 有效性(默认 ON) |
| `--no-test-credentials` | `test_credentials: false` | 关闭测 key |
| `--no-show-body` | — | 不打印每个 provider 响应体(默认打) |
| `--start-stopped-containers` | `scan.start_stopped_containers` | 启动 stopped 容器再扫 |
| `--jump HOST` | `jump.host` | 跳板机 |
| `--host H[:P]` | — | 单台覆盖 |
| `--check` | — | 只测连通性 |
| `--list-servers` | — | 列出所有 server + 认证摘要 |
| `--init-to PATH` | — | 生成模板 |

### 环境变量

| 变量 | 覆盖 |
|------|------|
| `TEST_PROXY` / `HTTPS_PROXY` / `https_proxy` | `--test-credentials` 走代理(中国用户填 127.0.0.1:7890) |
| `RESULT_DIR` | `result_dir` |
| `OUTPUT` | `output` |
| `PARALLEL` / `CMD_TIMEOUT` / `SCAN_DOCKER` / `SCAN_HISTORY` / `SCAN_NPM_PIP` / `VERBOSE` | `scan.*` |

### servers[] 字段

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `name` | 否 | `host` | 人类可读的服务器名,出现在报告里 |
| `host` | 是 | — | IP 或域名 |
| `port` | 否 | `22` | SSH 端口 |
| `user` | 否 | `root` | SSH 用户 |
| `identity_file` | 否 | `~/.ssh` | 公钥路径(支持 `~`),**不存在时跳过 key 阶段** |
| `bootstrap_password_secret` | 否 | _(空)_ | 首次连接密码(仅在 key 失败时使用) |

### 全局字段

| 字段 | 默认 | 说明 |
|------|------|------|
| `jump.host` / `jump.port` / `jump.user` / `jump.identity_file` / `jump.password` | _全空_ | 跳板机配置,启用后所有目标走 ProxyCommand |
| `scan.parallel` | `4` | 单主机内并行扫描用户的线程数 |
| `scan.cmd_timeout` | `15` | 单个远程命令超时(秒) |
| `scan.scan_docker` | `true` | 是否扫描 Docker |
| `scan.scan_history` | `true` | 是否扫 shell history |
| `scan.scan_npm_pip` | `true` | 是否扫全局 npm/pip |
| `scan.verbose` | `false` | 详细日志 |
| `output` | _(空)_ | JSON 报告路径 |

### 环境变量覆盖

进程环境可覆盖 YAML 中的可调项(优先级: 环境 > YAML > 默认):

- `PARALLEL`、`CMD_TIMEOUT`、`SCAN_DOCKER`、`SCAN_HISTORY`、`SCAN_NPM_PIP`、`VERBOSE`、`OUTPUT`

## 工作原理(单台主机)

1. `getent passwd` 枚举所有带 home 目录的账号
2. 对每个用户并行执行:
   - `ls ~/.codex/` 并检测 `auth.json` 大小
   - `grep` 用户 shell 启动文件中 `OPENAI_API_KEY` / `CODEX_API_KEY` 等
   - `grep` shell history 中的 `codex` / `openai` / `sk-`
   - `grep` `~/.npmrc` 中的 `_authToken`
3. 系统级:`grep` `/etc/environment` / `/etc/profile` / `/etc/profile.d/*`
4. 全局包:`npm root -g` 查 `codex` / `@openai/codex`,`pip show codex`,`command -v codex`
5. Docker:
   - running 容器:`docker exec` 进容器内做与主机相同的子集扫描
   - stopped 容器:`docker inspect` 列出 volume 挂载点供人工 follow-up

## 常见问题

**Q: 跳板用 key、目标用密码,反之亦然,可以吗?**
A: 可以。`jump.*` 和每个 `servers[].*` 独立配置,任意组合都行。

**Q: Bootstrap 密码在 key 已部署后忘了删怎么办?**
A: 强烈建议 `key` 部署完成后把 `bootstrap_password_secret` 那一行删掉,并在远端轮换该密码。脚本会优先用 key,所以保留密码字段本身不会泄露,但多一份明文备份就多一份风险。

**Q: Docker 容器里没有 bash 怎么办?**
A: 脚本用 `sh -c` 兼容 POSIX,只要容器内有 `sh` 即可,`getent` / `env` 失败时不会中断。

**Q: 跳板机需要密码,需要装 sshpass 吗?**
A: 跳板机用密码时脚本会自动调用 `sshpass`(未安装则失败,届时 `brew install sshpass`)。目标机密码由 paramiko 直接处理,不依赖 `sshpass`。

**Q: 扫描会不会改远程状态?**
A: **不会**。所有命令都是只读的(`ls` / `grep` / `test` / `getent` / `env` / `docker inspect` / `docker exec <只读子集>`)。

**Q: `--host` 单机覆盖时,认证怎么走?**
A: `--host` 创建一个无认证的占位 Server(只设 `host`/`port`,`user=root`),不传 `identity_file` 也不传 `password`,所以会落到第 3 步:用 SSH agent 或 `~/.ssh/` 默认 key。如果目标需要密码,建议在 `servers.yaml` 里配。

**Q: `--test-credentials` 在国内跑会怎么样?**
A: api.openai.com 在中国被墙,直接跑会全部 `None`(无法判断)。设 `export TEST_PROXY=http://127.0.0.1:7890` 走代理即可。如果代理是 PAC-only 或 socks5,需要转成 HTTP 代理(7890 一般是 HTTP)。

**Q: result_dir 里的文件是脱敏的吗?**
A: **不是**。这是有意为之——脱敏了你没法 `codex auth login --key=...` 直接用。文件权限 0600 防止同机其他用户读,但你本机一旦被入侵,这些明文 key 就泄露了。处理完后建议手动 `rm -rf findings/` 或整个 `findings/` 加进 `.gitignore`(本项目已加)。

## 开发

```bash
# 语法检查
.venv/bin/python -m py_compile codex_finder.py

# 模板生成 + 配置解析验证
.venv/bin/python codex_finder.py --init-to /tmp/test.yaml
.venv/bin/python codex_finder.py --config /tmp/test.yaml --list-servers
```

## 单点 OpenAI 凭证测试 (`test_openai_auth.py`)

`findings/.../codex_dir/operator_.codex/auth (api).json` 扫出来之后,通常还想当场验证“这份 token 还能不能调通 OpenAI”。`test_openai_auth.py` 是一个独立脚本,只依赖 Python 3.8+ 标准库,默认读取 `auth.json` 并对 `https://api.openai.com/v1/responses` 发一个最小请求。

### 用法

```bash
# 1) 直接用 OPENAI_API_KEY 测
.venv/bin/python test_openai_auth.py --auth auth.json --auth-type api-key

# 2) 用 ChatGPT 登录态 (tokens.access_token) 测
TEST_PROXY=http://127.0.0.1:7890 .venv/bin/python test_openai_auth.py \
  --auth auth.json --auth-type chatgpt --show-token-info

# 3) 先用 refresh_token 换新 access_token 再测
TEST_PROXY=http://127.0.0.1:7890 .venv/bin/python test_openai_auth.py \
  --auth auth.json --auth-type chatgpt --refresh --show-token-info

# 4) 刷新成功并把新 token 原子写回 auth.json
TEST_PROXY=http://127.0.0.1:7890 .venv/bin/python test_openai_auth.py \
  --auth auth.json --auth-type chatgpt --refresh --write-back

# 5) 纯本地解码 JWT,不发任何请求(看 exp/aud/scp/email/organizations)
.venv/bin/python test_openai_auth.py --auth auth.json --decode-token all
```

### 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--auth PATH` | `auth.json` | auth.json 路径 |
| `--auth-type {auto,api-key,chatgpt}` | `auto` | 凭证来源。`chatgpt` 用 `tokens.access_token` |
| `--model MODEL` | `gpt-4o-mini` | 调用模型 |
| `--prompt PROMPT` | `Reply with exactly: ok` | 测试 prompt |
| `--proxy PROXY` / `TEST_PROXY` | _空_ | 走 HTTP(S) 代理,国内用 `http://127.0.0.1:7890` |
| `--timeout N` | `30` | 请求超时秒 |
| `--raw` | _关_ | 打印完整 JSON 响应而不是文本 |
| `--refresh` | _关_ | 用 `tokens.refresh_token` 换新 access_token 后再测 |
| `--refresh-scope S` | `openid profile email offline_access api.responses.write` | OAuth 刷新时请求的 scope(注: 实际能否下发取决于账号当初登录时是否同意) |
| `--write-back` | _关_ | 与 `--refresh` 配合,原子写回 `auth.json` (tmp + rename, mode 0600) |
| `--show-token-info` | _关_ | 在 stderr 打印 `exp / aud / scp` |
| `--check-scopes` | _关_ | 只解码 JWT 输出 scopes,不发请求 |
| `--decode-token {access,id,refresh,all}` | _空_ | 纯本地解码 JWT,输出 `time_claims / identity / openai / scopes`,不调任何网络 |
| `--no-account-id` | _关_ | 不发 `chatgpt-account-id` 头(默认会用 `tokens.account_id`) |

### 401 / scope 错误诊断

`/v1/responses` 报 `401 insufficient permissions: Missing scopes: api.responses.write` 是 ChatGPT 登录态最常见的错误。脚本会:

1. 在 `--check-scopes` / `--decode-token` 阶段打印 token 的 `scp`,让你提前看到是否带 `api.responses.write`
2. 401 时在 stderr 单独打一行结构化警告:
   ```
   error=insufficient_scope; required=api.responses.write; http=401
   ```
3. token 缺少该 scope 时,即使走 `--refresh` 也没用 — Codex 公开 client_id `app_EMoamEEZ73f0CkXaXp7hrann` 在刷新时**不重新授予 scope**,新 token 的 `scp` 跟原 token 一致。

**唯一能让 token 拿到 `api.responses.write` 的办法**是:在远程机器上 `codex logout && codex login`,浏览器里走完一次同意页,新写入的 `~/.codex/auth.json` 才会带 API scope。

### 安全

- `auth.json` / `OPENAI_API_KEY` / `refresh_token` 都是高敏数据,本项目已加进 `.gitignore`
- 脚本不会打印 access_token / refresh_token 本体
- `--decode-token refresh` 只显示前 6 字符 + 总长度
- 用完含明文凭证的 `findings/` 目录请 `rm -rf`,并把它加进全局 `.gitignore`


## 安全备忘

- `servers.yaml` 必须加进 `.gitignore`(本项目模板已加)
- 不要再把 `bootstrap_password_secret` 或真实密码贴到聊天/工单里
- 报告里的 `sk-...` 都已脱敏为 `sk-XXXX**XX`,但路径可见,人 review 时请确认
- `findings/` 目录含明文凭证(权限 0600),用完后请 `rm -rf`,并把它加进全局 `.gitignore`
- 扫描只在 root 读得动的范围有效,如果 Codex 装在非 root 用户但 home 不可读会漏掉

## 许可

仅供个人盘点使用。扫描他人机器前请确保你有权访问。
