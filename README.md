# Experiment Notifier v0.2.0

一个 **统一安装、两个命令** 的企业微信实验通知器，不需要修改任何训练 Repo。

| 命令 | 用途 |
|---|---|
| `notify-run` | 普通训练通知：读取实验 JSON config，推送开始/结束、成功/失败、耗时 |
| `notify-run-optuna` | Optuna 通知：复用同一套 Webhook 和 Config 解析功能，在普通通知基础上额外读取 Best Trial / Best Params |

两者都是 **Experiment Notifier 同一个 Python package 里的 CLI 子工具**，不是两个独立安装包、两套 Webhook，也不是需要修改两个 Repo 的代码。**V1 的 `notify-run` 实现保持原样**，只是在同一个安装包中加入 `notify-run-optuna`。不依赖 SLURM；训练 stdout/stderr 原样显示，退出码随原命令返回。

## 安装

```bash
unzip experiment-notifier-v2.zip
cd experiment-notifier-v2
python -m pip install -e .
```

如果想直接读取 Optuna Storage（推荐），在 **运行 notify-run-optuna 的 Python 环境中**安装 Optuna：

```bash
python -m pip install -e '.[optuna]'
```

如果只使用 JSON 结果模式，不需要安装 Optuna。

## 配置企业微信

```bash
export QYAPI_WEBHOOK='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY'
```

建议放进 `~/.config/experiment-notifier/env`，权限设为 `chmod 600`，并在 `~/.bashrc` 中 `source`。

## 原 v1 命令：功能完全不变

```bash
notify-run python -u train.py --config config.json
notify-run --finish-only -- python -u train.py --config config.json
```

## v2 Optuna 命令

### A. 推荐：Study 使用 Optuna Storage

训练脚本已有 `storage="sqlite:///optuna.db"`，且对应 Study 已持久化时：

```bash
notify-run-optuna \
  --storage sqlite:///optuna.db \
  --study-name gaussian_query \
  -- \
  python -u optuna_search.py --config config.json
```

无需增加或更改任何训练脚本代码。通知内容：Repo、主机、实验 Config、运行耗时、退出码、Trial 数量、Complete/Pruned/Failed、Best Trial、Best Objective、Best Params。

如果 Storage 中**恰好只有一个 Study**，可以省略 `--study-name`。若已有多个，则必须指定以免读错。

### B. 从实验 config 自动提取 Storage

`notify-run-optuna` 自动识别配置字段，例如：

```json
{
  "learning_rate": 0.00002,
  "optuna": {
    "storage": "sqlite:///optuna.db",
    "study_name": "gaussian_query"
  }
}
```

因此命令可缩短为：

```bash
notify-run-optuna python -u optuna_search.py --config config.json
```

同样支持顶层 `study_name`、`optuna_storage`、`storage_url` 等字段。**读取 config 不意味着原 Optuna 脚本会自动启用 Storage；脚本本身必须确实写入相应 Storage。**

### C. 你的 Optuna 未使用 Storage，而是输出 JSON

如果脚本已有生成结果 JSON 的逻辑：

```bash
notify-run-optuna --results runs/optuna_results.json -- \
  python -u optuna_search.py --config config.json
```

识别以下结构：

```json
{
  "study_name": "gaussian_query",
  "best_trial_number": 37,
  "best_value": 0.6532,
  "best_params": {"lr": 0.00002, "sigma": 0.05, "mc": 32}
}
```

或者 `{"best_trial": {"number": 37, "value": 0.6532, "params": {...}}}`。工具检查文件修改时间，避免使用上次实验遗留的旧结果。

### D. 可选：有新 Best 时实时推送

```bash
notify-run-optuna \
  --storage sqlite:///optuna.db \
  --study-name gaussian_query \
  --poll-interval 60 \
  --notify-new-best \
  -- \
  python -u optuna_search.py --config config.json
```

每 60 秒检查一次 Storage，**仅检测到新的 Best Trial 时才推送**。轮询依赖持久化 Storage；进程结束时一定尝试最后一次汇总。若 Trial 优化很快，轮询中途的 Best 可能错过，但最终汇总仍包含最终最佳值。多目标 Study 会输出 Pareto trial 数量，而非虚构单个最佳目标。

### E. 仅推送最终结果

```bash
notify-run-optuna --finish-only --storage sqlite:///optuna.db --study-name gaussian_query -- \
  python -u optuna_search.py --config config.json
```

## 使用规则

- `notify-run-optuna` **不会自动创建 Optuna Storage**，不会把纯内存 Study 变为可读取的结果。无 Storage/JSON 时仍正常运行训练，最后说明缺少数据源。
- `--storage` 和 `--results` 属于通知器选项，放在 `--` 前；训练脚本原有的 `--config` 放在 `--` 后。
- 所有路径按执行命令时的当前工作目录解析。建议在训练 Repo 根目录运行。
- 配置字段名包含 password、token、secret、api_key 等时会被脱敏；请不要在一般字段的值内写入密码。
- 通知失败不会影响训练；不影响原进程正常退出码。
- 若通知器环境没有 Optuna，可以安装 `.[optuna]` 或用 `--results` 指向实验生成的 JSON。
- `notify-run` v1 的 Python 文件在 v2 包中未被更改；原始 v1 zip 也单独保留。

## 测试

在项目根目录：

```bash
python -m unittest discover -s tests -v
```
