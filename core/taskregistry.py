"""后台任务注册表——任务必有主（GOAL 贯穿性工程约束）。

事件循环对裸 ``asyncio.create_task`` 的任务只持弱引用，不持引用的任务可能
在执行中被 GC 静默丢弃（CPython asyncio 文档明示需自行保存引用）；插件
卸载时在飞任务还必须在存储关闭前被取消并等待完结，否则任务会在已关闭的
连接/状态对象上继续跑。本注册表收口三点：强引用、具名（日志可辨）、
done_callback 自动清理；terminate 走 cancel_and_wait_all。
约束：本模块是 ``create_task`` 的唯一合法入口（验收按 ``grep create_task``
逐处核对）；句柄另存于状态对象的任务（defer_task/running_task）用 adopt
登记，同样受取消管理。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger


class TaskRegistry:
    """具名后台任务注册表（每插件实例一个）。"""

    def __init__(self) -> None:
        self._tasks: dict[asyncio.Task, str] = {}

    @property
    def size(self) -> int:
        """当前登记的在飞任务数（含未清理的已完成任务通常为 0）。"""
        return len(self._tasks)

    def spawn(self, coro, name: str = "") -> asyncio.Task:
        """create_task 的唯一入口：强引用 + 具名 + 完成自动移除。"""
        task = (
            asyncio.create_task(coro, name=name) if name else asyncio.create_task(coro)
        )
        self._adopt(task, name)
        return task

    def adopt(self, task: asyncio.Task, name: str = "") -> asyncio.Task:
        """登记句柄另存于状态对象的任务（defer_task / planner running_task）。"""
        self._adopt(task, name)
        return task

    def _adopt(self, task: asyncio.Task, name: str) -> None:
        self._tasks[task] = name or task.get_name()
        task.add_done_callback(self._discard)

    def _discard(self, task: asyncio.Task) -> None:
        name = self._tasks.pop(task, None) or task.get_name()
        if not task.cancelled() and task.exception() is not None:
            # 异常不静默：任务自己吞异常是惯例（fail-silent），但意外穿透的
            # 异常至少要留痕（不向上抛——done_callback 里抛无人接）
            logger.debug(
                f"maisoul: 后台任务 {name} 异常退出", exc_info=task.exception()
            )

    async def cancel_and_wait_all(self, timeout: float = 5.0) -> None:
        """取消全部在飞任务并等待完结（超时兜底，卸载不挂死）。幂等。"""
        tasks = [t for t in self._tasks if not t.done()]
        self._tasks.clear()
        if not tasks:
            return
        for t in tasks:
            t.cancel()
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning(
                f"maisoul: {len(pending)} 个后台任务 {timeout}s 内未结束，放弃等待"
            )
