from __future__ import annotations

from manager.service.task_models import TaskContext


class ProgressRenderService:
    def push_progress_line(self, arr: list[str], text: str, limit: int = 24) -> None:
        text = (text or "").strip()
        if not text:
            return
        if arr and arr[-1] == text:
            return
        arr.append(text)
        if len(arr) > limit:
            del arr[0 : len(arr) - limit]

    def trim_progress_line(self, text: str, limit: int = 280) -> str:
        text = (text or "").strip().replace("\n", " ")
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def normalize_progress_line(self, *, event: str, stage: str, summary: str, ctx: TaskContext, now: float) -> str | None:
        summary = self.trim_progress_line(summary)
        if not summary:
            return None
        if stage == "turn_started":
            return "已开始处理本轮请求"
        if stage == "turn_completed":
            return "本轮处理完成"
        if stage == "reasoning":
            if (now - ctx.progress_last_message_at) < 8.0:
                return None
            ctx.progress_last_message_at = now
            return "正在分析"
        if stage == "message":
            if summary == "正在生成回复":
                if (now - ctx.progress_last_message_at) < 8.0:
                    return None
                ctx.progress_last_message_at = now
                return "正在生成回复"
            if summary.startswith("回复已生成"):
                return summary
            return None
        if stage == "plan":
            return f"plan: {summary}"
        if stage == "retrying":
            return f"retry: {summary}"
        if stage == "error":
            return f"error: {summary}"
        if stage == "command":
            return summary
        if stage == "file_change":
            return summary
        return summary

    def render_progress_text(self, ctx: TaskContext) -> str:
        head = f"working (node={ctx.node_id}, threadId={ctx.thread_id[-8:]}) ..."
        lines: list[str] = [head]
        if ctx.progress_lines:
            lines.append("")
            show = ctx.progress_lines
            if len(show) > 12:
                show = show[:4] + [f"... ({len(show) - 8} steps omitted) ..."] + show[-4:]
            lines.extend(show)
        return "\n".join(lines)

    def render_progress_done_text(self, ctx: TaskContext, *, ok: bool) -> str:
        lines = [self.render_progress_text(ctx), ""]
        status = "working done" if ok else "working failed"
        lines.append(f"{status} (node={ctx.node_id}, threadId={ctx.thread_id[-8:]})")
        return "\n".join(lines)

    def render_progress_batch_text(self, ctx: TaskContext, *, batch_size: int = 5) -> str:
        sess = (ctx.session_key or "").strip()
        header = (
            f"[progress] node={ctx.node_id} thread={ctx.thread_id[-8:]} "
            f"session={sess or '-'} changes={ctx.progress_change_count}"
        )
        lines: list[str] = [header]
        if ctx.progress_lines:
            tail = ctx.progress_lines[-max(1, int(batch_size)) :]
            lines.append("")
            lines.extend(tail)
        return "\n".join(lines)
