# python-ai

## 1. Parallel Agentic Workflows with Python

DigitalOcean 教程 [How to Build Parallel Agentic Workflows with Python](https://www.digitalocean.com/community/tutorials/how-to-build-parallel-agentic-workflows-with-python)
（作者 Andrew Dugan，2025-12-04）的配套代码，用原生 `asyncio` + `aiohttp` 手写 fan-out / fan-in 并行 agentic workflow。

### 文件

| 文件 | 说明 |
|---|---|
| `agentic_workflows.py` | Step 1–5：端点配置、异步调用层、三套系统提示、并发编排、路由逻辑 |
| `test_workflow.py` | Step 6：两个测试场景（缺 listing_id → 追问；补全 → 返回价格） |
| `requirements.txt` | 依赖：`aiohttp` |
| `mock_server.py` | 本地 mock 推理端点（仅用标准库），无 GPU / 无 API key 也能跑通全流程 |
| `demo_routes.py` | 遍历路由层的全部 7 个分支，验证 fan-out / fan-in 与短路判定 |
| `check_provider.py` | 接入真实厂商前的自检：端点可达性、key 有效性、模型可调用性 |

### 运行

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 指向你的 OpenAI 兼容端点（自建 vLLM / 第三方 API 均可）
export VLLM_SERVER_URL="http://your_server_ip:8000/v1/chat/completions"

python3 test_workflow.py
```

期望输出：

```
Response: Could you please provide the listing_id of the item you're asking about?
Response: The price for listing 123456 is $350,000.
```

不设 `VLLM_SERVER_URL` 时，代码回落到教程中的占位地址 `http://your_server_ip:8000/v1/chat/completions`，需自行替换。

### 本地无 GPU / 无 API key 时跑通

`mock_server.py` 提供一个 OpenAI 兼容的假端点，按 system prompt 关键字返回符合各提示约定格式的回复，足以验证并发调用、保序回填和路由分支：

```bash
# 终端 1
python3 mock_server.py --port 8000 --verbose

# 终端 2
export VLLM_SERVER_URL="http://127.0.0.1:8000/v1/chat/completions"
python3 test_workflow.py     # 教程两个场景
python3 demo_routes.py       # 7 个分支全覆盖
```

`demo_routes.py` 实测输出：

```
[pricing / no listing_id]
  -> Could you please provide the listing_id of the item you're asking about?
[pricing / listing_id given]
  -> The price for listing 123456 is $350,000.
[pricing / unknown listing_id]
  -> We are unable to find that listing ID in our records. Are you sure you have the correct listing ID?
[scheduling / date and time given]
  -> Perfect! I've scheduled a call for you on that date and time. A sales representative will reach out to you at that time.
[scheduling / missing date and time]
  -> What day and time are you available for the call?
[listing / general question]
  -> Please hold while I transfer you to a specialist for further assistance.
[fallback / out of scope]
  -> I apologize, I'm not sure how I can help with that. Let me transfer you to a human representative who can better assist you.
```

### 工作流结构

```
conversation_history
        |
        +-- pricing_prompt    --\
        +-- scheduling_prompt ---> asyncio.gather（并发，按序返回）--> routing logic --> final_response
        +-- listing_prompt    --/
```

- 三个提示彼此独立、无状态，各自只做一件事，未命中统一返回 `false`
- `asyncio.gather` 按入参顺序返回结果，配合 `prompt_names.index(...)` 回填响应
- 决策完全由确定性 `if/elif` 完成，LLM 只负责分类与抽取

### 接入真实模型：硅基流动 SiliconFlow

默认走 SiliconFlow：国内直连（已实测本机无需代理）、OpenAI 兼容、9B 以下模型永久免费。

**1. 拿 key**：注册 https://cloud.siliconflow.cn → 左侧「API 密钥」→ 新建密钥，复制 `sk-` 开头那串。

**2. 填配置**（复制 `.env.example` 为 `.env`，或直接 export）：

```bash
export VLLM_SERVER_URL="https://api.siliconflow.cn/v1/chat/completions"
export LLM_API_KEY="sk-xxxxxxxx"
export LLM_MODEL_ID="Qwen/Qwen2.5-7B-Instruct"
```

**3. 先自检再跑**：

```bash
python3 check_provider.py     # 验证端点 + key + 模型三者都通
python3 demo_routes.py        # 7 个分支全覆盖
python3 test_workflow.py      # 教程的两个场景
```

免费档常用模型：`Qwen/Qwen2.5-7B-Instruct`、`Qwen/Qwen3-8B`、`THUDM/GLM-4-9B-0414`、`deepseek-ai/DeepSeek-R1-0528-Qwen3-8B`。平台上下架和调价频繁，用 `check_provider.py` 拉一次模型列表确认。

**注意两点**：

- 免费档限速限并发（约 5–10 QPS，TPM 也有上限），超出返回 429「TPM limit reached」。本工作流每个 case 并发 3 个请求，密集跑容易撞上限——要么加 `Semaphore` 限流，要么换 Pro 档。
- 教程的提示要求模型**只**输出 `listing_id: XXXXXX` / `date: ...` / `false`。7B~9B 不一定守得住格式，换模型后必须 `demo_routes.py` 重跑，必要时改写提示或换更大模型。

**其他可切换的平台**（改上面两三个环境变量即可，都写在 `.env.example` 里）：

| 平台 | 免费额度 | 端点 |
|---|---|---|
| 智谱 AI | 2000 万 token + GLM-4-Flash 永久免费，30 并发 | `https://open.bigmodel.cn/api/paas/v4/chat/completions` |
| 阿里云百炼 | 7000 万 token，覆盖 70+ 模型 | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` |
| OpenRouter | 25+ 免费模型（ID 带 `:free` 后缀），约 200 req/天 | `https://openrouter.ai/api/v1/chat/completions` |
| Groq / Cerebras | 速度快、按 RPM 限额 | 各平台 `/v1/chat/completions` |

后两家部分地区需要代理。

环境变量说明：

- `LLM_API_KEY` 为空时不发送 `Authorization` 头，本地 vLLM / `mock_server.py` 照常工作
- `LLM_MODEL_ID` 设置了就覆盖所有提示的模型；不设则各提示用自己配置的 `model_id`
- **key 只放环境变量或 `.env`**（已在 `.gitignore` 中），不要写进代码或提交到 git

### 生产化补强（教程未覆盖）

1. `asyncio.gather(..., return_exceptions=True)` + `aiohttp.ClientTimeout`，避免单次失败/超时拖垮整批
2. `asyncio.Semaphore(n)` 限流，防止打爆自建 vLLM 或触发第三方 RPM 限制
3. 用 JSON mode / structured output 替代 `startswith("listing_id:")` 这类脆弱的字符串解析
4. `ClientSession` 提到外层复用连接池（当前每次调用都新建 session）
5. `call_models()` 用 `asyncio.run()` 包壳，Web 服务中应直接 `await _call_models_async()`
6. 记录每个提示的耗时、token 与命中率，逐提示迭代优化
