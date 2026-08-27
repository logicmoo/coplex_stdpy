"""Regenerate the PNG diagrams used by the coplex_stdpy design docs.

These are plain box-and-arrow diagrams rendered with matplotlib (no
Graphviz/mermaid-cli dependency, no network access needed at render time).
Run this whenever the architecture, run loop, task lifecycle, or permission
flow changes so the docs stay accurate:

    python docs/images/generate_diagrams.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

HERE = Path(__file__).resolve().parent

# Palette: keep colors consistent with the meaning of each layer/category.
CORE = "#dbeafe"      # LLMTaskHarness / runtime.py core
CORE_EDGE = "#1d4ed8"
HTTP = "#ede9fe"       # plugin.py HTTP/admin layer
HTTP_EDGE = "#6d28d9"
MANAGER = "#fef3c7"    # HarnessTaskManager durability layer
MANAGER_EDGE = "#b45309"
EXTERNAL = "#e2e8f0"   # things outside the plugin (repo, model, browser)
EXTERNAL_EDGE = "#334155"
GUARD = "#fee2e2"      # security/permission checks
GUARD_EDGE = "#b91c1c"
GOOD = "#dcfce7"       # terminal success / allow paths
GOOD_EDGE = "#15803d"
BAD = "#fee2e2"        # terminal failure / deny paths
BAD_EDGE = "#b91c1c"
NEUTRAL = "#f1f5f9"
NEUTRAL_EDGE = "#475569"


def box(ax, x, y, w, h, text, fc=NEUTRAL, ec=NEUTRAL_EDGE, fontsize=9.5, weight="normal", pad=0.02):
    patch = FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad={pad},rounding_size=0.10",
        facecolor=fc, edgecolor=ec, linewidth=1.4, zorder=2,
    )
    ax.add_patch(patch)
    ax.text(
        x + w / 2, y + h / 2, text,
        ha="center", va="center", fontsize=fontsize, weight=weight,
        color="#0f172a", zorder=3, linespacing=1.35,
    )
    return (x, y, w, h)


def center(b):
    x, y, w, h = b
    return x + w / 2, y + h / 2


def side(b, which):
    x, y, w, h = b
    return {
        "top": (x + w / 2, y + h),
        "bottom": (x + w / 2, y),
        "left": (x, y + h / 2),
        "right": (x + w, y + h / 2),
    }[which]


def arrow(ax, p_from, p_to, text=None, color=NEUTRAL_EDGE, style="-|>", rad=0.0, lw=1.3, fontsize=8, ls="-"):
    ax.annotate(
        "", xy=p_to, xytext=p_from,
        arrowprops=dict(
            arrowstyle=style, color=color, lw=lw, linestyle=ls,
            shrinkA=2, shrinkB=2,
            connectionstyle=f"arc3,rad={rad}",
        ),
        zorder=4,
    )
    if text:
        mx, my = (p_from[0] + p_to[0]) / 2, (p_from[1] + p_to[1]) / 2
        ax.text(
            mx, my, text, ha="center", va="center", fontsize=fontsize,
            color=color, zorder=5,
            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85),
        )


def new_axes(w, h):
    fig, ax = plt.subplots(figsize=(w, h), dpi=170)
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.axis("off")
    return fig, ax


def legend(ax, x, y, entries, fontsize=8.2):
    for i, (fc, ec, label) in enumerate(entries):
        yy = y - i * 0.34
        box(ax, x, yy, 0.34, 0.24, "", fc=fc, ec=ec, pad=0.02)
        ax.text(x + 0.45, yy + 0.12, label, ha="left", va="center", fontsize=fontsize, color="#0f172a")


# ---------------------------------------------------------------------------
# 1. System architecture
# ---------------------------------------------------------------------------

def diagram_architecture():
    w, h = 12.5, 9.6
    fig, ax = new_axes(w, h)
    ax.text(w / 2, h - 0.35, "coplex_stdpy — system architecture", ha="center", fontsize=14, weight="bold")

    browser = box(ax, 0.4, h - 1.5, 3.0, 0.8, "Browser\nstatic/console.html\n(task console UI)", fc=EXTERNAL, ec=EXTERNAL_EDGE)
    caller = box(ax, w - 3.4, h - 1.5, 3.0, 0.8, "Any HTTP client / CI\n(POST /coplex_stdpy/tasks)", fc=EXTERNAL, ec=EXTERNAL_EDGE)

    http = box(ax, 3.9, h - 2.9, 4.7, 1.05,
               "plugin.py\nWorkbench FastAPI router: /coplex_stdpy/*\n+ /admin settings descriptor", fc=HTTP, ec=HTTP_EDGE, weight="bold")

    manager = box(ax, 3.9, h - 4.3, 4.7, 1.1,
                  "HarnessTaskManager\nqueue \u2192 running \u2192 waiting_* \u2192 terminal\nThreadPoolExecutor(maxWorkers)", fc=MANAGER, ec=MANAGER_EDGE, weight="bold")

    disk = box(ax, 9.1, h - 4.3, 3.0, 1.1,
               "runtime/coplex_stdpy/tasks/<id>/\nrecord.json \u00b7 events.jsonl\ntranscript.jsonl", fc=EXTERNAL, ec=EXTERNAL_EDGE, fontsize=8.6)

    h1 = box(ax, 0.4, h - 5.8, 3.4, 1.0, "LLMTaskHarness\n(task A, own thread)", fc=CORE, ec=CORE_EDGE, weight="bold")
    h2 = box(ax, 4.4, h - 5.8, 3.4, 1.0, "LLMTaskHarness\n(task B, own thread)", fc=CORE, ec=CORE_EDGE, weight="bold")
    h3 = box(ax, 8.4, h - 5.8, 3.4, 1.0, "LLMTaskHarness\n(subagents: bounded,\nread-only, isolated msgs)", fc=CORE, ec=CORE_EDGE, weight="bold")

    tools = box(ax, 0.4, h - 7.1, 5.4, 0.95, "Tool registry (built-in + dynamically registered)\nrisk: read / write / execute / network / state / model", fc=GUARD, ec=GUARD_EDGE)
    adapter = box(ax, 6.1, h - 7.1, 5.7, 0.95, "OpenAICompatibleAdapter\n(swappable: any callable(request)->reply)", fc=CORE, ec=CORE_EDGE)

    repo = box(ax, 0.2, h - 8.5, 2.6, 1.0, "Repository files\n(read/write scoped,\ndenied globs)", fc=EXTERNAL, ec=EXTERNAL_EDGE, fontsize=8.4)
    proc = box(ax, 3.0, h - 8.5, 2.6, 1.0, "Approved process /\ntest execution\n(no implicit shell)", fc=EXTERNAL, ec=EXTERNAL_EDGE, fontsize=8.4)
    git = box(ax, 5.8, h - 8.5, 2.0, 1.0, "Git\n(read-only\ninspection)", fc=EXTERNAL, ec=EXTERNAL_EDGE, fontsize=8.4)
    net = box(ax, 8.0, h - 8.5, 2.2, 1.0, "Guarded HTTP\n(allowlisted hosts,\nSSRF-checked)", fc=EXTERNAL, ec=EXTERNAL_EDGE, fontsize=8.4)
    model = box(ax, 10.4, h - 8.5, 1.9, 1.0, "OpenAI-compatible\nmodel endpoint", fc=EXTERNAL, ec=EXTERNAL_EDGE, fontsize=8.4)

    arrow(ax, side(browser, "bottom"), side(http, "top"), "HTTP/JSON", color=HTTP_EDGE)
    arrow(ax, side(caller, "bottom"), side(http, "top"), "HTTP/JSON", color=HTTP_EDGE)
    arrow(ax, side(http, "bottom"), side(manager, "top"), "submit / cancel / approve / input", color=MANAGER_EDGE)
    arrow(ax, side(manager, "right"), side(disk, "left"), "durable record + events", color=MANAGER_EDGE)
    arrow(ax, (manager[0] + 0.9, manager[1]), (h1[0] + 1.7, h1[1] + h1[3]), color=MANAGER_EDGE)
    arrow(ax, (manager[0] + 2.35, manager[1]), (h2[0] + 1.7, h2[1] + h2[3]), color=MANAGER_EDGE)
    arrow(ax, (manager[0] + 3.8, manager[1]), (h3[0] + 1.7, h3[1] + h3[3]), color=MANAGER_EDGE, text="spawns / owns")
    arrow(ax, (h1[0] + 1.7, h1[1]), (tools[0] + 1.3, tools[1] + tools[3]), color=CORE_EDGE)
    arrow(ax, (h2[0] + 1.7, h2[1]), (tools[0] + 3.4, tools[1] + tools[3]), color=CORE_EDGE)
    arrow(ax, (h3[0] + 1.7, h3[1]), (adapter[0] + 4.6, adapter[1] + adapter[3]), color=CORE_EDGE)
    arrow(ax, (h1[0] + 2.8, h1[1]), (adapter[0] + 1.0, adapter[1] + adapter[3]), color=CORE_EDGE, text="model.request/response")
    arrow(ax, side(tools, "bottom"), side(repo, "top"), color=GUARD_EDGE)
    arrow(ax, (tools[0] + 3.6, tools[1]), side(proc, "top"), color=GUARD_EDGE)
    arrow(ax, (tools[0] + 4.8, tools[1]), side(git, "top"), color=GUARD_EDGE)
    arrow(ax, (tools[0] + 5.3, tools[1]), side(net, "top"), color=GUARD_EDGE)
    arrow(ax, side(adapter, "bottom"), side(model, "top"), "HTTPS (or loopback HTTP)", color=CORE_EDGE)

    legend(ax, 0.4, 0.95, [
        (HTTP, HTTP_EDGE, "HTTP / admin layer (plugin.py)"),
        (MANAGER, MANAGER_EDGE, "Durable orchestration (HarnessTaskManager)"),
        (CORE, CORE_EDGE, "Model/tool loop (LLMTaskHarness)"),
        (GUARD, GUARD_EDGE, "Permission-gated tool registry"),
        (EXTERNAL, EXTERNAL_EDGE, "External systems / disk"),
    ])
    fig.tight_layout()
    fig.savefig(HERE / "01-system-architecture.png")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 2. run() loop
# ---------------------------------------------------------------------------

def diagram_run_loop():
    w, h = 10.2, 16.4
    fig, ax = new_axes(w, h)
    ax.text(w / 2, h - 0.4, "LLMTaskHarness.run(task) — the model/tool loop", ha="center", fontsize=14, weight="bold")

    steps = [
        ("start", "run(task) called\nvalidate task, acquire per-harness run lock\n(one active run at a time)", CORE),
        ("context", "repository_context(budget)\nroot, platform, applicable AGENTS.md,\nbranch, git status, bounded file tree", CORE),
        ("seed", "seed messages =\n[system: context, user: task]\npersist to transcript_handler", CORE),
        ("loopstart", "loop: step += 1\nstep > max_steps ? \u2192 raise", NEUTRAL),
        ("request", "build request: instructions +\n_compacted_messages() + tool_specs()\n(deterministic size-bounded compaction)", CORE),
        ("adapter", "_invoke_adapter(request) on a worker thread\nhonors per-call timeout, overall_timeout,\nand cancellation (adapter.cancel_request)", CORE),
        ("assistant", "normalize + append assistant message\n(content, tool_calls, usage)", CORE),
        ("decide", "reply.tool_calls empty?", NEUTRAL),
        ("finish", "emit run.finished\nreturn answer", GOOD),
        ("tools", "for each tool_call:\nexecute_tool() \u2192 permission + approval +\nhandler + redact (see doc 03)", GUARD),
        ("failure", "_record_failure(): same failing call\n\u2265 repeated_failure_limit \u2192 raise", BAD),
        ("append", "append tool result message\n(role=tool, tool_call_id, content)", CORE),
    ]
    edge = {CORE: CORE_EDGE, GUARD: GUARD_EDGE, GOOD: GOOD_EDGE, BAD: BAD_EDGE, NEUTRAL: NEUTRAL_EDGE}

    bw, bh = 6.6, 1.02
    x0 = (w - bw) / 2
    ys = {}
    boxes = {}
    top = h - 1.3
    gap = 0.34
    for key, text, fc in steps:
        boxes[key] = box(ax, x0, top - bh, bw, bh, text, fc=fc, ec=edge[fc], fontsize=9.2)
        ys[key] = top - bh
        top = top - bh - gap

    order = [k for k, _, _ in steps]
    for a, b_ in zip(order, order[1:]):
        if a == "decide":
            continue
        arrow(ax, side(boxes[a], "bottom"), side(boxes[b_], "top"))

    # decide branch: yes -> finish (straight down), no -> tools (loop around the right side)
    arrow(ax, side(boxes["decide"], "bottom"), side(boxes["finish"], "top"))
    ax.text(x0 + bw / 2 + 0.55, ys["decide"] - 0.17, "yes", fontsize=9, ha="left", color=GOOD_EDGE, weight="bold")

    no_x = x0 + bw + 0.7
    p_from = (x0 + bw, ys["decide"] + boxes["decide"][3] / 2)
    p_to = (x0 + bw, ys["tools"] + boxes["tools"][3] / 2)
    ax.plot([p_from[0], no_x, no_x, p_to[0]], [p_from[1], p_from[1], p_to[1], p_to[1]], color=GUARD_EDGE, lw=1.3, zorder=4)
    arrow(ax, (no_x, p_to[1]), p_to, color=GUARD_EDGE)
    ax.text(no_x + 0.12, (p_from[1] + p_to[1]) / 2, "no", fontsize=9, color=GUARD_EDGE, rotation=90, va="center", weight="bold")

    # loop back from append to loopstart, routed down the left margin
    left_x = x0 - 0.7
    p_from = (x0, ys["append"] + boxes["append"][3] / 2)
    p_to = (x0, ys["loopstart"] + boxes["loopstart"][3] / 2)
    ax.plot([p_from[0], left_x, left_x, p_to[0]], [p_from[1], p_from[1], p_to[1], p_to[1]], color=NEUTRAL_EDGE, lw=1.3, zorder=4)
    arrow(ax, (left_x, p_to[1]), p_to, color=NEUTRAL_EDGE)
    ax.text(left_x - 0.14, (p_from[1] + p_to[1]) / 2, "next step", fontsize=8.4, color=NEUTRAL_EDGE, rotation=90, va="center")

    fig.tight_layout()
    fig.savefig(HERE / "02-run-loop.png")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 3. Task lifecycle state machine (HarnessTaskManager)
# ---------------------------------------------------------------------------

def diagram_task_lifecycle():
    w, h = 16.0, 10.2
    fig, ax = new_axes(w, h)
    ax.text(w / 2, h - 0.4, "HarnessTaskManager — durable task lifecycle", ha="center", fontsize=14, weight="bold")

    queued = box(ax, 0.5, 6.6, 2.0, 0.9, "queued", fc=NEUTRAL, ec=NEUTRAL_EDGE, weight="bold")
    running = box(ax, 4.3, 6.6, 2.0, 0.9, "running", fc=CORE, ec=CORE_EDGE, weight="bold")
    wa = box(ax, 8.1, 8.3, 2.9, 0.9, "waiting_approval\n(risky tool call pending)", fc=GUARD, ec=GUARD_EDGE, fontsize=8.8)
    wi = box(ax, 8.1, 4.9, 2.9, 0.9, "waiting_input\n(request_user_input pending)", fc=GUARD, ec=GUARD_EDGE, fontsize=8.8)

    completed = box(ax, 11.7, 8.3, 2.3, 0.9, "completed", fc=GOOD, ec=GOOD_EDGE, weight="bold")
    failed = box(ax, 11.7, 6.9, 2.3, 0.9, "failed", fc=BAD, ec=BAD_EDGE, weight="bold")
    cancelled = box(ax, 11.7, 5.5, 2.3, 0.9, "cancelled", fc=BAD, ec=BAD_EDGE, weight="bold")
    interrupted = box(ax, 11.7, 4.1, 2.3, 0.9, "interrupted", fc=BAD, ec=BAD_EDGE, fontsize=9)

    arrow(ax, side(queued, "right"), side(running, "left"), "executor picks up\n(ThreadPoolExecutor)")

    # running <-> waiting_approval / waiting_input, out-and-back on offset parallel paths
    arrow(ax, (running[0] + running[2], running[1] + 0.62), (wa[0], wa[1] + 0.55), "risk needs approval", rad=0.12, color=GUARD_EDGE)
    arrow(ax, (wa[0], wa[1] + 0.2), (running[0] + running[2], running[1] + 0.27), "allow / deny posted", rad=0.12, color=GUARD_EDGE)
    arrow(ax, (running[0] + running[2], running[1] + 0.62), (wi[0], wi[1] + 0.55), "request_user_input tool", rad=-0.12, color=GUARD_EDGE)
    arrow(ax, (wi[0], wi[1] + 0.2), (running[0] + running[2], running[1] + 0.27), "response posted", rad=-0.12, color=GUARD_EDGE)

    # running -> terminal states, fanned out to the right
    arrow(ax, (running[0] + running[2], running[1] + 0.75), (completed[0], completed[1] + 0.45), "final answer\n(no tool_calls)", color=GOOD_EDGE, rad=0.22)
    arrow(ax, (running[0] + running[2], running[1] + 0.45), (failed[0], failed[1] + 0.45), "unhandled exception /\nmax_steps / timeout", color=BAD_EDGE, rad=0.05)
    arrow(ax, (running[0] + running[2], running[1] + 0.15), (cancelled[0], cancelled[1] + 0.45), "cancelRequested\n+ future.cancel() ok", color=BAD_EDGE, rad=-0.12)

    right_edge = 11.7 + 2.3  # right edge shared by completed/failed/cancelled/interrupted

    # queued -> cancelled, routed under everything and back in from outside the right edge
    qx, qy = queued[0] + 0.4, queued[1]
    cy = cancelled[1] + 0.45
    mid_y = 2.4
    outer_x1 = right_edge + 0.6
    ax.plot([qx, qx, outer_x1, outer_x1], [qy, mid_y, mid_y, cy], color=BAD_EDGE, lw=1.3, zorder=4)
    arrow(ax, (outer_x1, cy), (right_edge, cy), color=BAD_EDGE)
    ax.text((qx + outer_x1) / 2, mid_y - 0.32, "cancel() before the executor started the task", fontsize=8.4, color=BAD_EDGE, ha="center")

    # running -> interrupted, dashed, routed under waiting_input and back in from outside the right edge
    rx, ry = running[0] + 1.4, running[1]
    iy = interrupted[1] + 0.45
    mid_y2 = 3.1
    outer_x2 = right_edge + 1.15
    ax.plot([rx, rx, outer_x2, outer_x2], [ry, mid_y2, mid_y2, iy], color=BAD_EDGE, lw=1.3, ls="--", zorder=4)
    arrow(ax, (outer_x2, iy), (right_edge, iy), color=BAD_EDGE, ls="--")
    ax.text((rx + outer_x2) / 2, mid_y2 - 0.32, "Workbench restarts while status was not yet terminal", fontsize=8.4, color=BAD_EDGE, ha="center")

    ax.text(0.5, 3.7,
            "task.status is always one of:\nqueued \u00b7 running \u00b7 waiting_approval \u00b7 waiting_input \u00b7\ncompleted \u00b7 failed \u00b7 cancelled \u00b7 interrupted\n\n"
            "completed / failed / cancelled / interrupted are terminal\n(HarnessTaskManager.TERMINAL) and never resume.",
            fontsize=9, color="#1e293b", va="top")

    legend(ax, 0.5, 2.0, [
        (NEUTRAL, NEUTRAL_EDGE, "pending"),
        (CORE, CORE_EDGE, "active"),
        (GUARD, GUARD_EDGE, "paused for a decision"),
        (GOOD, GOOD_EDGE, "terminal: success"),
        (BAD, BAD_EDGE, "terminal: not successful"),
    ])
    fig.tight_layout()
    fig.savefig(HERE / "03-task-lifecycle.png")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 4. Tool execution / permission flow
# ---------------------------------------------------------------------------

def diagram_permission_flow():
    w, h = 9.8, 11.8
    fig, ax = new_axes(w, h)
    ax.text(w / 2, h - 0.35, "execute_tool(name, arguments) — permission and approval flow", ha="center", fontsize=13.5, weight="bold")

    top = h - 1.1
    bw, bh, gap = 6.6, 0.8, 0.26
    x0 = (w - bw) / 2

    def step(text, fc, fontsize=8.6):
        nonlocal top
        b = box(ax, x0, top - bh, bw, bh, text, fc=fc, ec=edge_for(fc), fontsize=fontsize)
        top -= bh + gap
        return b

    def edge_for(fc):
        return {CORE: CORE_EDGE, GUARD: GUARD_EDGE, GOOD: GOOD_EDGE, BAD: BAD_EDGE, NEUTRAL: NEUTRAL_EDGE}[fc]

    b_call = step("model requests tool call\n{name, arguments, id}", CORE)
    b_known = step("tool registered in this harness\n(built-in or register_tool())?", NEUTRAL)
    b_unknown = box(ax, x0 + bw + 0.35, top + bh + gap, 2.4, bh, "no \u2192 error\nunknown_tool", fc=BAD, ec=BAD_EDGE, fontsize=8.2)
    arrow(ax, side(b_known, "right"), side(b_unknown, "left"), color=BAD_EDGE)

    b_risk = step("risk in PROFILE_RISKS[permission_profile]?\n(read-only / workspace-write / full-access)", GUARD)
    b_risk_no = box(ax, x0 + bw + 0.35, top + bh + gap, 2.4, bh, "no \u2192 error\npermission_error", fc=BAD, ec=BAD_EDGE, fontsize=8.2)
    arrow(ax, side(b_risk, "right"), side(b_risk_no, "left"), color=BAD_EDGE)

    b_flags = step("risk==network needs allow_network;\nrisk==execute needs allow_shell", GUARD)
    b_flags_no = box(ax, x0 + bw + 0.35, top + bh + gap, 2.4, bh, "disabled \u2192 error\npermission_error", fc=BAD, ec=BAD_EDGE, fontsize=8.2)
    arrow(ax, side(b_flags, "right"), side(b_flags_no, "left"), color=BAD_EDGE)

    b_approve = step("_approve(): approvalMode\nnever \u2192 allow \u00b7 deny \u2192 reject \u00b7\non-request \u2192 wait for POST .../approvals/{call_id}", GUARD)
    b_deny = box(ax, x0 + bw + 0.35, top + bh + gap, 2.4, bh, "denied/timeout/cancelled\n\u2192 permission_error", fc=BAD, ec=BAD_EDGE, fontsize=8.2)
    arrow(ax, side(b_approve, "right"), side(b_deny, "left"), color=BAD_EDGE)

    b_handler = step("definition.handler(arguments)\n(path/patch/command validation happens\ninside the specific tool; see doc 04)", CORE)
    b_result = step("wrap {ok, tool, ...data} or\n{ok:false, error:{type,message}}", CORE)
    b_redact = step("_redact_obj(result)\nstrip secrets/tokens before it can reach\nthe model, transcript, or events", GUARD)
    b_emit = step("emit tool.finished event\nreturn result as a role=tool message", GOOD)

    order = [b_call, b_known, b_risk, b_flags, b_approve, b_handler, b_result, b_redact, b_emit]
    for a, b_ in zip(order, order[1:]):
        arrow(ax, side(a, "bottom"), side(b_, "top"))

    ax.text(x0 - 0.15, ys_label(b_known) if False else (b_known[1] + b_known[3] / 2), "yes", fontsize=8.2, ha="right", color=GOOD_EDGE)
    ax.text(x0 - 0.15, b_risk[1] + b_risk[3] / 2, "allowed", fontsize=8.2, ha="right", color=GOOD_EDGE)
    ax.text(x0 - 0.15, b_flags[1] + b_flags[3] / 2, "ok / n\\a", fontsize=8.2, ha="right", color=GOOD_EDGE)
    ax.text(x0 - 0.15, b_approve[1] + b_approve[3] / 2, "allowed", fontsize=8.2, ha="right", color=GOOD_EDGE)

    legend(ax, 0.15, 1.7, [
        (CORE, CORE_EDGE, "core loop step"),
        (GUARD, GUARD_EDGE, "permission / approval gate"),
        (GOOD, GOOD_EDGE, "continues normally"),
        (BAD, BAD_EDGE, "short-circuits with an error result"),
    ])
    fig.tight_layout()
    fig.savefig(HERE / "04-permission-flow.png")
    plt.close(fig)


def ys_label(b):  # pragma: no cover - unused placeholder kept for clarity above
    return b[1] + b[3] / 2


if __name__ == "__main__":
    diagram_architecture()
    diagram_run_loop()
    diagram_task_lifecycle()
    diagram_permission_flow()
    print("wrote diagrams to", HERE)
