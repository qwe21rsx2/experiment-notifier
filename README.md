# Experiment Notifier

零侵入式企业微信实验通知器。无需修改任何训练 Repo，不依赖 SLURM，也无需第三方 Python 依赖。

## 安装

```bash
python -m pip install -e /path/to/experiment-notifier
```

建议安装到用于启动实验的 Python 环境。若训练使用另一个 Conda 环境，可将该环境的 Python 绝对路径放在 `notify-run` 后。

## 配置 Webhook

```bash
export QYAPI_WEBHOOK='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY'
```

注意：`export` 只对当前 Shell 会话及其子进程生效。不要将真实密钥提交到 Git。

## 使用

```bash
notify-run python -u train.py --config config.json
notify-run --finish-only -- python -u train.py --config config.json
notify-run --config configs/experiment.json -- python -u train.py
```

自动识别训练命令中的 `--config FILE`、`--config=FILE`、`-c FILE`；默认按 JSON 解析。配置读取失败时仍会启动训练，并在消息中提示。

自动推送：Repo 名称、主机、配置路径、脱敏后的实验配置、开始/结束状态、退出码和耗时。通知失败不影响训练；训练日志原样输出。支持 Ctrl+C / SIGTERM 信号转发。

**注意**：`notify-run` 不会自动推送指标文件；它仅推送配置与进程状态。对于 shell 管道、重定向等语法，请使用 `bash -lc '...'` 作为被包装的命令，并用 `--config` 显式指定配置文件。

**限制**：企业微信 Markdown 消息长度有限，过长配置会被截断。配置脱敏基于字段名，建议不要在配置值中保存密钥。
