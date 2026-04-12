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

自动委托 (auto_delegate):
  当成员完成任务后没有显式 @其他成员 时，
  自动委托给工作室中的另一成员继续处理。
  - 编码/修改完成后 → 自动 @审查者 请审查我刚修改的代码
  - 审查完成后     → 自动 @实现者 请根据我的审查意见修改
  上下文记忆确保每个成员都知道上一步发生了什么。
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

# 审查任务关键词
_REVIEW_KEYWORDS = ["审查", "检查", "评审", "审核", "review", "inspect"]

# 编码/修改任务关键词
_MODIFY_KEYWORDS = [
    "修改", "实现", "编写", "开发", "重构", "添加", "修复",
    "implement", "fix", "refactor", "modify", "create", "write",
]


# ===========================================================================
# 插件主类
# ===========================================================================

class StudioPlugin(Star):
    """
    动态工作室插件

    支持自由添加 SubAgent 成员，通过 @mention 进行任务委托。
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

        auto_delegate = "开启" if self.config.get("auto_delegate", True) else "关闭"
        logger.info(
            f"[{PLUGIN_NAME}] 工作室插件已加载，"
            f"当前 {len(self.studio_members)} 位成员 | "
            f"executor={'✅' if self._executor else '❌'} | "
            f"自动委托: {auto_delegate}"
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
                # v3 模块化结构：必须把父目录加入 sys.path，
                # 使包内的相对导入 (from ..models import ...) 能正确解析
                parent_str = str(cc_plugin_dir.parent)
                if parent_str not in sys.path:
                    sys.path.insert(0, parent_str)

                # 用动态包名导入，兼容重命名后的目录
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

            project_root = self.config.get("project_root", "").strip()
            workspace = Path(project_root) if project_root else PLUGIN_DIR / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)

            cfg = ClaudeConfig(
                auth_token="",
                api_key=api_key,
                api_base_url=base_url,
                model=model,
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                permission_mode="dontAsk",
                max_turns=self.config.get("max_tool_turns", 10),
                timeout_seconds=1800,
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
        """添加成员: /studio add <名称> <人格提示词>"""
        if not args_str:
            return (
                "用法: /studio add <名称> <人格提示词>\n"
                "示例: /studio add 架构师 你擅长系统设计和架构评审"
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
            "emoji": "🤖",
            "created_at": time.time(),
        }

        if self.config.get("persist_members", True):
            self._save_members()

        logger.info(
            f"[{PLUGIN_NAME}] 添加成员: {name} | "
            f"subagent_id={subagent_id} | "
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
    # 核心：_internal_delegate — 内部委托循环（含自动委托）
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

        支持最多 N 轮（可配置）内部委托。
        成员之间通过 @成员名 互相委托，形成多轮协作。
        当 auto_delegate 开启时，成员完成任务后自动委托给搭档成员。
        """
        session_id = self._get_studio_session_id(event)
        conv = self._get_or_create_conversation(session_id)
        max_rounds = self.config.get("max_internal_turns", 10)
        resp_max_len = self.config.get("response_max_length", 3000)
        auto_delegate_enabled = self.config.get("auto_delegate", True)

        # 确保 executor 可用（惰性重试：claudecode 可能后于 studio 加载）
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

        # 重置本轮对话（保留 auto_delegate 上下文追踪字段）
        preserved_context = {
            "last_modified_by": conv.get("last_modified_by"),
            "last_review_by": conv.get("last_review_by"),
            "last_action_type": conv.get("last_action_type"),
        }
        conv["turns"] = []
        conv["initial_member"] = to_member
        conv["status"] = "active"
        conv["updated_at"] = time.time()
        conv["auto_delegate_count"] = 0
        # 恢复上下文（新对话继承上次的上下文记忆）
        for k, v in preserved_context.items():
            if v is not None:
                conv[k] = v

        # 根据初始任务推断 action type
        if conv.get("last_action_type") is None:
            if any(kw in message.lower() for kw in _REVIEW_KEYWORDS):
                conv["last_action_type"] = "review"
            else:
                conv["last_action_type"] = "modification"

        start = time.monotonic()
        delegator = from_member
        current_member = to_member
        current_task = message

        logger.info(
            f"[{PLUGIN_NAME}] _internal_delegate 开始 | "
            f"{delegator} → {current_member} | "
            f"任务={current_task[:80]} | "
            f"自动委托={'开启' if auto_delegate_enabled else '关闭'}"
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
                        prev = conv["turns"][-1] if conv["turns"] else None
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

                # ---- 1) 构建 prompt（含上下文注入） ----
                prompt = self._build_prompt(
                    current_member, member, current_task, conv["turns"], conv
                )

                # ---- 2) 调用 claudecode 执行器 ----
                response = await self._call_executor(prompt)

                # ---- 3) 分段实时发送 ----
                try:
                    seg_size = self.config.get("response_segment_size", 400)
                    segments = self._split_response(response, seg_size)
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

                # ---- 4) 记录轮次到对话历史 ----
                turn = {
                    "from_member": delegator,
                    "to_member": current_member,
                    "message": current_task,
                    "response": response,
                    "delegated_to": None,
                    "auto_delegated": False,
                    "timestamp": time.time(),
                }
                conv["turns"].append(turn)

                # ---- 4b) 智能停止检测 ----
                if self.config.get("auto_stop_on_complete", True):
                    stop_info = self._check_auto_stop(response)
                    if stop_info:
                        conv["status"] = "completed"
                        try:
                            await event.send(
                                f"✅ {stop_info} — 自动结束本轮协作"
                            )
                        except Exception:
                            pass
                        break

                # ---- 5) 检测回复中是否有显式 @委托 ----
                delegation = self._detect_delegation(response)

                # ---- 5b) 自动委托（当无显式 @ 时） ----
                if not delegation and auto_delegate_enabled:
                    delegation = self._try_auto_delegate(
                        current_member, response, conv
                    )
                    if delegation:
                        turn["auto_delegated"] = True

                if delegation:
                    target_name, delegated_msg = delegation
                    turn["delegated_to"] = target_name

                    auto_tag = " (自动)" if turn.get("auto_delegated") else ""
                    logger.info(
                        f"[{PLUGIN_NAME}] 委托{auto_tag}: "
                        f"{current_member} → {target_name} | "
                        f"消息={delegated_msg[:60]}"
                    )

                    delegator = current_member
                    current_member = target_name
                    current_task = delegated_msg
                else:
                    conv["status"] = "completed"
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
        final_response = (
            conv["turns"][-1]["response"] if conv["turns"] else ""
        )
        if (
            self.config.get("auto_review", False)
            and conv["status"] == "completed"
            and len(conv["turns"]) > 1
        ):
            final_response = await self._auto_review(
                conv["initial_member"],
                message,
                conv["turns"],
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
    # 自动委托机制
    # ===================================================================

    def _get_auto_delegate_partner(self, current_member: str) -> Optional[str]:
        """
        获取自动委托的目标搭档成员。

        对于 2 人工作室，返回另一个成员。
        对于多人工作室，返回列表中的下一个其他成员。
        """
        others = [n for n in self.studio_members if n != current_member]
        return others[0] if others else None

    def _should_auto_delegate(self, response: str, conv: dict) -> bool:
        """
        判断是否应该触发自动委托。

        不触发的情况:
          - 执行失败
          - 审查者明确批准（LGTM / 没问题 / 无需修改）
          - 自动委托次数超过上限
        """
        # 执行失败时不自动委托
        if not response or response.startswith("[执行失败]"):
            return False

        # 检测批准/通过信号（审查者说"没问题"则不应再委托回去修改）
        approval_patterns = [
            # 英文
            r"(?i)lgtm",
            r"(?i)approve",
            r"(?i)looks good",
            r"(?i)no issues?",
            r"(?i)ship it",
            r"(?i)all (tests? )?passed",
            r"(?i)no (further |additional )?changes? (needed|required)",
            # 中文 - 明确通过
            r"全部通过",
            r"检视.*通过",
            r"审查.*通过",
            r"检查.*通过",
            r"结论.*通过",
            r"通过[。.]$",
            r"确认无误",
            r"修复正确",
            r"修复确认",
            r"无需修改",
            r"无需再做",
            r"无需.*改动",
            r"无额外修改",
            r"没有问题",
            r"代码.*正确",
            r"没有发现问题",
            r"不需要.*修改",
            r"可以合并",
            r"可以提交",
            r"一切正常",
            r"看起来不错",
        ]
        tail = response[-800:] if len(response) > 800 else response
        for pattern in approval_patterns:
            if re.search(pattern, tail):
                logger.info(
                    f"[{PLUGIN_NAME}] 检测到批准信号，跳过自动委托"
                )
                return False

        # 自动委托次数上限（默认最多 4 次自动委托，防止无限循环）
        max_auto = self.config.get("auto_delegate_max_rounds", 4)
        if conv.get("auto_delegate_count", 0) >= max_auto:
            logger.info(
                f"[{PLUGIN_NAME}] 自动委托已达上限 {max_auto} 次，停止"
            )
            return False

        return True

    def _build_auto_delegate_message(
        self, current_member: str, conv: dict
    ) -> str:
        """
        根据对话上下文构建自动委托的消息。

        逻辑:
          - 如果当前成员刚完成编码/修改 → 委托审查者审查
          - 如果当前成员刚完成审查     → 委托实现者根据意见修改
        """
        turns = conv.get("turns", [])
        last_turn = turns[-1] if turns else {}
        task = last_turn.get("message", "").lower()
        response_text = last_turn.get("response", "").lower()

        # 判断当前成员完成的任务类型
        # 优先检查任务描述中的关键词
        is_review_task = any(kw in task for kw in _REVIEW_KEYWORDS)

        # 如果任务描述不明确，检查回复内容是否像审查意见
        if not is_review_task:
            review_response_signals = [
                "建议", "问题", "需要修改", "应该修改",
                "改进建议", "发现以下", "需要修复",
                "issue", "suggestion", "should",
            ]
            is_review_task = any(
                sig in response_text for sig in review_response_signals
            )

        # 也参考上一次自动委托的类型（交替机制）
        last_action = conv.get("last_action_type")

        if is_review_task or last_action == "modification":
            # 当前成员完成了审查 → 委托回去修改
            conv["last_review_by"] = current_member
            conv["last_action_type"] = "review"
            return "请根据我的审查意见修改代码"
        else:
            # 当前成员完成了编码 → 委托审查
            conv["last_modified_by"] = current_member
            conv["last_action_type"] = "modification"
            return "请审查我刚修改的代码"

    def _try_auto_delegate(
        self, current_member: str, response: str, conv: dict
    ) -> Optional[tuple[str, str]]:
        """
        尝试自动委托给搭档成员。

        Returns:
            (target_name, message) 或 None
        """
        # 需要至少 2 个成员才能自动委托
        partner = self._get_auto_delegate_partner(current_member)
        if not partner:
            return None

        # 检查是否应该自动委托
        if not self._should_auto_delegate(response, conv):
            return None

        # 构建委托消息
        msg = self._build_auto_delegate_message(current_member, conv)

        # 更新自动委托计数
        conv["auto_delegate_count"] = conv.get("auto_delegate_count", 0) + 1

        logger.info(
            f"[{PLUGIN_NAME}] 自动委托: "
            f"{current_member} → {partner} | "
            f"消息={msg}"
        )

        return (partner, msg)

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
            auto_tag = " (自动委托)" if turn.get("auto_delegated") else ""
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
    # Prompt 构建（含上下文注入）
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

        结构: [角色设定] + [上下文提示] + [协作历史] + [当前任务] + [行为指引]

        conv 参数用于注入上下文记忆（last_modified_by / last_review_by）。
        """
        parts: list[str] = []

        # ---- 角色设定 ----
        parts.append(
            f"[角色设定]\n"
            f"你是「{member_name}」。\n"
            f"{member['persona_prompt']}"
        )
        parts.append("")

        # ---- 上下文注入（基于对话记忆） ----
        if conv and history:
            last_modified = conv.get("last_modified_by")
            last_review = conv.get("last_review_by")
            last_action = conv.get("last_action_type")

            if last_modified and last_modified != member_name:
                # 当前成员是审查者，告诉TA谁刚修改了代码
                parts.append(
                    f"[上下文提示]\n"
                    f"{last_modified} 刚刚完成了代码修改，"
                    f"请重点审查上述变更，检查潜在问题。"
                )
                parts.append("")
            elif last_review and last_review != member_name:
                # 当前成员是实现者，告诉TA谁审查了代码
                parts.append(
                    f"[上下文提示]\n"
                    f"{last_review} 刚刚完成了代码审查，"
                    f"请根据上述审查意见进行针对性修改。"
                )
                parts.append("")

        # ---- 协作历史 ----
        if history:
            parts.append("[之前的协作记录]")
            for turn in history:
                f_name = turn["from_member"]
                t_name = turn["to_member"]
                task_prev = turn["message"][:200]
                resp_prev = turn["response"][:500]
                if len(turn["response"]) > 500:
                    resp_prev += "..."

                auto_tag = " (自动)" if turn.get("auto_delegated") else ""
                parts.append(
                    f"  {t_name} (来自 {f_name}){auto_tag}: {task_prev}"
                )
                parts.append(f"    回复摘要: {resp_prev}")
                if turn.get("delegated_to"):
                    parts.append(
                        f"    → 委托给 {turn['delegated_to']}"
                    )
            parts.append("")

        # ---- 当前任务 ----
        parts.append(f"[当前任务]\n{task}")
        parts.append("")

        # ---- 行为指引 ----
        all_members = list(self.studio_members.keys())
        others = [n for n in all_members if n != member_name]
        delegate_hint = ""
        if others:
            others_str = " / ".join(f"@{n}" for n in others)
            delegate_hint = (
                f"\n3. 如需其他成员协助，在回复末尾写「{others_str} 具体要求」。"
            )

        auto_delegate_note = ""
        if self.config.get("auto_delegate", True) and others:
            auto_delegate_note = (
                "\n4. 如果你不需要其他成员协助，直接给出最终回复即可，"
                "系统会自动将结果转发给其他成员。"
            )

        parts.append(
            "[行为指引]\n"
            "1. 完成任务后，直接给出面向主人的最终回复。\n"
            "2. 保持人格风格一致。"
            f"{delegate_hint}\n"
            "3. 如果不需要委托，回复中不要包含任何 @。"
            f"{auto_delegate_note}"
        )

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
    # 委托检测
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
                "last_modified_by": None,   # 最近一次修改代码的成员
                "last_review_by": None,     # 最近一次审查代码的成员
                "last_action_type": None,   # 最近一次动作类型: "modification" / "review"
                "auto_delegate_count": 0,   # 本轮自动委托次数
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
        turns = conv["turns"]
        initial = conv.get("initial_member", "?")
        parts: list[str] = []

        # 统计自动委托次数
        auto_count = sum(1 for t in turns if t.get("auto_delegated"))

        if len(turns) > 1:
            chain = " → ".join(t["to_member"] for t in turns)
            auto_info = f" (含 {auto_count} 次自动委托)" if auto_count else ""
            parts.append(
                f"协作完成 | 发起: {initial} | "
                f"链路: {chain} | "
                f"{len(turns)} 轮{auto_info} | 耗时 {elapsed:.1}s"
            )
        else:
            parts.append(f"🤖 {initial} | 耗时 {elapsed:.1}s")

        if len(turns) > 1:
            parts.extend(["", "── 协作过程 ──"])
            for i, turn in enumerate(turns, 1):
                preview = turn["response"][:200]
                if len(turn["response"]) > 200:
                    preview += "..."
                auto_tag = " 🔄自动" if turn.get("auto_delegated") else ""
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
        if last_mod or last_rev:
            context_parts = []
            if last_mod:
                context_parts.append(f"最近修改: {last_mod}")
            if last_rev:
                context_parts.append(f"最近审查: {last_rev}")
            parts.append(f"\n📋 上下文: {' | '.join(context_parts)}")

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
        if last_mod or last_rev:
            ctx = []
            if last_mod:
                ctx.append(f"最近修改: {last_mod}")
            if last_rev:
                ctx.append(f"最近审查: {last_rev}")
            lines.append(f"  上下文: {' | '.join(ctx)}")

        lines.append("")

        for i, turn in enumerate(conv["turns"], 1):
            auto_tag = " 🔄自动" if turn.get("auto_delegated") else ""
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
        auto_delegate = (
            "✅ 开启" if self.config.get("auto_delegate", True) else "❌ 关闭"
        )

        lines = [
            "🏠 工作室状态",
            "",
            f"  执行引擎:     {'✅ claudecode 已连接' if executor_ok else '❌ 未连接'}",
            f"  智能停止:      {auto_stop}",
            f"  自动委托:      {auto_delegate}",
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
            "自动委托:\n"
            "  开启后，成员完成任务会自动转发给搭档。\n"
            "  编码完成 → 自动 @审查者 审查代码\n"
            "  审查完成 → 自动 @实现者 根据意见修改\n"
            "  形成「编码→审查→修改→再审查」的闭环。\n"
            "\n"
            "示例:\n"
            "  /studio add 架构师 你擅长系统设计和架构评审\n"
            "  /studio add 程序员 你擅长 Python 和 Go 编码实现\n"
            "  /studio chat @程序员 实现用户认证模块\n"
            "  /studio chat @架构师 设计一个微服务架构"
        )
