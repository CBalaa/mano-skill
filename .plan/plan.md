# Linux 模型 + Windows 执行代理计划

## 目标

把系统拆成两部分：

- Linux 端负责模型推理、任务规划、状态管理、日志和策略控制
- Windows 端只负责截图、执行鼠标键盘动作、回传结果

目标形态不是“Linux 直接通过 SSH 操控 Windows 桌面”，而是“Linux 作为大脑，Windows 作为本地手脚”。

---

## 现状判断

当前仓库 `Mano-P` 基本是文档仓库。实际可复用的执行代码在：

- `reference/mano-skill/visual/model/task_model.py`
- `reference/mano-skill/visual/computer/computer_action_executor.py`
- `reference/mano-skill/visual/computer/computer_use_util.py`

这套代码已经实现了：

- 本地截图
- 本地鼠标键盘控制
- 动作执行结果回传

当前默认接的是官方云端服务 `https://mano.mininglamp.com`，所以要做的是：

- 保留或改造 Windows 本地执行层
- 把云端服务替换成你自己的 Linux 服务

---

## 结论

这个方案可行，推荐实施。

但有一个硬约束：

- Windows 桌面自动化必须在 Windows 本机的交互式用户会话里执行

这意味着：

- 可以用 SSH 启动、更新、拉日志
- 不能只靠 SSH shell 代替桌面执行器
- Windows 端必须常驻一个本地 agent

---

## 推荐技术路线

### 路线 A：复用 `mano-skill` 的执行器，替换服务端

这是推荐路线，风险最低，开发量最小。

思路：

1. 保留 Windows 端已有的截图和动作执行代码
2. 给客户端增加可配置的 `server_url`
3. Linux 端实现兼容接口
4. Linux 端调用 OpenAI 模型产出动作 JSON

优点：

- 复用现成执行代码
- 已经有动作协议雏形
- 快速验证

缺点：

- 当前代码主要按 macOS 优化，Windows 需要补测试
- 现有客户端带一个 overlay UI，可能要做无界面模式

### 路线 B：重写一个更薄的 Windows agent

思路：

- 只保留最核心能力：截图、执行、回传
- 不复用 overlay、view model、当前 CLI 结构

优点：

- 架构更干净
- 更适合做服务化、后台常驻

缺点：

- 重写量更大
- 第一版速度慢于路线 A

### 路线 C：只做浏览器自动化，不接管整台 Windows

思路：

- Linux 跑 Playwright 或 browser-use
- 仅控制网页，不控制本地桌面程序

优点：

- 最稳定
- 最容易跑在 Linux

缺点：

- 不能满足“控制 Windows 桌面应用”的目标

结论：

- 如果你要的是完整桌面控制，选路线 A
- 如果你只要 Web，路线 C 更省事

---

## 推荐架构

### 1. Windows Agent

职责：

- 捕获主显示器截图
- 接收 Linux 下发的动作
- 执行动作
- 回传动作结果、错误和新截图

建议实现：

- Python 第一版：`mss` + `pynput` + `requests`
- 后续可打包成 `PyInstaller` 单文件 exe

运行方式建议：

- 不要做成普通 Windows Service 直接操控桌面
- 更稳的是“用户登录后自动启动”
- 可用“计划任务 At logon”或启动项

原因：

- Windows Service 往往不在用户交互桌面里
- GUI 自动化需要活的桌面会话

### 2. Linux Orchestrator

职责：

- 创建任务 session
- 接收截图和执行回执
- 调用模型规划下一步
- 返回结构化动作
- 管理暂停、停止、人工确认和审计日志

建议实现：

- `FastAPI` 或 `Flask`
- 第一版优先 `FastAPI`

建议接口：

- `POST /v1/sessions`
- `POST /v1/sessions/{id}/step`
- `POST /v1/sessions/{id}/close`
- `POST /v1/devices/{id}/stop`

这与 `reference/mano-skill` 现有调用方式兼容。

### 3. Model Layer

推荐起步：

- 主模型：`gpt-5.4`
- 成本优化候选：`gpt-5.4-mini`

建议策略：

- 复杂界面理解、歧义判断、多步规划：`gpt-5.4`
- 简单重复点击、表单填写、纯执行阶段：后续再切 `gpt-5.4-mini`

第一版不要过早优化模型路由，先把单模型链路跑通。

---

## 动作协议建议

建议继续沿用 `mano-skill` 现有的动作格式，让 Windows 端少改。

服务端返回示例：

```json
{
  "status": "RUNNING",
  "reasoning": "当前登录窗口已出现，下一步点击用户名输入框。",
  "action_desc": "点击用户名输入框",
  "actions": [
    {
      "id": "toolu_001",
      "name": "computer",
      "input": {
        "action": "left_click",
        "coordinate": [512, 318]
      }
    }
  ]
}
```

支持动作建议先限制在这几类：

- `left_click`
- `double_click`
- `right_click`
- `mouse_move`
- `type`
- `key`
- `scroll`
- `left_click_drag`
- `open_app`
- `open_url`
- `wait`
- `done`
- `fail`
- `call_user`

不要一开始就设计太多动作类型，先保证动作少而稳定。

---

## 模型调用建议

### 第一版策略

输入给模型：

- 用户目标
- 当前截图
- 上一步动作及结果
- 可用动作列表
- 输出格式约束

输出要求：

- 只返回结构化 JSON
- 最多返回 1 到 3 个原子动作
- 必须带简短 `reasoning`
- 遇到敏感操作返回 `call_user`

建议约束：

- 单步动作尽量短
- 不允许长链条“想当然”执行
- 每轮执行后必须重新看截图

### 为什么 GPT-5.4 够用

对你的目标来说，模型需要的是：

- 看图理解 GUI
- 根据截图选动作
- 生成稳定 JSON
- 多轮状态跟踪

这类任务 `gpt-5.4` 可以承担。第一版完全可以用它做核心决策层。

需要注意的是：

- 成本可能偏高
- step loop 太密时延迟会比较明显

所以推荐顺序是：

1. 先用 `gpt-5.4` 打通
2. 成功后再评估 `gpt-5.4-mini`

---

## 实施阶段

### Phase 1：打通最小链路

目标：

- Windows 截图发给 Linux
- Linux 返回一个点击动作
- Windows 执行并回传结果

要做的事：

1. 给 `reference/mano-skill` 客户端加 `--server-url`
2. 去掉对官方服务的写死依赖
3. 在 Linux 上实现一个假的 step server
4. 先不接模型，直接返回固定动作

验收标准：

- Windows 能收到动作并完成点击
- Linux 能收到回执和截图

### Phase 2：接入 GPT-5.4

目标：

- Linux 服务根据截图自动决定下一步

要做的事：

1. 定义系统提示词
2. 定义动作 JSON schema
3. 接入 OpenAI API
4. 把截图和历史上下文送入模型

验收标准：

- 能完成简单任务
- 例如打开应用、点击输入框、输入文本、提交

### Phase 3：补安全和人工确认

目标：

- 避免误操作

要做的事：

1. 对这些动作加确认：
   - 删除
   - 支付
   - 发送消息
   - 批量修改
   - 系统设置改动
2. 支持 `call_user`
3. 支持 stop/pause

验收标准：

- 敏感动作不会直接盲执行

### Phase 4：Windows 常驻化

目标：

- 让 Windows agent 稳定常驻

要做的事：

1. 加无界面模式
2. 打包 exe
3. 做开机登录后自动启动
4. 增加日志和崩溃恢复

验收标准：

- 用户登录后 agent 自动可用
- 不需要手工开控制台

### Phase 5：稳定性优化

目标：

- 提高真实任务成功率

要做的事：

1. 加重试策略
2. 加坐标校验
3. 限制动作速率
4. 增加 OCR 或 UI 辅助定位
5. 记录失败轨迹做回放

验收标准：

- 重复任务成功率明显提升

---

## 关键风险

### 1. Windows 交互会话问题

风险：

- agent 如果跑在非交互 session，鼠标键盘不会真正作用到用户桌面

对策：

- 只在用户登录后的会话里运行

### 2. 坐标缩放和多显示器

风险：

- DPI 缩放、分辨率变化、多屏会让坐标失真

对策：

- 第一版只支持主显示器
- 固定缩放逻辑
- 执行动作前后强制截图校验

### 3. 模型幻觉

风险：

- 模型看错元素，误点

对策：

- 每步后回看截图
- 原子动作化
- 敏感动作加确认

### 4. 成本和延迟

风险：

- 每一步都发图给大模型，开销会很高

对策：

- 第一版先接受
- 稳定后再引入：
  - 截图裁剪
  - 状态缓存
  - 小模型路由

---

## 不推荐的路线

### 1. Linux 直接通过 SSH 控制 Windows GUI

不推荐原因：

- SSH 不是 GUI 交互通道
- 无法替代本地桌面事件注入
- 对真实用户桌面状态不可见

### 2. Windows Service 直接桌面自动化

不推荐原因：

- 常见权限和 session 问题很多
- 运行稳定性差

---

## 近期工作清单

建议按这个顺序推进：

1. 从 `reference/mano-skill` 提取执行器
2. 增加 `--server-url`
3. 在 Linux 上写兼容 API 的最小服务
4. 用固定动作跑通链路
5. 接入 `gpt-5.4`
6. 增加 `call_user` 和 stop
7. 做 Windows 无界面常驻

---

## 最终建议

建议你采用：

- Windows：本地轻量 agent
- Linux：FastAPI + OpenAI 模型服务
- 协议：兼容现有 `mano-skill` step API
- 模型：先用 `gpt-5.4`，跑通后再考虑降到 `gpt-5.4-mini`

这是当前最现实、开发量最可控、最容易尽快得到结果的路线。
