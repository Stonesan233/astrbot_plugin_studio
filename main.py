"""
AstrBot 插件：动态工作室 (astrbot_plugin_studio)
=================================================

支持自由添加 SubAgent 成员的群聊协作工作室。
依赖 astrbot_plugin_claudecode (YukiRa1n) 作为底层执行引擎。

命令:
  /studio add <名称> <人格提示词>   添加工作室成员
  /studio list                      列出所有成员
  /studio remove <名称>             移除成员
  /studio info <名称>               查看成员详情
  /studio status                    显示工作室状态（含成员列表 + 活跃对话）
  /studio chat <消息>               在工作室中发起讨论（支持 @成员名）
  /studio history                   查看当前协作历史
  /studio reset                     重置当前协作
  /studio help                      显示帮助

协作机制:
  在 /studio chat 消息中输入 @成员名 即可指定由谁处理。
  成员也可以 @其他成员 进行内部委托，形成多轮协作。
  默认最多 10 轮（可配置），避免死循环。

LLM 驱动委托:
  每个成员完成任务后，LLM 自主判断是否需要委托给其他成员。
  通过在回复末尾使用【委托给X】或【无需委托，任务完成】标记来指示。
  上下文记忆确保每个成员都知道之前的协作过程。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

PLUGIN_NAME = "astrbot_plugin_studio"
PLUGIN_DIR = Path(__file__).resolve().parent

# 持久化文件路径
_MEMBERS_FILE = PLUGIN_DIR / "studio_members.json"

# 会话过期时间（秒）
_SESSION_TTL = 3600

# 检测 @委托的正则: @成员名 后面可跟可选分隔符和消息
_DELEGATE_RE = re.compile(
    r"(?im)@(?P<name>[\w\u4e00-\u9fff]+)"
    r"\s*[，,：:：]?\s*"
    r"(?P<msg>.+)$"
)

# 审查任务关键词（用于上下文判断，不再用于硬编码委托）
_REVIEW_KEYWORDS = ["审查", "检查", "评审", "审核", "review", "inspect"]

# LLM 委托标记正则
_DELEGATE_MARKER_RE = re.compile(r"【委托给(?P<name>[^】]+)】(?P<msg>[^【]*)")
_NO_DELEGATE_MARKER_RE = re.compile(r"【无需委托[^】]*】")


# ===========================================================================
# 插件主类
# ===========================================================================

class StudioPlugin(Star):
    """
    动态工作室插件

    支持自由添加 SubAgent 成员，通过 @mention 或 LLM 自主判断进行任务委托。
    底层使用 astrbot_plugin_claudecode 的 ClaudeExecutor 执行任务。
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        # claudecode 插件的 ClaudeExecutor 实例（初始化时获取）
        self._executor = None
        # 工作室成员: name → {name, subagent_id, persona_prompt, emoji, created_at}
        self.studio_members: dict[str, dict] = {}
        # 多轮对话历史: session_id → conversation dict
        self.conversations: dict[str, dict] = {}

    # ===================================================================
    # 生命周期
    # ===================================================================

    async def initialize(self):
        """插件初始化：检测并连接 claudecode 插件"""
        if not self.config.get("enable_studio", True):
            logger.info(f"[{PLUGIN_NAME}] 工作室功能已禁用")
            return

        # ---- 方式 1: 通过 AstrBot Context 获取已注册的 claudecode 插件 ----
        executor = self._find_claudecode_executor()
        if executor:
            self._executor = executor
            logger.info(f"[{PLUGIN_NAME}] 已连接 claudecode 插件 (via Context)")
        else:
            # ---- 方式 2: 直接导入 claudecode 包 ----
            executor = self._import_claudecode_executor()
            if executor:
                self._executor = executor
                logger.info(f"[{PLUGIN_NAME}] 已连接 claudecode 插件 (via import)")
            else:
                logger.warning(
                    f"[{PLUGIN_NAME}] 未找到 claudecode 插件，"
                    "工作室的 chat 功能将不可用。"
                    "请确保 astrbot_plugin_claudecode 已安装并配置。"
                )

        # 加载持久化成员
        if self.config.get("persist_members", True):
            self._load_members()

        llm_delegate = "开启" if self.config.get("llm_delegate", True) else "关闭"
        logger.info(
            f"[{PLUGIN_NAME}] 工作室插件已加载，"
            f"当前 {len(self.studio_members)} 位成员 | "
            f"executor={'✅' if self._executor else '❌'} | "
            f"LLM 自主委托: {llm_delegate}"
        )

    async def terminate(self):
        """插件销毁"""
        if self.config.get("persist_members", True):
            self._save_members()
        self._executor = None
        self.conversations.clear()
        logger.info(f"[{PLUGIN_NAME}] 已卸载")

    # ===================================================================
    # 检测 claudecode 插件
    # ===================================================================

    def _find_claudecode_executor(self):
        """
        通过 AstrBot Context 查找 claudecode 插件实例，
        返回其 ClaudeExecutor 对象。
        """
        try:
            # AstrBot 注册的插件可通过 context 获取
            stars = getattr(self.context, '_stars', None) or {}
            for star_name, star_instance in stars.items():
                if "claudecode" in star_name.lower() or "claude_code" in star_name.lower():
                    executor = getattr(star_instance, 'claude_executor', None)
                    if executor:
                        return executor
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] Context 查找失败: {e}")

        # 备用：遍历所有已注册 Star
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
            for handler in star_handlers_registry:
                instance = getattr(handler, 'instance', None)
                if instance and hasattr(instance, 'claude_executor'):
                    executor = instance.claude_executor
                    if executor:
                        return executor
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] star_handlers 查找失败: {e}")

        return None

    def _import_claudecode_executor(self):
        """
        直接导入 astrbot_plugin_claudecode 包，
        构建一个独立的 ClaudeExecutor 实例。

        兼容 v3 (模块化, 含相对导入) 和 v2 (单文件) 两种目录结构。
        """
        try:
            import sys
            plugin_parent = PLUGIN_DIR.parent
            cc_plugin_dir = None
            for candidate in [
                "astrbot_plugin_claude_code_custom",
                "astrbot_plugin_claudecode",
                "astrbot_plugin_claude_code",
            ]:
                d = plugin_parent / candidate
                if d.is_dir():
                    cc_plugin_dir = d
                    break

            if not cc_plugin_dir:
                return None

            pkg_name = cc_plugin_dir.name  # e.g. "astrbot_plugin_claudecode"
            has_v3 = (cc_plugin_dir / "application" / "executor.py").exists()

            if has_v3:
                # v3 模块化结构
                parent_str = str(cc_plugin_dir.parent)
                if parent_str not in sys.path:
                    sys.path.insert(0, parent_str)

                _models = __import__(
                    f"{pkg_name}.models", fromlist=["ClaudeConfig"]
                )
                _config = __import__(
                    f"{pkg_name}.claude_config", fromlist=["ClaudeConfigManager"]
                )
                _executor = __import__(
                    f"{pkg_name}.application.executor", fromlist=["ClaudeExecutor"]
                )
                ClaudeConfig = _models.ClaudeConfig
                ClaudeConfigManager = _config.ClaudeConfigManager
                ClaudeExecutor = _executor.ClaudeExecutor
            else:
                # v2 单文件结构
                if str(cc_plugin_dir) not in sys.path:
                    sys.path.insert(0, str(cc_plugin_dir))

                from claude_config import ClaudeConfigManager
                from claude_executor import ClaudeExecutor
                from types import ClaudeConfig

            # 从 studio 自身配置中读取 key/url/model 传给 executor
            api_key = self.config.get("claude_api_key", "").strip()
            base_url = self.config.get("base_url", "").strip()
            model = self.config.get("model", "claude-sonnet-4-20250514")
            skip_permissions = self.config.get("dangerously_skip_permissions", False)

            # 尝试从 claude 插件的已保存配置中读取
            if not api_key or not skip_permissions:
                try:
                    import os
                    claude_cfg_dir = PLUGIN_DIR.parent
                    for candidate in [
                        "astrbot_plugin_claude_code_custom",
                        "astrbot_plugin_claudecode",
                    ]:
                        config_json = claude_cfg_dir.parent / "config" / f"{candidate}_config.json"
                        if config_json.exists():
                            saved = json.loads(config_json.read_text(encoding="utf-8"))
                            if not api_key:
                                api_key = saved.get("api_key", api_key)
                            if not base_url:
                                base_url = saved.get("api_base_url", base_url)
                            if not model or model == "claude-sonnet-4-20250514":
                                model = saved.get("model", model)
                            if not skip_permissions:
                                skip_permissions = saved.get("dangerously_skip_permissions", False)
                            break
                except Exception:
                    pass

            project_root = self.config.get("project_root", "").strip()
            workspace = Path(project_root) if project_root else PLUGIN_DIR / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)

            if skip_permissions:
                permission_mode = None
            else:
                permission_mode = "dontAsk"

            cfg = ClaudeConfig(
                auth_token="",
                api_key=api_key,
                api_base_url=base_url,
                model=model,
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                permission_mode=permission_mode,
                max_turns=self.config.get("max_tool_turns", 10),
                timeout_seconds=1800,
                dangerously_skip_permissions=skip_permissions,
            )

            if skip_permissions:
                logger.warning(
                    f"[{PLUGIN_NAME}] 危险权限模式已启用，"
                    "所有 Bash 和文件操作将无提示执行"
                )
            config_mgr = None
            if ClaudeConfigManager:
                config_mgr = ClaudeConfigManager(cfg, workspace)
            return ClaudeExecutor(workspace=workspace, config_manager=config_mgr)

        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 直接导入 claudecode 失败: {e}")
            return None

    # ===================================================================
    # 成员持久化
    # ===================================================================

    def _load_members(self):
        """从文件加载成员列表"""
        if not _MEMBERS_FILE.exists():
            return
        try:
            data = json.loads(_MEMBERS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.studio_members = data
                logger.info(
                    f"[{PLUGIN_NAME}] 已加载 {len(data)} 位工作室成员"
                )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 加载成员文件失败: {e}")

    def _save_members(self):
        """保存成员列表到文件"""
        try:
            _MEMBERS_FILE.write_text(
                json.dumps(self.studio_members, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 保存成员文件失败: {e}")

    # ===================================================================
    # 命令入口 /studio
    # ===================================================================

    @filter.command("studio")
    async def studio_command(
        self, event: AstrMessageEvent, args: GreedyStr = ""
    ):
        """/studio 工作室命令入口"""
        if not self.config.get("enable_studio", True):
            yield event.plain_result("工作室功能已禁用。")
            return

        raw_args = args.strip()

        # 备用参数补全（AstrBot GreedyStr 有时会截断长文本）
        try:
            msg_text = getattr(event, "message_str", "") or ""
        except Exception:
            msg_text = ""

        if raw_args and " " not in raw_args and msg_text:
            m = re.search(r'/?studio\s+(.*)', msg_text, re.IGNORECASE)
            if m:
                full = m.group(1).strip()
                if len(full) > len(raw_args):
                    raw_args = full

        if not raw_args:
            yield event.plain_result(self._help_text())
            return

        parts = raw_args.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        # ---- 子命令分发 ----
        if sub == "help":
            yield event.plain_result(self._help_text())
            return
        if sub == "status":
            yield event.plain_result(self._status_text())
            return
        if sub == "list":
            yield event.plain_result(self._list_members())
            return
        if sub == "reset":
            yield event.plain_result(self._handle_reset(event))
            return
        if sub == "history":
            yield event.plain_result(self._handle_history(event))
            return
        if sub == "add":
            yield event.plain_result(self._handle_add(rest))
            return
        if sub == "remove":
            yield event.plain_result(self._handle_remove(rest))
            return
        if sub == "info":
            yield event.plain_result(self._handle_info(rest))
            return

        # 默认: chat 模式（支持 @成员名 路由）
        yield event.plain_result(await self._handle_chat(event, raw_args))

    # ===================================================================
    # 成员管理
    # ===================================================================

    def _handle_add(self, args_str: str) -> str:
        """添加成员: /studio add <名称> <人格提示词>
        也支持绑定 SubAgent: 由 API 传入 subagent_id + public_description"""
        if not args_str:
            return (
                "用法: /studio add <名称> <人格提示词>\n"
                "示例: /studio add 架构师 你擅长系统设计和架构评审\n"
                "也可通过 Dashboard 绑定已有 SubAgent"
            )

        parts = args_str.split(maxsplit=1)
        if len(parts) < 2:
            return (
                "用法: /studio add <名称> <人格提示词>\n"
                "示例: /studio add 架构师 你擅长系统设计和架构评审"
            )

        name = parts[0].strip().lstrip("@")
        persona_prompt = parts[1].strip()

        if not name or not persona_prompt:
            return "名称和人格提示词不能为空"

        return self._add_member_internal(name, persona_prompt)

    def _handle_bind_subagent(
        self, name: str, persona_prompt: str, subagent_name: str = "", description: str = ""
    ) -> str:
        """绑定已有 SubAgent 作为工作室成员"""
        if not name:
            return "SubAgent 名称不能为空"

        persona_prompt = persona_prompt or description or f"AstrBot SubAgent: {name}"

        return self._add_member_internal(
            name, persona_prompt, bound_subagent=subagent_name or name
        )

    def _add_member_internal(
        self, name: str, persona_prompt: str, bound_subagent: str = ""
    ) -> str:
        """内部添加成员逻辑"""
        max_members = self.config.get("max_members", 10)
        if len(self.studio_members) >= max_members:
            return (
                f"工作室已满（上限 {max_members} 人），"
                "请先 /studio remove 其他成员"
            )

        if name.lower() in {n.lower() for n in self.studio_members}:
            return f"成员「{name}」已存在，请使用其他名称"

        subagent_id = f"studio_{name.lower().replace(' ', '_')}_{int(time.time())}"

        self.studio_members[name] = {
            "name": name,
            "subagent_id": subagent_id,
            "persona_prompt": persona_prompt,
            "bound_subagent": bound_subagent,
            "emoji": "🤖",
            "created_at": time.time(),
        }

        if self.config.get("persist_members", True):
            self._save_members()

        logger.info(
            f"[{PLUGIN_NAME}] 添加成员: {name} | "
            f"subagent_id={subagent_id} | "
            f"bound_subagent={bound_subagent} | "
            f"提示词={persona_prompt[:60]}"
        )
        return (
            f"✅ 已添加成员「{name}」\n"
            f"   subagent_id: {subagent_id}\n"
            f"   人格: {persona_prompt[:200]}\n"
            f"   当前共 {len(self.studio_members)}/{max_members} 位成员"
        )

    def _handle_remove(self, args_str: str) -> str:
        """移除成员: /studio remove <名称>"""
        name = args_str.strip().lstrip("@")
        if not name:
            return "用法: /studio remove <名称>"

        found = None
        for existing in self.studio_members:
            if existing.lower() == name.lower():
                found = existing
                break

        if not found:
            return (
                f"成员「{name}」不存在。\n"
                "使用 /studio list 查看所有成员"
            )

        del self.studio_members[found]
        if self.config.get("persist_members", True):
            self._save_members()

        logger.info(f"[{PLUGIN_NAME}] 移除成员: {found}")
        return f"已移除成员「{found}」"

    def _handle_info(self, args_str: str) -> str:
        """查看成员详情: /studio info <名称>"""
        name = args_str.strip().lstrip("@")
        if not name:
            return "用法: /studio info <名称>"

        member = self._find_member(name)
        if not member:
            return f"成员「{name}」不存在"

        created = time.strftime(
            "%Y-%m-%d %H:%M",
            time.localtime(member.get("created_at", 0)),
        )
        return (
            f"成员: {member.get('emoji', '🤖')} {member['name']}\n"
            f"subagent_id: {member.get('subagent_id', '无')}\n"
            f"创建时间: {created}\n"
            f"人格设定:\n{member['persona_prompt']}"
        )

    def _list_members(self) -> str:
        """列出所有成员"""
        if not self.studio_members:
            return (
                "工作室当前没有成员。\n"
                "使用 /studio add <名称> <人格提示词> 添加第一位成员"
            )

        lines = [f"工作室成员 ({len(self.studio_members)}人):", ""]
        for i, (name, info) in enumerate(self.studio_members.items(), 1):
            emoji = info.get("emoji", "🤖")
            prompt = info["persona_prompt"]
            preview = prompt[:60] + "..." if len(prompt) > 60 else prompt
            lines.append(f"  {i}. {emoji} {name}")
            lines.append(f"     {preview}")

        return "\n".join(lines)

    # ===================================================================
    # 工作室协作入口
    # ===================================================================

    def _find_member(self, name: str) -> Optional[dict]:
        """按名称查找成员（大小写不敏感）"""
        name_clean = name.strip().lstrip("@").lower()
        for m_name, info in self.studio_members.items():
            if m_name.lower() == name_clean:
                return info
        return None

    def _detect_target_member(self, text: str) -> Optional[dict]:
        """从文本中检测 @成员名，返回成员信息"""
        # 清理群聊 @botname 前缀
        cleaned = re.sub(r"^@\S+\s*", "", text.strip())

        for m_name, info in self.studio_members.items():
            pattern = re.compile(
                r"@(" + re.escape(m_name) + r")\b",
                re.IGNORECASE,
            )
            if pattern.search(cleaned) or pattern.search(text):
                return info

        return None

    def _clean_mentions(self, text: str) -> str:
        """从文本中移除所有 @成员名"""
        for m_name in self.studio_members:
            text = re.sub(
                r"@" + re.escape(m_name) + r"\b",
                "",
                text,
                flags=re.IGNORECASE,
            )
        return text.strip()

    async def _handle_chat(
        self, event: AstrMessageEvent, text: str
    ) -> str:
        """解析消息中的 @成员名，确定目标 SubAgent，启动多轮委托循环"""
        if not text:
            return (
                "请输入讨论内容。\n"
                "示例: /studio chat @小明 帮我写一个排序算法\n"
                "       /studio chat 设计一个 RESTful API"
            )

        if not self.studio_members:
            return (
                "工作室没有成员。\n"
                "请先 /studio add <名称> <人格提示词> 添加成员"
            )

        # 确定目标成员
        target_member = self._detect_target_member(text)
        if target_member:
            target_name = target_member["name"]
        else:
            target_name = next(iter(self.studio_members))
            target_member = self.studio_members[target_name]

        task = self._clean_mentions(text)
        if not task:
            return "请输入具体任务内容"

        logger.info(
            f"[{PLUGIN_NAME}] 路由 → {target_name} | "
            f"任务={task[:80]}"
        )

        return await self._internal_delegate(
            from_member="master",
            to_member=target_name,
            message=task,
            event=event,
        )

    # ===================================================================
    # 核心：_internal_delegate — LLM 驱动的委托循环
    # ===================================================================

    async def _internal_delegate(
        self,
        from_member: str,
        to_member: str,
        message: str,
        event: AstrMessageEvent,
    ) -> str:
        """
        内部委托循环：根据 to_member 找到对应的 SubAgent，
        通过 claudecode 执行器执行任务，
        将每轮结果实时分段发给主人。

        支持:
        - 显式 @成员名 委托（成员回复中包含 @other）
        - LLM 自主判断委托（【委托给X】标记）
        - 自动检测完成（【无需委托】或无标记时结束）
        - 增强上下文传递（最近 3 轮、文件变更、审查意见）
        """
        session_id = self._get_studio_session_id(event)
        conv = self._get_or_create_conversation(session_id)
        max_rounds = self.config.get("max_internal_turns", 10)
        resp_max_len = self.config.get("response_max_length", 3000)
        llm_delegate_enabled = self.config.get("llm_delegate", True)

        # 确保 executor 可用（惰性重试）
        if not self._executor:
            self._executor = (
                self._find_claudecode_executor()
                or self._import_claudecode_executor()
            )
        if not self._executor:
            return (
                "执行引擎未就绪。\n"
                "请确保 astrbot_plugin_claudecode 已安装并正确配置。\n"
                "也可在 studio 插件设置中填写 claude_api_key。"
            )

        # 生成任务 ID，保留历史轮次不清空
        task_id = str(uuid.uuid4())[:8]
        conv["current_task_id"] = task_id
        conv["initial_member"] = to_member
        conv["status"] = "active"
        conv["updated_at"] = time.time()
        conv["auto_delegate_count"] = 0

        start = time.monotonic()
        delegator = from_member
        current_member = to_member
        current_task = message

        logger.info(
            f"[{PLUGIN_NAME}] _internal_delegate 开始 | "
            f"{delegator} → {current_member} | "
            f"任务={current_task[:80]} | "
            f"LLM自主委托={'开启' if llm_delegate_enabled else '关闭'}"
        )

        try:
            for round_num in range(1, max_rounds + 1):
                conv["updated_at"] = time.time()

                member = self.studio_members.get(current_member)
                if not member:
                    conv["status"] = "error"
                    return f"成员「{current_member}」已被移除，协作中断"

                logger.info(
                    f"[{PLUGIN_NAME}] 轮次 {round_num}/{max_rounds} | "
                    f"成员={current_member} | "
                    f"任务={current_task[:60]}"
                )

                # ---- 发送轮次开始通知 ----
                try:
                    if round_num > 1:
                        current_turns = self._current_task_turns(conv)
                        prev = current_turns[-1] if current_turns else None
                        prev_name = prev["to_member"] if prev else "?"
                        await asyncio.wait_for(
                            event.send(
                                f"🔄 第{round_num}轮: "
                                f"{current_member} 正在接手"
                                f"（来自 {prev_name} 的委托）..."
                            ),
                            timeout=5.0,
                        )
                    else:
                        await asyncio.wait_for(
                            event.send(
                                f"🤖 [{current_member}] 开始处理..."
                            ),
                            timeout=5.0,
                        )
                except asyncio.TimeoutError:
                    logger.debug(f"[{PLUGIN_NAME}] event.send 超时，跳过通知")
                except Exception:
                    pass

                # ---- 1) 构建 prompt（含增强上下文，仅当前任务轮次） ----
                current_turns = self._current_task_turns(conv)
                prompt = self._build_prompt(
                    current_member, member, current_task, current_turns, conv
                )

                # ---- 2) 调用 claudecode 执行器 ----
                response = await self._call_executor(prompt)

                # ---- 3) 解析委托标记（在 strip 之前） ----
                # 优先级: 显式 @mention > LLM 【委托给...】标记
                delegation = self._detect_delegation(response)

                if not delegation and llm_delegate_enabled:
                    delegation = self._parse_llm_delegation(response)

                # 从展示内容中清除委托标记
                clean_response = self._strip_delegation_markers(response)

                # ---- 4) 分段实时发送 ----
                try:
                    seg_size = self.config.get("response_segment_size", 400)
                    segments = self._split_response(clean_response, seg_size)
                    for si, seg in enumerate(segments):
                        prefix = (
                            f"🤖 [{current_member}] 第{round_num}轮"
                            if si == 0
                            else f"  (续{si})"
                        )
                        await asyncio.wait_for(
                            event.send(f"{prefix}:\n{seg}"),
                            timeout=5.0,
                        )
                except asyncio.TimeoutError:
                    logger.debug(f"[{PLUGIN_NAME}] 分段发送超时，跳过")
                except Exception:
                    pass

                # ---- 5) 记录轮次到对话历史 ----
                is_llm_delegated = (
                    delegation is not None
                    and not self._detect_delegation(response)
                )
                turn = {
                    "task_id": task_id,
                    "from_member": delegator,
                    "to_member": current_member,
                    "message": current_task,
                    "response": clean_response,
                    "delegated_to": None,
                    "auto_delegated": False,
                    "timestamp": time.time(),
                }
                conv["turns"].append(turn)

                # ---- 6) 更新对话级上下文（文件变更、审查追踪） ----
                self._update_conversation_context(
                    conv, current_member, current_task, response
                )

                # ---- 7) 智能停止检测 ----
                if self.config.get("auto_stop_on_complete", True):
                    stop_info = self._check_auto_stop(clean_response)
                    if stop_info:
                        conv["status"] = "completed"
                        try:
                            await event.send(
                                f"✅ {stop_info} — 自动结束本轮协作"
                            )
                        except Exception:
                            pass
                        break

                # ---- 8) 处理委托 ----
                if delegation:
                    target_name, delegated_msg = delegation
                    turn["delegated_to"] = target_name
                    if is_llm_delegated:
                        turn["auto_delegated"] = True
                        conv["auto_delegate_count"] = (
                            conv.get("auto_delegate_count", 0) + 1
                        )

                    tag = " (LLM自主)" if is_llm_delegated else ""
                    logger.info(
                        f"[{PLUGIN_NAME}] 委托{tag}: "
                        f"{current_member} → {target_name} | "
                        f"消息={delegated_msg[:60]}"
                    )

                    delegator = current_member
                    current_member = target_name
                    current_task = delegated_msg
                else:
                    # 无委托标记 → LLM 判定任务完成，或无 @mention
                    conv["status"] = "completed"
                    logger.info(
                        f"[{PLUGIN_NAME}] 无委托指令，协作结束 | "
                        f"轮次={round_num}"
                    )
                    break
            else:
                conv["status"] = "timeout"
                logger.warning(
                    f"[{PLUGIN_NAME}] 达到最大轮次 {max_rounds}，强制结束"
                )

        except asyncio.CancelledError:
            conv["status"] = "error"
            return "协作被取消。"

        except Exception as e:
            conv["status"] = "error"
            logger.error(
                f"[{PLUGIN_NAME}] _internal_delegate 异常:\n"
                f"{traceback.format_exc()}"
            )
            return f"协作异常: {e}"

        elapsed = time.monotonic() - start

        # ---- 自动审阅（可选） ----
        current_turns = self._current_task_turns(conv)
        final_response = (
            current_turns[-1]["response"] if current_turns else ""
        )
        if (
            self.config.get("auto_review", False)
            and conv["status"] == "completed"
            and len(current_turns) > 1
        ):
            final_response = await self._auto_review(
                conv["initial_member"],
                message,
                current_turns,
                final_response,
            )

        # ---- 格式化最终输出 ----
        result = self._format_output(conv, final_response, elapsed)

        if len(result) > resp_max_len:
            result = (
                result[:resp_max_len]
                + f"\n\n... (已截断，原始 {len(result)} 字符)"
            )

        logger.info(
            f"[{PLUGIN_NAME}] _internal_delegate 结束 | "
            f"状态={conv['status']} | "
            f"轮次={len(conv['turns'])} | "
            f"耗时={elapsed:.1f}s"
        )

        self._cleanup_stale_conversations()
        return result

    # ===================================================================
    # LLM 驱动委托解析
    # ===================================================================

    def _parse_llm_delegation(self, response: str) -> Optional[tuple[str, str]]:
        """
        从 LLM 回复中解析【委托给X】标记。

        Returns:
            (target_name, message) 或 None
        """
        if not response:
            return None

        # 先检查【无需委托...】标记 → 明确不委托
        if _NO_DELEGATE_MARKER_RE.search(response):
            logger.info(f"[{PLUGIN_NAME}] LLM 标记【无需委托】，不继续委托")
            return None

        # 查找【委托给X】标记（取最后一个）
        matches = list(_DELEGATE_MARKER_RE.finditer(response))
        if not matches:
            return None

        last = matches[-1]
        name_raw = last.group("name").strip()
        msg = last.group("msg").strip()

        member = self._find_member(name_raw)
        if not member:
            logger.info(
                f"[{PLUGIN_NAME}] LLM 委托目标「{name_raw}」不是工作室成员，忽略"
            )
            return None

        if not msg:
            msg = "请协助处理上述任务"

        return (member["name"], msg)

    def _strip_delegation_markers(self, response: str) -> str:
        """从展示内容中清除委托标记"""
        cleaned = _DELEGATE_MARKER_RE.sub("", response)
        cleaned = _NO_DELEGATE_MARKER_RE.sub("", cleaned)
        return cleaned.strip()

    # ===================================================================
    # 增强上下文：文件变更提取 & 上下文构建
    # ===================================================================

    def _extract_file_changes(self, response: str) -> list[str]:
        """从执行器输出中提取被修改的文件路径"""
        files: list[str] = []
        seen: set[str] = set()

        patterns = [
            # 中文
            r'(?:编辑|修改|写入|更新|创建|删除)\s*[了：:]\s*[`"\']?([^\s`"\'：:,，()\n]+\.\w+)',
            r'文件\s*[`"\']([^\s`"\']+\.\w+)[`"\']',
            # 英文 / Claude Code 风格
            r'(?:Edit|Write|Modified|Updated?|Creat|Delet)\w*\s*[：:]\s*[`"\']?([^\s`"\'：:,，()\n]+\.\w+)',
            r'(?:file|path)\s*[`"\']([^\s`"\']+\.\w+)[`"\']',
            # 反引号包裹的文件路径
            r'`([^`]+\.[a-zA-Z]\w*)`',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, response, re.IGNORECASE):
                path = match.group(1).strip().strip("'\"")
                # 过滤明显的非文件路径
                if (
                    "." in path
                    and len(path) < 200
                    and not path.startswith("http")
                    and not path.startswith("(")
                    and path not in seen
                ):
                    seen.add(path)
                    files.append(path)

        return files[:15]  # 最多 15 个文件

    def _update_conversation_context(
        self, conv: dict, member_name: str, task: str, raw_response: str
    ):
        """每轮结束后更新对话级别的上下文追踪"""
        # 追踪文件变更
        files = self._extract_file_changes(raw_response)
        if files:
            existing = conv.get("modified_files", [])
            for f in files:
                if f not in existing:
                    existing.append(f)
            conv["modified_files"] = existing[-30:]  # 最多保留 30 个
            conv["last_modified_by"] = member_name

        # 追踪审查意见
        task_lower = task.lower()
        if any(kw in task_lower for kw in _REVIEW_KEYWORDS):
            conv["last_review_by"] = member_name
            # 保留审查意见摘要（原始回复，包含标记）
            conv["last_review_summary"] = raw_response[:800]

    def _build_rich_context(
        self, member_name: str, history: list[dict], conv: dict
    ) -> str:
        """
        构建增强上下文段落，注入到 prompt 中。

        包含:
          - 最近 3 轮对话历史（任务 + 回复摘要）
          - 本对话中被修改的文件列表
          - 上一轮审查意见摘要
          - 谁最后做了什么（叙事性描述）
        """
        parts: list[str] = ["[协作上下文]"]

        # ---- 1) 最近 3 轮对话历史 ----
        recent = history[-3:] if len(history) > 3 else history
        if recent:
            parts.append("")
            parts.append("── 最近协作记录 ──")
            for i, turn in enumerate(recent, 1):
                f_name = turn["from_member"]
                t_name = turn["to_member"]
                task_text = turn["message"][:300]
                resp_text = turn["response"][:600]
                if len(turn["response"]) > 600:
                    resp_text += "...(已截断)"

                auto_tag = " (LLM自主委托)" if turn.get("auto_delegated") else ""
                parts.append(
                    f"  第{i}轮: {f_name} → {t_name}{auto_tag}"
                )
                parts.append(f"    任务: {task_text}")
                parts.append(f"    回复摘要: {resp_text}")
                if turn.get("delegated_to"):
                    parts.append(f"    → 继续委托给 {turn['delegated_to']}")
                parts.append("")

        # ---- 2) 被修改的文件列表 ----
        modified_files = conv.get("modified_files", [])
        last_modifier = conv.get("last_modified_by")
        if modified_files:
            who = last_modifier or "某位成员"
            parts.append(f"── {who} 在本次协作中修改了以下文件 ──")
            for f in modified_files:
                parts.append(f"  - {f}")
            parts.append("")

        # ---- 3) 上一轮审查意见 ----
        last_reviewer = conv.get("last_review_by")
        last_review = conv.get("last_review_summary")
        if last_reviewer and last_review and last_reviewer != member_name:
            review_preview = last_review[:400]
            if len(last_review) > 400:
                review_preview += "..."
            parts.append(
                f"── {last_reviewer} 的上一轮审查意见（供参考） ──"
            )
            parts.append(review_preview)
            parts.append("")

        # ---- 4) 叙事性上下文摘要 ----
        context_narratives: list[str] = []
        if last_modifier and last_modifier != member_name:
            context_narratives.append(
                f"{last_modifier} 刚刚完成了代码修改"
            )
        if last_reviewer and last_reviewer != member_name:
            context_narratives.append(
                f"{last_reviewer} 刚刚完成了代码审查"
            )
        if context_narratives:
            parts.append(
                "当前状况: " + "，".join(context_narratives) + "。"
            )
            parts.append("")

        # 如果只有标题行，说明没有实际内容
        if len(parts) <= 1:
            return ""

        return "\n".join(parts)

    # ===================================================================
    # 调用 claudecode 执行器
    # ===================================================================

    async def _call_executor(self, prompt: str) -> str:
        """
        调用 claudecode 插件的 ClaudeExecutor 执行任务。

        ClaudeExecutor.execute(task) 返回:
          {"success": bool, "output": str, "error": str, "cost_usd": float}
        """
        if not self._executor:
            raise RuntimeError("ClaudeExecutor 未初始化")

        result = await self._executor.execute(prompt)

        if result.get("success"):
            return result.get("output", "")
        else:
            error = result.get("error", "未知错误")
            return f"[执行失败] {error}"

    # ===================================================================
    # 自动审阅
    # ===================================================================

    async def _auto_review(
        self,
        reviewer_name: str,
        original_task: str,
        turns: list[dict],
        raw_final: str,
    ) -> str:
        """auto_review 开启时，由发起成员审阅整理最终输出"""
        logger.info(f"[{PLUGIN_NAME}] 开始自动审阅")

        member = self.studio_members.get(reviewer_name)
        if not member:
            return raw_final

        parts: list[str] = [
            f"[角色设定]\n你是「{reviewer_name}」。\n"
            f"{member['persona_prompt']}",
            "",
            "[审阅任务]",
            "以下是刚才内部协作的完整过程。请以你的风格，"
            "整理一份面向主人的最终报告。"
            "保留关键技术信息，去掉内部协调细节。",
            "",
            f"原始任务: {original_task}",
            "",
            "[协作过程]",
        ]

        for i, turn in enumerate(turns, 1):
            auto_tag = " (LLM自主委托)" if turn.get("auto_delegated") else ""
            parts.append(f"第{i}轮 - {turn['to_member']}{auto_tag}:")
            parts.append(f"  任务: {turn['message'][:300]}")
            parts.append(f"  回复: {turn['response'][:1000]}")
            parts.append("")

        parts.append("请直接给出最终整理后的回复：")

        try:
            prompt = "\n".join(parts)
            reviewed = await self._call_executor(prompt)
            if reviewed.strip():
                logger.info(f"[{PLUGIN_NAME}] 自动审阅完成")
                return reviewed
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] 自动审阅失败，使用原始回复: {e}"
            )

        return raw_final

    # ===================================================================
    # Prompt 构建（增强上下文 + LLM 委托指令）
    # ===================================================================

    def _build_prompt(
        self,
        member_name: str,
        member: dict,
        task: str,
        history: list[dict],
        conv: dict = None,
    ) -> str:
        """
        构建 SubAgent 的任务提示词。

        结构: [角色设定] + [协作上下文] + [当前任务] + [行为指引]

        核心改进:
        - 增强上下文注入（最近 3 轮、文件变更、审查意见）
        - LLM 自主委托指令（【委托给X】/ 【无需委托，任务完成】）
        """
        parts: list[str] = []

        # ---- 角色设定 ----
        parts.append(
            f"[角色设定]\n"
            f"你是「{member_name}」。\n"
            f"{member['persona_prompt']}"
        )
        parts.append("")

        # ---- 增强上下文注入 ----
        if conv and (history or conv.get("modified_files")):
            context = self._build_rich_context(member_name, history, conv)
            if context:
                parts.append(context)

        # ---- 当前任务 ----
        parts.append(f"[当前任务]\n{task}")
        parts.append("")

        # ---- 行为指引（含 LLM 委托指令） ----
        all_members = list(self.studio_members.keys())
        others = [n for n in all_members if n != member_name]
        llm_delegate_enabled = self.config.get("llm_delegate", True)

        guidance_lines: list[str] = [
            "[行为指引]",
            "1. 完成任务后，直接给出面向主人的最终回复。",
            "2. 保持人格风格一致。",
        ]

        if llm_delegate_enabled and others:
            # LLM 自主委托模式
            guidance_lines.append(
                "3. 关于是否需要其他成员继续处理："
            )
            for other in others:
                guidance_lines.append(
                    f"   - 如果你认为需要让 {other} 继续处理，"
                    f"在回复最末尾写：【委托给{other}】<具体要求>"
                )
            guidance_lines.append(
                "   - 如果你认为任务已完成，不需要委托，"
                "在回复最末尾写：【无需委托，任务完成】"
            )
            guidance_lines.append(
                "4. 如果回复中没有以上任何标记，系统将视为任务完成。"
            )
            guidance_lines.append(
                "5. 请务必根据任务实际情况自主判断，"
                "不要机械地委托。只有确实需要对方继续处理时才委托。"
            )
        elif others:
            # 仅支持显式 @mention 委托
            others_str = " / ".join(f"@{n}" for n in others)
            guidance_lines.append(
                f"3. 如需其他成员协助，在回复末尾写「{others_str} 具体要求」。"
            )
            guidance_lines.append(
                "4. 如果不需要委托，回复中不要包含任何 @。"
            )

        parts.append("\n".join(guidance_lines))

        return "\n".join(parts)

    # ===================================================================
    # 智能停止检测
    # ===================================================================

    _AUTO_STOP_PATTERNS = [
        r"任务完成[。.]?$",
        r"已完成[。.]?$",
        r"审查完毕[。.]?$",
        r"以上是最终结果[。.]?$",
        r"结论如下[：:].*",
        r"最终代码如下[：:]",
        r"完成[。.]$",
        r"task complete[.。]?$",
        r"done[.。]?$",
        r"finished[.。]?$",
        r"all done[.。]?$",
        r"here is the (final | complete) (result | code | solution)[。.]?",
        r"conclusion[：:]",
    ]

    def _check_auto_stop(self, text: str) -> Optional[str]:
        """检测回复末尾是否包含完成声明关键词"""
        tail = text[-200:] if len(text) > 200 else text
        for pattern in self._AUTO_STOP_PATTERNS:
            if re.search(pattern, tail, re.IGNORECASE | re.MULTILINE):
                return f"检测到完成声明「{pattern}」，自动停止"
        return None

    # ===================================================================
    # 委托检测（显式 @mention）
    # ===================================================================

    def _detect_delegation(self, text: str) -> Optional[tuple[str, str]]:
        """从回复中检测 @成员名 委托。取最后一条 @委托。"""
        matches = list(_DELEGATE_RE.finditer(text))
        if not matches:
            return None

        last = matches[-1]
        name_raw = last.group("name").strip()
        msg = last.group("msg").strip()

        member = self._find_member(name_raw)
        if not member:
            return None

        if not msg:
            msg = "请协助处理上述任务"

        return (member["name"], msg)

    # ===================================================================
    # 会话状态管理
    # ===================================================================

    def _get_studio_session_id(self, event: AstrMessageEvent) -> str:
        """生成工作室会话 ID（群聊按用户隔离）"""
        umo = getattr(event, "unified_msg_origin", None) or ""
        sender = getattr(event, "sender_id", None) or ""
        if sender and "group" in umo.lower():
            return f"{umo}::{sender}"
        return str(umo) if umo else str(uuid.uuid4())

    def _get_or_create_conversation(self, session_id: str) -> dict:
        """获取或创建多轮对话会话（含上下文记忆字段）"""
        if session_id not in self.conversations:
            self.conversations[session_id] = {
                "id": session_id,
                "turns": [],
                "initial_member": None,
                "status": "idle",
                "max_rounds": self.config.get("max_internal_turns", 10),
                "created_at": time.time(),
                "updated_at": time.time(),
                # ---- 上下文记忆 ----
                "last_modified_by": None,       # 最近一次修改代码的成员
                "last_review_by": None,         # 最近一次审查代码的成员
                "modified_files": [],           # 本对话中被修改的文件列表
                "last_review_summary": None,    # 上一轮审查意见摘要
                "auto_delegate_count": 0,       # 本轮 LLM 自主委托次数
            }
        return self.conversations[session_id]

    def _cleanup_stale_conversations(self):
        """清理超过 TTL 的空闲会话，防止内存泄漏"""
        now = time.time()
        stale = [
            sid
            for sid, conv in self.conversations.items()
            if conv["status"] != "active"
            and (now - conv["updated_at"]) > _SESSION_TTL
        ]
        for sid in stale:
            del self.conversations[sid]
        if stale:
            logger.debug(
                f"[{PLUGIN_NAME}] 清理 {len(stale)} 个过期会话"
            )

    def _current_task_turns(self, conv: dict) -> list[dict]:
        """获取当前任务的轮次（按 task_id 过滤）"""
        task_id = conv.get("current_task_id", "")
        if not task_id:
            return conv.get("turns", [])
        return [t for t in conv.get("turns", []) if t.get("task_id") == task_id]

    def _split_response(
        self, text: str, chunk_size: int = 400
    ) -> list[str]:
        """将长文本按段落/换行拆分为 ~chunk_size 字一段"""
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]

        segments: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= chunk_size:
                segments.append(remaining)
                break

            cut = remaining.rfind("\n", 0, chunk_size + 50)
            if cut <= 0 or cut > chunk_size + 50:
                cut = chunk_size
            segments.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")

        return segments

    # ===================================================================
    # 输出格式化
    # ===================================================================

    def _format_output(
        self,
        conv: dict,
        final_response: str,
        elapsed: float,
    ) -> str:
        """格式化最终呈现给主人的输出"""
        turns = self._current_task_turns(conv)
        initial = conv.get("initial_member", "?")
        parts: list[str] = []

        # 统计 LLM 自主委托次数
        auto_count = sum(1 for t in turns if t.get("auto_delegated"))

        if len(turns) > 1:
            chain = " → ".join(t["to_member"] for t in turns)
            auto_info = f" (含 {auto_count} 次 LLM 自主委托)" if auto_count else ""
            parts.append(
                f"协作完成 | 发起: {initial} | "
                f"链路: {chain} | "
                f"{len(turns)} 轮{auto_info} | 耗时 {elapsed:.1f}s"
            )
        else:
            parts.append(f"🤖 {initial} | 耗时 {elapsed:.1f}s")

        if len(turns) > 1:
            parts.extend(["", "── 协作过程 ──"])
            for i, turn in enumerate(turns, 1):
                preview = turn["response"][:200]
                if len(turn["response"]) > 200:
                    preview += "..."
                auto_tag = " 🔄LLM自主" if turn.get("auto_delegated") else ""
                parts.append(f"  [{i}] {turn['to_member']}{auto_tag}: {preview}")
                if turn.get("delegated_to"):
                    parts.append(
                        f"      ↳ 委托 → {turn['delegated_to']}"
                    )
            parts.extend(["", "── 最终结果 ──"])

        parts.extend(["", final_response])

        if conv["status"] == "timeout":
            parts.append(
                f"\n⚠️ 达到上限 ({conv['max_rounds']} 轮)，已强制结束。"
            )

        # 上下文记忆摘要
        last_mod = conv.get("last_modified_by")
        last_rev = conv.get("last_review_by")
        modified_files = conv.get("modified_files", [])
        if last_mod or last_rev or modified_files:
            ctx_parts = []
            if last_mod:
                ctx_parts.append(f"最近修改: {last_mod}")
            if last_rev:
                ctx_parts.append(f"最近审查: {last_rev}")
            if modified_files:
                ctx_parts.append(f"涉及文件: {len(modified_files)} 个")
            parts.append(f"\n📋 上下文: {' | '.join(ctx_parts)}")

        return "\n".join(parts)

    # ===================================================================
    # 子命令处理器
    # ===================================================================

    def _handle_reset(self, event: AstrMessageEvent) -> str:
        """重置当前协作"""
        sid = self._get_studio_session_id(event)
        if sid in self.conversations:
            del self.conversations[sid]
            return "工作室协作已重置。"
        return "当前无活跃协作。"

    def _handle_history(self, event: AstrMessageEvent) -> str:
        """查看当前协作历史"""
        sid = self._get_studio_session_id(event)
        conv = self.conversations.get(sid)
        if not conv or not conv["turns"]:
            return (
                "当前无协作历史。\n"
                "发送 /studio chat <消息> 开始讨论。"
            )

        se = {
            "active": "🔄", "completed": "✅",
            "timeout": "⏰", "error": "❌", "idle": "💤",
        }.get(conv["status"], "❓")

        lines = [
            f"{se} 协作历史",
            f"  发起成员: {conv.get('initial_member', '?')}",
            f"  状态: {conv['status']}",
            f"  轮次: {len(conv['turns'])}/{conv['max_rounds']}",
        ]

        # 上下文记忆
        last_mod = conv.get("last_modified_by")
        last_rev = conv.get("last_review_by")
        modified_files = conv.get("modified_files", [])
        if last_mod or last_rev or modified_files:
            ctx = []
            if last_mod:
                ctx.append(f"最近修改: {last_mod}")
            if last_rev:
                ctx.append(f"最近审查: {last_rev}")
            if modified_files:
                ctx.append(f"修改文件: {len(modified_files)} 个")
            lines.append(f"  上下文: {' | '.join(ctx)}")

        lines.append("")

        for i, turn in enumerate(conv["turns"], 1):
            auto_tag = " 🔄LLM自主" if turn.get("auto_delegated") else ""
            lines.append(
                f"[{i}] {turn['to_member']}{auto_tag} "
                f"(来自 {turn['from_member']})"
            )
            lines.append(f"    任务: {turn['message'][:150]}")
            lines.append(f"    回复: {turn['response'][:250]}")
            if turn.get("delegated_to"):
                lines.append(f"    → 委托: {turn['delegated_to']}")
            lines.append("")

        return "\n".join(lines)

    def _status_text(self) -> str:
        """显示工作室状态：引擎 + 成员列表 + 活跃对话"""
        executor_ok = self._executor is not None
        n_members = len(self.studio_members)
        max_members = self.config.get("max_members", 10)
        active = sum(
            1 for c in self.conversations.values()
            if c["status"] == "active"
        )
        total = len(self.conversations)
        max_rounds = self.config.get("max_internal_turns", 10)
        auto_stop = (
            "✅ 开启" if self.config.get("auto_stop_on_complete", True) else "❌ 关闭"
        )
        llm_delegate = (
            "✅ 开启" if self.config.get("llm_delegate", True) else "❌ 关闭"
        )

        lines = [
            "🏠 工作室状态",
            "",
            f"  执行引擎:     {'✅ claudecode 已连接' if executor_ok else '❌ 未连接'}",
            f"  智能停止:      {auto_stop}",
            f"  LLM 自主委托:  {llm_delegate}",
            f"  委托轮次上限:  {max_rounds}",
            f"  成员数:       {n_members}/{max_members}",
            f"  协作会话:     {active} 活跃 / {total} 总计",
            f"  持久化:       {'✅ 开启' if self.config.get('persist_members', True) else '❌ 关闭'}",
            f"  自动审阅:     {'✅ 开启' if self.config.get('auto_review', False) else '❌ 关闭'}",
        ]

        if n_members > 0:
            lines.extend(["", "── 成员列表 ──"])
            for name, info in self.studio_members.items():
                emoji = info.get("emoji", "🤖")
                lines.append(f"  {emoji} {name}")
            lines.append("")

        active_convs = [
            (sid, c) for sid, c in self.conversations.items()
            if c["status"] == "active"
        ]
        if active_convs:
            lines.append("── 活跃对话 ──")
            for sid, c in active_convs:
                initiator = c.get("initial_member", "?")
                n_turns = len(c["turns"])
                lines.append(
                    f"  🔄 {initiator} | "
                    f"{n_turns}/{c['max_rounds']} 轮 | "
                    f"id={sid[:24]}..."
                )

        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "🏠 动态工作室 — 可自由添加 SubAgent 成员\n"
            "\n"
            "命令:\n"
            "  /studio add <名称> <提示词>   添加成员\n"
            "  /studio remove <名称>         移除成员\n"
            "  /studio list                  列出所有成员\n"
            "  /studio info <名称>           查看成员详情\n"
            "  /studio status                工作室状态\n"
            "  /studio chat <消息>           发起讨论\n"
            "  /studio history               协作历史\n"
            "  /studio reset                 重置协作\n"
            "  /studio help                  显示帮助\n"
            "\n"
            "协作机制:\n"
            "  在消息中使用 @成员名 指定处理人。\n"
            "  成员之间也可以 @对方 进行内部委托。\n"
            "  默认最多 10 轮（可配置），避免死循环。\n"
            "\n"
            "LLM 自主委托:\n"
            "  每个成员完成任务后，LLM 会自主判断是否\n"
            "  需要委托给其他成员继续处理。\n"
            "  通过【委托给X】或【无需委托，任务完成】标记指示。\n"
            "  LLM 会根据实际情况决定，不会机械委托。\n"
            "\n"
            "上下文记忆:\n"
            "  系统自动维护协作上下文，包括：\n"
            "  - 最近 3 轮对话记录\n"
            "  - 被修改的文件列表\n"
            "  - 上一轮审查意见\n"
            "  确保每个成员都了解之前发生了什么。\n"
            "\n"
            "示例:\n"
            "  /studio add 架构师 你擅长系统设计和架构评审\n"
            "  /studio add 程序员 你擅长 Python 和 Go 编码实现\n"
            "  /studio chat @程序员 实现用户认证模块\n"
            "  /studio chat @架构师 设计一个微服务架构"
        )
