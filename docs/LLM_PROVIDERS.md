# MTCA LLM 接入指南（v0.4 / T18）

> 适用版本：v0.4（M1 阶段）
> 实现位置：`src/llm/provider.py`（T10）
> 配置文件：`~/.mtca/config.toml`

---

## 1. 概述（为什么需要抽象层）

MTCA 上层业务（动态打分 T7、召回 T8、雾化 T6、未来压缩 T24）都需要调用 LLM，但用户机器上的 LLM 运行环境千差万别：

- 有人用 **Ollama** 起本地服务（最省事）
- 有人用 **LM Studio** 加载 GGUF 模型（GUI 友好）
- 有人用 **llama.cpp** 直接 in-process 推理（最低延迟）
- 有人只能访问 **云端 API**（OpenAI / Anthropic）

如果上层代码直接 `import openai` 或写死 `localhost:11434`，换后端就要改一堆业务代码。

`src/llm/provider.py` 用一个 `LLMProvider` Protocol 屏蔽 4 类后端：

```
上层业务（打分 / 召回 / 压缩）
        │
        ▼
   LLMProvider（generate / embed / is_available）
        │
   ┌────┼────┬────────┬─────────┐
   ▼    ▼    ▼        ▼         ▼
Ollama LM   llama    Cloud
       Studio .cpp    (OpenAI/Anthropic)
```

收益：

- **切换零成本**：改 `config.toml` 的 `default` 字段即可
- **mock 友好**：`CloudProvider` 在 `api_key=""` 时返回稳定伪向量，便于单测
- **自动降级**：所有本地后端都不可用时，自动落到 `fallback`（默认 cloud）

---

## 2. 4 种后端对比表

| 后端 | 安装难度 | 启动速度 | 推理延迟 | 隐私 | 嵌入支持 | 适用场景 |
|---|---|---|---|---|---|---|
| **Ollama** | ⭐ 最简 | 1–2s（常驻） | 中 | ✅ 本地 | ✅ `/api/embeddings` | 推荐首选；命令行一键拉模型 |
| **LM Studio** | ⭐⭐ 简单 | 1–2s（常驻） | 中 | ✅ 本地 | ✅ `/v1/embeddings` | GUI 用户；OpenAI 兼容协议 |
| **llama.cpp** | ⭐⭐⭐ 需编译 | 5–10s（首次） | 最低 | ✅ 本地 | ✅ `create_embedding` | 追求极致延迟；常驻服务 |
| **Cloud (OpenAI)** | ⭐⭐ 需 Key | 即时 | 受网络影响 | ❌ 上传 | ✅ `/v1/embeddings` | M1 mock；M3 接真实流量 |

> **M1 阶段推荐**：本地 Ollama + Cloud mock 双备份，配置即用。

---

## 3. Ollama 接入（详细步骤）

Ollama 是 macOS / Linux / Windows 通吃的本地 LLM 运行时，一条命令拉模型，一条命令起服务。

### 3.1 安装 Ollama

```bash
# macOS
brew install ollama

# Linux 一键安装
curl -fsSL https://ollama.com/install.sh | sh

# Windows：从 https://ollama.com/download 下载安装包
```

### 3.2 启动服务（后台常驻）

```bash
# macOS / Linux：后台启动
ollama serve &
# 或前台启动（另一个终端）
ollama serve
```

默认监听 `http://localhost:11434`。

### 3.3 拉取推荐模型

```bash
# 推荐：通义千问 3.6 MoE（35B 激活 3B，CPU 也能跑）
ollama pull qwen3.6:35b-a3b

# 备选：千问 2.5 7B（更小、更快）
ollama pull qwen2.5:7b

# 嵌入模型（中文友好，384 维）
ollama pull bge-small-zh
```

### 3.4 验证服务可用

```bash
# 列出本地模型
curl http://localhost:11434/api/tags

# 跑一次生成
curl -X POST http://localhost:11434/api/generate \
  -d '{"model":"qwen3.6:35b-a3b","prompt":"hello","stream":false}'
```

### 3.5 写入 MTCA 配置

编辑 `~/.mtca/config.toml`：

```toml
[llm]
default = "ollama"        # auto / ollama / lmstudio / llamacpp / cloud
fallback = "cloud"

[ollama]
base_url = "http://localhost:11434"
model = "qwen3.6:35b-a3b"
timeout = 30
```

> `model` 字段填你实际拉取的标签；`bge-small-zh` 用作嵌入时单独指定。

### 3.6 在 Python 中调用

```python
from src.llm.provider import get_provider

llm = get_provider("ollama")
print(llm.generate("用一句话介绍 MTCA"))
vec = llm.embed("MTCA 是本地优先的长期记忆中间件")
print(f"向量维度：{len(vec)}")
```

---

## 4. LM Studio 接入（详细步骤）

LM Studio 是带 GUI 的本地 LLM 工具，对 OpenAI 协议兼容（`/v1/chat/completions`、`/v1/embeddings`），适合不爱敲命令行的用户。

### 4.1 安装 LM Studio

从 [https://lmstudio.ai/download](https://lmstudio.ai/download) 下载对应平台安装包，按向导装好。

### 4.2 加载并启动本地服务

1. 打开 LM Studio
2. 搜索并下载模型（推荐 `Qwen2.5-7B-Instruct-GGUF`）
3. 切到 **Developer** 标签页
4. 顶部 **Start Server** 按钮（默认 `http://localhost:1234`）

### 4.3 验证服务可用

```bash
# 列出已加载模型
curl http://localhost:1234/v1/models

# 跑一次对话
curl -X POST http://localhost:1234/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-7b-instruct","messages":[{"role":"user","content":"hi"}]}'
```

### 4.4 写入 MTCA 配置

```toml
[llm]
default = "lmstudio"
fallback = "ollama"

[lmstudio]
base_url = "http://localhost:1234"
model = "qwen2.5-7b-instruct"   # 与 LM Studio 显示的模型名一致
timeout = 30
```

### 4.5 注意事项

- LM Studio 一次只能加载 1 个模型；切模型要重启服务
- 模型名严格区分大小写，必须与 `curl /v1/models` 返回的一致
- 嵌入模型也要在 LM Studio 里手动加载（搜索 `text-embedding-nomic-embed-text-v1.5`）

---

## 5. llama.cpp 接入（可选）

llama.cpp 把模型加载到内存中 in-process 推理，无网络层，最快；但首次加载慢（5–10s），需要 Python 绑定 `llama-cpp-python`。

### 5.1 安装依赖

```bash
# CPU 版（通用）
pip install llama-cpp-python

# GPU 加速（NVIDIA，需先装 CUDA）
CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python --force-reinstall --no-cache-dir

# Apple Silicon（macOS M1/M2/M3）
CMAKE_ARGS="-DGGML_METAL=on" pip install llama-cpp-python --force-reinstall --no-cache-dir
```

### 5.2 下载 GGUF 模型

```bash
# 推荐：千问 2.5 7B 量化版
mkdir -p ~/models
cd ~/models
curl -L -o qwen2.5-7b-instruct-q4_k_m.gguf \
  https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf
```

### 5.3 写入 MTCA 配置

```toml
[llm]
default = "llamacpp"
fallback = "ollama"

[llamacpp]
model_path = "~/models/qwen2.5-7b-instruct-q4_k_m.gguf"
n_ctx = 4096          # 上下文窗口（token 数）
n_threads = 8         # CPU 线程数（建议 = 物理核数）
```

### 5.4 注意事项

- `is_available()` 只检查文件存在 + 库可导入，不实际加载（避免阻塞）
- 首次 `generate()` 才真正加载模型（数秒），后续调用走内存
- 模型文件 ≥ 4 GB，确保磁盘空间

---

## 6. 云 API 接入（OpenAI / Anthropic）

M1 阶段 **不实调**（见 `AI_RULES.md` §12），仅留接口。`api_key=""` 时走 mock，返回固定伪向量，方便单测。

### 6.1 配置（mock 模式）

```toml
[llm]
default = "cloud"
fallback = "ollama"

[cloud]
provider = "mock"                          # mock 模式不发起 HTTP
api_base = "https://api.openai.com/v1"      # 仅占位
api_key = ""                                # 空 = mock
model = "gpt-4o-mini"
timeout = 30
```

### 6.2 切换到真实 OpenAI（M3 启用）

```bash
# 强烈推荐：把 key 放进环境变量，不要写进 config.toml
export MTCA_CLOUD_KEY="sk-xxxxxxxxxxxxxxxxxxxx"
```

```toml
[cloud]
provider = "openai"
api_base = "https://api.openai.com/v1"
api_key = "env:MTCA_CLOUD_KEY"   # 读取环境变量的语法（待实现）
model = "gpt-4o-mini"
```

> **当前实现（M1）**：`api_key` 字段直接读字符串；环境变量语法 M3 再加。临时方案：手动 `export` 后写文件（M1 不推荐）。

### 6.3 Anthropic 配置示例

```toml
[cloud]
provider = "anthropic"
api_base = "https://api.anthropic.com/v1"
api_key = "sk-ant-xxxxxxxxxxxxxxxxxxxx"
model = "claude-3-5-sonnet-latest"
timeout = 60
```

> M1 阶段 Anthropic 路径为占位，未实现协议适配；M3 补齐 `/v1/messages` 调用。

---

## 7. 自动探测逻辑

调用 `get_provider("auto")` 时，MTCA 按以下顺序探测，找到第一个可用的就返回：

```python
# src/llm/provider.py:66
_PROBE_ORDER = ("ollama", "lmstudio", "llamacpp", "cloud")
```

但 **优先尝试** `[llm].default` 配的后端（默认 `ollama`）。完整算法：

```
1. 读 [llm].default（默认 "ollama"）
2. 按 default 优先 + _PROBE_ORDER 补齐的顺序，逐个调用 is_available()
3. 首个 is_available()=True 的 provider 直接返回
4. 全部不可用 → 按 [llm].fallback（默认 "cloud"）返回实例
   （cloud 在 mock 模式下 is_available() 永远 True，所以总能拿到）
```

### 7.1 一键探测所有后端

```python
from src.llm.provider import probe_all
print(probe_all())
# {'ollama': True, 'lmstudio': False, 'llamacpp': False, 'cloud': True}
```

### 7.2 强制指定后端

```python
from src.llm.provider import get_provider

# 显式指定（auto 探测跳过）
llm = get_provider("ollama")

# 不存在则抛 ValueError
try:
    get_provider("gpt5")
except ValueError as e:
    print(e)
# 未知 provider 名称：'gpt5'；合法值：['cloud', 'llamacpp', 'lmstudio', 'ollama'] 或 'auto'
```

### 7.3 探测超时

`is_available()` 用 2 秒硬超时（`_HTTP_TIMEOUT`），不会卡住上层 50 ms 召回主链路。

---

## 8. 故障排查（5 个常见问题）

### Q1：`is_available()` 一直返回 False？

**排查步骤**：

```bash
# 1. 手动 curl 探活（Ollama 示例）
curl -v http://localhost:11434/api/tags
# 期望：HTTP/1.1 200 OK + JSON 列表

# 2. 检查端口监听
# Windows
netstat -ano | findstr :11434
# macOS / Linux
lsof -i :11434

# 3. 检查防火墙（Windows Defender 可能拦截）
```

**根因 90%**：`ollama serve` 没后台运行，或 base_url 配错（写成了 `127.0.0.1` 而服务监听 `0.0.0.0`，或反之）。

### Q2：`generate()` 报 `Ollama 返回缺少 response 字段`？

**根因**：模型未拉到本地，或模型名拼错。

```bash
# 列出已下载模型
ollama list

# 重新拉取
ollama pull qwen3.6:35b-a3b

# 配置里的 model 必须与 ollama list 完全一致
```

### Q3：嵌入维度对不上（sqlite 存 384，模型返 1024）？

**根因**：嵌入模型与生成模型混用。MTCA 默认 `bge-small-zh`（384 维）。

```toml
[ollama]
# 生成用千问
model = "qwen3.6:35b-a3b"
```

调用嵌入时单独指定嵌入模型（在 `provider.py` 中预留扩展点）：

```python
from src.llm.provider import OllamaProvider

embed_provider = OllamaProvider(model="bge-small-zh")
vec = embed_provider.embed("测试文本")
assert len(vec) == 384
```

### Q4：llama.cpp 报 `模型文件不存在`？

```bash
# 1. 展开 ~ 路径（config 写 ~/models/...，代码里 os.path.expanduser）
ls -la ~/models/*.gguf

# 2. 用绝对路径写配置（最稳）
model_path = "/Users/yourname/models/qwen2.5-7b-instruct-q4_k_m.gguf"
```

### Q5：Cloud 模式返回了 mock 数据，上层断言失败？

**预期行为**：M1 阶段 `api_key=""` 走 mock，返回 `[mock:gpt-4o-mini] echo: ...`。

```toml
# 确认是 mock（api_key 留空）
[cloud]
provider = "openai"
api_key = ""        # ← 留空 = mock
```

如果想切真实调用（M3 启用），填入 key 即可；M1 阶段云端真实调用 **不允许**（见 `AI_RULES.md` §12）。

---

## 附录：完整配置模板

```toml
# ~/.mtca/config.toml — MTCA v0.4

[llm]
default = "ollama"        # auto / ollama / lmstudio / llamacpp / cloud
fallback = "cloud"        # 主 provider 不可用时的备选

[ollama]
base_url = "http://localhost:11434"
model = "qwen3.6:35b-a3b"
timeout = 30

[lmstudio]
base_url = "http://localhost:1234"
model = "qwen2.5-7b-instruct"
timeout = 30

[llamacpp]
model_path = "~/models/qwen2.5-7b-instruct-q4_k_m.gguf"
n_ctx = 4096
n_threads = 8

[cloud]
provider = "mock"                          # M1 仅 mock
api_base = "https://api.openai.com/v1"
api_key = ""
model = "gpt-4o-mini"
timeout = 30
```

**反馈**：配置问题 → 在 GitHub 提 Issue；协议扩展 → 改 `src/llm/provider.py` + 同步本文档。