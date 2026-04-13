# mano-skill

本仓库现在是一个“本地桌面执行端 + 仓库内 Linux orchestrator”的第一版实现。

当前默认链路：

```text
User / CLI -> local client -> local orchestrator -> planner -> local client executes actions
```

客户端仍然负责本地截图、鼠标键盘执行、动作结果回传。变化点是默认服务端不再是 `mano.mininglamp.com`，而是本地 orchestrator。

## 当前已实现

- `run` 和 `stop` 都支持 `--server-url`
- 默认服务地址是 `http://127.0.0.1:8000`
- `MANO_CUA_SERVER_URL` / `MANO_SERVER_URL` 可覆盖默认地址
- 支持 `--headless` / `--no-overlay`
- 仓库内 FastAPI orchestrator，已实现：
  - `POST /v1/sessions`
  - `POST /v1/sessions/{id}/step`
  - `POST /v1/sessions/{id}/close`
  - `POST /v1/devices/{id}/stop`
  - `POST /v1/sessions/{id}/go_no`
  - `GET /v1/sessions/{id}`
  - `GET /healthz`
- 无 `OPENAI_API_KEY` 时使用 mock planner
- 有 `OPENAI_API_KEY` 时优先使用 OpenAI Responses API planner

## 开发环境安装

如果你已经有 conda 环境 `mano-skill-dev`：

```bash
conda run -n mano-skill-dev python -m pip install -r requirements.txt
```

如果还没有环境，可以先创建：

```bash
conda create -y -n mano-skill-dev python=3.11
conda run -n mano-skill-dev python -m pip install -r requirements.txt
```

## 快速开始

### 1. 启动 orchestrator

默认监听 `127.0.0.1:8000`：

```bash
conda run -n mano-skill-dev python -m orchestrator
```

或使用 `uvicorn`：

```bash
conda run -n mano-skill-dev uvicorn orchestrator.app:app --host 127.0.0.1 --port 8000 --reload
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
```

如果 orchestrator 跑在远程 Linux 上，也建议仍然监听 `127.0.0.1:8000`，然后通过 SSH 隧道给 Windows 客户端使用，不要直接把端口暴露到公网。

### 2. 启动客户端

默认连接本地 orchestrator：

```bash
conda run -n mano-skill-dev python visual/vla.py run "Observe the desktop and finish cleanly"
```

显式指定服务地址：

```bash
conda run -n mano-skill-dev python visual/vla.py run "Open a page" --server-url http://127.0.0.1:8000
```

无 overlay 模式：

```bash
conda run -n mano-skill-dev python visual/vla.py run "Observe the desktop and finish cleanly" --headless
```

停止当前设备上的活动 session：

```bash
conda run -n mano-skill-dev python visual/vla.py stop --server-url http://127.0.0.1:8000
```

### 3. 跨机运行

如果 orchestrator 跑在 Linux 机器上，而客户端跑在另一台机器上：

```bash
conda run -n mano-skill-dev python visual/vla.py run "Your task" --server-url http://<linux-host>:8000
```

也可以用环境变量：

```bash
export MANO_CUA_SERVER_URL=http://<linux-host>:8000
conda run -n mano-skill-dev python visual/vla.py run "Your task"
```

### 4. Windows 通过 SSH 连接 Linux orchestrator

假设：

- 你已经可以在 Windows 上执行 `ssh tt`
- Linux 上的 orchestrator 已经启动在 `127.0.0.1:8000`
- Windows 本地有这个仓库和 conda 环境 `mano-skill-dev`

先在 Linux 上启动 orchestrator：

```bash
ssh tt
cd /path/to/mano-skill
conda run -n mano-skill-dev python -m orchestrator
```

然后在 Windows 上建立本地转发：

```powershell
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 8000:127.0.0.1:8000 tt
```

转发成功后，Windows 本地的 `http://127.0.0.1:8000` 就会连到远程 Linux 的 orchestrator。

验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

然后在 Windows 本地运行 client：

```powershell
conda run -n mano-skill-dev python .\visual\vla.py run "打开记事本并输入 hello" --server-url http://127.0.0.1:8000 --headless
```

停止当前设备任务：

```powershell
conda run -n mano-skill-dev python .\visual\vla.py stop --server-url http://127.0.0.1:8000
```

注意：

- Windows 本地必须运行 client，因为截图和输入注入都发生在 Windows 本机
- SSH 只负责把 HTTP API 连起来，不负责桌面控制
- 跑任务期间，SSH tunnel 那个窗口不能关闭

### 5. Windows 批处理脚本

仓库里提供了一个现成脚本：

[`scripts/windows/run_client_via_ssh.bat`](/home/chh/gitprojects/mano-skill/scripts/windows/run_client_via_ssh.bat)

它会做这些事：

- 检查本地 `http://127.0.0.1:8000/healthz`
- 如果隧道还没起来，就自动用 `ssh tt` 拉起一个隧道窗口
- 通过本地转发地址运行 client
- 额外支持 `stop`、`status`、`go_no`

默认配置：

- `SSH_ALIAS=tt`
- `LOCAL_PORT=8000`
- `REMOTE_HOST=127.0.0.1`
- `REMOTE_PORT=8000`
- `CONDA_ENV=mano-skill-dev`
- `CLIENT_FLAGS=--headless`

Windows 用法：

```bat
scripts\windows\run_client_via_ssh.bat run "打开记事本并输入 hello"
scripts\windows\run_client_via_ssh.bat stop
scripts\windows\run_client_via_ssh.bat status <session_id>
scripts\windows\run_client_via_ssh.bat go_no <session_id>
```

如果你想改 SSH 别名或本地端口，可以在当前终端先设环境变量：

```powershell
$env:SSH_ALIAS = "tt"
$env:LOCAL_PORT = "18000"
scripts\windows\run_client_via_ssh.bat run "your task"
```

如果改了 `LOCAL_PORT`，脚本会自动把 client 指向新的本地转发地址。

## Headless 与人工确认

当 planner 返回 `CALL_USER` 时：

- 有 overlay 时，可以在界面里继续
- `--headless` 时，客户端会轮询 session 状态，等待服务端恢复

headless 模式下恢复一个等待确认的 session：

```bash
curl -X POST http://127.0.0.1:8000/v1/sessions/<session_id>/go_no
```

查询 session 状态：

```bash
curl http://127.0.0.1:8000/v1/sessions/<session_id>
```

## OpenAI Planner

未设置 `OPENAI_API_KEY` 时，orchestrator 默认使用 mock planner。

设置 OpenAI：

```bash
export OPENAI_API_KEY=...
export MANO_OPENAI_MODEL=gpt-5.4
export MANO_OPENAI_REASONING_EFFORT=medium
export MANO_PLANNER_MODE=auto
```

可选环境变量：

```bash
export MANO_ORCHESTRATOR_HOST=127.0.0.1
export MANO_ORCHESTRATOR_PORT=8000
export MANO_OPENAI_TIMEOUT=90
```

说明：

- planner 使用 OpenAI Responses API
- 截图通过 `input_image` 传入
- 输出通过 `json_schema` 约束为动作 JSON
- OpenAI 调用失败时会回退到 mock planner

## Mock Planner 行为

mock planner 的目标是先把端到端链路跑通：

- 第一步要求客户端上传截图
- 第二步返回一个无害的 `wait`
- 下一步返回 `DONE`
- 若任务文本看起来包含敏感操作，会先返回 `CALL_USER`

## 约束与已知边界

- 真正执行桌面动作仍然需要交互式桌面会话
- Windows 端如果要稳定执行 GUI 自动化，建议在用户登录后的会话中运行
- Linux 下 `pynput` 需要图形会话；没有 `DISPLAY` 时无法真正执行桌面控制
- 当前版本已完成本地链路和服务端验证，但还没有在真实 Windows 桌面上做完整验收

## 最小验证

编译检查：

```bash
conda run -n mano-skill-dev python -m compileall visual orchestrator tests
```

单元测试：

```bash
conda run -n mano-skill-dev python -m unittest tests.test_orchestrator
```

CLI 参数检查：

```bash
conda run -n mano-skill-dev python visual/vla.py --help
```
