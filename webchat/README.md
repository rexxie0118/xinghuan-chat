# 星环 Chat · Web 版 AI 对话工作台（Demo）

一个可以直接跑起来的网页版 AI 对话应用：

- **多部门知识库**：每个部门有自己的知识库，登录后可切换；会话按「用户 + 知识库」隔离。
- **Chat / Agent 双模式**：在输入框内切换；Agent 模式可选择默认 Agent、导入 Agent 或导入 Skill。
- **Agent / Skill 管理**：左侧菜单维护导入项，支持本地 Markdown + 脚本，也支持从 GitHub 仓库/目录自动导入。
- **导入 Agent**：上传 Markdown 定义 + 参考脚本（Python / Bash / Node），在对话中触发执行，并展示 stdout / stderr / exit code / 轨迹。
- **接入独立 Agent 服务**：也可以把另一个 repo 的 Agent 暴露成 HTTP 契约；失败自动回退本地 RAG。
- **多用户**：注册 / 登录、HttpOnly Session、部门权限、会话与消息严格按 `user_id` 隔离。
- **对话记录持久化**：SQLite 保存会话和消息，刷新、换设备或重新登录后仍在。
- **RAG + Ontology**：复用仓库根目录的 `kb` 引擎，回答带引用，右侧实时展示实体关系图和原文证据。
- **离线可跑**：默认 `mock` provider + 规则抽取，不需要任何 API Key。

![星环 Chat 工作台](screenshots/workspace.jpg)

演示账号：

| 用户名 | 密码 | 部门 | 可访问知识库 |
| --- | --- | --- | --- |
| `demo` | `demo1234` | 产品部 | 产品知识库、公司公共知识库 |
| `alice` | `alice1234` | 研发部 | 研发知识库、公司公共知识库 |
| `bob` | `bob1234` | 人事部 | 人事知识库、公司公共知识库 |

也可以直接注册新账号，注册时选择部门，系统会自动授予「部门知识库 + 公司公共知识库」。

---

## 1. 启动

```bash
cd xinghuan-chat

# 依赖已装在仓库根目录的 .venv 中；如果是新环境：
# python3 -m venv .venv
# .venv/bin/pip install -r webchat/requirements.txt

.venv/bin/python -m webchat --host 127.0.0.1 --port 8787
# 或：./webchat/run.sh --port 8787
```

打开 <http://127.0.0.1:8787>，用 `demo / demo1234` 登录。

首次启动会自动：

1. 在 `webchat/data/webchat.sqlite3` 建用户 / 会话 / 消息表并写入演示账号；
2. 按 `webchat/kbs.json` 创建 4 个部门知识库，分别从 `webchat/knowledge/<kb>/` 摄取到 `webchat/data/kb_<kb>.sqlite3`；
3. 启动后即可提问。新增 Markdown 放到对应部门目录，重启应用即可增量摄取（内容没变会自动跳过）。

> 生产环境请把 `WEBCHAT_SEED_DEMO_USERS` 设为 `false`，并使用 HTTPS + `WEBCHAT_SECURE_COOKIE=true`。

---

## 2. 界面

| 部门知识库切换 | Agent / Skill 管理 |
| --- | --- |
| ![知识库切换](screenshots/kb-menu.jpg) | ![Agent / Skill 菜单](screenshots/agent-skill-menu.jpg) |

GitHub 导入见 [screenshots/github-import.jpg](screenshots/github-import.jpg)，Agent 脚本执行见 [screenshots/chat.jpg](screenshots/chat.jpg)。登录页见 [screenshots/login.jpg](screenshots/login.jpg)，移动端见 [screenshots/mobile.jpg](screenshots/mobile.jpg)。

- **左侧**：**知识库切换器**、新建对话、会话搜索、当前知识库的历史记录、重命名 / 删除。
- **中间**：流式回答、Markdown、引用角标、脚本执行结果、可展开的执行轨迹、追问建议。
- **输入框**：`Chat` / `Agent` 切换；Agent 模式下右侧出现 Agent 选择器。
- **右侧**：当前知识库的本体图谱（可拖拽 / 缩放 / 点节点看证据）、引用列表、知识资料与 provider 状态。
- **顶部**：`Chat` / `Agent` 引擎切换（和输入框同步），当前 Agent 来源（本地 / 外部 / 导入）。

两种模式：

| 模式 | 行为 |
| --- | --- |
| `Chat` | 混合检索 → 直接生成带引用回答（内部 mode=`rag`） |
| `Agent` | 检索 → 展开 2 跳图谱 → 组合证据 → 默认 Agent 或导入 Agent（可执行脚本） |

离线模式下回答会明确标注「离线抽取」，不会假装调用过大模型。

### 通用知识与知识库回答

Chat 模式分两条路径：

- 知识库有足够证据：严格引用式回答，带 `[1]` 引用和「LLM 生成」标记；
- 知识库没有证据或模型拒答：切换到 DeepSeek 通用知识回答，标记为「LLM 通用」，明确提示没有知识库引用。

例如问「给出一个西安的介绍」：

```text
空知识库证据 → LLM 通用回答
提示：以下为通用知识，并非知识库结论
```

问「北极星项目的负责人是谁？」仍然走严格 RAG：

```text
LLM 生成 → 林晓 [1]
```

这样可以避免“知识库里没有西安，就完全不能回答”的体验，同时公司内部问题仍然优先使用知识库证据。

### 多轮对话

- 会话历史持久化在 SQLite，刷新后仍然存在；
- Chat / Agent / Prompt Skill 会把最近 8 条消息放进 LLM 上下文；
- 检索阶段会把「他 / 它 / 那…呢 / 还有」这类追问与上一轮问题合并，避免只拿半句话去检索；
- 本地 mock 没有语言推理能力，不参与多轮理解；配置 DeepSeek/ARK 后才是完整的多轮体验。

### 执行轨迹中的代码逻辑

![执行轨迹代码逻辑](screenshots/trace-code-logic.jpg)

每个轨迹步骤都可以展开「代码逻辑」，直接看到这一步实际做了什么：

- **理解问题**：`strip()`、追问合并、指代处理；
- **检索知识库**：向量召回、FTS/BM25、图谱召回、RRF 融合、rerank；
- **扩展本体图谱**：seed 实体、2 跳 BFS；
- **组合证据**：chunk 编号、历史块、图谱事实块、prompt 拼接；
- **生成引用式回答**：LLM / mock 分支、引用编号提取；
- **校验引用**：越界编号过滤、chunk 回填；
- **执行脚本**：临时目录、最小环境变量、进程组超时、artifact 持久化。

这样用户不需要读源码，也能在轨迹里看到每一步的实现逻辑。

---

## 3. 多部门知识库

配置在 [`kbs.json`](kbs.json)：

```json
{
  "departments": ["产品部", "研发部", "人事部"],
  "knowledge_bases": [
    {
      "id": "product",
      "name": "产品知识库",
      "department": "产品部",
      "dir": "knowledge/product",
      "sample_questions": ["北极星项目的负责人是谁？"]
    },
    {
      "id": "common",
      "name": "公司公共知识库",
      "department": "all",
      "dir": "knowledge/common"
    }
  ]
}
```

访问规则：

- `department` 等于用户部门的知识库：可读写自己的会话。
- `department: "all"` 的知识库：所有部门可见，用于公司制度、术语和公共资料。
- 其他部门的知识库：接口返回 403，切换器里也不会出现。
- 对话创建时写入 `kb_id`；切换知识库后只看到该知识库下的会话。

知识库目录：

| 知识库 | 部门 | 语料 | 示例问题 |
| --- | --- | --- | --- |
| 产品知识库 | 产品部 | 6 篇 | 北极星负责人、星环能力、Agent 接入 |
| 研发知识库 | 研发部 | 4 篇 | 依赖关系、发布回滚、P1 值班 |
| 人事知识库 | 人事部 | 4 篇 | 年假、试用期、报销、远程 |
| 公司公共知识库 | 全公司 | 3 篇 | 公司业务、信息安全、术语 |

新增部门只要：加一个语料目录、在 `kbs.json` 增加条目、在 `departments` 数组增加部门名，然后重启。新注册用户会自动获得对应部门的知识库。

---

## 4. 导入 Agent 并执行脚本

> 执行层已经抽象成 `SandboxRunner`；后续换 Docker/K8s 不需要改 Agent 编排或前端，见 [架构扩展点](docs/architecture.md)。

左侧菜单有 **Agent / Skill** 子菜单：

- 点击任一条目：切到 Agent 模式并选用它；
- 「管理」：查看、删除、上传；
- 「GitHub 导入」：粘贴 GitHub 仓库/目录地址自动导入。

![Agent / Skill 菜单](screenshots/agent-skill-menu.jpg)

输入框右侧的 Agent 选择器也提供同样的列表。

### 从 GitHub 自动导入

![GitHub 导入](screenshots/github-import.jpg)

支持以下地址：

```text
https://github.com/owner/repo
https://github.com/owner/repo/tree/main/skills/pdf
https://github.com/owner/repo/blob/main/skills/pdf/SKILL.md
https://raw.githubusercontent.com/owner/repo/main/skills/pdf/SKILL.md
```

导入规则：

- 目录下优先找 `SKILL.md`，其次 `AGENT.md` / `agent.md`，最后 `README.md`；
- `SKILL.md` → `kind=skill`，`AGENT.md` → `kind=agent`；
- 最多 250 个文本文件、单文件 512KB、总大小 5MB，目录最多 4 层；
- 只要文本类脚本/引用（`.py` / `.sh` / `.js` / `.mjs` / `.html` / `.css` / `.md` / `.json` / `.yaml` 等），跳过图片和二进制；
- 私有仓库需要给 Web 服务配置 `GITHUB_TOKEN`；
- 只接受 `github.com` / `raw.githubusercontent.com`，通过 GitHub API 拉取，不做任意 URL 代理。

GitHub Skill 通常没有单一入口脚本，所以导入后：

- 有 `entrypoint`：Agent 模式执行脚本；
- 没有 `entrypoint`：这是 **Prompt Skill**，需要真实 LLM 读取 `SKILL.md`。配置 DeepSeek/ARK 后会把 Skill 指令 + 知识库证据交给模型；
- mock 环境不会假装执行 Skill，而是明确提示「Prompt Skill 需要真实 LLM」，不再输出无关的 RAG 答案；
- 想让 Skill 直接运行脚本，请给它配置 `entrypoint`，或按 Agent 类型导入。

### 导入时测试

导入成功（本地或 GitHub）后不会直接关闭弹窗，而是进入测试面板：

1. **入口脚本**：entrypoint 是否存在；
2. **本地命令**：python / bash / node 是否可用；
3. **实际执行**：在沙箱里跑一次测试输入，返回 exit code；
4. **stdout**：是否有输出；
5. **产物下载**：是否产生文件，并给出下载链接。

对应接口：

```http
POST /api/agents/{id}/test
{"question": "测试执行"}
```

也可以之后在「Agent / Skill 管理」里点勾选按钮重新测试。

### 执行配置与产物下载

在「Agent / Skill 管理」里点条目的编辑按钮，可以配置：

- `entrypoint`：如 `scripts/run.py` / `scripts/new-deck.sh`
- `runtime`：`python` / `bash` / `node`
- `timeout`：1–15 秒

脚本执行后：

- `AGENT_OUTPUT_DIR` 里的文件会保存为产物；
- 脚本在输入目录中新生成的文件（例如 `examples/<name>/`）会打成一个 `workspace.zip`；
- 聊天消息的执行面板会显示「生成文件」下载链接；
- 下载接口会校验该 run 属于当前登录用户。

例如把 `html-ppt` 的 `scripts/new-deck.sh` 配成 entrypoint，输入一个 deck 名称，就会执行脚手架并生成包含 skill 资源的 `workspace.zip`。Demo 里另建了 `html-ppt-builder` 演示这种用法。

### Pipeline：Prompt Skill + 脚本产物

![Pipeline 生成与下载](screenshots/pipeline.jpg)

`html-ppt-auto` 把内容生成和文件生成串起来：

```text
Prompt Skill 读取 SKILL.md
  → DeepSeek 生成完整 HTML
  → 写入 _generated_content.html
  → 执行 scripts/auto_deck.sh
  → new-deck.sh 创建脚手架
  → 用生成的 HTML 覆盖 index.html
  → workspace.zip 作为可下载产物
```

启用方式是在 Markdown frontmatter 中声明：

```yaml
pipeline: true
```

并配置一个读取 `AGENT_CONTENT_FILE` 的 entrypoint。Pipeline Agent 的 trace 会显示：

```text
生成 PPT 内容 → 执行脚本 → 生成 PPT 文件
```

### 本地上传

在「Agent / Skill 管理」里也可以上传：

- 一个 **Agent Markdown**：描述目标、步骤和约束；
- 一组 **参考脚本**：`.py` / `.sh` / `.js` / `.mjs`（也可带 `.txt` / `.json` 数据文件）；
- 类型可选 **Agent** 或 **Skill**；可选填入口脚本。

Markdown 可以用 frontmatter 声明入口：

```markdown
---
name: 发布值守助手
description: 发布失败后的止损与回滚建议
entrypoint: run.py
runtime: python
timeout: 8
---

# 发布值守助手

先止损，再判断是否回滚，最后同步值班群。
```

脚本约定：

- `argv[1]` 是用户问题；
- stdin 是一段 JSON：`{question, agent, context}`，其中 `context.hits` 是本次 RAG 召回；
- stdout 直接作为回答（支持 Markdown）；写 `[1]`、`[2]` 会映射为引用；
- stderr 和退出码会展示在「脚本执行」面板里。

界面触发链路：

```
切到 Agent → 选择导入的 Agent → 发送消息
  → 检索当前知识库
  → 加载 Agent Markdown + 脚本
  → 写临时目录 → 执行 entrypoint（timeout）
  → stdout 作为回答 / 失败回退知识库
  → 展示脚本执行 + 工具轨迹
```

参考示例：[examples/sample_agent/AGENT.md](examples/sample_agent/AGENT.md) 和 [run.py](examples/sample_agent/run.py)。

### Apple Container 执行后端

Apple Container 把每个执行放进一个轻量 Linux VM，适合比本地 subprocess 更强的隔离：

```bash
# macOS 26+ / Apple silicon
container system start

cd /Users/rex/Documents/DeepseekWS/xinghuan-chat
WEBCHAT_SANDBOX=apple-container \
APPLE_CONTAINER_IMAGE=python:3.12-slim \
../.venv/bin/python -m webchat --port 8787
```

实现：[`webchat/sandbox/apple.py`](sandbox/apple.py)。每次 `run()`：

- `container run --rm` 创建一次性容器；
- `/agent/in` 挂载输入，`/agent/out` 挂载输出；
- rootfs `--read-only`，`--cap-drop ALL`，默认使用 Apple Container 的 `default` 网络；Apple CLI 目前没有 `none`，无网需要宿主防火墙/代理。
- 只挂载临时输入/输出目录；
- 超时后 `container stop` / `kill` / `delete`；
- 产物在执行结束前复制到宿主机 artifact store。

镜像必须包含所需 runtime：

- Python：`python`
- Bash：`bash`
- Node：`node`

限制：

- `apple.py` 是一次性执行容器；`apple_runtime.py` 已提供每用户/每 session 常驻容器管理器，但尚未接入聊天路径；
- Apple Container 官方要求 macOS 26+、Apple silicon；
- 生产多租户 Linux 环境仍建议 Kubernetes + gVisor/Firecracker；
- 不要在不可信 Agent 场景使用 `container machine`（它会挂载 home、开放全部 capability）。

### 常驻运行时管理器

`webchat/sandbox/apple_runtime.py` 提供：

```python
runtime = AppleContainerRuntime(settings)
runtime.ensure("user:<id>")          # volume + create + start
runtime.stop("user:<id>")            # 保留容器和 volume
runtime.ensure("user:<id>")          # 再次 start
runtime.delete("user:<id>", delete_volume=False)
runtime.list_managed()
```

命名是确定性的：

```text
key → xh-rt-<hash> + xh-vol-<hash>
```

容器/volume 带 `xh.managed`、`xh.key`、`xh.volume` label，方便清理和恢复。

当前仅在 `app.state.container_runtime` 挂载，**还没有接入聊天请求**；下一步才是 per-user/session 映射和 idle reaper。

### per-user 容器与 workspace 映射

映射持久化在：

```sql
user_runtimes(user_id, runtime_key, container_name, volume_name, image, state,
              created_at, last_active_at, updated_at)
container_workspaces(conversation_id, user_id, runtime_key, workspace_path,
                     created_at, last_active_at)
```

API：

```text
GET    /api/runtimes/me
POST   /api/runtimes/me/ensure          {conversation_id?}
POST   /api/runtimes/me/restore         {conversation_id}
POST   /api/runtimes/me/stop
DELETE /api/runtimes/me?delete_volume=false
```

### 空闲回收与 checkpoint

后台 `RuntimeReaper` 在 Apple sandbox 下运行，默认：

```text
30 分钟无活动 → stop
7 天无活动   → delete container，保留 volume
30 天无活动  → purge volume + 映射
```

可通过环境变量调整：

```dotenv
WEBCHAT_RUNTIME_IDLE_ENABLED=true
WEBCHAT_RUNTIME_IDLE_STOP_SECONDS=1800
WEBCHAT_RUNTIME_IDLE_DELETE_SECONDS=604800
WEBCHAT_RUNTIME_IDLE_PURGE_SECONDS=2592000
WEBCHAT_RUNTIME_REAP_INTERVAL=60
```

checkpoint 存在 `container_checkpoints`，记录最近消息、run、产物和轨迹摘要；恢复时先 ensure 容器/workspace，再加载 checkpoint。

Apple sandbox 下，首次使用导入 Agent 会自动 ensure 用户容器和会话 workspace。

### 失败策略与取消

`webchat/runtime/failures.py` 提供：

- `FailureClass`：transient / rate_limit / timeout / auth / permission / input / upstream / cancelled
- `RetryPolicy`：只重试安全类别，支持指数退避和 `Retry-After`
- `CircuitBreaker`：外部 Agent 连续失败后打开，半开探测

外部 Agent 调用现在会重试 transient/timeout/429，并把失败类别写入轨迹；脚本失败会写入 `execution.failure`。

取消：

```http
POST /api/conversations/{id}/cancel
```

输入框生成中会显示「停止」按钮；取消后保存一条 `answer_mode=cancelled` 的消息。取消是协作式检查，正在进行的单次 LLM/脚本调用不会被强行中断。

### 执行边界

这不是强沙箱，只是 Demo 级隔离：

- 临时工作目录，脚本文件只在该目录内材料化；
- 可选 Apple Container runner：每次执行一个一次性 Linux VM，rootfs 只读、无 capability、默认使用隔离网络配置；
- 环境变量清空，只保留 PATH / TMPDIR 等最小集合；
- 单次 timeout 最大 15 秒，输出截断；
- 最多 250 个文件、单文件 512KB、总大小 5MB；
- 子进程独立进程组，超时会 kill 整个进程组；
- 脚本仍可能读取宿主文件或访问网络。**只导入你信任的脚本**，生产请放到容器 / 独立 job runner / gVisor 等强隔离环境。

服务端可以通过 `WEBCHAT_ALLOW_SCRIPT_EXEC=0` 完全关闭脚本执行；Agent 管理接口：

| 接口 | 作用 |
| --- | --- |
| `GET /api/agents` | 当前用户导入的 Agent / Skill |
| `POST /api/agents` | 导入 Markdown + files |
| `POST /api/agents/import/github` | 从 GitHub 仓库/目录自动导入 |
| `GET /api/agents/{id}` | 完整定义与脚本内容 |
| `PATCH /api/agents/{id}` | 配置 entrypoint / runtime / timeout |
| `GET /api/agent-runs/{run_id}/artifacts/{name}` | 下载本次运行的产物 |
| `DELETE /api/agents/{id}` | 删除自己的 Agent / Skill |

选中导入 Agent 后，发送消息会在请求体带 `agent_id`；服务端校验归属后执行。导入 Agent 优先于外部 Agent 服务。

---

## 5. MCP 工具注册表

MVP 已加在 `webchat/tools.py`，不依赖额外 MCP SDK：

- **stdio MCP**：newline-delimited JSON-RPC，自动 `initialize` + `notifications/initialized`；
- **HTTP MCP**：JSON-RPC POST，支持 JSON 和 SSE 响应、`Mcp-Session-Id`；
- 工具发现：`tools/list`；
- 调用：`tools/call`；
- 每个 server 必须显式 `allow_tools`；
- 单次调用超时、输出截断（20k 字符）、server 状态隔离；
- 配置里的 `${ENV_VAR}` 会展开，token 不进入代码。

### 配置

```bash
cp webchat/mcp.example.json webchat/mcp.json
```

`webchat/mcp.json` 已 gitignore。示例：

```json
{
  "servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "enabled": true,
      "allow_tools": ["read_file", "list_directory"],
      "timeout": 10
    }
  }
}
```

HTTP server：

```json
{
  "servers": {
    "remote": {
      "transport": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
      "enabled": true,
      "allow_tools": ["search"],
      "timeout": 10
    }
  }
}
```

配置完成后重启服务。

### API

| 接口 | 作用 |
| --- | --- |
| `GET /api/tools` | server 状态、已发现工具、JSON Schema |
| `POST /api/tools/{name}/call` | 调用工具，body `{"arguments": {...}}` |
| `GET /api/meta` | 包含 `tools` 摘要 |

registry、调用层和 LLM Tool Loop 已经打通；有工具时 Agent 模式会自动选择并调用。

安全边界：

- `allow_tools` 是白名单，默认 `["*"]` 仅用于本地受信配置；
- stdio server 以独立进程运行，但仍是本机权限，不是强沙箱；
- HTTP server 的 URL/header 由管理员配置，不对普通用户开放修改；
- 工具输出会截断，避免无界上下文。

## 6. 接入另一个 repo 的 Agent

星环 Chat **不 import** Agent 代码，只依赖 HTTP。配置：

```bash
export WEBCHAT_AGENT_URL=http://127.0.0.1:8790
export WEBCHAT_AGENT_TOKEN=optional-bearer-token
export WEBCHAT_AGENT_TIMEOUT=25
.venv/bin/python -m webchat
```

请求：

```http
POST {AGENT_BASE_URL}/chat
Authorization: Bearer <可选>
Content-Type: application/json
```

```json
{
  "user": {"id": "usr_...", "name": "演示用户"},
  "conversation_id": "conv_...",
  "knowledge_base": {"id": "product", "name": "产品知识库", "department": "产品部"},
  "mode": "agent",
  "messages": [{"role": "user", "content": "北极星项目的负责人是谁？"}],
  "context": {
    "question": "北极星项目的负责人是谁？",
    "hits": [
      {
        "index": 1,
        "chunk_id": "chk_...",
        "doc_title": "团队与职责",
        "heading_path": "团队与职责 › 分工",
        "text": "……",
        "score": 0.04
      }
    ],
    "graph": {"nodes": [], "edges": []},
    "memories": []
  }
}
```

响应：

```json
{
  "answer": "最终回答，支持 Markdown",
  "citations": [1, 2],
  "confidence": 0.72,
  "trace": [
    {"title": "调用工具", "detail": "search_knowledge(...)", "status": "done"}
  ],
  "suggestions": ["继续追问 1", "继续追问 2"]
}
```

- `citations` 可以是编号数组（映射到请求里的 `hits`），也可以是完整引用对象。
- 外部 Agent 超时、非 200 或 JSON 非法时，服务端记录错误并**自动回退到本地 RAG**，用户消息和会话不受影响。
- 可运行的参考实现：[`examples/external_agent_server.py`](examples/external_agent_server.py)。把它复制到 Agent repo，替换 `answer()` 里的策略即可。

本地验证外部 Agent：

```bash
# 终端 A
cd webchat/examples && ../../.venv/bin/python external_agent_server.py

# 终端 B
cd xinghuan-chat
WEBCHAT_AGENT_URL=http://127.0.0.1:8790 .venv/bin/python -m webchat --port 8788
```

---

## 7. RAG + Ontology

知识库引擎来自仓库根目录的 `kb/`，Web 层只做适配：

```
用户问题
  ├─ 向量召回 (特征哈希 / 真 embedding)
  ├─ FTS 中文子串召回
  ├─ 图谱多跳召回（实体别名 → 2 跳关系）
  └─ RRF 融合 → 片段 + 实体 + 关系 + 证据句
        ├─ 回答（LLM 或离线抽取式）
        ├─ 引用回填 [n]
        └─ 右侧图谱 / 引用 / 资料面板
```

相关接口（都要求登录）：

| 接口 | 作用 |
| --- | --- |
| `GET /api/kbs` | 当前用户可访问的知识库与文档量 |
| `GET /api/kb/overview?kb=` | 文档、实体、关系、类型、provider 状态 |
| `GET /api/kb/graph?kb=&query=` | 全局图，或问题相关子图 |
| `GET /api/kb/search?kb=&q=` | 只检索不生成，含三路排名与耗时 |
| `GET /api/kb/entities/{id}?kb=` | 实体详情、关系、原文证据 |

每个知识库是独立的 SQLite 文件（`kb_product.sqlite3`、`kb_engineering.sqlite3`…），互不污染。部门权限在 Web 层校验，检索器本身不需要知道用户是谁。

### 接真实模型

#### 方式 A：DeepSeek（最快）

```bash
export WEBCHAT_KB_PROVIDER=deepseek
export DEEPSEEK_API_KEY=sk-你的key
# 可选
export DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
export DEEPSEEK_MODEL=deepseek-chat

.venv/bin/python -m webchat --port 8787
```

也可以写进 `webchat/.env`（已 gitignore，应用启动时自动读取，不会覆盖已存在的环境变量）：

```dotenv
WEBCHAT_KB_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-你的key
```

DeepSeek 只提供 OpenAI 兼容的 **chat** 接口，没有 embeddings 接口。因此这个模式：

- 回答生成走 DeepSeek；
- 检索继续使用本地 hash embedding + FTS + 图谱，**不需要重建知识库**；
- 新文档的实体抽取使用规则抽取，避免启动时消耗 DeepSeek token（回答仍然走 DeepSeek）。

验证是否生效：登录后右下角「资料」面板里，`生成` 应为 `deepseek-chat`；回答徽标会从「离线抽取」变成「LLM 生成」。

#### 方式 B：ARK / OpenAI 兼容（chat + embedding）

```bash
export WEBCHAT_KB_PROVIDER=ark
export ARK_API_KEY=...
.venv/bin/python -m webchat
```

`kb` 也支持 `openai` / 任意 OpenAI 兼容网关。注意：如果原来用 mock 建过索引，换 embedding 模型后需要重建知识库（删除 `webchat/data/kb_*.sqlite3` 后重启，或使用 `kb reindex` 指向该库），否则向量维度不一致会降级为全文检索。

---

## 8. 多用户与数据模型

```sql
users(id, username UNIQUE, display_name, password_hash, department, created_at)
sessions(token_hash PK, user_id, expires_at)
conversations(id, user_id, title, mode, kb_id, created_at, updated_at)
messages(id, conversation_id, user_id, role, content,
         citations_json, meta_json, created_at)
agents(id, user_id, name, description, markdown, entrypoint,
       runtime, timeout, files_json, created_at)
```

- 密码：`scrypt` 加盐哈希；Session Token 只把 SHA-256 摘要写库。
- Cookie：`HttpOnly` + `SameSite=Lax`，默认 7 天，可设 `Secure`。
- 所有会话 / 消息查询都同时带 `conversation_id` 和 `user_id`；猜到别人的 ID 也只会返回 404。
- 会话额外带 `kb_id`；加载会话和发送消息时都会重新校验用户对该知识库的访问权限。
- 旧库启动时自动迁移：补 `users.department` 和 `conversations.kb_id` 字段，默认归到产品知识库。
- 每次回答的引用、轨迹、置信度、图谱快照都存在 `messages.meta_json`，刷新后可完整还原。
- CSRF：写请求校验 `Origin` 与 `Host` 一致；CSP 禁止内联脚本。

自动生成标题：首条用户消息生成会话标题（超过 22 字截断），之后可手动重命名。

---

### Docs：迭代记录与架构历史

页面右上角 **Docs** 进入 `/docs/`。

每次迭代必须同时新增：

```text
webchat/docs/iterations/<date>-<nn>.html   迭代记录 + Todo 快照
webchat/docs/architecture/<date>-*.svg    当前架构图
```

然后更新 `webchat/docs/manifest.json`。架构图只追加、不覆盖，旧版本保留在「架构历史」中。

规则见 [`webchat/docs/README.md`](docs/README.md)。

### Postgres / Redis / Worker / S3

这是可选的生产化层。安装：

```bash
pip install -r requirements-infra.txt
```

配置：

```dotenv
WEBCHAT_DB_URL=postgresql://xinghuan:change-me@127.0.0.1:5432/xinghuan
WEBCHAT_QUEUE_URL=redis://127.0.0.1:6379/0
WEBCHAT_OBJECT_STORE=s3
WEBCHAT_S3_BUCKET=xinghuan
WEBCHAT_S3_ENDPOINT=http://127.0.0.1:9000
```

拆分：

```bash
# Web
python -m webchat --host 0.0.0.0 --port 8787

# Worker
python -m webchat.worker
```

Web 只负责 API/SSE 和入队；Worker 消费 Redis 中的 `agent_run` job，执行 Agent 并写回共享数据库。

队列 API：

```http
POST /api/jobs/agent/{conversation_id}
GET  /api/jobs/{job_id}
```

本地无 Redis 时队列 API 返回 `503`，普通聊天仍使用进程内执行。

对象存储：

```text
WEBCHAT_OBJECT_STORE=local   # data/artifacts
WEBCHAT_OBJECT_STORE=s3      # S3 / MinIO / R2 兼容
```

知识库检索库仍是 SQLite 文件；Postgres 当前用于 WebChat 控制面。完整容器化示例见 `docker-compose.yml`。

### 并发准入与背压

内容入口现在经过 `ConcurrencyGate`：

```text
全局 semaphore   WEBCHAT_MAX_CONCURRENT_RUNS=4
每用户 semaphore WEBCHAT_MAX_USER_RUNS=1
等待队列         WEBCHAT_MAX_QUEUE=32
排队超时         WEBCHAT_QUEUE_TIMEOUT_SECONDS=30
运行时限         WEBCHAT_RUN_TIMEOUT_SECONDS=180
```

行为：

- 队列满 → HTTP `429` + `Retry-After`
- 排队超时 → HTTP `429`
- 运行超时 → 取消 token，生成一条 cancelled 消息
- 客户端断开 → worker 继续保存消息；gate lease 在 worker 结束时释放
- `/api/meta` 返回 `concurrency` 指标

核心文件：[concurrency.py](webchat/concurrency.py)。

### 用户级长期记忆

存储：

```sql
user_memories(
  id, user_id, kind, content, subject, predicate, object_text,
  importance, confidence, hits, source_conversation_id, source_message_id,
  created_at, updated_at, last_used_at, valid_to, superseded_by
)
```

类型：`fact` / `preference` / `task`。

自动提取（只匹配明确表达，避免把每句话都写成记忆）：

```text
记住：...
我叫...
我的名字是...
我更喜欢 / 我偏好...
以后叫我 / 回答用...
```

召回：

```text
词法相似度 0.65
+ importance 0.25
+ recency 0.10
```

记忆只进入软上下文，**不作为文档证据**，不参与 `[n]` 引用。

API：

```http
GET    /api/memories
POST   /api/memories        {kind, content, subject?, predicate?, importance?}
DELETE /api/memories/{id}
```

Topbar「记忆」打开管理弹窗：

- 按 fact / preference / task 筛选；
- 显示来源会话、创建日期、命中次数；
- 支持手动添加、硬删除。

删除是硬删除；冲突槽位通过 `valid_to` / `superseded_by` 保留旧值审计。

### Token 级上下文预算

`kb/context_budget.py` 提供不需要 tokenizer 的保守估算：

```text
CJK: 约 1 token / 字
ASCII: 约 3.5 字符 / token
```

配置：

```dotenv
WEBCHAT_CONTEXT_MAX_TOKENS=12000
WEBCHAT_CONTEXT_RESERVE_TOKENS=1200
```

预算分配：

```text
question（必选）
→ evidence（知识库证据）
→ history（最近对话）
→ graph（实体）
```

Tool Loop 中：

- 工具结果先截断；
- 每轮调用前用 `fit_messages()` 再检查；
- system 和最新 user 消息保留；
- 旧 tool / history 内容优先压缩；
- `meta.context` 记录 `max_tokens / used_tokens / truncated / dropped_sections`。

严格 RAG prompt 使用 `answer.max_context_tokens`（默认 7000）；超出后压缩证据并过滤已删除的引用编号。

### Tool Loop

Agent 模式下，如果：

- provider 不是 mock；
- ToolRegistry 里有 MCP/plugin tools；
- `WEBCHAT_TOOL_LOOP_ENABLED=true`；

则走真正的 LLM tool calling 循环：

```text
system + 历史 + 证据 + tools
  → LLM 返回 tool_calls
  → ToolRegistry.call
  → role=tool 结果回填
  → LLM 继续
  → 直到回答或 max_steps
```

配置：

```dotenv
WEBCHAT_TOOL_LOOP_ENABLED=true
WEBCHAT_TOOL_MAX_STEPS=4
```

边界：

- 工具名会转成 OpenAI 函数名 `demo_echo`，再映射回 `demo.echo`；
- 工具结果截断后回填，避免上下文爆炸；
- 工具错误会作为 `role=tool` 错误结果回给模型；
- 达到 max_steps 或 LLM 失败会回退知识库回答；
- Tool Loop 只用于默认本地 Agent，不覆盖导入 Agent / 外部 Agent。

回答元数据：

```json
{
  "answer_mode": "tool_loop",
  "tool_steps": 1,
  "tools_used": ["demo.echo"]
}
```

### Trusted Plugin API 与 manifest

当前只支持**可信内部插件**，运行在 Web 进程内。示例：

```bash
cp webchat/plugins.example.json webchat/plugins.json
```

```json
{
  "plugins": [
    {
      "name": "audit",
      "version": "1.0.0",
      "entrypoint": "webchat.plugins.audit:plugin",
      "trusted": true,
      "permissions": ["hooks", "tools"],
      "hooks": ["agent_end"]
    }
  ]
}
```

插件可以：

- `ctx.on(event, handler)`：订阅生命周期；
- `ctx.register_tool(...)`：注册 `plugin.<name>.<tool>`；
- manifest 必须 `trusted=true`；
- 默认只允许 `webchat.plugins.*`；
- 没有热加载；重启生效。

API：

```text
GET /api/plugins
```

安全边界：

- 插件是进程内代码，不做租户隔离；
- 租户代码只能走 MCP / Apple Container；
- `plugins.json` 已 gitignore。

### AgentKernel 与插件扩展点

`webchat/runtime/` 是 P0 的 kernel 层，不重写现有 RAG / ImportedAgent：

```text
AgentKernel
  ├── HookBus          同步生命周期事件，hook 异常隔离
  ├── retrieval_start / retrieval_end
  ├── answer_start / answer_end
  ├── tool_call / tool_result
  └── agent_start / agent_end / agent_error
```

当前插件是**可信内部插件**，在 Web 进程内运行：

```python
class MyPlugin:
    name = "my-plugin"

    def setup(self, ctx):
        ctx.on("retrieval_end", lambda event, payload, _ctx: ...)
```

不把租户上传的 Python 插件加载进 Web 进程；外部工具走 MCP，不可信 Agent 走 Apple Container。

## 9. 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WEBCHAT_HOST` / `WEBCHAT_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `WEBCHAT_DATA_DIR` | `webchat/data` | SQLite 与各知识库数据目录 |
| `WEBCHAT_DB` | `<data>/webchat.sqlite3` | 用户 / 会话库 |
| `WEBCHAT_KBS_CONFIG` | `webchat/kbs.json` | 部门、知识库与示例问题配置 |
| `WEBCHAT_KB_PROVIDER` | `mock` | 所有知识库共用：`mock` / `deepseek` / `ark` / `openai` / `openai-compatible` |
| `WEBCHAT_ALLOW_SCRIPT_EXEC` | `true` | 是否允许执行导入 Agent 的参考脚本 |
| `WEBCHAT_SANDBOX` | `local` | `local` / `apple-container`；docker/k8s 可注册 |
| `APPLE_CONTAINER_BIN` | `container` | Apple Container CLI 路径 |
| `APPLE_CONTAINER_IMAGE` | `python:3.12-slim` | Agent runtime 镜像，需包含对应 runtime |
| `APPLE_CONTAINER_CPUS` | `2` | 每个执行容器的 CPU 配额 |
| `APPLE_CONTAINER_MEMORY` | `2G` | 每个执行容器的内存配额 |
| `APPLE_CONTAINER_NETWORK` | `default` | Apple Container 网络；不支持 `none`，需要无网时用宿主防火墙 |
| `GITHUB_TOKEN` | 空 | 导入私有 GitHub 仓库 / 提高 API 速率限制 |
| `WEBCHAT_MCP_CONFIG` | `webchat/mcp.json` | MCP server 配置；文件不存在则工具为空 |
| `WEBCHAT_TOOLS_ENABLED` | `true` | 是否启用 MCP 工具注册表 |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek Key；配合 `WEBCHAT_KB_PROVIDER=deepseek` |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | DeepSeek 兼容网关地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 可换 `deepseek-reasoner` |
| `WEBCHAT_AGENT_URL` | 空 | 外部 Agent 服务地址；为空用本地 Agent |
| `WEBCHAT_AGENT_TOKEN` | 空 | 外部 Agent Bearer Token |
| `WEBCHAT_AGENT_TIMEOUT` | `25` | 外部 Agent 超时（秒） |
| `WEBCHAT_SESSION_DAYS` | `7` | Session 有效期 |
| `WEBCHAT_SECURE_COOKIE` | `false` | HTTPS 下应设 `true` |
| `WEBCHAT_SEED_DEMO_USERS` | `true` | 是否写入 demo / alice / bob |

完整示例见 [`.env.example`](.env.example)。

---

## 10. 测试

```bash
cd xinghuan-chat
.venv/bin/python -m unittest discover -s webchat/tests -v
```

覆盖：登录 / 错误密码 / 登出、注册校验、部门知识库访问控制、知识库维度的会话隔离、SSE 流式回答与持久化、自动标题、双账号会话隔离、多轮历史进入提示词 / 追问检索、知识图谱与实体详情、Agent/Skill 导入、嵌套脚本路径、GitHub URL 解析、脚本执行 / 产物下载 / 用户隔离 / Pipeline 标志与 HTML 提取 / 导入测试预检、MCP stdio/HTTP 工具发现与调用、空工具配置 API、Apple Container 执行与产物、AgentKernel hook bus/plugin 生命周期、docs 静态站点、Apple Container 常驻运行时命名/契约、per-user 运行时/workspace 映射、checkpoint、idle reaper、失败分类/重试/熔断/取消、trusted plugin manifest/loader、LLM tool loop、token 级上下文预算、用户级长期记忆、记忆管理 UI、准入控制/并发队列、Postgres/Redis/Worker/S3 适配。

> 当前共 57 项测试，全部通过。

---

## 11. 目录

```
webchat/
├── main.py                 FastAPI 路由、SSE、鉴权依赖、静态资源
├── config.py               Settings（全部环境变量）
├── db.py / repository.py   SQLite schema 与数据访问（按 user_id 隔离）
├── security.py             scrypt、Session Token
├── knowledge.py            kb 引擎适配、多知识库注册表、检索/图谱/实体
├── agent.py                本地 / 外部 / 导入 Agent 编排
├── runtime/                AgentKernel + hook bus + trusted plugin seam
├── tools.py                MCP stdio/HTTP client + Tool Registry
├── sandbox/apple.py        Apple Container 一次性执行 runner
├── sandbox/apple_runtime.py Apple Container 常驻容器/volume 管理器
├── agent_defs.py           Agent / Skill 导入、解析、存储
├── github_importer.py      GitHub 仓库/目录导入（API + URL 校验）
├── artifacts.py            运行产物持久化、下载、run 归属校验
├── sandbox/                SandboxRunner 协议 + local 实现（可扩展 Docker/K8s）
├── kbs.json                部门、知识库、示例问题配置
├── knowledge/              17 篇演示语料
│   ├── product/            产品部 · 6 篇
│   ├── engineering/        研发部 · 4 篇
│   ├── hr/                 人事部 · 4 篇
│   └── common/             全公司 · 3 篇
├── static/                 index.html / styles.css / app.js / favicon
├── examples/               外部 Agent 参考服务 + sample_agent/
├── tests/                  TestClient 端到端测试
└── data/                   运行时生成（已 gitignore）
```

---

## 12. 边界与下一步

这是可运行的 Demo，不是生产系统：

- 默认离线抽取式回答只会“组织原文”，不具备真正的语言推理；接真实模型后质量取决于模型与语料。
- 知识库按「部门」授权，一个用户当前只属于一个部门；跨部门项目组、文档级 ACL、管理员后台还没有做。
- 注册时自选部门只适合 Demo；生产环境应由管理员或 SSO 分配部门，不能让用户自己提升权限。
- 用户不能上传文档。生产需要管理员摄取、审核、配额和审计日志。
- 外部 Agent 契约是同步 JSON，Web 端做本地打字流；如需真正的 token 级流式，可把 `/chat` 改成 SSE 并把 `delta` 透传。
- 导入 Agent 的脚本执行是非强沙箱；生产必须换成容器 / 独立 job runner 并加资源配额、网络策略和审计。
- 没有限流、审计日志、邮箱验证、密码找回；上线前应补齐。
- 图谱布局是前端轻量力导向，适合几十个节点；大规模图谱应换成 Cytoscape / G6 + 后端采样。

### 已经为后续留好的扩展点

1. **Agent**：实现 `/chat` 契约即可，不需要改 Web 代码；`trace` 可直接渲染成时间线。
2. **知识库**：`knowledge.py` 是唯一适配层；新增部门只需加语料目录和 `kbs.json` 条目。
3. **多模型**：`kb` 的 provider 已抽象 embedding / chat，接 ARK、OpenAI、本地 vLLM 都走同一配置。
4. **消息级扩展**：`meta_json` 可继续塞 token 用量、工具参数、审批状态、评分反馈。
