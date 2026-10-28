import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

SENSITIVE = re.compile(r"(password|passwd|secret|token|api[_-]?key|webhook|credential|authorization|private[_-]?key)", re.I)

def redact(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if SENSITIVE.search(str(k)) else redact(v)) for k,v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value

def find_config(command):
    for i, arg in enumerate(command):
        if arg in ("--config", "-c") and i + 1 < len(command):
            return command[i+1]
        if arg.startswith("--config="):
            return arg.split("=",1)[1]
    return None

def repo_name():
    try:
        result = subprocess.run(["git","rev-parse","--show-toplevel"], capture_output=True,text=True,timeout=3)
        if result.returncode == 0:
            return Path(result.stdout.strip()).name
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path.cwd().name

def send(webhook, title, body):
    if not webhook:
        print("[notify-run] QYAPI_WEBHOOK not set; notifications disabled.", file=sys.stderr)
        return
    content = f"**{title}**\n{body}"
    # WeCom markdown messages have size limits; truncate long configs.
    if len(content.encode("utf-8")) > 3500:
        content = content[:1100] + "\n...(configuration truncated)"
    payload = json.dumps({"msgtype":"markdown","markdown":{"content":content}}).encode("utf-8")
    request = Request(webhook, data=payload, headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("errcode") != 0:
            print(f"[notify-run] API error: {result.get('errcode')} {result.get('errmsg')}",file=sys.stderr)
    except (URLError, HTTPError, TimeoutError, ValueError, OSError) as exc:
        print(f"[notify-run] notification failed: {type(exc).__name__}: {exc}",file=sys.stderr)

def main():
    parser=argparse.ArgumentParser(description="Run any command and send experiment config/status to WeCom.")
    parser.add_argument("--config",dest="override_config",help="Explicit experiment config path")
    parser.add_argument("--finish-only",action="store_true",help="Only send final notification")
    parser.add_argument("command",nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.command
    if command and command[0]=="--":
        command=command[1:]
    if not command:
        parser.error("Missing command. Example: notify-run python train.py --config config.json")
    config_path=args.override_config or find_config(command)
    config={}
    config_error=None
    if config_path:
        try:
            with open(config_path,encoding="utf-8") as handle:
                config=json.load(handle)
        except (OSError,ValueError) as exc:
            config_error=f"{type(exc).__name__}: {exc}"
    else:
        config_error="No --config argument found"
    safe_config=json.dumps(redact(config),ensure_ascii=False,indent=2)
    metadata=f"> Repo: {repo_name()}\n> Host: {socket.gethostname()}\n> Config: {config_path or 'N/A'}\n"
    if config_error:
        metadata+=f"> Config warning: {config_error}\n"
    started=datetime.now().astimezone().isoformat(timespec="seconds")
    webhook=os.environ.get("QYAPI_WEBHOOK")
    if not args.finish_only:
        send(webhook,"🚀 Experiment Started",metadata+f"> Started: {started}\n\n**Configuration**\n```json\n{safe_config}\n```")
    start=time.monotonic()
    child=None
    pending_signal=None
    def forward(sig, frame):
        nonlocal pending_signal
        pending_signal=sig
        if child and child.poll() is None:
            child.send_signal(sig)
    old_handlers={}
    for sig in (signal.SIGINT,signal.SIGTERM):
        old_handlers[sig]=signal.signal(sig,forward)
    try:
        child=subprocess.Popen(command)
        if pending_signal and child.poll() is None:
            child.send_signal(pending_signal)
        returncode=child.wait()
    except OSError as exc:
        print(f"[notify-run] cannot launch command: {exc}",file=sys.stderr)
        returncode=127
    finally:
        for sig,handler in old_handlers.items():
            signal.signal(sig,handler)
    duration=time.monotonic()-start
    hours,rem=divmod(int(duration),3600)
    minutes,seconds=divmod(rem,60)
    title="✅ Experiment Completed" if returncode==0 else "❌ Experiment Failed"
    send(webhook,title,metadata+f"> Exit code: {returncode}\n> Duration: {hours:02}:{minutes:02}:{seconds:02}\n\n**Configuration**\n```json\n{safe_config}\n```")
    return returncode if returncode>=0 else 128-returncode

if __name__=="__main__":
    sys.exit(main())
