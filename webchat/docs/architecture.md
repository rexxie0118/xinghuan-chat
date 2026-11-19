# 扩展点：Agent 执行与文件边界

这份文档说明「哪些东西可以替换、替换时不需要动哪些层」，方便后续把本地 subprocess 换成 Docker / Kubernetes / gVisor。

## 分层

```
Browser
  └─ POST message {content, mode, agent_id}
       │
       ▼
FastAPI (webchat/main.py)
  ├─ 校验 user / kb / agent 归属
  ├─ 持久化 user message
  └─ AgentRunner.run(..., kb, imported_agent)
       │
       ├─ KnowledgeRegistry → 检索当前部门知识库
       ├─ AgentRunner 组装 trace / citations / meta
       └─ SandboxRunner.run(RunRequest) → RunResult
            │
            └─ LocalSubprocessRunner（当前）
               DockerSandboxRunner（未来）
               K8sJobRunner（未来）
```

**关键约束：AgentRunner 不直接调用 `subprocess`**。它只依赖 `SandboxRunner` 协议。这样换执行后端时，Agent 编排、数据库、SSE、UI 都不用改。

## 1. SandboxRunner

协议定义在 [webchat/sandbox/__init__.py](../sandbox/__init__.py)：

```python
class SandboxRunner(Protocol):
    name: str
    def run(self, request: RunRequest) -> RunResult: ...
```

`RunRequest` 只包含执行所需的一切：

- `run_id`
- 输入文件（Markdown 不在这里，脚本和参考数据在 `files`）
- `entrypoint` / `runtime` / `timeout`
- `question`
- `context`（RAG hits + graph）
- `agent_name` / `agent_description` / `agent_markdown`
- `allow_exec`

`RunResult` 包含：

- `ran`
- `command` / `exit_code` / `stdout` / `stderr`
- `timed_out` / `duration_ms`
- `artifacts`（文件名、大小、sha256）
- `runner` / `run_id`

### 新增 Docker runner

```python
# webchat/sandbox/docker.py
from . import RunRequest, RunResult, register_runner

class DockerSandboxRunner:
    name = "docker"
    def __init__(self, settings):
        self.settings = settings
    def run(self, request: RunRequest) -> RunResult:
        # 1. 建 runs/<run_id>/in, out
        # 2. 写 files 到 in
        # 3. docker run --network none --read-only ...
        # 4. 收集 out → artifacts
        # 5. kill/cleanup
        ...

register_runner("docker", lambda settings: DockerSandboxRunner(settings))
```

然后：

```bash
WEBCHAT_SANDBOX=docker
WEBCHAT_SANDBOX_IMAGE=agent-sandbox@sha256:...
```

不需要改 `AgentRunner`、`main.py` 或前端。

## 2. 文件边界

Local runner 现在会创建：

```
临时根目录/
├── in/       # RunRequest.files 写在这里，脚本 cwd
└── out/      # 约定：脚本只能往这里生成文件
```

并通过环境变量告诉脚本：

- `AGENT_INPUT_DIR`
- `AGENT_OUTPUT_DIR`
- `AGENT_RUN_ID`

未来的 Docker runner 应保持同样的目录语义：

```
宿主机 runs/<run_id>/in  → 容器 /work:ro
宿主机 runs/<run_id>/out → 容器 /out:rw
```

这样脚本代码不需要因为执行后端改变而修改。

### ArtifactStore（预留）

`RunResult.artifacts` 目前只有元数据；Local runner 在 `finally` 清理临时目录，所以生成文件不会保留。

下一步可以加：

```python
class ArtifactStore(Protocol):
    def put(self, *, run_id, user_id, name, content: bytes) -> str: ...
    def get(self, *, run_id, user_id, name) -> bytes: ...
```

实现可以是本地受控目录、S3/MinIO 或数据库 BLOB；再增加：

- `agent_runs` 表
- `agent_artifacts` 表
- 鉴权下载接口 `/api/agent-runs/{run_id}/artifacts/{name}`
- TTL 清理

这些都可以独立于 SandboxRunner 增加，不需要改 Agent 定义格式。

## 3. 其他扩展点

### Agent 定义

`agent_defs.py` 只负责：

- 解析 frontmatter
- 校验文件名/扩展名/大小
- 写入 `agents` 表
- 返回 `ImportedAgent`

要支持更多格式（例如 `.zip`、多文件目录、`agent.yaml`），只改这一层；执行层仍接收 `files` 列表。

### 外部 Agent 服务

`AgentRunner._run_external()` 调 `POST /chat`。它是与「导入 Agent」并列的另一种执行方式，未来也可以做成 `AgentBackend` 接口；当前先保留，避免过度抽象。

### 知识库

`KnowledgeRegistry` 按部门提供 `KnowledgeService`。新增部门、切换租户、换检索器都只影响这一层。

### 存储

`repository.py` 管用户/会话；`agent_defs.py` 管 Agent 定义；`webchat/db.py` 管 schema。
未来加 `agent_runs` / `agent_artifacts` 不需要改 UI 的消息结构。

## 4. 当前不做的事

- 不实现 Docker/K8s runner
- 不持久化生成文件
- 不做 per-user 执行配额/队列
- 不做强沙箱

但接口、目录语义、`RunResult.artifacts` 和注册表已经留好，后续接入时不需要重写 Agent 编排或前端触发逻辑。

## 5. 相关文件

- [sandbox/__init__.py](../sandbox/__init__.py)
- [sandbox/local.py](../sandbox/local.py)
- [agent.py](../agent.py)
- [agent_defs.py](../agent_defs.py)
- [main.py](../main.py)
