"""Allowlisted CNB CPU accounting; never publish private API responses or IDs."""
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import stat

TERMINAL = frozenset(("success", "fail", "failed", "error", "cancel", "cancelled", "canceled", "timeout"))
FREE_CORE_HOURS = Decimal(160)  # Community baseline, not the organization's remaining balance.


def not_used():
    return {"basis": "not-used", "coreHours": 0, "billingSeconds": 0, "pipelines": 0}


def pending():
    return {"basis": "pending", "coreHours": None, "billingSeconds": None, "pipelines": 0}


def number(value, maximum):
    if type(value) not in (int, float):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result <= maximum else None
    except InvalidOperation:
        return None


def cpu_count(pipeline):
    labels = pipeline.get("labels")
    if not isinstance(labels, list) or len(labels) > 64:
        return None
    values = [label.get("value") for label in labels if isinstance(label, dict) and label.get("key") == "cpus"]
    if len(values) != 1 or not isinstance(values[0], list) or len(values[0]) != 1:
        return None
    value = values[0][0]
    if type(value) is int or (type(value) is str and re.fullmatch(r"[1-9][0-9]?", value)):
        return int(value) if 1 <= int(value) <= 64 else None
    return None


def from_status(state):
    """Sum all pipelines, including failures; never turn missing/partial data into zero."""
    if not isinstance(state, dict) or type(state.get("status")) is not str or state["status"] not in TERMINAL:
        return pending()
    pipelines = state.get("pipelinesStatus")
    if not isinstance(pipelines, dict) or not 1 <= len(pipelines) <= 64:
        return pending()
    total, duration, estimated, duration_complete = Decimal(0), Decimal(0), False, True
    for pipeline in pipelines.values():
        if not isinstance(pipeline, dict) or type(pipeline.get("status")) is not str or pipeline["status"] not in TERMINAL:
            return pending()
        milliseconds = number(pipeline.get("metricDuration"), 43200000)
        if milliseconds is None:
            duration_complete = False
        else:
            duration += milliseconds
        hours = number(pipeline.get("metricCoreHours"), 768)
        if hours is None:
            # Only an absent metric may be estimated from the documented billing
            # duration. Do not use controller elapsed time, queue time or stages.
            cores = cpu_count(pipeline)
            if pipeline.get("metricCoreHours") is not None or milliseconds is None or cores is None:
                return pending()
            hours = Decimal(cores) * milliseconds / Decimal(3600000)
            estimated = True
        total += hours
    return {"basis": "estimated" if estimated else "reported", "coreHours": float(total),
            "billingSeconds": float(duration / 1000) if duration_complete else None,
            "pipelines": len(pipelines)}


def render(usage, operation, version, outcome):
    if (operation not in ("verify", "stage", "deploy", "resume", "automatic", "desktop") or
            outcome not in ("success", "failed", "cancelled") or
            (version is not None and (not isinstance(version, str) or len(version) > 100 or
                                     not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)))):
        raise ValueError("Invalid usage context")
    basis = usage.get("basis")
    labels = {"not-used": "未启动 CNB 构建", "reported": "CNB API 上报的计费核时",
              "estimated": "估算（包含 CPU 核数 × 计费时长）", "pending": "待补录，不能按 0 计"}
    if basis not in labels:
        raise ValueError("Invalid usage basis")
    hours = number(usage.get("coreHours"), 49152)
    seconds = number(usage.get("billingSeconds"), 2764800)
    count = usage.get("pipelines")
    if type(count) is not int or not 0 <= count <= 64:
        raise ValueError("Invalid pipeline count")
    if basis == "pending":
        hours, seconds = None, None
    elif hours is None or (basis == "not-used" and (hours != 0 or count != 0)):
        raise ValueError("Invalid usage amount")
    text = "待补录" if hours is None else f"{hours:.4f} 核时"
    percent = "待补录" if hours is None else f"{hours / FREE_CORE_HOURS * 100:.4f}%"
    duration = "未提供" if seconds is None else f"{seconds:.3f} 秒"
    summary = ("## CNB 发布额度\n\n| 项目 | 本次记录 |\n|---|---|\n"
               f"| 版本 | {version or '未读取'} |\n| 操作 | {operation} |\n| 发布结果 | {outcome} |\n"
               f"| 统计口径 | {labels[basis]} |\n| 新增 CPU 用量 | {text} |\n"
               f"| 各流水线累计计费时长 | {duration} |\n"
               f"| 占标准 160 核时月度免费额度 | {percent} |\n\n"
               "比例仅供比较，不代表组织本月剩余额度或实际扣款；存储用量不在此 CPU 统计内。"
               "失败、取消、重新构建分别计入各次运行；原包 resume 不重复计算原构建。\n\n")
    if basis == "pending":
        summary += "计费数据尚未完整返回，请按本次 CNB 构建补录；当前发布结果不受统计失败影响。\n\n"
    line = "CNB release usage: basis=" + basis + "; core_hours=" + ("pending" if hours is None else f"{hours:.4f}") + "."
    return summary, line


def report(usage, env, operation, version, outcome, status=print):
    """Accounting is non-blocking. Only fixed labels and bounded numbers are public."""
    try:
        summary, line = render(usage, operation, version, outcome)
        status(line)
        if not env.get("GITHUB_STEP_SUMMARY"):
            return
        runner = Path(env["RUNNER_TEMP"]).resolve(strict=True)
        destination = Path(env["GITHUB_STEP_SUMMARY"])
        if not destination.parent.resolve(strict=True).is_relative_to(runner):
            return
        descriptor = os.open(destination, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            info = os.fstat(handle.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size < 512 * 1024:
                handle.write(summary)
    except Exception:
        # Do not replace a release/cancellation result or print exception content.
        pass
