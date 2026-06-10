#!/usr/bin/env python3
"""
codex_finder.py — 跨 SSH 扫描 Codex CLI 凭证残留工具

读取项目下 servers.yaml 中的服务器列表,登录每台目标遍历所有用户、home 目录、
Docker 容器、npm/pip 全局安装、shell history,查找 Codex / OpenAI 相关凭证残留。

支持模式:
    --local         : 从本机直接 SSH(默认)
    --jump HOST     : 通过指定跳板机访问所有目标
    --host H        : 只扫描指定的一台(覆盖配置文件)
    --init-to PATH  : 在指定路径生成 servers.yaml 模板
    --check         : 只测试连通性,不扫描

凭据安全(重要):
    - servers.yaml 含 bootstrap_password_secret,必须加入 .gitignore
    - 报告中所有凭证值只保留首 4 + 末 2 字符
    - 远程仅做只读扫描(ls / grep / test / getent / env / docker inspect),
      不修改任何远程状态
    - 不会自动导出/转发凭证内容,需要 follow-up 时请用 scp 单独拉取

SSH 认证流程(D-03 Bootstrap-then-Key):
    1. 先尝试 SSH agent / identity_file 公钥认证
    2. 失败则用 bootstrap_password_secret 密码兜底
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime
import hashlib
import json
import os
import re
import shlex
import sys
import textwrap
from pathlib import Path
from typing import Any

try:
    import paramiko
except ImportError:
    sys.stderr.write(
        "[错误] 缺少依赖 paramiko,请先安装:\n"
        "    pip install -r requirements.txt\n"
        "如果在国内网络,可设置代理:\n"
        "    HTTPS_PROXY=http://127.0.0.1:7890 pip install -r requirements.txt\n"
    )
    sys.exit(1)

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "[错误] 缺少依赖 PyYAML,请先安装:\n"
        "    pip install -r requirements.txt\n"
    )
    sys.exit(1)

# 静默 paramiko 内部的 traceback(它会把 socket.timeout 当 Exception 打到 stderr)
import logging
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)


# ============================================================
# 常量
# ============================================================

CRED_KEYWORDS = (
    "OPENAI_API_KEY",
    "OPENAI_KEY",
    "CODEX_API_KEY",
    "CODEX_TOKEN",
    "OPENAI_TOKEN",
    "OPENAI_ORG_ID",
    "OPENAI_BASE_URL",
    "CODEX_HOME",
)

HISTORY_KEYWORDS = ("codex", "openai", "sk-", "api.openai.com")

# History 里的"高危"模式:export 一个 *_KEY/_TOKEN/_SECRET,或 codex 命令带 --api-key
EXPORT_CREDS_RE = re.compile(
    r"(?:^|\s)export\s+[A-Za-z_][A-Za-z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET)[A-Za-z0-9_]*"
    r"\s*=\s*(\S+)",
    re.IGNORECASE,
)
CODEX_CMD_CREDS_RE = re.compile(
    r"\bcodex\b.*?(?:--?api[_-]?key[=\s]+|--?token[=\s]+|--?key[=\s]+)(\S+)",
    re.IGNORECASE,
)


def is_urgent_history_line(line: str) -> bool:
    """是否包含 export 一个 *_KEY/_TOKEN/_SECRET,或 codex 命令带 --api-key"""
    return bool(EXPORT_CREDS_RE.search(line) or CODEX_CMD_CREDS_RE.search(line))


# ============================================================
# Provider registry (config.toml 里的 model_providers + 已知中转站)
# ============================================================

# 已知 OpenAI 兼容中转站,凭据本地测试时都试一遍
# 新加的 provider 会被自动持久化到 ~/.codex_finder/discovered_providers.json
# 下次启动自动加载
DEFAULT_PROVIDERS: dict[str, str] = {
    "openai":       "https://api.openai.com/v1",
    "deepseek":     "https://api.deepseek.com/v1",
    "zhipu":        "https://open.bigmodel.cn/api/paas/v4",
    "moonshot":     "https://api.moonshot.cn/v1",
    "dashscope":    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openrouter":   "https://openrouter.ai/api/v1",
    "anthropic":    "https://api.anthropic.com/v1",
    "gemini":       "https://generativelanguage.googleapis.com/v1beta",
    "mistral":      "https://api.mistral.ai/v1",
    "groq":         "https://api.groq.com/openai/v1",
    "perplexity":   "https://api.perplexity.ai",
    "siliconflow":  "https://api.siliconflow.cn/v1",
    "yi":           "https://api.lingyiwanwu.com/v1",
    "baichuan":     "https://api.baichuan-ai.com/v1",
    "stepfun":      "https://api.stepfun.com/v1",
    "minimax":     "https://api.MiniMax.chat/v1",
    # 中转站 / 第三方 OpenAI 兼容 (持续更新)
    "bobdong":      "https://bobdong.cn/v1",
    "tokenshop":    "https://api.tokenshop.homes/v1",
    "yunwu":        "https://yunwu.ai/v1",
    "xcode":        "https://xcode.best/v1",
    "taijiai":      "https://www.taijiai.online/v1",
}

# 环境变量名 → 优先测试的 provider(用名字 hint 加速)
KEY_PROVIDER_HINTS: dict[str, str] = {
    "OPENAI_API_KEY":      "openai",
    "OPENAI_KEY":          "openai",
    "CODEX_API_KEY":       "openai",
    "OPENAI_TOKEN":        "openai",
    "DEEPSEEK_API_KEY":    "deepseek",
    "ZHIPU_API_KEY":       "zhipu",
    "GLM_API_KEY":         "zhipu",
    "MOONSHOT_API_KEY":    "moonshot",
    "DASHSCOPE_API_KEY":   "dashscope",
    "QWEN_API_KEY":        "dashscope",
    "OPENROUTER_API_KEY":  "openrouter",
    "ANTHROPIC_API_KEY":   "anthropic",
    "GEMINI_API_KEY":      "gemini",
    "GOOGLE_API_KEY":      "gemini",
    "MISTRAL_API_KEY":     "mistral",
    "GROQ_API_KEY":        "groq",
    "PERPLEXITY_API_KEY":  "perplexity",
    "SILICONFLOW_API_KEY": "siliconflow",
    "YI_API_KEY":          "yi",
    "BAICHUAN_API_KEY":    "baichuan",
    "STEPFUN_API_KEY":     "stepfun",
    "MiniMax_API_KEY":    "MiniMax",
    "CUSTOM_OPENAI_API_KEY": "openai",  # 用户截图里出现的
}


def parse_model_providers(toml_text: str) -> dict[str, str]:
    """从 config.toml 文本里抽 model_providers.{name}.base_url。
    策略:
      1. 先找 `model_providers = { ... }` 块,再解析其内部 `name = { base_url = "..." }` 条目
      2. 兜底:任何顶层 `name = { base_url = "..." }` 块
    内部条目 body 允许一层嵌套(覆盖 `query_params = { ... }` 之类)
    """
    providers: dict[str, str] = {}
    if not toml_text:
        return providers

    # 内部条目正则: 允许 body 内有 {x=y} 这种单层嵌套
    INNER = re.compile(
        r'(\w+)\s*=\s*\{((?:[^{}]|\{[^{}]*\})*)\}'
    )

    def _scan_body(body: str) -> None:
        for m in INNER.finditer(body):
            name = m.group(1)
            inner = m.group(2)
            bm = re.search(r'base_url\s*=\s*["\']([^"\']+)["\']', inner)
            if bm:
                providers[name] = bm.group(1)

    # 1) 优先找 model_providers = { ... } 块
    outer = re.search(
        r'model_providers\s*=\s*\{((?:[^{}]|\{[^{}]*\})*)\}',
        toml_text,
        re.DOTALL,
    )
    if outer:
        _scan_body(outer.group(1))
    else:
        # 2) 兜底:扫整个文本
        _scan_body(toml_text)
    return providers


def merge_providers(*sources: dict[str, str]) -> dict[str, str]:
    """合并多个 provider 字典,后到的覆盖先到的(base_url 相同时)."""
    out: dict[str, str] = {}
    for src in sources:
        if not src:
            continue
        for k, v in src.items():
            if isinstance(k, str) and isinstance(v, str) and k and v:
                out[k] = v
    return out


def pick_primary_provider(key_hint: str | None, providers: dict[str, str]) -> str | None:
    """根据 key 名字(env var name)选最可能的 provider 用于优先测试。"""
    if not key_hint:
        return None
    up = key_hint.upper()
    if up in KEY_PROVIDER_HINTS:
        name = KEY_PROVIDER_HINTS[up]
        if name in providers:
            return name
    # 模糊:env var 名里有 provider 名字
    for prov in providers:
        if prov.upper() in up:
            return prov
    return None


# ============================================================
# 持久化"用户发现的新 provider"到 ~/.codex_finder/discovered_providers.json
# 每次跑自动加载,新发现的 base_url 自动写入(去重、按发现时间排序)
# ============================================================

def _discovered_path() -> Path:
    return Path.home() / ".codex_finder" / "discovered_providers.json"


def load_discovered_providers() -> dict[str, str]:
    """从 ~/.codex_finder/discovered_providers.json 加载"""
    p = _discovered_path()
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {
            str(k).strip(): str(v).strip()
            for k, v in raw.items()
            if str(k).strip() and str(v).strip()
        }
    except Exception:
        return {}


def save_discovered_providers(providers: dict[str, str]) -> None:
    """把 providers 写回 ~/.codex_finder/discovered_providers.json"""
    p = _discovered_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.write_text(
            json.dumps(providers, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(p, 0o600)
    except Exception as e:
        PROGRESS.write(f"[警告] 写 discovered_providers.json 失败: {e}\n")


def name_for_discovered_url(url: str, existing: dict[str, str]) -> str:
    """从 base_url 派生一个简短名字(name 不重复则返回,重复则加 -2 -3...)"""
    from urllib.parse import urlparse
    try:
        host = urlparse(url.rstrip("/")).hostname or "unknown"
    except Exception:
        host = "unknown"
    # 取主域(去掉 .com/.cn/.ai 等 TLD)
    parts = host.split(".")
    stem = parts[-2] if len(parts) >= 2 else host
    # 去掉 api. / www. 前缀
    stem = stem.removeprefix("api.")
    base = stem.replace("-", "_")
    name = base
    i = 2
    while name in existing:
        name = f"{base}_{i}"
        i += 1
    return name


_DISCOVERED_PATH = _discovered_path()  # backward compat (某些测试可能还引用)

# 用户的 shell 启动文件
USER_SHELL_FILES = (
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".zshenv",
)

# 全局敏感配置文件
SYSTEM_ENV_FILES = (
    "/etc/environment",
    "/etc/profile",
    "/etc/bash.bashrc",
    "/etc/zsh/zshenv",
)

ENV_FILE_GLOBS = (
    "/etc/profile.d/*.sh",
    "/etc/profile.d/*.env",
    "~/.config/environment.d/*.conf",
)

PROGRESS = sys.stderr


# ============================================================
# 类型转换辅助
# ============================================================

def cfg_bool(val: Any, default: bool = False) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def cfg_int(val: Any, default: int) -> int:
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def cfg_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val).strip()


# ============================================================
# YAML 配置模型
# ============================================================

@dataclasses.dataclass
class Server:
    """单台目标服务器"""
    name: str
    host: str
    port: int = 22
    user: str = "root"
    identity_file: str = ""
    bootstrap_password_secret: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Server":
        if "host" not in d:
            raise ValueError(f"server entry missing required 'host': {d!r}")
        return cls(
            name=cfg_str(d.get("name"), d.get("host", "?")),
            host=cfg_str(d["host"]),
            port=cfg_int(d.get("port"), 22),
            user=cfg_str(d.get("user"), "root") or "root",
            identity_file=cfg_str(d.get("identity_file")),
            bootstrap_password_secret=cfg_str(d.get("bootstrap_password_secret")),
        )

    @property
    def short_label(self) -> str:
        return f"{self.name} ({self.user}@{self.host}:{self.port})"

    @property
    def host_port(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def has_key(self) -> bool:
        return bool(self.identity_file) and os.path.exists(os.path.expanduser(self.identity_file))

    @property
    def has_password(self) -> bool:
        return bool(self.bootstrap_password_secret)

    def auth_summary(self) -> str:
        """脱敏展示认证配置(给 --check / 错误信息用)"""
        parts: list[str] = []
        if self.identity_file:
            expanded = os.path.expanduser(self.identity_file)
            tag = "key✓" if os.path.exists(expanded) else "key✗"
            parts.append(f"{tag}({os.path.basename(expanded)})")
        if self.bootstrap_password_secret:
            parts.append("pwd✓")
        if not parts:
            return "agent-only"
        return "+".join(parts)


@dataclasses.dataclass
class JumpConfig:
    host: str = ""
    port: int = 22
    user: str = "root"
    identity_file: str = ""
    password: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "JumpConfig":
        d = d or {}
        return cls(
            host=cfg_str(d.get("host")),
            port=cfg_int(d.get("port"), 22),
            user=cfg_str(d.get("user"), "root") or "root",
            identity_file=cfg_str(d.get("identity_file")),
            password=cfg_str(d.get("password")),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def to_proxy_tuple(self) -> tuple | None:
        if not self.enabled:
            return None
        return (
            self.host,
            self.port,
            self.user,
            self.identity_file or None,
            self.password or None,
        )


@dataclasses.dataclass
class ScanOptions:
    parallel: int = 16
    cmd_timeout: int = 15
    scan_docker: bool = True
    scan_history: bool = True
    scan_npm_pip: bool = True
    verbose: bool = False
    start_stopped_containers: bool = False
    providers: dict[str, str] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ScanOptions":
        d = d or {}
        providers_raw = d.get("providers") or {}
        providers = {
            str(k).strip(): str(v).strip()
            for k, v in (providers_raw.items() if isinstance(providers_raw, dict) else [])
            if str(k).strip() and str(v).strip()
        }
        return cls(
            parallel=cfg_int(d.get("parallel"), 16),
            cmd_timeout=cfg_int(d.get("cmd_timeout"), 15),
            scan_docker=cfg_bool(d.get("scan_docker"), True),
            scan_history=cfg_bool(d.get("scan_history"), True),
            scan_npm_pip=cfg_bool(d.get("scan_npm_pip"), True),
            verbose=cfg_bool(d.get("verbose"), False),
            start_stopped_containers=cfg_bool(d.get("start_stopped_containers"), False),
            providers=providers,
        )

    def apply_env(self) -> None:
        """进程环境变量可覆盖 YAML 中的可调项"""
        if (v := os.environ.get("PARALLEL")) is not None:
            self.parallel = cfg_int(v, self.parallel)
        if (v := os.environ.get("CMD_TIMEOUT")) is not None:
            self.cmd_timeout = cfg_int(v, self.cmd_timeout)
        if (v := os.environ.get("VERBOSE")) is not None:
            self.verbose = cfg_bool(v, self.verbose)
        if (v := os.environ.get("SCAN_DOCKER")) is not None:
            self.scan_docker = cfg_bool(v, self.scan_docker)
        if (v := os.environ.get("SCAN_HISTORY")) is not None:
            self.scan_history = cfg_bool(v, self.scan_history)
        if (v := os.environ.get("SCAN_NPM_PIP")) is not None:
            self.scan_npm_pip = cfg_bool(v, self.scan_npm_pip)


@dataclasses.dataclass
class AppConfig:
    servers: list[Server]
    jump: JumpConfig
    scan: ScanOptions
    output: str = ""
    result_dir: str = "./findings"
    test_credentials: bool = True

    @classmethod
    def load(cls, path: str) -> "AppConfig":
        p = Path(path)
        if not p.exists():
            return cls(servers=[], jump=JumpConfig(), scan=ScanOptions())
        with p.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path} 顶层必须是 mapping,得到 {type(raw).__name__}")
        servers: list[Server] = []
        for entry in raw.get("servers") or []:
            if not isinstance(entry, dict):
                continue
            try:
                servers.append(Server.from_dict(entry))
            except ValueError as e:
                PROGRESS.write(f"[警告] 跳过非法 server 条目: {e}\n")
        cfg = cls(
            servers=servers,
            jump=JumpConfig.from_dict(raw.get("jump")),
            scan=ScanOptions.from_dict(raw.get("scan")),
            output=cfg_str(raw.get("output"), "findings.json"),
            result_dir=cfg_str(raw.get("result_dir"), "./findings"),
            test_credentials=cfg_bool(raw.get("test_credentials"), True),
        )
        cfg.scan.apply_env()
        if (v := os.environ.get("OUTPUT")) is not None:
            cfg.output = cfg_str(v, cfg.output)
        if (v := os.environ.get("RESULT_DIR")) is not None:
            cfg.result_dir = cfg_str(v, cfg.result_dir)
        if (v := os.environ.get("TEST_CREDENTIALS")) is not None:
            cfg.test_credentials = cfg_bool(v, cfg.test_credentials)
        if (v := os.environ.get("START_STOPPED_CONTAINERS")) is not None:
            cfg.scan.start_stopped_containers = cfg_bool(v, cfg.scan.start_stopped_containers)
        return cfg


# ============================================================
# 数据模型
# ============================================================

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "info": 0}


@dataclasses.dataclass
class Finding:
    server: str
    location: str  # host_home / host_system / host_npm / host_pip
                   # container_running / container_volumes / host_history
    user: str
    path: str
    kind: str  # codex_dir / env_var / npm_pkg / pip_pkg / binary
               # history_match / volume / error
    severity: str  # high / medium / low / info
    detail: str = ""
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ============================================================
# 去重
# ============================================================

def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """按 (location, path, kind) 去重。
    多个 user 共享同一路径(如 operator 和 root 都用 /root 当 home)会被合并。
    同 key 同文件的多行匹配也会合并;severity 取最高。
    """
    by_key: dict[tuple[str, str, str], Finding] = {}
    for f in findings:
        key = (f.location, f.path, f.kind)
        if key in by_key:
            existing = by_key[key]
            users = sorted({
                u.strip() for u in (
                    *existing.user.split(","),
                    *f.user.split(","),
                ) if u.strip()
            })
            existing.user = ", ".join(users)
            if SEVERITY_RANK.get(f.severity, 0) > SEVERITY_RANK.get(existing.severity, 0):
                existing.severity = f.severity
                existing.detail = f.detail
            # 合并 meta(后到的覆盖前到的同名 key)
            if f.meta:
                existing.meta = {**(existing.meta or {}), **f.meta}
        else:
            by_key[key] = f
    return list(by_key.values())


# ============================================================
# 脱敏
# ============================================================

class Redactor:
    SK_LIKE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")

    @classmethod
    def short(cls, val: str) -> str:
        """保留前 4 后 2,中间 ***"""
        if not val:
            return ""
        if len(val) <= 8:
            return "***"
        return f"{val[:4]}***{val[-2:]}"

    @classmethod
    def is_cred_key(cls, key: str) -> bool:
        up = key.upper()
        return any(k in up for k in ("KEY", "TOKEN", "SECRET", "PASSWORD"))

    @classmethod
    def value(cls, key: str, val: str) -> str:
        if cls.is_cred_key(key) or cls.SK_LIKE.search(val):
            return cls.short(val)
        return val

    @classmethod
    def line(cls, line: str) -> str:
        """对 shell history 行做轻量脱敏"""
        line = cls.SK_LIKE.sub(lambda m: cls.short(m.group(0)), line)
        line = re.sub(
            r"(--?api[_-]?key[=\s]+)([^\s\'\";&|]+)",
            r"\1***",
            line,
            flags=re.IGNORECASE,
        )
        line = re.sub(
            r"(OPENAI_API_KEY=)([^\s\'\";&|]+)",
            r"\1***",
            line,
        )
        return line


# ============================================================
# SSH 远程执行
# ============================================================

class RemoteExecutor:
    """对单台远程主机的命令执行封装,内置跳板 + Bootstrap-then-Key 认证"""

    def __init__(
        self,
        server: Server,
        jump: tuple | None = None,
        timeout: int = 10,
    ):
        self.server = server
        self.host = server.host
        self.port = server.port
        self.user = server.user
        self.key_path = (
            os.path.expanduser(server.identity_file)
            if server.identity_file else None
        )
        self.password = server.bootstrap_password_secret or None
        self.jump = jump
        self.timeout = timeout
        self._client: paramiko.SSHClient | None = None
        self._auth_method: str = ""  # 用于错误信息:key / password / agent

    @property
    def label(self) -> str:
        return self.server.short_label

    def _base_connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": self.timeout,
            "banner_timeout": self.timeout,
            "auth_timeout": self.timeout,
        }
        if self.jump:
            kwargs["sock"] = self._make_proxy_sock(self.jump)
        return kwargs

    def connect(self) -> None:
        """D-03 Bootstrap-then-Key: 先 key/agent,失败用密码。
        网络/协议错误立即抛出(不与认证失败混淆)。
        """
        last_auth_err: Exception | None = None

        # 第 1 步: 尝试 key + agent
        if self.key_path or self.server.identity_file:
            try:
                kwargs = self._base_connect_kwargs()
                kwargs["allow_agent"] = True
                kwargs["look_for_keys"] = True
                if self.key_path:
                    kwargs["key_filename"] = self.key_path
                self._client = self._new_client()
                self._client.connect(**kwargs)
                self._auth_method = "key" if self.key_path else "agent"
                return
            except paramiko.AuthenticationException as e:
                last_auth_err = e
                self._client = None
            # 网络/协议错误自然 raise

        # 第 2 步: 密码兜底 (只在 key 阶段报过认证错后才走)
        if last_auth_err is not None and self.password:
            try:
                kwargs = self._base_connect_kwargs()
                kwargs["allow_agent"] = False
                kwargs["look_for_keys"] = False
                kwargs["password"] = self.password
                self._client = self._new_client()
                self._client.connect(**kwargs)
                self._auth_method = "password"
                return
            except paramiko.AuthenticationException as e:
                last_auth_err = e
                self._client = None
            # 网络/协议错误自然 raise

        # 第 3 步: 啥都没有,试 agent + look_for_keys
        if not self.key_path and not self.password:
            try:
                kwargs = self._base_connect_kwargs()
                kwargs["allow_agent"] = True
                kwargs["look_for_keys"] = True
                self._client = self._new_client()
                self._client.connect(**kwargs)
                self._auth_method = "agent"
                return
            except paramiko.AuthenticationException as e:
                last_auth_err = e
                self._client = None
            # 网络/协议错误自然 raise

        # 走到这里说明三种方式都报过 AuthenticationException
        if last_auth_err is None:
            raise paramiko.AuthenticationException(
                f"{self.label}: 没有可用的认证方式 "
                f"(identity_file={self.key_path!r}, password={'***' if self.password else '<none>'})"
            )
        # 把根因(可能是底层 socket.timeout 等)展开
        cause = last_auth_err.__cause__ or last_auth_err.__context__
        cause_str = ""
        if cause and cause is not last_auth_err:
            cause_str = f" [cause: {type(cause).__name__}: {cause}]"
        raise paramiko.AuthenticationException(
            f"{self.label}: 所有认证方式均失败 "
            f"({type(last_auth_err).__name__}: {last_auth_err}){cause_str}"
        )

    def _new_client(self) -> paramiko.SSHClient:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        return c

    def _make_proxy_sock(self, jump: tuple):
        jh, jp, ju, jk, jpw = jump
        jk_expanded = os.path.expanduser(jk) if jk else None
        parts = [
            "ssh",
            "-W",
            "%h:%p",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            f"ConnectTimeout={self.timeout}",
            "-p",
            str(jp),
            f"{ju}@{jh}",
        ]
        if jk_expanded and os.path.exists(jk_expanded):
            parts.extend(["-i", jk_expanded])
        cmd = " ".join(shlex.quote(p) for p in parts)
        if jpw:
            cmd = f"sshpass -p {shlex.quote(jpw)} {cmd}"
        return paramiko.ProxyCommand(cmd)

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __enter__(self) -> "RemoteExecutor":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def run(self, cmd: str, timeout: int = 30) -> tuple[str, str, int]:
        """执行命令,返回 (stdout, stderr, rc)"""
        if not self._client:
            self.connect()
        assert self._client is not None
        try:
            stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()
            return out, err, rc
        except Exception as e:
            return "", f"[exec error] {e}", -1

    def is_alive(self) -> bool:
        if not self._client:
            return False
        try:
            transport = self._client.get_transport()
            return bool(transport and transport.is_active())
        except Exception:
            return False


# ============================================================
# 扫描器
# ============================================================

class HostScanner:
    # 开关属性名带 with_ 前缀,避免和同名方法(self.scan_docker)撞名
    def __init__(
        self,
        executor: RemoteExecutor,
        server_label: str,
        parallel: int = 16,
        cmd_timeout: int = 15,
        with_docker: bool = True,
        with_history: bool = True,
        with_npm_pip: bool = True,
        verbose: bool = False,
        start_stopped_containers: bool = False,
    ):
        self.ex = executor
        self.server = server_label
        self.parallel = max(1, parallel)
        self.cmd_timeout = cmd_timeout
        self.with_docker = with_docker
        self.with_history = with_history
        self.with_npm_pip = with_npm_pip
        self.verbose = verbose
        self.start_stopped_containers = start_stopped_containers
        self.verbose = verbose

    def log(self, msg: str) -> None:
        if self.verbose:
            PROGRESS.write(f"  [verbose] {msg}\n")

    def _progress(self, msg: str) -> None:
        PROGRESS.write(f"  {msg}\n")

    # ---------- 用户枚举 ----------

    def list_users(self) -> list[tuple[str, str]]:
        """返回 [(user, home), ...]"""
        cmd = (
            "(getent passwd 2>/dev/null || cat /etc/passwd) "
            "| awk -F: '$6 != \"\" {print $1\":\"$6}'"
        )
        out, _, _ = self.ex.run(cmd, timeout=10)
        users: list[tuple[str, str]] = []
        for line in out.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            u, _, h = line.partition(":")
            users.append((u, h))
        return users

    # ---------- 顶层 ----------

    def scan_all(self) -> list[Finding]:
        findings: list[Finding] = []
        users = self.list_users()
        total = len(users)
        self._progress(
            f"[用户] 发现 {total} 个账号 (并行度 {self.parallel})"
        )
        completed = 0
        last_reported = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.parallel
        ) as pool:
            futs = {
                pool.submit(self.scan_user_safe, u, h): u
                for u, h in users
            }
            for fut in concurrent.futures.as_completed(futs):
                u = futs[fut]
                try:
                    findings.extend(fut.result())
                except Exception as e:
                    findings.append(
                        Finding(
                            server=self.server,
                            location="host_home",
                            user=u,
                            path="?",
                            kind="error",
                            severity="info",
                            detail=f"扫描失败: {e}",
                        )
                    )
                completed += 1
                # 每 25% 或每 8 个报一次进度
                if (
                    completed == total
                    or completed - last_reported >= max(8, total // 4)
                ):
                    self._progress(
                        f"[用户] {completed}/{total} 已扫描"
                    )
                    last_reported = completed

        self._progress("[扫描] 系统级 env / npm / pip ...")
        findings.extend(self.scan_system_env())
        if self.with_npm_pip:
            findings.extend(self.scan_npm_pip_global())
        if self.with_docker:
            findings.extend(self.scan_docker())
        self._progress(f"[完成] 主机扫描结束,共 {len(findings)} 项")
        return findings

    def scan_user_safe(self, user: str, home: str) -> list[Finding]:
        try:
            return self.scan_user(user, home)
        except Exception as e:
            return [
                Finding(
                    server=self.server,
                    location="host_home",
                    user=user,
                    path=home,
                    kind="error",
                    severity="info",
                    detail=f"扫描异常: {e}",
                )
            ]

    # ---------- 单用户 ----------

    def scan_user(self, user: str, home: str) -> list[Finding]:
        out: list[Finding] = []
        out.extend(self._scan_codex_dir(user, home))
        out.extend(self._scan_user_env(user, home))
        if self.with_history:
            out.extend(self._scan_user_history(user, home))
        out.extend(self._scan_user_npmrc(user, home))
        return out

    def _shell_quote(self, s: str) -> str:
        # POSIX shell 单引号转义
        return "'" + s.replace("'", "'\"'\"'") + "'"

    def _expand_paths(self, base: str, names: list[str]) -> list[str]:
        return [f"{base.rstrip('/')}/{n}" for n in names]

    def _read_grep(self, pattern: str, paths: list[str]) -> list[tuple[str, str]]:
        """在 paths 上 grep pattern,返回 [(path, line), ...]"""
        if not paths:
            return []
        results: list[tuple[str, str]] = []
        # 一次性 grep 多个文件
        path_args = " ".join(self._shell_quote(p) for p in paths if p)
        if not path_args:
            return []
        cmd = (
            f"grep -hnE {self._shell_quote(pattern)} {path_args} 2>/dev/null"
        )
        out, _, _ = self.ex.run(cmd, timeout=self.cmd_timeout)
        for line in out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            results.append(("", line))  # path 留空,后面靠 meta 区分
        return results

    def _scan_codex_dir(self, user: str, home: str) -> list[Finding]:
        codex_dir = f"{home.rstrip('/')}/.codex"
        out, _, _ = self.ex.run(
            f"test -d {self._shell_quote(codex_dir)} && echo Y || echo N",
            timeout=5,
        )
        if "Y" not in out:
            return []

        # 顶层 ls -1(保留含空格/括号的真实文件名)
        ls_out, _, _ = self.ex.run(
            f"ls -1 {self._shell_quote(codex_dir)} 2>/dev/null", timeout=5
        )
        top_items = [
            ln.strip() for ln in ls_out.splitlines()
            if ln.strip() and not ln.startswith(".")
        ]

        # auth/ 子目录(新版 codex 把 token 分文件存到这里)
        auth_dir = f"{codex_dir}/auth"
        auth_sub_files: list[str] = []
        als_out, _, als_rc = self.ex.run(
            f"test -d {self._shell_quote(auth_dir)} && "
            f"ls -1 {self._shell_quote(auth_dir)} 2>/dev/null", timeout=5
        )
        if als_rc == 0 and als_out.strip() and "No such" not in als_out:
            for ln in als_out.splitlines():
                fn = ln.strip()
                if fn and not fn.startswith("."):
                    auth_sub_files.append(f"auth/{fn}")

        # 列出所有可能含凭证的 JSON 文件(顶层 + auth/ 子目录)
        candidate_jsons: list[str] = []
        for fn in top_items:
            if fn.lower().endswith(".json") and "auth" in fn.lower():
                candidate_jsons.append(fn)
        for fn in top_items:
            if fn.lower().endswith(".json") and "auth" not in fn.lower() and fn != "config.json":
                candidate_jsons.append(fn)
        for fn in auth_sub_files:
            if fn.lower().endswith(".json"):
                candidate_jsons.append(fn)
        # 兼容旧版 auth.json
        if "auth.json" in top_items and "auth.json" not in candidate_jsons:
            candidate_jsons.insert(0, "auth.json")

        # 取每个 JSON 的大小
        auth_files_meta: list[dict[str, Any]] = []
        for rel in candidate_jsons:
            p = f"{codex_dir}/{rel}"
            sz_out, _, _ = self.ex.run(
                f"test -f {self._shell_quote(p)} "
                f"&& wc -c < {self._shell_quote(p)} || echo 0",
                timeout=5,
            )
            try:
                sz = int(sz_out.strip().split()[0])
            except (ValueError, IndexError):
                sz = 0
            if sz > 0:
                auth_files_meta.append({"path": rel, "size": sz})

        # 读 config.toml 全文(用于 model_providers 解析)
        cfg_path = f"{codex_dir}/config.toml"
        cfg_out, _, _ = self.ex.run(
            f"test -f {self._shell_quote(cfg_path)} "
            f"&& cat {self._shell_quote(cfg_path)} 2>/dev/null | head -200",
            timeout=5,
        )
        user_providers = parse_model_providers(cfg_out)
        all_providers = merge_providers(DEFAULT_PROVIDERS, user_providers)

        # severity
        severity = "high" if auth_files_meta else "medium"
        if auth_files_meta:
            detail_auth = ", ".join(
                f"{a['path']}({a['size']}B)" for a in auth_files_meta
            )
        else:
            detail_auth = "无凭证 JSON"

        return [
            Finding(
                server=self.server,
                location="host_home",
                user=user,
                path=codex_dir,
                kind="codex_dir",
                severity=severity,
                detail=(
                    f".codex/ 目录存在 ({len(top_items)} 项, "
                    f"凭证 JSON: {detail_auth})"
                ),
                meta={
                    "items": top_items[:20],
                    "auth_subdir_files": auth_sub_files[:20],
                    "auth_files": auth_files_meta,
                    "config_toml_preview": cfg_out[:2000],
                    "model_providers": user_providers,
                    "all_providers": all_providers,
                },
            )
        ]

    def _scan_user_env(self, user: str, home: str) -> list[Finding]:
        out: list[Finding] = []
        candidates = self._expand_paths(home, list(USER_SHELL_FILES))
        # 也尝试 XDG 环境目录
        candidates.append(f"{home}/.config/environment.d")
        if not candidates:
            return out
        pat = r"^\s*(export\s+)?(" + "|".join(CRED_KEYWORDS) + r")\s*=\s*(.*)$"
        # 先展开可能存在的 .config/environment.d
        env_d = f"{home}/.config/environment.d"
        ls, _, _ = self.ex.run(
            f"ls {self._shell_quote(env_d)}/*.conf {self._shell_quote(env_d)}/*.env 2>/dev/null",
            timeout=5,
        )
        candidates.extend(p for p in ls.splitlines() if p.strip())
        # 去重
        seen: set[str] = set()
        candidates = [c for c in candidates if not (c in seen or seen.add(c))]
        # grep -HnE:带文件名 + 行号,这样能定位到具体哪个文件
        path_args = " ".join(self._shell_quote(p) for p in candidates)
        cmd = f"grep -HnE {self._shell_quote(pat)} {path_args} 2>/dev/null"
        gout, _, _ = self.ex.run(cmd, timeout=self.cmd_timeout)
        if not gout.strip():
            return out
        for line in gout.splitlines():
            # 格式: "<path>:<line>:<content>" 或 "<path>:<content>"
            m1 = re.match(r"^([^:]+):(\d+):(.*)$", line)
            m2 = re.match(r"^([^:]+):(.*)$", line) if not m1 else None
            if m1:
                fpath, lineno, content = m1.group(1), m1.group(2), m1.group(3)
            elif m2:
                fpath, content = m2.group(1), m2.group(2)
                lineno = "?"
            else:
                continue
            m = re.match(pat, content)
            if not m:
                continue
            key = m.group(2)
            val = m.group(3).strip().strip('"').strip("'")
            out.append(
                Finding(
                    server=self.server,
                    location="host_home",
                    user=user,
                    path=fpath,  # 真实文件路径(原 "<shell rc>")
                    kind="env_var",
                    severity="high",
                    detail=f"{key}={Redactor.value(key, val)}",
                    meta={"line": lineno, "raw": content},
                )
            )
        return out

    def _scan_user_history(self, user: str, home: str) -> list[Finding]:
        out: list[Finding] = []
        hist_files = [f"{home}/.bash_history", f"{home}/.zsh_history"]
        for hf in hist_files:
            kw_re = "|".join(re.escape(k) for k in HISTORY_KEYWORDS)
            cmd = (
                f"grep -niE '{kw_re}' {self._shell_quote(hf)} 2>/dev/null | head -8"
            )
            hout, _, _ = self.ex.run(cmd, timeout=self.cmd_timeout)
            if not hout.strip():
                continue
            for line in hout.splitlines()[:8]:
                # zsh history 格式: `: timestamp:duration;command`
                # 同时去掉 grep -n 加的 "数字:" 前缀
                content = re.sub(r"^\d+:\s*", "", line)
                content = re.sub(r"^:\s*\d+:\d+;", "", content)
                # 原始行(含 grep 行号),给 ResultFetcher 落地用
                raw_with_line = line
                content_redacted = Redactor.line(content)
                urgent = is_urgent_history_line(content)
                sev = "high" if urgent else "low"
                out.append(
                    Finding(
                        server=self.server,
                        location="host_history",
                        user=user,
                        path=hf,
                        kind="history_match",
                        severity=sev,
                        detail=content_redacted[:240],
                        meta={
                            "urgent": urgent,
                            "raw_line": content,
                            "raw_with_line": raw_with_line,
                        },
                    )
                )
        return out

    def _scan_user_npmrc(self, user: str, home: str) -> list[Finding]:
        out: list[Finding] = []
        npmrc = f"{home}/.npmrc"
        cmd = (
            f"grep -iE '(_authToken|//registry\\.npmjs\\.org/:_authToken|openai)' "
            f"{self._shell_quote(npmrc)} 2>/dev/null"
        )
        gout, _, _ = self.ex.run(cmd, timeout=5)
        for line in gout.splitlines():
            out.append(
                Finding(
                    server=self.server,
                    location="host_home",
                    user=user,
                    path=npmrc,
                    kind="npmrc",
                    severity="low",
                    detail=Redactor.line(line.strip()[:240]),
                )
            )
        return out

    # ---------- 系统级 ----------

    def scan_system_env(self) -> list[Finding]:
        out: list[Finding] = []
        paths: list[str] = list(SYSTEM_ENV_FILES)
        for g in ENV_FILE_GLOBS:
            ls, _, _ = self.ex.run(f"ls {g} 2>/dev/null", timeout=5)
            paths.extend(p.strip() for p in ls.splitlines() if p.strip())
        if not paths:
            return out
        pat = r"^\s*(export\s+)?(" + "|".join(CRED_KEYWORDS) + r")\s*=\s*(.*)$"
        path_args = " ".join(self._shell_quote(p) for p in paths)
        cmd = f"grep -HnE {self._shell_quote(pat)} {path_args} 2>/dev/null"
        gout, _, _ = self.ex.run(cmd, timeout=self.cmd_timeout)
        for line in gout.splitlines():
            m1 = re.match(r"^([^:]+):(\d+):(.*)$", line)
            m2 = re.match(r"^([^:]+):(.*)$", line) if not m1 else None
            if m1:
                fpath, lineno, content = m1.group(1), m1.group(2), m1.group(3)
            elif m2:
                fpath, content = m2.group(1), m2.group(2)
                lineno = "?"
            else:
                continue
            m = re.match(pat, content)
            if not m:
                continue
            key = m.group(2)
            val = m.group(3).strip().strip('"').strip("'")
            out.append(
                Finding(
                    server=self.server,
                    location="host_system",
                    user="root",
                    path=fpath,  # 真实文件路径
                    kind="env_var",
                    severity="high",
                    detail=f"{key}={Redactor.value(key, val)}",
                    meta={"line": lineno, "raw": content},
                )
            )
        return out

    def scan_npm_pip_global(self) -> list[Finding]:
        out: list[Finding] = []

        # npm global root
        npm_root, _, rc = self.ex.run("npm root -g 2>/dev/null", timeout=10)
        npm_root = npm_root.strip()
        if npm_root and rc == 0:
            for pkg_rel in ("codex", "@openai/codex", "@openai/codex-cli"):
                p = f"{npm_root.rstrip('/')}/{pkg_rel}"
                chk, _, _ = self.ex.run(
                    f"test -d {self._shell_quote(p)} && echo Y || echo N",
                    timeout=5,
                )
                if "Y" in chk:
                    pkg_json_out, _, _ = self.ex.run(
                        f"cat {self._shell_quote(p + '/package.json')} 2>/dev/null | head -40",
                        timeout=5,
                    )
                    out.append(
                        Finding(
                            server=self.server,
                            location="host_npm",
                            user="root",
                            path=p,
                            kind="npm_pkg",
                            severity="medium",
                            detail=f"npm 全局包: {pkg_rel}",
                            meta={"package_json_preview": pkg_json_out[:500]},
                        )
                    )

        # pip
        for cmd in ("pip show codex", "pip3 show codex"):
            po, _, prc = self.ex.run(cmd, timeout=10)
            if prc == 0 and "Name:" in po:
                loc = re.search(r"Location:\s*(\S+)", po)
                ver = re.search(r"Version:\s*(\S+)", po)
                out.append(
                    Finding(
                        server=self.server,
                        location="host_pip",
                        user="root",
                        path=loc.group(1) if loc else "?",
                        kind="pip_pkg",
                        severity="medium",
                        detail=f"pip 包 codex {ver.group(1) if ver else ''}".strip(),
                    )
                )

        # which codex
        wc, _, _ = self.ex.run(
            "command -v codex 2>/dev/null || which codex 2>/dev/null",
            timeout=5,
        )
        bin_path = wc.strip()
        if bin_path:
            out.append(
                Finding(
                    server=self.server,
                    location="host_npm",
                    user="root",
                    path=bin_path,
                    kind="binary",
                    severity="medium",
                    detail="codex 可执行文件",
                )
            )
        return out

    def scan_docker(self) -> list[Finding]:
        out: list[Finding] = []
        vout, _, vrc = self.ex.run(
            "docker version --format '{{.Server.Version}}' 2>/dev/null",
            timeout=10,
        )
        if vrc != 0 or not vout.strip():
            PROGRESS.write("  [docker] 不可用,跳过\n")
            return out

        ps_out, _, prc = self.ex.run(
            "docker ps -a --format '{{.ID}}|{{.Image}}|{{.State}}|{{.Names}}|{{.Status}}' 2>/dev/null",
            timeout=15,
        )
        if prc != 0 or not ps_out.strip():
            return out

        containers: list[dict[str, str]] = []
        for line in ps_out.splitlines():
            if "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            containers.append(
                {
                    "id": parts[0],
                    "image": parts[1],
                    "state": parts[2],
                    "name": parts[3],
                    "status": parts[4],
                }
            )
        if not containers:
            return out
        PROGRESS.write(f"  [docker] 发现 {len(containers)} 个容器\n")

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(4, len(containers))
        ) as pool:
            futs = []
            for c in containers:
                if c["state"] == "running":
                    futs.append(pool.submit(self._scan_running_container, c))
                else:
                    futs.append(pool.submit(self._scan_stopped_container, c))
            for f in futs:
                try:
                    out.extend(f.result())
                except Exception as e:
                    out.append(
                        Finding(
                            server=self.server,
                            location="container_running",
                            user="?",
                            path="?",
                            kind="error",
                            severity="info",
                            detail=f"docker 扫描失败: {e}",
                        )
                    )
        return out

    def _scan_running_container(self, c: dict[str, str]) -> list[Finding]:
        out: list[Finding] = []
        cid = c["id"]
        user_label = f"container:{cid[:12]}"

        # 用 sh 兼容,不一定有 bash
        # 1) 找 home 目录并查 .codex
        home_cmd = (
            f"docker exec {shlex.quote(cid)} sh -c "
            f"'ls -la /root/.codex /home/*/.codex 2>/dev/null | grep -E \"\\\\.codex$\" || true'"
        )
        hout, _, _ = self.ex.run(home_cmd, timeout=self.cmd_timeout)
        for line in hout.splitlines():
            if ".codex" not in line:
                continue
            parts = line.split()
            p = parts[-1] if parts else "?"
            out.append(
                Finding(
                    server=self.server,
                    location="container_running",
                    user=user_label,
                    path=p,
                    kind="codex_dir",
                    severity="high",
                    detail=f"容器 {c['name']} ({c['image']}) 内 .codex/",
                )
            )

        # 2) 容器内环境变量
        env_out, _, _ = self.ex.run(
            f"docker exec {shlex.quote(cid)} env 2>/dev/null",
            timeout=self.cmd_timeout,
        )
        pat = re.compile(rf"^({'|'.join(CRED_KEYWORDS)})=(.*)$")
        for line in env_out.splitlines():
            m = pat.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            out.append(
                Finding(
                    server=self.server,
                    location="container_running",
                    user=user_label,
                    path="<container-env>",
                    kind="env_var",
                    severity="high",
                    detail=f"{key}={Redactor.value(key, val)}",
                )
            )

        # 3) 全局 npm (容器内)
        npm_root, _, _ = self.ex.run(
            f"docker exec {shlex.quote(cid)} sh -c "
            f"'npm root -g 2>/dev/null || echo'",
            timeout=self.cmd_timeout,
        )
        nr = npm_root.strip()
        if nr and " " not in nr:
            for pkg_rel in ("codex", "@openai/codex", "@openai/codex-cli"):
                p = f"{nr.rstrip('/')}/{pkg_rel}"
                chk, _, _ = self.ex.run(
                    f"docker exec {shlex.quote(cid)} sh -c "
                    f"'test -d {shlex.quote(p)} && echo Y || echo N'",
                    timeout=5,
                )
                if "Y" in chk:
                    out.append(
                        Finding(
                            server=self.server,
                            location="container_running",
                            user=user_label,
                            path=p,
                            kind="npm_pkg",
                            severity="medium",
                            detail=f"容器内 npm 全局包: {pkg_rel}",
                        )
                    )
        return out

    def _scan_stopped_container(self, c: dict[str, str]) -> list[Finding]:
        out: list[Finding] = []
        cid = c["id"]
        user_label = f"container:{cid[:12]}(stopped)"
        ins, _, _ = self.ex.run(
            f"docker inspect --format '{{{{range .Mounts}}}}{{{{.Source}}}} -> "
            f"{{{{.Destination}}}}{{{{\"\\n\"}}}}{{{{end}}}}' {shlex.quote(cid)} 2>/dev/null",
            timeout=10,
        )
        for line in ins.splitlines():
            if " -> " not in line:
                continue
            src, _, dst = line.partition(" -> ")
            out.append(
                Finding(
                    server=self.server,
                    location="container_volumes",
                    user=user_label,
                    path=f"{src.strip()} -> {dst.strip()}",
                    kind="volume",
                    severity="info",
                    detail=f"容器 {c['name']} ({c['image']}) 停止中,卷挂载点",
                    meta={"image": c["image"], "name": c["name"]},
                )
            )

        # 尝试 docker start 启动后扫描(只对 'exited' 状态; 'dead'/'created' 跳过)
        if self.start_stopped and c["state"] == "exited":
            import time
            self._progress(
                f"[docker] 启动 {c['name']} ({cid[:12]}) 以便扫描 ..."
            )
            _, _, ssrc = self.ex.run(
                f"docker start {shlex.quote(cid)}", timeout=30
            )
            if ssrc == 0:
                time.sleep(2)  # 等容器起来
                # 复用 _scan_running_container 跑一遍
                c_running = dict(c)
                c_running["state"] = "running"
                # 临时改 user label,标记是被启动扫的
                running_findings = self._scan_running_container(c_running)
                started_label = f"container:{cid[:12]}(started-for-scan)"
                for f in running_findings:
                    f.user = started_label
                    f.meta = f.meta or {}
                    f.meta["started_for_scan"] = True
                out.extend(running_findings)
                self._progress(
                    f"[docker] 停止 {c['name']} (恢复原状) ..."
                )
                self.ex.run(
                    f"docker stop --time 5 {shlex.quote(cid)}",
                    timeout=15,
                )
            else:
                self._progress(
                    f"[docker] 启动失败,跳过 {c['name']}"
                )
        return out


# ============================================================
# Result Fetcher (把工件复制到 result_dir,分类保存)
# ============================================================

class ResultFetcher:
    """对每个 finding 调对应的 _fetch_xxx,把文件/匹配行复制到本地 result_dir。

    目录权限 0700,文件权限 0600。URGENT(history 中的 export / codex login)
    单独存到 result_dir/URGENT/ 下,便于快速 triage。
    """

    def __init__(
        self,
        executor: "RemoteExecutor",
        server_short: str,
        result_dir: str | os.PathLike,
    ):
        self.ex = executor
        self.server_short = server_short
        self.result_dir = Path(result_dir)
        self._sftp = None
        self._makedirs_done: set[str] = set()

    # ---------- 工具 ----------

    def _host_dir(self) -> Path:
        safe = re.sub(r"[/:\\]", "_", self.server_short)
        d = self.result_dir / safe
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def _urgent_dir(self) -> Path:
        d = self.result_dir / "URGENT"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def _safe_name(self, name: str) -> str:
        s = re.sub(r'[/\\:*?"<>|\s]', "_", name).strip("_")
        return s[:180] if s else "unnamed"

    def _write(self, local: Path, content: str | bytes) -> None:
        local.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if isinstance(content, str):
            local.write_text(content, encoding="utf-8")
        else:
            local.write_bytes(content)
        os.chmod(local, 0o600)

    def _append(self, local: Path, content: str) -> None:
        local.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with local.open("a", encoding="utf-8") as f:
            f.write(content)
        os.chmod(local, 0o600)

    def _get_sftp(self):
        if self._sftp is None and self.ex._client:
            try:
                self._sftp = self.ex._client.open_sftp()
            except Exception:
                self._sftp = None
        return self._sftp

    def _sftp_read(self, remote: str, max_bytes: int = 200_000) -> str | None:
        """通过 SFTP 读文件,限制大小避免拉爆。失败返回 None。"""
        sftp = self._get_sftp()
        if sftp is None:
            return None
        try:
            with sftp.open(remote, "rb") as f:
                data = f.read(max_bytes + 1)
            return data.decode("utf-8", errors="replace")
        except Exception:
            return None

    # ---------- 主入口 ----------

    def fetch(self, finding: Finding) -> Path | None:
        try:
            method = getattr(self, f"_fetch_{finding.kind}", None)
            if method is None:
                return None
            return method(finding)
        except Exception as e:
            finding.meta = finding.meta or {}
            finding.meta["fetch_error"] = f"{type(e).__name__}: {e}"
            return None

    # ---------- 各 kind 的具体复制 ----------

    def _canonical_user(self, finding: Finding) -> str:
        users = [u.strip() for u in finding.user.split(",") if u.strip()]
        # 多用户合并时,取第一个(字母序最小)
        return sorted(users)[0] if users else "unknown"

    def _fetch_codex_dir(self, f: Finding) -> Path | None:
        codex_dir = f.path.rstrip("/")
        user = self._canonical_user(f)
        out_dir = (
            self._host_dir() / "codex_dir" /
            self._safe_name(f"{user}_{Path(codex_dir).name}")
        )
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 递归列所有文件(包含 auth/ 子目录,真实文件名)
        find_out, _, _ = self.ex.run(
            f"find {shlex.quote(codex_dir)} -type f 2>/dev/null | head -100",
            timeout=10,
        )
        remotes = [
            ln.strip() for ln in find_out.splitlines()
            if ln.strip() and not ln.strip().endswith("/.")
        ]
        prefix = codex_dir.rstrip("/") + "/"
        for remote in remotes:
            if not remote.startswith(prefix):
                continue
            rel = remote[len(prefix):]
            if not rel or rel.startswith("."):
                continue
            # 跳过过大的(>1MB)
            content = self._sftp_read(remote, max_bytes=1_000_000)
            if content is not None:
                # 本地路径: 用 / 拼接,空格/括号保留(macOS 支持)
                local_rel = Path(rel)
                # 父目录保留
                local = out_dir / local_rel
                self._write(local, content)
        # 复制后写一个 _meta.json 总结(给后续 reader)
        meta = {
            "remote_dir": codex_dir,
            "user": user,
            "items": (f.meta or {}).get("items", []),
            "auth_files": (f.meta or {}).get("auth_files", []),
            "auth_subdir_files": (f.meta or {}).get("auth_subdir_files", []),
            "model_providers": (f.meta or {}).get("model_providers", {}),
        }
        try:
            (out_dir / "_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(out_dir / "_meta.json", 0o600)
        except Exception:
            pass
        f.meta = f.meta or {}
        f.meta["local_path"] = str(out_dir)
        return out_dir

    def _fetch_env_var(self, f: Finding) -> Path | None:
        key = f.detail.split("=", 1)[0]
        lineno = (f.meta or {}).get("line", "?")
        user = self._canonical_user(f)
        out_dir = self._host_dir() / "env_var"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fname = self._safe_name(
            f"{user}_{Path(f.path).name}_{key}_L{lineno}.txt"
        )
        local = out_dir / fname
        content = (
            f"# Remote: {user}@{self.server_short}\n"
            f"# File:   {f.path}:{lineno}\n"
            f"# Match:  {f.detail}\n"
            f"# Raw:    {(f.meta or {}).get('raw', '?')}\n"
        )
        self._write(local, content)
        f.meta = f.meta or {}
        f.meta["local_path"] = str(local)
        return local

    def _fetch_history_match(self, f: Finding) -> Path | None:
        urgent = (f.meta or {}).get("urgent", False)
        if urgent:
            out_dir = self._urgent_dir()
        else:
            out_dir = self._host_dir() / "history"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        user = self._canonical_user(f)
        prefix = "URGENT_" if urgent else ""
        fname = self._safe_name(f"{prefix}{user}_{Path(f.path).name}.txt")
        local = out_dir / fname
        # 同一 user 的 history 可能有多个匹配,append 模式
        block = (
            f"\n# {user}@{self.server_short}  severity={f.severity}  "
            f"urgent={urgent}\n"
            f"# raw: {(f.meta or {}).get('raw_with_line', f.detail)}\n"
            f"# redacted: {f.detail}\n"
        )
        self._append(local, block)
        f.meta = f.meta or {}
        f.meta["local_path"] = str(local)
        return local

    def _fetch_npm_pkg(self, f: Finding) -> Path | None:
        pkg_json = f.path.rstrip("/") + "/package.json"
        out_dir = self._host_dir() / "npm_pkg"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        local = out_dir / self._safe_name(
            f"{Path(f.path).parent.name}_{Path(f.path).name}_package.json"
        )
        content = self._sftp_read(pkg_json)
        if content is not None:
            self._write(local, content)
            f.meta = f.meta or {}
            f.meta["local_path"] = str(local)
            return local
        return None

    def _fetch_pip_pkg(self, f: Finding) -> Path | None:
        out_dir = self._host_dir() / "pip_pkg"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        local = out_dir / self._safe_name(
            f"codex_{Path(f.path).name}.txt"
        )
        content = (
            f"# Remote: {self.server_short}\n"
            f"# pip: codex\n"
            f"# Location: {f.path}\n"
            f"# Detail: {f.detail}\n"
        )
        self._write(local, content)
        f.meta = f.meta or {}
        f.meta["local_path"] = str(local)
        return local

    def _fetch_binary(self, f: Finding) -> Path | None:
        out_dir = self._host_dir() / "binary"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        local = out_dir / self._safe_name(f"codex_{Path(f.path).name}.txt")
        info, _, _ = self.ex.run(
            f"file {shlex.quote(f.path)} 2>/dev/null; "
            f"echo ---; "
            f"{shlex.quote(f.path)} --version 2>&1 | head -5",
            timeout=5,
        )
        content = (
            f"# Remote: {self.server_short}\n"
            f"# Path: {f.path}\n"
            f"---\n{info}\n"
        )
        self._write(local, content)
        f.meta = f.meta or {}
        f.meta["local_path"] = str(local)
        return local

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None


# ============================================================
# Credential Tester (GET /v1/models 验 key 是否还有效)
# ============================================================

class CredentialTester:
    """对每个 finding 抽取候选 key,对一组 providers 并行调 `{base_url}/models` 验证。

    返回 dict[provider_name, True/False/None]:
      - True:  200/类似成功(有效)
      - False: 401/403 类似鉴权失败(失效)
      - None:  网络/超时/其他(无法判断,可能中转站格式不同)

    自动读 TEST_PROXY / HTTPS_PROXY 环境变量(走本机 7890 代理)。
    同一个 key 的测试结果会被缓存,避免重复打。
    启动时从 ~/.codex_finder/discovered_providers.json 加载用户发现的新中转站。
    """

    PROBE_PATH = "/models"
    TIMEOUT = 10
    PARALLEL = 8

    def __init__(
        self,
        executor: "RemoteExecutor",
        providers: dict[str, str] | None = None,
        extra_providers_from_findings: dict[str, str] | None = None,
    ):
        self.ex = executor
        self._initial_providers = providers or {}  # 用于排除已知的
        self._discovered = load_discovered_providers()
        # 合并顺序:DEFAULT → discovered → 用户 YAML → 本次 finding 解析出的
        self.providers = merge_providers(
            DEFAULT_PROVIDERS,
            self._discovered,
            providers or {},
            extra_providers_from_findings or {},
        )
        # cache: key -> {provider_name -> {valid, status, body_preview, issue, error?}}
        self._cred_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._new_discovered: dict[str, str] = {}  # 本次新发现的,save 时写入

    def test(self, finding: Finding) -> tuple[str, dict[str, dict[str, Any]]] | None:
        """返回 (key, {provider_name: {valid, status, body_preview, issue, error?}})"""
        cred = self._extract(finding)
        if not cred:
            return None
        if cred in self._cred_cache:
            return cred, self._cred_cache[cred]
        if not self.providers:
            return cred, {}
        results: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.PARALLEL, len(self.providers))
        ) as pool:
            futs = {
                pool.submit(self._call, f"{url.rstrip('/')}{self.PROBE_PATH}", cred): name
                for name, url in self.providers.items()
            }
            for f in concurrent.futures.as_completed(futs):
                name = futs[f]
                try:
                    results[name] = f.result()
                except Exception as e:
                    results[name] = {
                        "valid": None,
                        "status": None,
                        "body_preview": "",
                        "issue": None,
                        "error": f"{type(e).__name__}: {e}",
                    }
        self._cred_cache[cred] = results

        # 收集本次新发现的 provider(测试过但不在 DEFAULT 也不在已持久化的)
        for name, url in self.providers.items():
            if name in DEFAULT_PROVIDERS:
                continue
            if name in self._discovered:
                continue
            if name in (self._initial_providers or {}):
                continue
            self._new_discovered[name] = url
        return cred, results

    def extract_key(self, finding: Finding) -> str | None:
        """公开版 _extract,给 scan_one_server 用(生成 test script 时拿 key)"""
        return self._extract(finding)

    def persist_discovered(self) -> None:
        """把本次新发现的 provider 写盘(~/.codex_finder/discovered_providers.json)"""
        if not self._new_discovered:
            return
        existing = load_discovered_providers()
        merged = merge_providers(existing, self._new_discovered)
        save_discovered_providers(merged)

    def generate_test_script(
        self,
        key: str,
        source_info: dict[str, Any] | None = None,
        script_path: Path | None = None,
    ) -> str:
        """生成独立可复现的测试脚本(返回脚本内容)。
        source_info 包含 {'server': ..., 'path': ..., 'line': ..., 'key_name': ...}
        脚本捕获响应体 (body_preview) 并检测 quota/rate_limit/expired 等"假成功"。
        """
        providers_json = json.dumps(
            {n: u for n, u in self.providers.items()},
            ensure_ascii=False, indent=2,
        )
        # 用 hash 避免 filename 泄露 key
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        meta_json = json.dumps(source_info or {}, ensure_ascii=False, indent=2)
        script = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reproducible credential test
  key_hash: {key_hash}
  source : {source_info or "(unknown)"}

可以重跑来再次验证(轮换/重新生成 key 后):
    python3 {script_path or "test_" + key_hash + ".py"}

输出(stdout): JSON {{
  results: {{
    provider_name: {{
      valid:     True / False / None,
      status:    200 / 401 / ... / null,
      body_preview: "<前 300 字符响应体>",
      issue:     null / "quota_issue" / "rate_limit" / "account_deactivated" / "invalid_in_body"
    }}
  }},
  issues:   ["<provider with issue>", ...],
  any_valid: bool,  any_invalid: bool
}}
exit 0: 全部测完(可能有 None = 网络问题)
exit 2: 有 provider 确认失效(401/403 或 body 显式 invalid)
exit 3: 有 provider 200 但 body 有 issue (quota/expired 等,需人工确认)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

KEY = {key!r}
PROXY = os.environ.get("TEST_PROXY") or os.environ.get("HTTPS_PROXY")
PROVIDERS = {providers_json}
SOURCE_META = {meta_json}
TIMEOUT = 10
PROBE_PATH = "/models"
MAX_BODY = 300


def check_issue(body: str, status: int) -> str | None:
    """检测 200 但 body 显式 invalid / quota / rate_limit / expired"""
    if status != 200 or not body:
        return None
    bl = body.lower()
    if '"error"' not in bl and '"code"' not in bl:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        for marker, issue in (
            ("quota exceeded", "quota_issue"),
            ("insufficient_quota", "quota_issue"),
            ("rate limit", "rate_limit"),
            ("too many requests", "rate_limit"),
            ("billing", "quota_issue"),
            ("account has been deactivated", "account_deactivated"),
            ("account has been deleted", "account_deactivated"),
            ("expired", "account_expired"),
        ):
            if marker in bl:
                return issue
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    code = str(err.get("code") or err.get("type") or "").lower()
    message = str(err.get("message") or "").lower()
    combined = f"{{code}} {{message}}"
    if any(k in combined for k in ("quota", "insufficient", "billing")):
        return "quota_issue"
    if any(k in combined for k in ("rate_limit", "rate limit", "too many")):
        return "rate_limit"
    if any(k in combined for k in ("deactivated", "deleted", "expired")):
        return "account_deactivated"
    if any(k in combined for k in ("invalid", "incorrect", "unauthorized", "wrong")):
        return "invalid_in_body"
    return None


def probe(name: str, base: str) -> dict[str, Any]:
    url = base.rstrip("/") + PROBE_PATH
    try:
        if PROXY:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({{"https": PROXY, "http": PROXY}})
            )
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(
            url, headers={{"Authorization": f"Bearer {{KEY}}"}}
        )
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read(2000)
            body = raw.decode("utf-8", errors="replace") if raw else ""
            status = resp.status
            issue = check_issue(body, status)
            return {{
                "valid": status == 200 and not issue,
                "status": status,
                "body_preview": body[:MAX_BODY].replace(chr(10), " "),
                "issue": issue,
            }}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            raw = e.read(2000)
            body = raw.decode("utf-8", errors="replace") if raw else ""
        except Exception:
            pass
        if e.code in (401, 403):
            return {{
                "valid": False,
                "status": e.code,
                "body_preview": body[:MAX_BODY].replace(chr(10), " "),
                "issue": None,
            }}
        return {{
            "valid": None,
            "status": e.code,
            "body_preview": body[:MAX_BODY].replace(chr(10), " "),
            "issue": None,
        }}
    except Exception as e:
        return {{
            "valid": None,
            "status": None,
            "body_preview": "",
            "issue": None,
            "error": f"{{type(e).__name__}}: {{e}}",
        }}


results: dict[str, Any] = {{}}
for name, base in PROVIDERS.items():
    results[name] = probe(name, base)

issues = [n for n, r in results.items() if r.get("issue")]
report = {{
    "key_hash": "{key_hash}",
    "source": SOURCE_META,
    "proxy": PROXY,
    "results": results,
    "issues": issues,
    "any_valid": any(r.get("valid") is True for r in results.values()),
    "any_invalid": any(r.get("valid") is False for r in results.values()),
    "any_issue": bool(issues),
}}
print(json.dumps(report, ensure_ascii=False, indent=2))
if report["any_invalid"]:
    sys.exit(2)
elif report["any_issue"]:
    sys.exit(3)
else:
    sys.exit(0)
'''
        return script

    def _extract(self, f: Finding) -> str | None:
        kind = f.kind
        if kind == "env_var":
            return self._extract_env_var(f)
        if kind == "codex_dir":
            return self._extract_auth_files(f)
        if kind == "history_match":
            return self._extract_history(f)
        return None

    def _extract_env_var(self, f: Finding) -> str | None:
        key = f.detail.split("=", 1)[0]
        # 从文件里直接 grep 出值
        cmd = (
            f"grep -hE {shlex.quote(key + '=')} {shlex.quote(f.path)} "
            f"2>/dev/null | head -1"
        )
        out, _, _ = self.ex.run(cmd, timeout=5)
        m = re.search(
            r"(?:^|\s)(?:export\s+)?" + re.escape(key) +
            r"""\s*=\s*["']?([^"'\s&;]+)""",
            out,
        )
        return m.group(1) if m else None

    def _extract_auth_files(self, f: Finding) -> str | None:
        """从 .codex/ 下所有 auth*.json (含 auth/ 子目录) 里抽 key"""
        codex_dir = f.path.rstrip("/")
        # 列出候选 JSON
        find_out, _, _ = self.ex.run(
            f"find {shlex.quote(codex_dir)} -maxdepth 2 -type f "
            f"-name 'auth*.json' 2>/dev/null | head -20",
            timeout=5,
        )
        candidates = [
            ln.strip() for ln in find_out.splitlines() if ln.strip()
        ]
        for path in candidates:
            out, _, _ = self.ex.run(
                f"cat {shlex.quote(path)} 2>/dev/null", timeout=5
            )
            if not out.strip():
                continue
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                continue
            key = self._key_from_dict(data)
            if key:
                return key
        return None

    @classmethod
    def _key_from_dict(cls, data: dict) -> str | None:
        for k in ("OPENAI_API_KEY", "api_key", "access_token", "token", "key"):
            v = data.get(k)
            if isinstance(v, str) and v:
                return v
        for v in data.values():
            if isinstance(v, dict):
                n = cls._key_from_dict(v)
                if n:
                    return n
        return None

    def _extract_history(self, f: Finding) -> str | None:
        content = (f.meta or {}).get("raw_line", f.detail)
        m = re.search(
            r"""export\s+\w*(?:API[_-]?KEY|TOKEN|SECRET)\w*\s*=\s*["']?([^"'\s&;]+)""",
            content,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)
        m = re.search(
            r"""--?api[_-]?key\s*[=\s]+["']?([^"'\s&;]+)""",
            content,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)
        m = re.search(
            r"""--?token\s*[=\s]+["']?([^"'\s&;]+)""",
            content,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)
        return None

    def _call(self, url: str, api_key: str) -> dict[str, Any]:
        """打一次 {base_url}/models,返回:
          {valid, status, body_preview, issue, error?}
        - valid: True=真有效, False=真失效(401/403 或 body 显式 invalid),
                 None=无法判断(网络/超时/429/5xx/quota 等需人工)
        - issue: 额外警示,如 quota_issue / rate_limit / account_expired / invalid_in_body
        """
        import urllib.request
        import urllib.error

        proxy = (
            os.environ.get("TEST_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
        )
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": proxy, "http": proxy})
            )
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            with opener.open(req, timeout=self.TIMEOUT) as resp:
                raw = resp.read(2000)
                body = raw.decode("utf-8", errors="replace") if raw else ""
                status = resp.status
                issue = self._check_body_for_issue(body, status)
                return {
                    "valid": status == 200 and not issue,
                    "status": status,
                    "body_preview": body[:300].replace("\n", " "),
                    "issue": issue,
                }
        except urllib.error.HTTPError as e:
            body = ""
            try:
                raw = e.read(2000)
                body = raw.decode("utf-8", errors="replace") if raw else ""
            except Exception:
                pass
            if e.code in (401, 403):
                return {
                    "valid": False,
                    "status": e.code,
                    "body_preview": body[:300].replace("\n", " "),
                    "issue": None,
                }
            return {
                "valid": None,
                "status": e.code,
                "body_preview": body[:300].replace("\n", " "),
                "issue": None,
            }
        except Exception as e:
            return {
                "valid": None,
                "status": None,
                "body_preview": "",
                "issue": None,
                "error": str(e),
            }

    @staticmethod
    def _check_body_for_issue(body: str, status: int) -> str | None:
        """从响应体里检测 quota / rate_limit / account_expired 等"假成功"。

        OpenAI 兼容 API 偶尔会 200 + body 内嵌 error(比如某些中转站)
        """
        if status != 200 or not body:
            return None
        bl = body.lower()

        # 1) 纯文本 fallback (非 JSON,但有相关关键词直接返回)
        for marker, issue in (
            ("quota exceeded", "quota_issue"),
            ("insufficient_quota", "quota_issue"),
            ("rate limit", "rate_limit"),
            ("too many requests", "rate_limit"),
            ("billing", "quota_issue"),
            ("account has been deactivated", "account_deactivated"),
            ("account has been deleted", "account_deactivated"),
            ("expired", "account_expired"),
        ):
            if marker in bl:
                return issue

        # 2) JSON 结构化检测
        if '"error"' not in bl and '"code"' not in bl:
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        err = data.get("error")
        if not isinstance(err, dict):
            return None
        code = str(err.get("code") or err.get("type") or "").lower()
        message = str(err.get("message") or "").lower()
        combined = f"{code} {message}"
        if any(k in combined for k in ("quota", "insufficient", "billing")):
            return "quota_issue"
        if any(k in combined for k in ("rate_limit", "rate limit", "too many")):
            return "rate_limit"
        if any(k in combined for k in ("deactivated", "deleted", "expired")):
            return "account_deactivated"
        if any(k in combined for k in ("invalid", "incorrect", "unauthorized", "wrong")):
            return "invalid_in_body"
        return None


# ============================================================
# 报告
# ============================================================

class Reporter:
    COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"

    SEV_COLOR = {
        "high": "\033[1;31m",  # bold red
        "medium": "\033[33m",  # yellow
        "low": "\033[36m",  # cyan
        "info": "\033[90m",  # gray
    }

    @classmethod
    def _c(cls, color: str, text: str) -> str:
        if not cls.COLOR:
            return text
        return f"{color}{text}{cls.RESET}"

    @classmethod
    def _format_valid_mark(cls, v: Any) -> str:
        """valid 字段可能是 bool/None 或 {provider: {valid, issue, ...}} 矩阵"""
        if isinstance(v, dict) and v:
            parts: list[str] = []
            for name, info in v.items():
                if isinstance(info, dict):
                    valid_val = info.get("valid")
                    issue = info.get("issue")
                    if valid_val is True and not issue:
                        parts.append(cls._c(cls.GREEN, f"{name}✓"))
                    elif valid_val is False:
                        parts.append(cls._c(cls.RED, f"{name}✗"))
                    elif issue:
                        parts.append(cls._c(cls.YELLOW, f"{name}⚠"))
                    else:
                        parts.append(cls._c(cls.GRAY, f"{name}?"))
                elif info is True:
                    parts.append(cls._c(cls.GREEN, f"{name}✓"))
                elif info is False:
                    parts.append(cls._c(cls.RED, f"{name}✗"))
                else:
                    parts.append(cls._c(cls.GRAY, f"{name}?"))
            return " " + cls._c(cls.BOLD, "[") + " ".join(parts) + cls._c(cls.BOLD, "]")
        if v is True:
            return " " + cls._c(cls.GREEN, "[✓ valid]")
        if v is False:
            return " " + cls._c(cls.RED, "[✗ invalid]")
        if v is None:
            return " " + cls._c(cls.GRAY, "[? unknown]")
        return ""

    @classmethod
    def print_summary(
        cls,
        results: list[dict[str, Any]],
        errors: list[dict[str, str]],
        show_body: bool = False,
    ) -> None:
        total = sum(len(r["findings"]) for r in results)
        by_sev: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "info": 0}
        # 有效性统计(对 dict-of-provider: 任意 provider True 且无 issue 算"valid")
        valid_cnt = {"valid": 0, "invalid": 0, "untested": 0, "issue": 0}
        for r in results:
            for f in r["findings"]:
                by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
                v = (f.get("meta") or {}).get("valid")
                has_issue = False
                if isinstance(v, dict):
                    values = []
                    for info in v.values():
                        if isinstance(info, dict):
                            values.append(info.get("valid"))
                            if info.get("issue"):
                                has_issue = True
                        else:
                            values.append(info)
                    if has_issue:
                        valid_cnt["issue"] += 1
                    elif any(x is True for x in values):
                        valid_cnt["valid"] += 1
                    elif any(x is False for x in values):
                        valid_cnt["invalid"] += 1
                    else:
                        valid_cnt["untested"] += 1
                elif v is True:
                    valid_cnt["valid"] += 1
                elif v is False:
                    valid_cnt["invalid"] += 1
                else:
                    valid_cnt["untested"] += 1

        print()
        print(cls._c(cls.BOLD, "══════════════════════════════════════════════"))
        print(cls._c(cls.BOLD, "  Codex 凭证扫描报告"))
        print(cls._c(cls.BOLD, "══════════════════════════════════════════════"))
        print(
            f"  扫描主机: {len(results)}    "
            f"失败: {len(errors)}    "
            f"命中: {cls._c(cls.SEV_COLOR['high'], str(by_sev['high']) + ' 高')}"
            f" / {cls._c(cls.SEV_COLOR['medium'], str(by_sev['medium']) + ' 中')}"
            f" / {cls._c(cls.SEV_COLOR['low'], str(by_sev['low']) + ' 低')}"
            f" / {cls._c(cls.SEV_COLOR['info'], str(by_sev['info']) + ' 信息')}"
        )
        if valid_cnt["valid"] or valid_cnt["invalid"] or valid_cnt["issue"]:
            print(
                f"  有效性: "
                f"{cls._c(cls.GREEN, '✓' + str(valid_cnt['valid']) + ' 有效')}"
                f" / {cls._c(cls.RED, '✗' + str(valid_cnt['invalid']) + ' 失效')}"
                + (
                    f" / {cls._c(cls.YELLOW, '⚠' + str(valid_cnt['issue']) + ' 假成功')}"
                    if valid_cnt["issue"]
                    else ""
                )
                + f" / {cls._c(cls.GRAY, str(valid_cnt['untested']) + ' 未测')}"
            )
        print()

        if errors:
            print(cls._c(cls.RED, "✗ 失败的主机:"))
            for e in errors:
                print(f"  · {e['server']}: {e['error']}")
            print()

        if not results:
            print(cls._c(cls.GRAY, "  (无结果)"))
            return

        for r in results:
            server = r["server"]
            findings = r["findings"]
            print(
                cls._c(cls.CYAN, f"▸ {server}")
                + cls._c(cls.GRAY, f"  ({len(findings)} 项)")
            )
            if not findings:
                print(cls._c(cls.GRAY, "    (无命中)"))
                continue
            # 按 severity 倒序
            findings_sorted = sorted(
                findings,
                key=lambda f: -SEVERITY_RANK.get(f["severity"], 0),
            )
            for f in findings_sorted:
                sev = f["severity"]
                tag = cls._c(
                    cls.SEV_COLOR[sev], f"[{sev.upper():<6}]"
                )
                loc = cls._c(cls.GRAY, f"<{f['location']}>")
                user = cls._c(cls.DIM, f"{f['user']}")
                urgent_mark = ""
                meta = f.get("meta") or {}
                if meta.get("urgent"):
                    urgent_mark = " " + cls._c(cls.RED + cls.BOLD, "🚨 URGENT")
                valid_mark = ""
                if "valid" in meta:
                    valid_mark = cls._format_valid_mark(meta["valid"])
                print(
                    f"    {tag} {loc} {user}  "
                    f"{cls._c(cls.BOLD, f['path'])}"
                    f"{urgent_mark}{valid_mark}"
                )
                detail = f.get("detail") or ""
                if detail:
                    print(
                        "           "
                        + cls._c(cls.GRAY, textwrap.shorten(detail, 200))
                    )
                if meta.get("local_path"):
                    print(
                        "           "
                        + cls._c(cls.CYAN, "↓ " + meta["local_path"])
                    )
                # 显示 body_preview:有 issue 的总是显示;show_body=True 时全部显示
                v = meta.get("valid")
                has_issue_any = False
                if isinstance(v, dict):
                    body_lines: list[str] = []
                    for prov, info in v.items():
                        if not isinstance(info, dict):
                            continue
                        issue = info.get("issue")
                        body_p = info.get("body_preview", "")
                        valid_v = info.get("valid")
                        if issue:
                            has_issue_any = True
                            body_lines.append(
                                f"  ⚠ {prov}  issue={issue}  body: {body_p[:140]}"
                            )
                        elif show_body and body_p:
                            body_lines.append(
                                f"  · {prov}  status={info.get('status')}  body: {body_p[:140]}"
                            )
                    if has_issue_any or show_body:
                        for bl in body_lines[:6]:
                            print(
                                "           "
                                + cls._c(cls.YELLOW if "⚠" in bl else cls.GRAY, bl)
                            )
                        if len(body_lines) > 6:
                            print(
                                "           "
                                + cls._c(cls.GRAY, f"  ...还有 {len(body_lines) - 6} 个")
                            )
            print()


# ============================================================
# 顶层流程
# ============================================================

def resolve_jump(jump_cfg: JumpConfig, cli_host: str | None) -> tuple | None:
    """CLI --jump 覆盖 YAML 中的 jump.host"""
    if cli_host:
        return (
            cli_host,
            jump_cfg.port,
            jump_cfg.user,
            jump_cfg.identity_file or None,
            jump_cfg.password or None,
        )
    return jump_cfg.to_proxy_tuple()


def make_synthetic_server(host_spec: str) -> Server:
    """从 --host HOST[:PORT] 构造一个无认证的占位 Server(走 agent/默认 key)"""
    h, _, p = host_spec.partition(":")
    port = int(p) if p else 22
    return Server(name="<cli>", host=h, port=port, user="root")


def scan_one_server(
    server: Server,
    opts: ScanOptions,
    jump: tuple | None,
    result_dir: str = "",
    test_credentials: bool = False,
) -> tuple[dict, dict | None]:
    PROGRESS.write(
        f"→ 扫描 {server.short_label}  [{server.auth_summary()}]\n"
    )
    started = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        executor = RemoteExecutor(server=server, jump=jump, timeout=10)
        with executor:
            scanner = HostScanner(
                executor=executor,
                server_label=server.short_label,
                parallel=opts.parallel,
                cmd_timeout=opts.cmd_timeout,
                with_docker=opts.scan_docker,
                with_history=opts.scan_history,
                with_npm_pip=opts.scan_npm_pip,
                verbose=opts.verbose,
                start_stopped_containers=opts.start_stopped_containers,
            )
            # 最小可达性
            out, _, rc = executor.run("uname -a", timeout=10)
            if rc != 0:
                return (
                    {"server": server.short_label, "started": started, "findings": []},
                    {"server": server.short_label,
                     "error": f"uname 失败: {out or '<empty>'}"},
                )
            PROGRESS.write(
                f"  ✓ 连接成功 [auth={executor._auth_method}]  "
                f"uname: {out.strip()[:100]}\n"
            )
            findings = scanner.scan_all()

            # 1) 去重(同 path + kind + location 合并)
            findings = dedupe_findings(findings)

            # 2) 复制到 result_dir
            fetcher = None
            if result_dir:
                fetcher = ResultFetcher(
                    executor, server.short_label, result_dir
                )
                for f in findings:
                    fetcher.fetch(f)
                if any(
                    f.meta and f.meta.get("fetch_error") for f in findings
                ):
                    PROGRESS.write(
                        f"  [result_dir] 写入失败 {sum(1 for f in findings if f.meta and f.meta.get('fetch_error'))} 项,"
                        f" 检查 stderr / local {result_dir}\n"
                    )
                urgent_n = sum(
                    1 for f in findings
                    if f.meta and f.meta.get("urgent")
                )
                if urgent_n:
                    PROGRESS.write(
                        f"  [URGENT] {urgent_n} 项高优 history 写入 {result_dir}/URGENT/\n"
                    )

            # 3) 测试 key 是否还有效(多 provider,返回含 body_preview + issue 的结构体)
            tested_n = 0
            valid_keys: set[str] = set()
            invalid_keys: set[str] = set()
            issue_providers: list[str] = []
            tested_credentials_meta: list[dict] = []
            tester: CredentialTester | None = None
            if test_credentials:
                host_providers: dict[str, str] = {}
                for f in findings:
                    if f.kind == "codex_dir":
                        mp = (f.meta or {}).get("model_providers") or {}
                        host_providers.update(mp)
                tester = CredentialTester(
                    executor,
                    providers=opts.providers,
                    extra_providers_from_findings=host_providers,
                )
                PROGRESS.write(
                    f"  [validity] 测试 {len(tester.providers)} 个 providers: "
                    f"{', '.join(list(tester.providers.keys())[:6])}"
                    f"{' ...' if len(tester.providers) > 6 else ''}\n"
                )
                for f in findings:
                    res = tester.test(f)
                    if res is None:
                        continue
                    key, r = res
                    if not r or not key:
                        continue
                    f.meta = f.meta or {}
                    f.meta["valid"] = r
                    f.meta["providers_tested"] = list(tester.providers.keys())
                    f.meta["key_hash"] = hashlib.sha256(key.encode()).hexdigest()[:12]
                    tested_n += 1
                    key_preview = (
                        f.detail.split("=", 1)[-1] if "=" in f.detail else f.detail
                    )
                    for prov, info in r.items():
                        if not isinstance(info, dict):
                            continue
                        valid_val = info.get("valid")
                        if valid_val is True:
                            valid_keys.add(f"{prov}::{key_preview}")
                        elif valid_val is False:
                            invalid_keys.add(f"{prov}::{key_preview}")
                        issue = info.get("issue")
                        if issue:
                            issue_providers.append(
                                f"{prov}({issue}: {info.get('body_preview','')[:60]})"
                            )
                    tested_credentials_meta.append({
                        "key_hash": f.meta["key_hash"],
                        "key_preview": key_preview,
                        "source_server": server.short_label,
                        "source_path": f.path,
                        "source_kind": f.kind,
                        "source_user": f.user,
                        "results": r,
                        "providers_tested": list(tester.providers.keys()),
                    })
                if tested_n:
                    PROGRESS.write(
                        f"  [validity] {tested_n} 个 key 测过: "
                        f"✓{len(valid_keys)} 有效, ✗{len(invalid_keys)} 失效"
                    )
                    if issue_providers:
                        PROGRESS.write(
                            f"\n  [issues]   {len(issue_providers)} 个 provider 有异常 "
                            f"(quota/expired/rate_limit 等,需人工确认)"
                        )
                        for ip in issue_providers[:8]:
                            PROGRESS.write(f"\n    · {ip}")
                        if len(issue_providers) > 8:
                            PROGRESS.write(
                                f"\n    ... 还有 {len(issue_providers) - 8} 个"
                            )
                    PROGRESS.write("\n")
                # 持久化本次新发现的 provider
                tester.persist_discovered()

            # 4) 生成每 key 的可复现测试脚本 + 写 test_results.json
            if result_dir and tester and tested_credentials_meta:
                safe_server = re.sub(r"[/:\\]", "_", server.short_label)
                test_scripts_dir = (
                    Path(result_dir) / safe_server / "test_scripts"
                )
                test_scripts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                test_results_path = (
                    Path(result_dir) / safe_server / "test_results.json"
                )
                # cache 里反查每个 unique key
                cred_by_hash: dict[str, str] = {}
                for cred in tester._cred_cache:
                    h = hashlib.sha256(cred.encode()).hexdigest()[:12]
                    if h not in cred_by_hash:
                        cred_by_hash[h] = cred
                key_to_meta: dict[str, dict] = {}
                for m in tested_credentials_meta:
                    if m["key_hash"] not in key_to_meta:
                        key_to_meta[m["key_hash"]] = m
                scripts_written = 0
                for h, key in cred_by_hash.items():
                    meta = key_to_meta.get(h, {})
                    script_content = tester.generate_test_script(
                        key,
                        source_info={
                            "server": server.short_label,
                            "path": meta.get("source_path"),
                            "kind": meta.get("source_kind"),
                            "user": meta.get("source_user"),
                        },
                    )
                    script_file = test_scripts_dir / f"test_{h}.py"
                    script_file.write_text(script_content, encoding="utf-8")
                    os.chmod(script_file, 0o600)
                    scripts_written += 1
                # test_results.json
                report = {
                    "server": server.short_label,
                    "generated": datetime.datetime.now().isoformat(timespec="seconds"),
                    "providers_count": len(tester.providers),
                    "providers": tester.providers,
                    "credentials": tested_credentials_meta,
                }
                test_results_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.chmod(test_results_path, 0o600)
                PROGRESS.write(
                    f"  [test_scripts] {scripts_written} 个 key → {test_scripts_dir}/  "
                    f"(`python3 test_<hash>.py` 可复跑)\n"
                )
                PROGRESS.write(
                    f"  [test_results] {test_results_path}\n"
                )

            if fetcher is not None:
                fetcher.close()
        return (
            {
                "server": server.short_label,
                "server_name": server.name,
                "host": server.host,
                "port": server.port,
                "started": started,
                "finished": datetime.datetime.now().isoformat(timespec="seconds"),
                "auth_method": executor._auth_method,
                "findings": [f.to_dict() for f in findings],
            },
            None,
        )
    except Exception as e:
        return (
            {"server": server.short_label, "started": started, "findings": []},
            {"server": server.short_label,
             "error": f"{type(e).__name__}: {e}"},
        )


# ============================================================
# main
# ============================================================

YAML_TEMPLATE = textwrap.dedent(
    """\
    # ============================================
    # codex_finder — 远端服务器清单
    # 格式兼容 autoresearch 的 servers schema (D-03 Bootstrap-then-Key)
    # ============================================
    # 复制本文件为 servers.yaml 后填实际值, 不填的服务器会被忽略。

    servers:
      - name: server-1
        host: 192.168.1.10
        port: 22
        user: root
        identity_file: ~/.ssh/id_ed25519
        # 首次连接一次性密码 (bootstrap 阶段用, 部署完 key 后可删)
        # bootstrap_password_secret: ""

      # - name: server-2
      #   host: 192.168.1.11
      #   port: 22
      #   user: root
      #   identity_file: ~/.ssh/id_ed25519
      #   bootstrap_password_secret: ""

    # --- 跳板机 (可选) ---
    jump:
      host: ""
      port: 22
      user: root
      identity_file: ""
      password: ""

    # --- 扫描行为 ---
    scan:
      parallel: 16             # 单主机内并行扫描用户的线程数
      cmd_timeout: 15          # 单个远程命令超时(秒)
      scan_docker: true        # 是否扫描 Docker
      scan_history: true       # 是否扫描 shell history
      scan_npm_pip: true       # 是否扫描全局 npm/pip
      start_stopped_containers: false  # 启动 stopped 容器再扫(可能副作用)
      verbose: false           # 详细日志
      # 已知 OpenAI 兼容中转站(并行测试)
      # 加上你在 config.toml 里 model_providers 声明的 provider 都会并到测试集
      providers: {}            # 自定义: {name: base_url}

    # --- JSON 报告路径 ---
    output: findings.json

    # --- 把工件复制到本地(分类保存到 <host:port>/<kind>/) ---
    # 包含 ~/.codex/auth.json 等实际文件,目录 mode 0700,文件 mode 0600
    # 包含 export *KEY / codex login 的 history 行单独存到 URGENT/
    # 每次扫描会自动加时间戳子目录(findings/20260610-110000/),避免覆盖
    result_dir: ./findings

    # --- 对每个 key 测是否还有效(对所有 providers 并行打) ---
    # 走本机代理: 设置 TEST_PROXY 或 HTTPS_PROXY 环境变量即可
    test_credentials: true
    """
)


def cmd_init(args) -> int:
    target = Path(args.init_to or "servers.yaml")
    if target.exists():
        PROGRESS.write(f"{target} 已存在,未覆盖\n")
        return 0
    target.write_text(YAML_TEMPLATE, encoding="utf-8")
    PROGRESS.write(f"已生成 {target},请编辑后填入服务器列表与 SSH 配置\n")
    PROGRESS.write(
        f"[!] {target.name} 含 bootstrap_password_secret 等敏感字段,\n"
        f"    请确认已加入 .gitignore(已默认加入本项目模板)。\n"
    )
    return 0


def cmd_check(args, app_cfg: AppConfig, servers: list[Server]) -> int:
    """只测连通性"""
    jump = resolve_jump(app_cfg.jump, args.jump)
    failed = 0
    for s in servers:
        try:
            ex = RemoteExecutor(server=s, jump=jump, timeout=10)
            with ex:
                out, _, _ = ex.run("uname -a", timeout=10)
            print(
                f"  ✓ {s.short_label}  "
                f"[auth={ex._auth_method}]  uname: {out.strip()[:100]}"
            )
        except Exception as e:
            failed += 1
            print(f"  ✗ {s.short_label}  {type(e).__name__}: {e}")
    print(f"\n连通性检查完成: {len(servers) - failed}/{len(servers)} 通过")
    return 0 if failed == 0 else 1


def cmd_list_servers(args, app_cfg: AppConfig) -> int:
    if args.host:
        servers = [make_synthetic_server(args.host)]
    else:
        servers = list(app_cfg.servers)
    if not servers:
        print("(无服务器)")
        return 0
    for s in servers:
        print(f"  {s.short_label}  [{s.auth_summary()}]")
    return 0


def _timestamped_result_dir(base: str) -> str:
    """给 result_dir 加时间戳后缀,避免多次跑覆盖。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(base)
    target = root / ts
    if target.exists():
        for i in range(1, 100):
            cand = root / f"{ts}-{i}"
            if not cand.exists():
                target = cand
                break
    return str(target)


def cmd_scan(args, app_cfg: AppConfig) -> int:
    if args.host:
        servers = [make_synthetic_server(args.host)]
    else:
        servers = list(app_cfg.servers)
    if not servers:
        PROGRESS.write(
            f"[错误] 没有可用服务器,使用 --host 指定或编辑 {args.config}\n"
        )
        return 2

    jump = resolve_jump(app_cfg.jump, args.jump)

    PROGRESS.write(
        f"开始扫描: 模式={'jump' if jump else 'local'}, "
        f"{len(servers)} 台主机, 并行度 {app_cfg.scan.parallel}\n"
    )

    # CLI 覆盖 YAML
    result_dir = args.result_dir or app_cfg.result_dir
    if args.test_credentials_off:
        test_creds = False
    else:
        test_creds = args.test_credentials or app_cfg.test_credentials
    start_stopped = args.start_stopped_containers or app_cfg.scan.start_stopped_containers
    opts = dataclasses.replace(app_cfg.scan, start_stopped_containers=start_stopped)

    # result_dir 加时间戳
    if result_dir:
        result_dir = _timestamped_result_dir(result_dir)
        PROGRESS.write(
            f"  [result_dir] 扫描结果会复制到 {result_dir}/ (mode 0700, 含 URGENT/ 子目录)\n"
        )
    if test_creds:
        proxy = (
            os.environ.get("TEST_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
        )
        provider_set = merge_providers(DEFAULT_PROVIDERS, opts.providers)
        PROGRESS.write(
            f"  [validity] 对每个 key 测 {len(provider_set)} 个 providers"
            + (f"  proxy={proxy}" if proxy else "  无代理,直接联网")
            + "\n"
        )
    if start_stopped:
        PROGRESS.write(
            "  [docker] 会对 stopped 容器尝试 docker start → 扫描 → docker stop(可能触发容器副作用)\n"
        )

    results: list[dict] = []
    errors: list[dict] = []
    for s in servers:
        result, err = scan_one_server(
            s, opts, jump,
            result_dir=result_dir,
            test_credentials=test_creds,
        )
        results.append(result)
        if err:
            errors.append(err)

    Reporter.print_summary(results, errors, show_body=args.show_body)

    if app_cfg.output:
        try:
            Path(app_cfg.output).write_text(
                json.dumps(
                    {
                        "generated": datetime.datetime.now().isoformat(),
                        "results": results,
                        "errors": errors,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            PROGRESS.write(f"\n[OK] JSON 报告已写入: {app_cfg.output}\n")
        except Exception as e:
            PROGRESS.write(f"\n[错误] 写 JSON 失败: {e}\n")

    return 0 if not errors else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="codex_finder",
        description=(
            "跨 SSH 扫描 Codex CLI 凭证残留。"
            "默认从项目下 servers.yaml 读取服务器列表与 SSH 配置。"
            "SSH 认证采用 Bootstrap-then-Key 流程: "
            "先 key/agent,失败回落到 bootstrap_password_secret。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--config", default="servers.yaml",
        help="YAML 配置文件路径(默认 servers.yaml)",
    )
    ap.add_argument(
        "--init", action="store_true",
        help="在当前目录生成 servers.yaml 模板(默认文件名)",
    )
    ap.add_argument(
        "--init-to", metavar="PATH",
        help="生成 servers.yaml 模板到指定路径(独立生效)",
    )
    ap.add_argument(
        "--list-servers", action="store_true",
        help="列出 YAML 中解析到的服务器",
    )
    ap.add_argument(
        "--check", action="store_true",
        help="只测试 SSH 连通性,不扫描",
    )
    ap.add_argument(
        "--host", metavar="HOST[:PORT]",
        help="只扫描指定的一台主机(覆盖 YAML)",
    )
    ap.add_argument(
        "--jump", metavar="HOST", default=None,
        help="通过指定跳板机访问所有目标(覆盖 YAML 中的 jump.host)",
    )
    ap.add_argument(
        "--result-dir", metavar="PATH", default=None,
        help="把扫描到的文件/匹配行复制到此目录(分类保存, mode 0700, "
             "URGENT/ 子目录存 export *KEY / codex login)",
    )
    ap.add_argument(
        "--test-credentials", action="store_true",
        help="对每个找到的 key 测是否还有效(对 DEFAULT + YAML + config.toml 里的所有 providers 并行打,"
             " 自动读 TEST_PROXY/HTTPS_PROXY)",
    )
    ap.add_argument(
        "--no-test-credentials", dest="test_credentials_off",
        action="store_true",
        help="关掉 YAML 默认开的 test_credentials",
    )
    ap.add_argument(
        "--start-stopped-containers", action="store_true",
        help="对 stopped 容器尝试 docker start → 扫描 → docker stop(可能触发容器副作用,默认关)",
    )
    ap.add_argument(
        "--show-body", action="store_true",
        help="把每个 provider 的响应体前 140 字符也打到终端(默认只在 issue 时打)",
    )
    ap.add_argument(
        "--no-color", action="store_true",
        help="关闭彩色输出",
    )

    args = ap.parse_args()

    if args.no_color:
        os.environ["NO_COLOR"] = "1"
        Reporter.COLOR = False

    if args.init or args.init_to:
        return cmd_init(args)

    # 加载 YAML 配置
    try:
        app_cfg = AppConfig.load(args.config)
    except ValueError as e:
        PROGRESS.write(f"[错误] 解析 {args.config} 失败: {e}\n")
        return 2
    except yaml.YAMLError as e:
        PROGRESS.write(f"[错误] YAML 语法错误 in {args.config}: {e}\n")
        return 2

    if not app_cfg.servers and not args.host:
        PROGRESS.write(
            f"[提示] {args.config} 中没有 server 条目。\n"
            f"      使用 --init-to {args.config} 生成模板。\n"
        )

    if args.list_servers:
        return cmd_list_servers(args, app_cfg)

    if args.host:
        servers: list[Server] = [make_synthetic_server(args.host)]
    else:
        servers = list(app_cfg.servers)

    if args.check:
        if not servers:
            PROGRESS.write("[错误] 没有可用服务器\n")
            return 2
        return cmd_check(args, app_cfg, servers)

    return cmd_scan(args, app_cfg)


if __name__ == "__main__":
    sys.exit(main())
