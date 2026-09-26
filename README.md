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
| `agentic_workflows_prod.py` | **生产化版**：超时/重试/限流/降级/结构化输出，教程原文件不动 |
| `test_prod.py` | 生产化补强的回归测试：本地故障注入（离线）+ 真实端点路由 |
| `debug_fanout.py` | 打印每条路由决策背后的三路原始输出，定位是哪一路判错 |
| `.env.example` | 配置模板，复制为 `.env` 后填写；`.env` 已在 `.gitignore` 中 |

### 通读顺序

不要按文件名顺序读，按"数据怎么流"读，两遍就能通：

**第一遍：把链路跑通（只看教程版，30 分钟）**

| 顺序 | 看什么 | 目的 |
|---|---|---|
| 1 | `agentic_workflows.py` 的 `ORIGINAL_PROMPTS` | 先搞清模型被要求吐什么格式 —— 后面所有路由判断都是围绕这份输出契约 |
| 2 | `_call_single_model` → `_call_models_async` | 再看这些提示怎么并发打出去（`gather` 的并发 + 保序两个性质） |
| 3 | `run_agentic_workflow` | 最后看 fan-out 组装、fan-in 归集、if/elif 优先级路由 |
| 4 | `mock_server.py` + `demo_routes.py` | 不要 key 跑一遍，把 7 个分支走通，建立直觉 |

**第二遍：看生产化补了什么（对照着读）**

`agentic_workflows_prod.py` 的函数调用链已在文件头画出，照着走一遍即可。
每一处补强都对应 `test_prod.py` 里的一条故障注入断言（用例名就是补强项）：
429 重试、持续 500 降级、单路失败跳过、超时熔断、坏 JSON 不重试、
脏输出归一化、白名单拦跑偏文本、并发峰值 1/2/3。

读的时候最容易卡住的三点，文件里都有注释标出：
- `load_env()` **必须早于** `import agentic_workflows`（后者在模块级就把配置求值了）
- `asyncio.run()` 不能在已运行的 event loop 里调用（常驻服务要用 async 入口）
- `mock_server` 按提示里的关键字分流（两套变体都有这些关键字，教程版都能跑）；
  但 prod 版的 `STRICT_TOKENS` 白名单会拦掉 mock 的自由文本追问句，跑 prod + mock 时要关掉它

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

第四个变量 `PROMPT_VARIANT` 选提示集，见下方「提示变体」。

### 怎么验证效果：分层测试

从上往下跑，越往下越接近真实、越花钱。只想"看一眼效果"跑 L2 就够；改完代码要确认没跑偏，跑 L4+L5。

| 层 | 命令 | 需要 API key | 验证什么 | 通过标准 |
|---|---|---|---|---|
| L0 自检 | `python check_provider.py` | ✅ | 端点可达 → key 有效 → 模型可调用（三级） | 末尾打印「自检通过」 |
| L1 离线跑通 | 见下方「本地无 GPU / 无 API key 时跑通」 | ❌ | 并发 fan-out/fan-in + 7 个路由分支 | 7 条输出与下表一致 |
| L2 教程场景 | `python test_workflow.py` | ✅ | 教程 Step 6 的两个场景 | 两行 `Response:` 与教程逐字一致 |
| L3 全路由 | `python demo_routes.py` | ✅ | 7 个分支全覆盖（真实模型） | 7 条分支命中正确 |
| L4 补强离线 | `python test_prod.py --only fault` | ❌ | 超时/重试/限流/降级/白名单等 9 项补强 | `18/18 通过` |
| L5 补强真实 | `python test_prod.py --only real` | ✅ | prod 版 7 条路由 + 同步入口 | `8/8 通过` |
| L6 单句排查 | `python agentic_workflows_prod.py "..." --json` | ✅ | 一句话背后的三路原始输出 | 看 `raw` 字段哪一路判错 |

```bash
python test_prod.py                # L4 + L5 全跑
python debug_fanout.py --case 1 --repeat 3   # 同一条多采样，区分「模型抖动」还是「代码 bug」
```

**2026-09-26 实测基线**（SiliconFlow `THUDM/GLM-4-9B-0414`，`PROMPT_VARIANT=strict`）：
L0 通过 → L2 与教程逐字一致 → L3 7/7 → L4 18/18 → L5 8/8。
L5 单条耗时约 1.4s（三路并发），L3+L5 合计约 50 次调用，花费几分钱。

L1/L3 期望的 7 条输出（`demo_routes.py` 打印的就是这张表）：

| 分支 | 输入意图 | 期望回复 |
|---|---|---|
| pricing / 无 id | 问价但没给 listing_id | `Could you please provide the listing_id...?` |
| pricing / 有 id | 问价且给了 `123456` | `The price for listing 123456 is $350,000.` |
| pricing / 未知 id | 给了 `999999` | `We are unable to find that listing ID...` |
| scheduling / 有日期 | 约电话且给了日期时间 | `Perfect! I've scheduled a call for you...` |
| scheduling / 无日期 | 约电话但没给时间 | `What day and time are you available...?` |
| listing | 通用房源问题 | `Please hold while I transfer you to a specialist...` |
| fallback | 完全超纲 | `I apologize, I'm not sure how I can help...` |

> **用哪个 python：** 依赖只有 `aiohttp`。在 WorkBuddy 终端里用它自带的 venv
> （`C:\Users\xiaozeng\.workbuddy\binaries\python\envs\default\Scripts\python.exe`）直接可跑；
> 新装的系统 Python 3.13.15 是干净的，先 `python -m pip install -r requirements.txt`
> 或 `python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt`。
> 看到 `ModuleNotFoundError: aiohttp` 就是没装依赖，不是代码问题。

### 提示变体（真实模型必读）

教程的三套提示是为 Mistral-Small-3.2-24B 写的，9B 级别的模型扛不住（实测数据见「排查」）。
所以代码里内置两套，用 `PROMPT_VARIANT` 切换：

| 值 | 说明 | SiliconFlow `THUDM/GLM-4-9B-0414` 实测 |
|---|---|---|
| `original` | 教程原文，逐字未改 | 7 个分支 **5/7** 正确 |
| `strict`（默认推荐） | 同样三个分类任务，但每路只允许输出固定 token，追问文案交给代码生成 | 7 个分支 **7/7** 正确 |

```bash
PROMPT_VARIANT=strict python3 demo_routes.py
```

`strict` 下分类器只返回 `listing_id: XXXXXX` / `need_listing_id` / `false`（scheduling 路为 `date: ...` / `need_datetime` / `false`），
命中"缺参数"分支时由代码输出追问句。这更贴合教程自己的主张：**LLM 只做不确定的分类抽取，确定性文案留在代码里**。

`mock_server.py` 按三路提示里的关键字分流（"price of a listing" / "schedule a call" / "question about a listing"），
这三个短语在两套变体里都有，所以**教程版跑 mock 时 `PROMPT_VARIANT` 取哪个都行**。
唯一例外是 prod 版：它的 token 白名单（`STRICT_TOKENS=true`）会拦掉 mock 在"缺参数"分支返回的自由文本追问句
（判为非法输出 → 落 fallback、`degraded=True`），想看追问分支就加 `STRICT_TOKENS=false`。

### 获取 API key

> 没有"开源的 API key"这回事——key 是厂商签发的身份凭证，与模型是否开源无关。这里说的是**开源/免费模型的调用额度**。

#### 智谱 BigModel（唯一实测不用充值的，推荐先走这条）

1. 注册：打开 https://open.bigmodel.cn ，手机号注册
2. **实名认证**：控制台内完成（与 SiliconFlow 的实名不通用，两家各认一次）
3. 建 key：左侧 **API Keys** → 新建 → 复制 `sk-` 开头那串（只显示一次）
4. 填进 `.env`：

```bash
VLLM_SERVER_URL=https://open.bigmodel.cn/api/paas/v4/chat/completions
LLM_API_KEY=sk-你的key
LLM_MODEL_ID=glm-4-flash
```

`glm-4-flash` 系（`glm-4-flash` / `glm-4.7-flash`）是官方承诺的**永久免费**模型，注册后无需充值即可调用。限流约 3 req/s（单模型并发 1），跑本 demo 够用。

#### DeepSeek（当前默认，需充值）

1. 注册：打开 https://platform.deepseek.com ，手机号或微信登录
2. 建 key：左侧 **API keys** → **Create new key** → 复制 `sk-` 开头那串
   - **只显示一次**，关掉弹窗就找不回来了
3. **充值**：DeepSeek **没有免费额度**，余额为 0 时所有模型一律 `402 Insufficient Balance`
   - 查余额：`GET https://api.deepseek.com/user/balance`
   - `deepseek-flash` 极便宜，充 10 元够高强度跑很久
4. 填进 `.env`：`LLM_API_KEY=sk-你的key`

可用模型（2026-09 实测 `/v1/models`，老的 `deepseek-chat` / `deepseek-reasoner` **已下架**）：

| 模型 ID | 说明 |
|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash，1M 上下文，便宜，跑本 demo 用这个 |
| `deepseek-v4-pro` | 更强更贵 |

#### 硅基流动 SiliconFlow（已踩坑，不推荐优先试）

1. 注册：打开 https://cloud.siliconflow.cn ，手机号验证码或 GitHub 登录
2. **实名认证**：控制台内完成
3. 建 key：左侧 **API 密钥** → 新建密钥（直达 https://cloud.siliconflow.cn/account/ak ）

**2026-09-26 实测：已跑通。** 光实名不够，两步都要做，缺一个就一直 402：

1. **领代金券**：控制台左侧 → 活动中心 / **认证专享礼** → 领 16 元代金券（180 天有效）。实名**不会自动到账**，必须手动领一次
2. **充 0.01 元激活**：账户从未有过实盘充值余额时，代金券不生效，**连零价模型也 402**。充完立刻可用（实测充值后第一次调用就 200）

**代金券不需要手动"使用"**——调用 API 产生 token 消耗时自动抵扣。扣费顺序：先扣充值余额（那 0.01 元），余额耗尽后自动从代金券扣。查看路径：账户管理 → 余额充值 → 代金券。

> 余额查询接口 `GET /v1/user/info` 已 410 废弃，查不了余额，只能靠实际调用反推：200 = 有钱，402 = 没钱。

平台另外两个坑：无 Mistral 系列（教程原配的 `Mistral-Small-3.2-24B` 用不了）；零价档限速 5–10 QPS + TPM 上限。

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
同时又强调"否则只返回 `false`"，9B 级别的模型会保守地选后者。实测（SiliconFlow `THUDM/GLM-4-9B-0414`，temperature 0.1，两次采样一致）：

```
[user] Hi, can you tell me the price of one of your listings?
pricing_prompt     'false'    <- 期望输出追问，实际判否
scheduling_prompt  'false'
listing_prompt     'true'     <- 于是落到 listing 分支，输出"转专员"
```

修法：切 `PROMPT_VARIANT=strict`（见「提示变体」），让模型只在固定 token 里三选一，追问文案由代码生成。
同一模型同一批 case：`original` 5/7，`strict` 7/7。

同理 `scheduling` 路也有这个毛病，需要显式声明"只要是在约时间就算预约意图，有没有日期不影响判定"，
否则 `Can I schedule a call with an agent?` 会被判成 `false`。

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

`"\nfalse" != "false"`，会被当成"命中"处理。已在 `_call_single_model` 里统一 `strip()`，如自行改代码别漏掉这步。

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

默认走 DeepSeek：国内直连（已实测本机无需代理）、OpenAI 兼容，但**必须充值**。备选智谱（`glm-4-flash` 永久免费，不用充值）、SiliconFlow、百炼、OpenRouter，都在 `.env.example` 里。

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
| **智谱 AI** | `glm-4-flash` 系永久免费，注册+实名即可用（2026-09 唯一实测不用充值的） | `https://open.bigmodel.cn/api/paas/v4/chat/completions` |
| 阿里云百炼 | 新用户各模型约 100 万 token（限期） | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` |
| 魔搭 ModelScope | 2000 req/天，需绑阿里云账号+实名 | `https://api-inference.modelscope.cn/v1/chat/completions` |
| OpenRouter | 25+ 免费模型（ID 带 `:free` 后缀），约 200 req/天 | `https://openrouter.ai/api/v1/chat/completions` |
| Groq / Cerebras | 速度快、按 RPM 限额 | 各平台 `/v1/chat/completions` |

OpenRouter、Groq、Cerebras 部分地区需要代理。
SiliconFlow 未列入：实名后实测仍 402，实际等同必须充值（详见上文）。

环境变量说明：

- `LLM_API_KEY` 为空时不发送 `Authorization` 头，本地 vLLM / `mock_server.py` 照常工作
- `LLM_MODEL_ID` 设置了就覆盖所有提示的模型；不设则各提示用自己配置的 `model_id`
- **key 只放环境变量或 `.env`**（已在 `.gitignore` 中），不要写进代码或提交到 git

### 生产化补强（`agentic_workflows_prod.py`）

教程版 `agentic_workflows.py` 刻意保持极简，下面这些是它上到真实端点会踩的坑，已在 **`agentic_workflows_prod.py`** 逐个补掉。
教程原文件不动，两套提示保持单一来源（prod 版直接 `import` 教程模块的 `SYSTEM_PROMPT_CONFIGURATIONS`）。

| # | 教程版做法 | 线上会怎样 | prod 版做法 |
|---|---|---|---|
| 1 | 每次调用新建 `ClientSession` | 连接池反复重建 | 一次 fan-out 共用一个 session；常驻服务可传入复用 |
| 2 | 无 timeout | 端点挂起 → 请求永久卡死 | `ClientTimeout`(connect 5s / sock_read 15s / total 20s) |
| 3 | 无重试 | 429 / 5xx 抖动直接失败 | 指数退避 + 抖动重试，尊重 `Retry-After`，默认最多 3 次尝试 |
| 4 | 不区分错误 | 401 / 402 也跟着重试，白烧时间和钱 | 按状态码分诊：401/402/403/400/404 **fail fast**，只有 408/429/5xx 重试 |
| 5 | 三路全并发 | 打爆免费档 QPS（上一节那条 429 警告） | `asyncio.Semaphore`，`MAX_CONCURRENCY` 默认 3 |
| 6 | `gather` 不接异常 | 一路挂 → 整个 fan-out 崩 | `return_exceptions=True` + 该路降级为"未命中"，其余分支照常判定 |
| 7 | 只返回一段字符串 | 出问题没法排障 | 返回 `RouteDecision`：`route` / `matched_prompt` / `raw` / `errors` / `degraded` / `elapsed_ms` |
| 8 | `startswith()` 解析 | `\nfalse`、`**false**`、尾随解释全部误判命中 | `normalize()` 归一化 + strict token 白名单 |
| 9 | 同步 `call_models()` 包壳 | Web 服务里 `asyncio.run()` 嵌套直接报错 | 同步/异步双入口，async 版可在已有 loop 中 `await` |

第 8 点的白名单不是洁癖：路由层里 pricing / scheduling 各有一个"原样透出"的兜底分支，
模型跑偏时吐出的长文本会被当成追问句直接发给用户。实测抓到过一次
（GLM-4-9B 在 scheduling 路上返回 "I'm sorry, but I don't have access to real-time data ..."），
开白名单后该路按未命中处理，落到真正命中的分支。

**用法**

```python
from agentic_workflows_prod import run_agentic_workflow

decision = run_agentic_workflow(history)
decision.reply        # 教程版那个字符串
decision.route        # pricing / scheduling / listing / fallback
decision.degraded     # True = 有分类路失败或输出非法，结果是降级出来的

# 已有 event loop（FastAPI 等常驻服务）：用 async 入口并复用 session
decision = await run_agentic_workflow_async(history, session=session)

# 价格查询可注入真实 DB，不再硬编码字典
decision = run_agentic_workflow(history, price_lookup=lambda lid: db.get_price(lid))
```

```bash
python3 agentic_workflows_prod.py "Can I schedule a call with an agent?"
python3 agentic_workflows_prod.py "..." --json     # 完整 RouteDecision（含每路原文与错误）
python3 agentic_workflows_prod.py "..." -v         # 打开重试/降级日志
```

**新增环境变量**（都有默认值，不配也能跑）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_CONCURRENCY` | `3` | 并发上限。免费档 1–3，别盲目调大 |
| `REQUEST_TIMEOUT_TOTAL` | `20` | 单次尝试总超时（秒） |
| `REQUEST_TIMEOUT_CONNECT` | `5` | 建连超时（秒） |
| `REQUEST_TIMEOUT_SOCK_READ` | `15` | 读取超时（秒） |
| `MAX_RETRIES` | `2` | 额外重试次数（0 = 不重试） |
| `RETRY_BASE_DELAY` / `RETRY_MAX_DELAY` / `RETRY_JITTER` | `0.5` / `8` / `0.3` | 退避基数、上限、抖动比例 |
| `MAX_TOKENS` / `TEMPERATURE` | `100` / `0.1` | 采样参数。分类任务建议 `TEMPERATURE=0` 降抖动 |
| `STRICT_TOKENS` | 跟随 `PROMPT_VARIANT` | token 白名单开关。跑 `original` 变体时必须为 `false` |

**回归测试**

```bash
python3 test_prod.py --only fault   # 离线：注入 429 / 500 / 超时 / 坏 JSON / 脏输出 / 跑偏长文本
python3 test_prod.py --only real    # 真实端点：7 条路由
python3 test_prod.py                # 全跑
```

`--only fault` 用本地故障端点验证补强真的生效，实测 26/26 通过，其中几条关键断言：

- 429 连打两次后重试成功 → 不降级、不抛异常
- 持续 500 → 三路全失败，降级到 fallback 而不是崩溃
- 只有 pricing 路挂 → 跳过它，scheduling 分支照样命中
- 端点 sleep 不返回 → 1.01s 熔断（不卡死）
- HTTP 200 但响应体缺 `choices` → 只打 1 次（不可重试错误不重试）
- `MAX_CONCURRENCY=1/2/3` → 实测并发峰值 1/2/3，耗时 1.23s / 0.81s / 0.41s（三路 × 0.4s，与限流数学一致）
- 白名单关 → 跑偏的长文本被透出给用户（反面用例）；白名单开 → 拦下，落到正确分支
