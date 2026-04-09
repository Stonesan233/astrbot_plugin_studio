"""
AstrBot 插件：动态工作室 (astrbot_plugin_studio)
=================================================

支持自由添加 SubAgent 成员的群聊协作工作室。
依赖 cc-astrbot-agent 作为底层 Coding Agent 引擎。

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
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

# ---------------------------------------------------------------------------
# 将 cc-astrbot-agent 的 src 加入 import 路径
# ---------------------------------------------------------------------------
_PLUGIN_DIR = Path(__file__).resolve().parent
_CANDIDATE_NAMES = ["cc-astrbot-agent", "astrbot_plugin_claude_code_custom"]
for _name in _CANDIDATE_NAMES:
    _dir = _PLUGIN_DIR.parent / _name
    _src = _dir / "src"
    for _p in [str(_src), str(_dir)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

from cc_agent.agent import ClaudeCodeAgent  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_studio"

# 持久化文件路径
_MEMBERS_FILE = _PLUGIN_DIR / "studio_members.json"

# 会话过期时间（秒）
_SESSION_TTL = 3600


# ===========================================================================
# 检测 @委托的正则
# [\w\u4e00-\u9fff]+ 匹配中文/英文/数字/下划线组成的名字
# ===========================================================================
_DELEGATE_RE = re.compile(
    r"(?im)@(?P<name>[\w\u4e00-\u9fff]+)"
    r"\s*[，,：:：]?\s*"
    r"(?P<msg>.+)$"
)


# ===========================================================================
# 插件主类
# ===========================================================================

class StudioPlugin(Star):
    """
    动态工作室插件

    支持自由添加 SubAgent 成员，通过 @mention 进行任务委托。
    每个成员对应一个 SubAgent persona，通过共享的 ClaudeCodeAgent 执行。
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._agent: Optional[ClaudeCodeAgent] = None

        # 工作室成员: name → {name, subagent_id, persona_prompt, emoji, created_at}
        self.studio_members: dict[str, dict] = {}

        # 多轮对话历史: studio_session_id → conversation dict
        self.conversations: dict[str, dict] = {}

    # ===================================================================
    # 生命周期
    # ===================================================================

    async def initialize(self):
        """插件初始化"""
        if not self.config.get("enable_studio", True):
            logger.info(f"[{PLUGIN_NAME}] 工作室功能已禁用")
            return

        api_key = self.config.get("claude_api_key", "").strip()
        if not api_key:
            logger.warning(
                f"[{PLUGIN_NAME}] 未配置 claude_api_key，"
                "请在插件设置中填写"
            )

        project_root = self.config.get("project_root", "").strip()
        if not project_root:
            project_root = str(_PLUGIN_DIR)

        model = self.config.get("model", "claude-3-7-sonnet-20250219")
        base_url = self.config.get("base_url", "").strip() or None

        try:
            self._agent = ClaudeCodeAgent(
                project_root=project_root,
                claude_api_key=api_key or None,
                model=model,
                base_url=base_url,
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Agent 初始化失败: {e}")
            self._agent = None
            return

        if self.config.get("persist_members", True):
            self._load_members()

        logger.info(
            f"[{PLUGIN_NAME}] 工作室插件已加载，"
            f"当前 {len(self.studio_members)} 位成员 | "
            f"model={model} | root={project_root}"
        )

    async def terminate(self):
        """插件销毁"""
        if self.config.get("persist_members", True):
            self._save_members()
        self._agent = None
        self.conversations.clear()
        logger.info(f"[{PLUGIN_NAME}] 已卸载")

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
        """
        /studio 工作室命令

        子命令:
          add <名称> <提示词>   添加成员
          remove <名称>         移除成员
          list                  列出成员
          info <名称>           查看成员详情
          status                工作室状态（含成员 + 活跃对话）
          chat <消息>           发起讨论（支持 @成员名）
          history               协作历史
          reset                 重置协作
          help                  帮助
        """
        if not self.config.get("enable_studio", True):
            yield event.plain_result("工作室功能已禁用。")
            return

        raw_args = args.strip()

        # 备用参数补全（AstrBot GreedyStr 有时会截断长文本）
        msg_text = ""
        try:
            msg_text = (
                event.message_str
                if hasattr(event, "message_str")
                else ""
            )
        except Exception:
            pass

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

        # 生成 subagent_id（用于 agent.run_task 的 persona 参数）
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

        lines = [
            f"工作室成员 ({len(self.studio_members)}人):",
            "",
        ]
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
        """
        从文本中检测 @成员名，返回成员信息。
        支持中文名、英文名、大小写不敏感。
        清理群聊 @bot 前缀后再匹配。
        """
        # 清理群聊 @botname 前缀
        cleaned = re.sub(r"^@\S+\s*", "", text.strip())

        for m_name, info in self.studio_members.items():
            # 匹配 @成员名（整个词边界，支持中文）
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
        """
        解析消息中的 @成员名，确定目标 SubAgent，
        然后调用 _internal_delegate 启动多轮委托循环。
        """
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
    # 核心：_internal_delegate — 内部委托循环
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
        调用 agent.run_task(prompt, subagent_id)，
        将每轮结果实时分段 yield/stream 给主人。

        支持最多 10 轮（可配置）内部委托。
        成员之间通过 @成员名 互相委托，形成多轮协作。

        流程:
          1. 获取 / 创建会话 (self.conversations[session_id])
          2. 每轮: 构建 prompt → agent.run_task() → 检测 @委托
          3. 有委托 → 切换目标成员，继续
          4. 无委托 或达到上限 → 结束，返回最终结果
        """
        session_id = self._get_studio_session_id(event)
        conv = self._get_or_create_conversation(session_id)
        max_rounds = self.config.get("max_internal_turns", 10)
        resp_max_len = self.config.get("response_max_length", 3000)

        # 确保 Agent 可用
        if not self._agent:
            return "Agent 未就绪，请检查插件配置"
        if not self._agent.api_key:
            return "API Key 未配置，请在插件设置中填写"

        # 重置本轮对话
        conv["turns"] = []
        conv["initial_member"] = to_member
        conv["status"] = "active"
        conv["updated_at"] = time.time()

        start = time.monotonic()
        delegator = from_member
        current_member = to_member
        current_task = message

        logger.info(
            f"[{PLUGIN_NAME}] _internal_delegate 开始 | "
            f"{delegator} → {current_member} | "
            f"任务={current_task[:80]}"
        )

        try:
            for round_num in range(1, max_rounds + 1):
                conv["updated_at"] = time.time()

                member = self.studio_members.get(current_member)
                if not member:
                    conv["status"] = "error"
                    return (
                        f"成员「{current_member}」已被移除，协作中断"
                    )

                logger.info(
                    f"[{PLUGIN_NAME}] 轮次 {round_num}/{max_rounds} | "
                    f"成员={current_member} | "
                    f"subagent_id={member.get('subagent_id', current_member)} | "
                    f"任务={current_task[:60]}"
                )

                # ---- 发送轮次开始通知 ----
                try:
                    if round_num > 1:
                        prev = conv["turns"][-1] if conv["turns"] else None
                        prev_name = prev["to_member"] if prev else "?"
                        await event.send(
                            f"🔄 第{round_num}轮: "
                            f"{current_member} 正在接手"
                            f"（来自 {prev_name} 的委托）..."
                        )
                    else:
                        await event.send(
                            f"🤖 [{current_member}] 开始处理..."
                        )
                except Exception:
                    pass

                # ---- 1) 构建 prompt ----
                prompt = self._build_prompt(
                    current_member, member, current_task, conv["turns"]
                )

                # ---- 2) 调用 agent.run_task() 转发给 SubAgent ----
                response = await self._call_agent(
                    prompt,
                    member.get("subagent_id", current_member),
                )

                # ---- 3) 分段实时 yield 给主人 ----
                try:
                    seg_size = self.config.get("response_segment_size", 400)
                    segments = self._split_response(response, seg_size)
                    for si, seg in enumerate(segments):
                        prefix = (
                            f"🤖 [{current_member}] 第{round_num}轮"
                            if si == 0
                            else f"  (续{si})"
                        )
                        await event.send(f"{prefix}:\n{seg}")
                except Exception:
                    pass

                # ---- 4) 记录轮次到对话历史 ----
                turn = {
                    "from_member": delegator,
                    "to_member": current_member,
                    "message": current_task,
                    "response": response,
                    "delegated_to": None,
                    "timestamp": time.time(),
                }
                conv["turns"].append(turn)

                # ---- 5) 检测回复中是否有 @委托 ----
                delegation = self._detect_delegation(response)
                if delegation:
                    target_name, delegated_msg = delegation
                    turn["delegated_to"] = target_name

                    logger.info(
                        f"[{PLUGIN_NAME}] 委托: "
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
            logger.info(f"[{PLUGIN_NAME}] 协作被取消")
            return "协作被取消。"

        except Exception as e:
            conv["status"] = "error"
            tb = traceback.format_exc()
            logger.error(
                f"[{PLUGIN_NAME}] _internal_delegate 异常:\n{tb}"
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
    # Agent 调用
    # ===================================================================

    async def _call_agent(self, prompt: str, subagent_id: str) -> str:
        """
        调用底层 ClaudeCodeAgent.run_task()。

        subagent_id 作为 persona 参数传入，
        agent 会用它标记日志，不影响核心逻辑。
        """
        if not self._agent:
            raise RuntimeError("Agent 未初始化")

        chunks: list[str] = []
        async for chunk in self._agent.run_task(
            task=prompt,
            persona=subagent_id,
        ):
            chunks.append(chunk)

        return "".join(chunks)

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
            parts.append(f"第{i}轮 - {turn['to_member']}:")
            parts.append(f"  任务: {turn['message'][:300]}")
            parts.append(f"  回复: {turn['response'][:1000]}")
            parts.append("")

        parts.append("请直接给出最终整理后的回复：")

        try:
            prompt = "\n".join(parts)
            reviewed = await self._call_agent(
                prompt,
                member.get("subagent_id", reviewer_name),
            )
            if reviewed.strip():
                logger.info(f"[{PLUGIN_NAME}] 自动审阅完成")
                return reviewed
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] 自动审阅失败，使用原始回复: {e}"
            )

        return raw_final

    # ===================================================================
    # Prompt 构建
    # ===================================================================

    def _build_prompt(
        self,
        member_name: str,
        member: dict,
        task: str,
        history: list[dict],
    ) -> str:
        """
        构建 SubAgent 的任务提示词。

        结构: [角色设定] + [协作历史] + [当前任务] + [行为指引]
        """
        parts: list[str] = []

        # ---- 角色设定 ----
        parts.append(
            f"[角色设定]\n"
            f"你是「{member_name}」。\n"
            f"{member['persona_prompt']}"
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

                parts.append(
                    f"  {t_name} (来自 {f_name}): {task_prev}"
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

        n_directive = "3" if others else "3"
        parts.append(
            "[行为指引]\n"
            "1. 完成任务后，直接给出面向主人的最终回复。\n"
            "2. 保持人格风格一致。"
            f"{delegate_hint}\n"
            f"{n_directive}. "
            "如果不需要委托，回复中不要包含任何 @。"
        )

        return "\n".join(parts)

    # ===================================================================
    # 委托检测
    # ===================================================================

    def _detect_delegation(
        self, text: str
    ) -> Optional[tuple[str, str]]:
        """
        从回复中检测 @成员名 委托。

        返回 (target_member_name, delegated_message) 或 None。
        取最后一条 @委托（避免中间讨论被误识别）。
        """
        matches = list(_DELEGATE_RE.finditer(text))
        if not matches:
            return None

        last = matches[-1]
        name_raw = last.group("name").strip()
        msg = last.group("msg").strip()

        # 查找匹配的成员
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
        """
        生成工作室会话 ID。

        群聊: unified_msg_origin + sender_id（群内按用户隔离）
        私聊: unified_msg_origin
        """
        umo = getattr(event, "unified_msg_origin", None) or ""
        sender = getattr(event, "sender_id", None) or ""
        if sender and "group" in umo.lower():
            return f"{umo}::{sender}"
        return str(umo) if umo else str(uuid.uuid4())

    def _get_or_create_conversation(self, session_id: str) -> dict:
        """获取或创建多轮对话会话"""
        if session_id not in self.conversations:
            self.conversations[session_id] = {
                "id": session_id,
                "turns": [],
                "initial_member": None,
                "status": "idle",
                "max_rounds": self.config.get("max_internal_turns", 10),
                "created_at": time.time(),
                "updated_at": time.time(),
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
        """
        将长文本按段落/换行拆分为 ~chunk_size 字一段。
        优先在换行符处切割，避免截断句子。
        """
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

        if len(turns) > 1:
            chain = " → ".join(t["to_member"] for t in turns)
            parts.append(
                f"协作完成 | 发起: {initial} | "
                f"链路: {chain} | "
                f"{len(turns)} 轮 | 耗时 {elapsed:.1}s"
            )
        else:
            parts.append(f"🤖 {initial} | 耗时 {elapsed:.1}s")

        if len(turns) > 1:
            parts.extend(["", "── 协作过程 ──"])
            for i, turn in enumerate(turns, 1):
                preview = turn["response"][:200]
                if len(turn["response"]) > 200:
                    preview += "..."
                parts.append(f"  [{i}] {turn['to_member']}: {preview}")
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
            "active": "🔄",
            "completed": "✅",
            "timeout": "⏰",
            "error": "❌",
            "idle": "💤",
        }.get(conv["status"], "❓")

        lines = [
            f"{se} 协作历史",
            f"  发起成员: {conv.get('initial_member', '?')}",
            f"  状态: {conv['status']}",
            f"  轮次: {len(conv['turns'])}/{conv['max_rounds']}",
            "",
        ]
        for i, turn in enumerate(conv["turns"], 1):
            lines.append(
                f"[{i}] {turn['to_member']} "
                f"(来自 {turn['from_member']})"
            )
            lines.append(f"    任务: {turn['message'][:150]}")
            lines.append(f"    回复: {turn['response'][:250]}")
            if turn.get("delegated_to"):
                lines.append(f"    → 委托: {turn['delegated_to']}")
            lines.append("")

        return "\n".join(lines)

    def _status_text(self) -> str:
        """显示工作室状态：Agent + 成员列表 + 活跃对话"""
        agent_ok = self._agent is not None
        api_ok = self._agent is not None and bool(self._agent.api_key)
        model = self._agent.model if self._agent else "未知"
        n_members = len(self.studio_members)
        max_members = self.config.get("max_members", 10)
        active = sum(
            1 for c in self.conversations.values()
            if c["status"] == "active"
        )
        total = len(self.conversations)
        max_rounds = self.config.get("max_internal_turns", 10)

        lines = [
            "🏠 工作室状态",
            "",
            f"  Agent:      {'✅ 就绪' if agent_ok else '❌ 未初始化'}",
            f"  API Key:    {'✅ 已配置' if api_ok else '❌ 未配置'}",
            f"  模型:       {model}",
            f"  成员数:    {n_members}/{max_members}",
            f"  协作会话:  {active} 活跃 / {total} 总计",
            f"  最大轮次:  {max_rounds}",
            f"  持久化:    "
            f"{'✅ 开启' if self.config.get('persist_members', True) else '关闭'}",
            f"  自动审阅:  "
            f"{'✅ 开启' if self.config.get('auto_review', False) else '关闭'}",
        ]

        # 成员列表
        if n_members > 0:
            lines.extend(["", "── 成员列表 ──"])
            for name, info in self.studio_members.items():
                emoji = info.get("emoji", "🤖")
                lines.append(f"  {emoji} {name}")
            lines.append("")

        # 活跃对话
        active_convs = [
            (sid, c)
            for sid, c in self.conversations.items()
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
            "示例:\n"
            "  /studio add 架构师 你擅长系统设计和架构评审\n"
            "  /studio add 程序员 你擅长 Python 和 Go 编码实现\n"
            "  /studio chat @架构师 设计一个微服务架构\n"
            "  /studio chat @程序员 实现用户认证模块"
        )
