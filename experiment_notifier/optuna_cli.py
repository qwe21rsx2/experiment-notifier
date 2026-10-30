"""Optuna-aware, external CLI process wrapper. No changes to training repos."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

from .cli import find_config, redact, repo_name, send


def log(message):
    print(f"[notify-run-optuna] {message}", file=sys.stderr)


def nested_get(data, *paths):
    """Read the first non-empty key from a flat or nested config."""
    for path in paths:
        value = data
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            if value is not None and value != "":
                return value
    return None


def resolve_options(args, config):
    storage = args.storage or nested_get(
        config,
        ("optuna", "storage"), ("optuna", "storage_url"),
        ("optuna_storage",), ("storage_url",),
        ("study", "storage"),
    )
    study_name = args.study_name or nested_get(
        config,
        ("optuna", "study_name"), ("optuna", "name"),
        ("study_name",), ("study", "name"),
    )
    results = args.results or nested_get(
        config,
        ("optuna", "results_json"), ("optuna", "results_path"),
        ("optuna_results",), ("optuna_results_json",),
        ("results_json",),
    )
    return storage, study_name, results


def read_config(config_path):
    if not config_path:
        return {}, "No --config found in training command"
    try:
        with open(config_path, encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return {}, "Config must be a JSON object"
        return data, None
    except (OSError, ValueError) as exc:
        return {}, f"Cannot read config: {type(exc).__name__}: {exc}"


def load_storage_summary(storage, study_name):
    """Official Optuna storage API, optional dependency in the wrapper environment."""
    try:
        import optuna
    except ImportError:
        raise RuntimeError("Install Optuna in the notifier environment: pip install 'experiment-notifier[optuna]'")

    if not study_name:
        studies = optuna.get_all_study_summaries(storage=storage)
        if len(studies) == 1:
            study_name = studies[0].study_name
        elif not studies:
            raise RuntimeError("No studies found in storage")
        else:
            raise RuntimeError("Multiple studies found; specify --study-name")

    study = optuna.load_study(study_name=str(study_name), storage=storage)
    trials = study.get_trials(deepcopy=False)
    counts = {state: 0 for state in ("COMPLETE", "PRUNED", "FAIL", "RUNNING", "WAITING")}
    for trial in trials:
        if trial.state.name in counts:
            counts[trial.state.name] += 1
    directions = [str(direction.name).lower() for direction in study.directions]
    summary = {
        "source": "Optuna storage",
        "study_name": study.study_name,
        "trials_total": len(trials),
        "counts": counts,
        "directions": directions,
    }
    if len(study.directions) == 1:
        try:
            best = study.best_trial
        except ValueError:
            pass  # No completed trials.
        else:
            summary["best_number"] = best.number
            summary["best_value"] = best.value
            summary["best_params"] = best.params
    else:
        summary["pareto_count"] = len(study.best_trials)
        summary["pareto_examples"] = [
            {"number": t.number, "values": t.values, "params": t.params}
            for t in study.best_trials[:3]
        ]
    return summary


def load_json_summary(path, started_at):
    file = Path(path).expanduser()
    if not file.is_file():
        raise FileNotFoundError(f"Optuna results file not found: {file}")
    # Prevent accidentally reporting results from a previous experiment.
    if file.stat().st_mtime < started_at - 2:
        raise RuntimeError(f"Results JSON has not been updated during this run: {file}")
    with file.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Optuna results JSON must be an object")
    best = data.get("best_trial")
    if not isinstance(best, dict):
        best = {}
    summary = {"source": f"Results JSON ({file.name})"}
    for key in ("study_name", "n_trials", "total_trials", "completed_trials", "pruned_trials", "failed_trials"):
        if key in data:
            summary[key] = data[key]
    summary["best_number"] = data.get("best_trial_number", best.get("number"))
    summary["best_value"] = data.get("best_value", best.get("value"))
    summary["best_params"] = data.get("best_params", best.get("params"))
    # Handle both a flat JSON summary and a nested "study" section.
    if "study_name" not in summary and isinstance(data.get("study"), dict):
        summary["study_name"] = data["study"].get("name")
    if summary.get("best_value") is None and not summary.get("best_params"):
        raise ValueError("Results JSON has no recognizable best_value/best_params fields")
    return summary


def choose_summary(storage, study_name, results_path, started_at):
    errors = []
    if storage:
        try:
            return load_storage_summary(storage, study_name), errors
        except Exception as exc:
            errors.append(f"Storage read failed: {type(exc).__name__}: {exc}")
    if results_path:
        try:
            return load_json_summary(results_path, started_at), errors
        except Exception as exc:
            errors.append(f"Results read failed: {type(exc).__name__}: {exc}")
    if not storage and not results_path:
        errors.append("No Optuna storage/results configured. Provide --storage or --results.")
    return None, errors


def format_config(config):
    safe = redact(config)
    rendered = json.dumps(safe, ensure_ascii=False, default=str, separators=(",", ":"))
    # Preserve useful fields and avoid outgrowing the WeCom markdown limit.
    if len(rendered.encode("utf-8")) > 850:
        rendered = rendered[:500] + "... [truncated]"
    return f"**Experiment Config**\n```json\n{rendered}\n```"


def format_summary(summary):
    if not summary:
        return "**Optuna Results**\n> No readable Optuna results; see warnings."
    parts = ["**Optuna Results**", f"> Source: {summary['source']}"]
    if summary.get("study_name"):
        parts.append(f"> Study: {summary['study_name']}")
    for key, label in (("trials_total", "Total trials"), ("n_trials", "Total trials"),
                       ("total_trials", "Total trials"), ("completed_trials", "Completed"),
                       ("pruned_trials", "Pruned"), ("failed_trials", "Failed")):
        if key in summary:
            parts.append(f"> {label}: {summary[key]}")
    if summary.get("counts"):
        counts = summary["counts"]
        parts.append("> Complete / Pruned / Failed / Running: " + " / ".join(
            str(counts.get(k, 0)) for k in ("COMPLETE", "PRUNED", "FAIL", "RUNNING")))
    if summary.get("directions"):
        parts.append("> Direction: " + ", ".join(summary["directions"]))
    if summary.get("best_number") is not None:
        parts.append(f"> Best trial: #{summary['best_number']}")
    if summary.get("best_value") is not None:
        parts.append(f"> Best objective: {summary['best_value']}")
    if summary.get("best_params") is not None:
        rendered = json.dumps(redact(summary["best_params"]), ensure_ascii=False, default=str)
        parts.append(f"> Best parameters: {rendered[:750]}")
    if "pareto_count" in summary:
        parts.append(f"> Pareto-optimal trials: {summary['pareto_count']}")
        for item in summary.get("pareto_examples", []):
            parts.append(f"> Trial #{item['number']} objectives: {item['values']}")
    return "\n".join(parts)


def best_signature(summary):
    if summary and summary.get("best_number") is not None:
        return (summary["best_number"], summary.get("best_value"))
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Run an Optuna script and notify via WeCom without modifying the script."
    )
    parser.add_argument("--config", dest="override_config", help="Explicit experiment JSON config")
    parser.add_argument("--storage", help="Optuna storage URL, e.g. sqlite:///study.db")
    parser.add_argument("--study-name", help="Study name in Optuna storage")
    parser.add_argument("--results", help="JSON summary saved by your Optuna script")
    parser.add_argument("--finish-only", action="store_true", help="Skip startup notification")
    parser.add_argument("--poll-interval", type=float, default=0,
                        help="Seconds between storage polls (0: disabled)")
    parser.add_argument("--notify-new-best", action="store_true",
                        help="Notify when a new best is detected (requires storage and polling)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("Missing command. E.g. notify-run-optuna python optuna_search.py --config config.json")
    if args.poll_interval < 0:
        parser.error("--poll-interval must be >= 0")
    if args.notify_new_best and args.poll_interval == 0:
        parser.error("--notify-new-best requires --poll-interval SECONDS")

    config_path = args.override_config or find_config(command)
    config, config_error = read_config(config_path)
    storage, study_name, results_path = resolve_options(args, config)
    webhook = os.getenv("QYAPI_WEBHOOK")
    metadata = (f"> Repo: {repo_name()}\n> Host: {socket.gethostname()}\n"
                f"> Config: {config_path or 'N/A'}")
    if config_error:
        metadata += f"\n> Config warning: {config_error}"
    start_time = datetime.now().astimezone().isoformat(timespec="seconds")
    # Snapshot existing best so old-study results aren't announced as a *new* best.
    previous_best = None
    if storage and args.notify_new_best:
        try:
            previous_best = best_signature(load_storage_summary(storage, study_name))
        except Exception:
            pass  # Study may not exist until the script starts.

    if not args.finish_only:
        send(webhook, "🚀 Optuna Study Started",
             f"{metadata}\n> Started: {start_time}\n\n{format_config(config)}")

    started_at = time.time()
    start_monotonic = time.monotonic()
    child = None
    pending_signal = None

    def forward(sig, frame):
        nonlocal pending_signal
        pending_signal = sig
        if child is not None and child.poll() is None:
            child.send_signal(sig)

    saved_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        saved_handlers[sig] = signal.signal(sig, forward)

    returncode = 127
    try:
        try:
            child = subprocess.Popen(command)
        except OSError as exc:
            log(f"Cannot start experiment: {exc}")
        else:
            if pending_signal is not None and child.poll() is None:
                child.send_signal(pending_signal)
            if args.poll_interval == 0:
                returncode = child.wait()
            else:
                while True:
                    try:
                        returncode = child.wait(timeout=args.poll_interval)
                        break
                    except subprocess.TimeoutExpired:
                        if not (storage and args.notify_new_best):
                            continue
                        try:
                            summary = load_storage_summary(storage, study_name)
                            signature = best_signature(summary)
                            if signature is not None and signature != previous_best:
                                previous_best = signature
                                send(webhook, "🏆 New Optuna Best Trial", metadata + "\n\n" + format_summary(summary))
                        except Exception as exc:
                            log(f"Polling skipped: {type(exc).__name__}: {exc}")
    finally:
        for sig, handler in saved_handlers.items():
            signal.signal(sig, handler)

    duration = int(time.monotonic() - start_monotonic)
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    summary, warnings = choose_summary(storage, study_name, results_path, started_at)
    if warnings:
        for warning in warnings:
            log(warning)
    status = "✅ Optuna Study Completed" if returncode == 0 else "❌ Optuna Study Failed"
    body = (f"{metadata}\n> Exit code: {returncode}\n"
            f"> Duration: {hours:02}:{minutes:02}:{seconds:02}\n\n"
            f"{format_summary(summary)}\n\n{format_config(config)}")
    if warnings:
        body += "\n> Warning: " + "; ".join(warnings)[:300]
    send(webhook, status, body)
    return returncode if returncode >= 0 else 128 - returncode


if __name__ == "__main__":
    sys.exit(main())
