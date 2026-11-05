# android-debug-agent (ada)

LLM 驱动的安卓真机调试 / 自动化测试 / 日志诊断智能体。
设计文档：《安卓设备智能调试与测试 Agent 系统设计方案 V2.0》（学习使我快乐/安卓智能调试Agent/）。

## 架构核心

- **唯一通道收口**：所有设备操作只经 `channel.AdbChannel`——风险分级（绿/黄/红）、破坏性操作确认门、审计日志、统一超时重试都在这一层
- **结构化贯穿**：模块间只传结构化对象（DeviceProfile / AppState / ExecutionTrace / LogBundle / Diagnosis），不透传原始终端文本
- **能力探测降级**：环境不支持时返回明确错误而非崩溃（`UnsupportedError`）
- **裸 shell 逃生口默认关闭**：`ADA_ALLOW_SHELL=1` 才开放

## MVP 已实现（V0.1.0）

| 模块 | 能力 |
|---|---|
| 设备接入 | 枚举（USB/网络区分）、画像（型号/SDK/ABI/ROM/root/屏幕）、会话缓存、健康检查 |
| 应用操作 | 安装（ABI/minSdk 预检 + 自动授权）、启动、force-stop、清数据、pm grant/revoke |
| 场景执行 | YAML 用例确定性回放；语义定位（id/text/desc）；14 种动作；每步截图+时间轴标记 |
| 日志捕获 | logcat 独立线程 + ring buffer；FATAL/native/ANR 实时检测；步骤-日志时间戳对齐 |
| 规则诊断 | 崩溃栈解析（帧→源码行）、模式库（NPE/OOM/ANR/权限/IllegalState/native）、崩溃指纹（跨版本稳定） |
| 报告 | Markdown：环境信息 + 执行过程表 + 诊断结论（含证据与指纹） |

## 使用

```bash
pip install -e .            # 依赖仅 PyYAML
ada devices                 # 列出设备
ada profile <serial>        # 设备画像
ada install app.apk -s <serial>
ada run examples/login_flow.yaml -s <serial> -p com.example.app   # 执行+诊断+报告
ada diagnose crash.log -p com.example.app                         # 离线日志诊断
```

MCP Server（挂 WorkBuddy / Claude / Cursor）：

```bash
pip install -e '.[mcp]'
ada-mcp    # stdio transport
```

工具：`ada_devices` `ada_device_profile` `ada_health` `ada_install` `ada_launch` `ada_run_scenario` `ada_logcat` `ada_diagnose_log`。
MCP 模式下无交互能力，RED 级操作一律拒绝（安全默认）；CLI 模式走人工确认门。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests   # 40 项
```

## 路线图（对应设计方案 §9）

- 二期：崩溃指纹库 CrashDB（指纹已产出）+ 时间轴回放 + 探索模式 + LLM 诊断层
- 三期：修复验证闭环 + 用例自动沉淀 + 复现路径压缩 + 脱敏管道
- 四期：多设备对照 + 诱发测试 + 规则自学习 + 性能基线
