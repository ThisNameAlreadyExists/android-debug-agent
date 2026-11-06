# android-debug-agent (ada)

LLM 驱动的安卓真机调试 / 自动化测试 / 日志诊断智能体。

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

## 二期已实现（V0.2.0）

| 模块 | 能力 |
|---|---|
| CrashDB 指纹库 | SQLite 持久化；历史命中秒回；已修复指纹重现自动回归告警；跨设备/版本聚合统计（`ada crashes history/show/fixed`） |
| LLM 诊断层 | 规则未命中时走本地 Ollama；外发前正则脱敏（手机号/身份证/token/IP/邮箱）；LLM 故障自动降级回规则结论 |
| 探索模式 | `ada explore "目标"`：UI 树→LLM 决策→执行循环；界面指纹循环检测 + 步数上限 + 危险控件黑名单（卸载/支付/短信等）+ 崩溃即停 |
| 时间轴回放 | `replay_<用例>.html`：单文件零外链，内嵌截图 + 时间轴滑块/播放，每步含动作、Activity、该步时间窗日志 |

真机验证（Smart POS 1 / Android 13）：脚本模式全链路通过；探索模式决策-执行闭环通过（含真实 qwen2.5:14b 驱动）。

## 三期已实现（V0.3.0）

| 模块 | 能力 |
|---|---|
| 修复验证闭环 | `ada verify case.yaml --fingerprint <fp> [--apk 修复版.apk]`：装新版→重跑→原指纹消失且全过=修复确认→自动 mark_fixed + 用例沉淀 |
| 用例自动沉淀 | 修复确认后复现用例固化入 `regression_cases/`（按指纹去重）；`ada regress` 一键全量回归，原指纹重现即报回归失败 |
| 复现路径压缩 | `ada minimize case.yaml --fingerprint <fp>`：ddmin 变体自动删减步骤，产出最小可复现 YAML（结构性步骤 launch/clear_data/grant 保护不删） |
| 隐私分级管道 | `ADA_PRIVACY=L0/L1/L2`：L0 仅本地模型 / L1 脱敏后可发云端（默认）/ L2 全量；每批外发记 `privacy_audit.jsonl` 审计 |

**多设备说明**：所有命令带 `-s <serial>` 路由指定设备，多台连接时可分别操作；`ada mrun` 已支持多设备并行对照（见四期）。

## 四期已实现（V0.4.0）

| 模块 | 能力 |
|---|---|
| 多设备对照诊断 | `ada mrun case.yaml -s S1,S2,...`：同一用例多设备并行执行（独立通道/线程隔离），自动 diff 通过情况、崩溃指纹（设备特异识别）、执行路径分叉点 |
| 主动诱发测试 | 用例中插入 `chaos` 步骤：低内存（send-trim-memory）、后台回收、旋转风暴、运行中权限回收、断网恢复 |
| 规则自学习 | `ada learn --exception X --category Y --cause ... --fix ...` 沉淀规则；诊断漏斗变为 内置规则→学习规则→LLM，LLM 调用随使用递减 |
| 性能基线回归 | `ada run` 通过后顺带采集冷启动时长/内存峰值/帧 P95，与最近 10 次中位数基线对比，超 +20% 自动报性能回归 |

## 使用

```bash
pip install -e .            # 依赖仅 PyYAML
ada devices                 # 列出设备
ada profile <serial>        # 设备画像
ada install app.apk -s <serial>
ada run examples/login_flow.yaml -s <serial> -p com.example.app   # 执行+诊断+报告+回放
ada run case.yaml -s <serial> --llm                               # 启用 LLM 深度诊断
ada explore "测试登录流程找 bug" -s <serial> -p com.example.app    # LLM 自主探索
ada diagnose crash.log -p com.example.app                         # 离线日志诊断
ada crashes history                                               # 崩溃指纹库历史
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
