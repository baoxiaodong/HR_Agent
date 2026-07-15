"""
基于 asyncio 的进程内邮件抓取调度器。

每个启用自动抓取的邮箱配置对应一个后台 Task 和一个停止 Event。循环任务每次都创建
独立数据库会话、重新读取最新配置并执行抓取，然后等待配置的分钟间隔；配置变更时会
取消旧任务并按新参数重建，应用关闭时统一停止全部任务。
"""
import asyncio
import re
from typing import Dict
from app.core.database import AsyncSessionLocal
from app.models.email_config import EmailConfig
from app.services.email_service import EmailConfigService, EmailFetchService


class EmailScheduler:
    def __init__(self):
        self.tasks: Dict[str, asyncio.Task] = {}
        self.stoppers: Dict[str, asyncio.Event] = {}

    async def start(self):
        # 启动时用短生命周期会话读取全部配置；离开 with 后，后台任务不复用这条会话。
        async with AsyncSessionLocal() as db:
            svc = EmailConfigService(db)
            configs = await svc.list(skip=0, limit=1000)

        # 只有同时开启 auto_fetch 且状态 active 的配置才创建循环任务。
        for cfg in configs:
            if getattr(cfg, "auto_fetch", False) and getattr(cfg, "status", "active") == "active":
                # 最小间隔限制为 1 分钟，避免错误配置形成无等待忙循环。
                interval_min = max(1, int(getattr(cfg, "fetch_interval", 30) or 30))
                stopper = asyncio.Event()
                self.stoppers[str(cfg.id)] = stopper
                task = asyncio.create_task(self._run_fetch_loop(str(cfg.id), interval_min, stopper))
                self.tasks[str(cfg.id)] = task

    async def _run_fetch_loop(self, config_id: str, interval_min: int, stopper: asyncio.Event):
        while not stopper.is_set():
            try:
                # 每轮新建会话并重新读取配置，既隔离事务，也能看到数据库中的最新启停状态。
                async with AsyncSessionLocal() as db:
                    cfg_svc = EmailConfigService(db)
                    fetch_svc = EmailFetchService(db)
                    cfg = await cfg_svc.get(config_id)
                    if cfg and getattr(cfg, "auto_fetch", False) and getattr(cfg, "status", "active") == "active":
                        # 主题关键词支持中英文逗号；空配置使用招聘领域默认词。
                        raw = (getattr(cfg, 'subject_keywords', '') or '').strip()
                        print('subject_keywords:',raw)
                        if raw:
                            parts = [p.strip() for p in re.split(r"[,，]", raw) if p.strip()]
                            kws = parts if parts else ['简历']
                        else:
                            kws = ['简历','招聘','岗位','职位']
                        # 抓取服务只 flush 日志状态；本轮会话在这里统一提交。
                        log = await fetch_svc.fetch_recent_attachments(cfg, cfg.created_by, limit=10, subject_keyword=kws)
                        await db.commit()
            except Exception:
                # 调度器故意吞掉单轮异常以维持后续周期，但当前不会记录异常原因。
                pass

            # wait_for 同时实现定时等待和可中断停止：stopper.set() 会立即结束等待。
            try:
                await asyncio.wait_for(stopper.wait(), timeout=interval_min * 60)
            except asyncio.TimeoutError:
                # 超时表示一个周期结束，继续下一轮抓取。
                continue

    async def shutdown(self):
        # 先设置事件让处于等待阶段的循环自然感知停止，再 cancel 仍在运行/阻塞的任务。
        for stopper in self.stoppers.values():
            stopper.set()
        for task in self.tasks.values():
            try:
                task.cancel()
            except Exception:
                pass
        # 当前实现不 await 已取消任务完成；清空字典只移除调度器引用。
        self.tasks.clear()
        self.stoppers.clear()

    async def stop_task_for_config(self, config_id: str):
        stopper = self.stoppers.get(config_id)
        if stopper:
            stopper.set()
        task = self.tasks.get(config_id)
        if task:
            try:
                task.cancel()
            except Exception:
                pass
        self.tasks.pop(config_id, None)
        self.stoppers.pop(config_id, None)

    async def start_task_for_config(self, config_id: str, interval_min: int):
        # 同一配置只保留一个任务：先停止旧任务，再用新间隔重建。
        await self.stop_task_for_config(config_id)
        stopper = asyncio.Event()
        self.stoppers[config_id] = stopper
        task = asyncio.create_task(self._run_fetch_loop(config_id, interval_min, stopper))
        self.tasks[config_id] = task

    async def refresh_for_config(self, config: EmailConfig):
        cfg_id = str(config.id)
        await self.stop_task_for_config(cfg_id)
        if getattr(config, "auto_fetch", False) and getattr(config, "status", "active") == "active":
            interval_min = max(1, int(getattr(config, "fetch_interval", 30) or 30))
            print('interval_min:%s' % interval_min)
            await self.start_task_for_config(cfg_id, interval_min)
