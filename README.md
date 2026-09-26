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
| `env_loader.py` | 零依赖 `.env` 加载器（不引入 python-dotenv），命令行 export 的值优先 |
| `.env.example` | 配置模板，复制为 `.env` 后填写；`.env` 已在 `.gitignore` 中 |

### 运行

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # 填 VLLM_SERVER_URL / LLM_API_KEY / LLM_MODEL_ID
# 也可以不建 .env，直接 export（export 优先级高于 .env）

python3 test_workflow.py
```

期望输出：

```
Response: Could you please provide the listing_id of the item you're asking about?
Response: The price for listing 123456 is $350,000.
```

配置读取顺序：**命令行 export > `.env` > 代码默认值**（`http://your_server_ip:8000/v1/chat/completions`，需自行替换）。
三个入口脚本（`test_workflow.py` / `demo_routes.py` / `check_provider.py`）启动时都会自动加载 `.env`，无需反复 export。

### 获取 API key

> 没有"开源的 API key"这回事——key 是厂商签发的身份凭证，与模型是否开源无关。这里说的是**开源/免费模型的调用额度**。

#### DeepSeek（当前默认）

1. 注册：打开 https://platform.deepseek.com ，手机号或微信登录
2. 建 key：左侧 **API keys** → **Create new key** → 复制 `sk-` 开头那串
   - **只显示一次**，关掉弹窗就找不回来了
3. **充值**：DeepSeek **没有免费额度**，余额为 0 时所有模型一律 `402 Insufficient Balance`
   - 查余额：`GET https://api.deepseek.com/user/balance`
4. 填进 `.env`：`LLM_API_KEY=sk-你的key`

可用模型（2026-09 实测 `/v1/models`，老的 `deepseek-chat` / `deepseek-reasoner` **已下架**）：

| 模型 ID | 说明 |
|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash，1M 上下文，便宜，跑本 demo 用这个 |
| `deepseek-v4-pro` | 更强更贵 |

#### 硅基流动 SiliconFlow（备用）

1. 注册：打开 https://cloud.siliconflow.cn ，手机号验证码或 GitHub 登录
2. **实名认证**：控制台内完成，未实名领不到免费额度（国内平台合规要求），无实名时调用报 `402`
3. 领额度：账户中心 → 资源包，注册赠送的 token 在此确认到账
4. 建 key：左侧 **API 密钥** → **新建密钥** → 复制 `sk-` 开头那串
   - 直达链接：https://cloud.siliconflow.cn/account/ak
5. 填进 `.env`，并把端点换成 `https://api.siliconflow.cn/v1/chat/completions`

9B 以下模型永久免费（`Qwen/Qwen2.5-7B-Instruct` 等），但限速约 5–10 QPS 且有 TPM 上限，超了返回 429。平台**没有 Mistral 系列**，教程原配的 `Mistral-Small-3.2-24B` 用不了。

#### 自检

```bash
python3 check_provider.py     # 端点 -> key -> 模型，逐级验证
python3 demo_routes.py        # 7 个路由分支
```

key 有效但没余额时的输出（DeepSeek / SiliconFlow 都是 402）：

```
endpoint : https://api.deepseek.com/v1/chat/completions
api key  : 已设置 (sk-***f702)
model    : deepseek-flash

[OK]   端点可达，共 2 个模型。前 20 个：
       deepseek-flash  <-- 当前使用
       deepseek-v4-pro

[FAIL] HTTP 402：账户余额不足，key 本身有效。去控制台充值/领额度后重试
```

`402` = key 有效但没钱，`401` = key 无效，两者别搞混——前者不用去重新建 key。

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

### 排查：真实模型跑出非预期结果

先看 fan-out 的三路原始输出，路由代码本身极少出错：

```bash
python3 debug_fanout.py --case 1            # 打印 pricing/scheduling/listing 三路原始回复
python3 debug_fanout.py --case 1 --repeat 3 # 多次采样，看是否稳定
python3 debug_fanout.py --ask "how much is listing 123456?"
```

**现象 1：路由走到"转人工/转专员"，但用户明明在问价格**

根因在 `pricing_prompt`。教程原提示要求模型**自由生成追问句**（"ask them for the listing id number"），
同时又强调"否则只返回 `false`"，7B~14B 模型会保守地选后者。实测（SiliconFlow `Qwen/Qwen2.5-7B-Instruct`，temperature 0.1，两次采样一致）：

```
pricing_prompt     'false'    <- 期望是追问，实际判否
scheduling_prompt  'false'
listing_prompt     'true'     <- 于是落到 listing 分支，输出"转专员"
```

修法：让模型只在固定 token 里三选一（`listing_id: X` / `need_listing_id` / `false`），
追问文案由代码生成，别让模型自由发挥。教程作者用的 Mistral-Small-3.2-24B 才能扛住原提示，
SiliconFlow 平台没有 Mistral 系列——**模型选型本身就是教程强调的迭代过程**。

**现象 2：`KeyError: 'choices'`**

模型调用失败了（余额不足、限流、模型名写错），教程代码没检查 HTTP 状态就直接取字段。
先把原始错误体打出来：

```
HTTP 402 {"code":30001,"message":"Sorry, your account balance is insufficient"}
```

- `402` 余额不足：只有 9B 以下模型在免费档；14B/32B/72B 都要付费额度。去控制台实名认证领赠送额度，或充值
- `401` / `code 30014` key 无效或没填
- `429` 触发限流（免费档约 5–10 QPS + TPM 上限），歇一分钟再跑

**现象 3：回复带前导换行**（实测 `THUDM/GLM-4-9B-0414` 返回 `'\nfalse'`）

`"\nfalse" != "false"`，会被当成"命中"处理。解析前统一 `strip()`。

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

### 接入真实模型

默认走 DeepSeek：国内直连（已实测本机无需代理）、OpenAI 兼容。备选 SiliconFlow（9B 以下免费）、智谱、百炼、OpenRouter，都在 `.env.example` 里。

**1. 拿 key**：见上一节。

**2. 填配置**（复制 `.env.example` 为 `.env`，或直接 export）：

```bash
export VLLM_SERVER_URL="https://api.deepseek.com/v1/chat/completions"
export LLM_API_KEY="sk-xxxxxxxx"
export LLM_MODEL_ID="deepseek-flash"
```

**3. 先自检再跑**：

```bash
python3 check_provider.py     # 验证端点 + key + 模型三者都通
python3 demo_routes.py        # 7 个分支全覆盖
python3 test_workflow.py      # 教程的两个场景
```

模型上下架和调价频繁，用 `check_provider.py` 拉一次列表确认当前可用 ID。

**注意两点**：

- 免费档限速限并发（约 5–10 QPS，TPM 也有上限），超出返回 429「TPM limit reached」。本工作流每个 case 并发 3 个请求，`demo_routes.py` 一次 21 次调用，密集跑容易撞上限——要么加 `Semaphore` 限流，要么换付费档。
- 教程的提示要求模型**只**输出 `listing_id: XXXXXX` / `date: ...` / `false`。小模型不一定守得住格式，换模型后必须 `demo_routes.py` 重跑，必要时改写提示或换更大模型。

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
