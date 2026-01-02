# services/context.py
import datetime
from typing import Optional, Dict, Any, List
from astrbot.api import logger
from ..config import SharingType

class ContextService:
    def __init__(self, context_obj, config):
        self.context = context_obj
        self.config = config
        self._life_plugin = None
        self._memos_plugin = None

    # ==================== 基础辅助方法 ====================

    def _find_plugin(self, keyword: str):
        """查找插件实例"""
        try:
            plugins = self.context.get_all_stars()
            for plugin in plugins:
                if keyword in getattr(plugin, "name", ""):
                    return getattr(plugin, "star_cls", None)
        except Exception as e:
            logger.warning(f"[Context] Find plugin '{keyword}' error: {e}")
        return None

    def _get_memos_plugin(self):
        """懒加载获取 Memos 插件"""
        if not self._memos_plugin:
            self._memos_plugin = self._find_plugin("astrbot_plugin_memos_integrator")
        return self._memos_plugin

    def _is_group_chat(self, target_umo: str) -> bool:
        """判断是否为群聊"""
        try:
            if not target_umo or not isinstance(target_umo, str):
                return False
            
            parts = target_umo.split(':')
            if len(parts) < 2:
                return False
            
            message_type = parts[1].lower()
            # 群聊类型关键词
            group_keywords = ['group', 'guild', 'channel', 'room']
            return any(keyword in message_type for keyword in group_keywords)
        except Exception as e:
            return False

    # ==================== 生活上下文 (Life Scheduler) ====================

    async def get_life_context(self) -> Optional[str]:
        """获取生活上下文"""
        if not self.config.get("enable_life_context", True): 
            return None
            
        if not self._life_plugin: 
            self._life_plugin = self._find_plugin("life_scheduler")
            
        if self._life_plugin and hasattr(self._life_plugin, 'get_life_context'):
            try: 
                ctx = await self._life_plugin.get_life_context()
                if ctx and len(ctx.strip()) > 10:
                    return ctx
            except Exception as e: 
                logger.warning(f"[Context] Life Scheduler error: {e}")
        return None

    def format_life_context(self, context: str, sharing_type: SharingType, is_group: bool, group_info: dict = None) -> str:
        """格式化生活上下文 (统一入口)"""
        if not context: return ""
        
        if is_group:
            return self._format_life_context_for_group(context, sharing_type, group_info)
        else:
            return self._format_life_context_for_private(context, sharing_type)

    def _format_life_context_for_group(self, context: str, sharing_type: SharingType, group_info: dict = None) -> str:
        """格式化群聊生活上下文"""
        if not self.config.get("life_context_in_group", True): return ""
        
        # 如果是心情分享，且群聊热度高，则不带生活状态（避免在大家讨论由于时突然说自己心情不好）
        if sharing_type == SharingType.MOOD and group_info and group_info.get("chat_intensity") == "high":
            return ""

        lines = context.split('\n')
        weather, period, busy = None, None, False
        for line in lines:
            if '天气' in line or '温度' in line: weather = line.strip()
            elif '时段' in line: period = line.strip()
            elif '今日计划' in line or '约会' in line: busy = True
        
        hint = "\n\n【你的状态】\n"
        if sharing_type == SharingType.GREETING:
            if weather: hint += f"{weather}\n💡 可以提醒大家注意天气\n"
            if period: hint += f"{period}\n"
            if busy: hint += "今天有些安排\n💡 可以简单提一下你今天比较忙\n"
            return hint
        elif sharing_type == SharingType.NEWS:
            if weather: return f"\n\n【当前场景】\n{weather}\n💡 可以说在什么天气下看到这个新闻\n"
        elif sharing_type == SharingType.MOOD:
            hint_str = f"\n\n【你的状态】\n{weather or ''}\n"
            if busy: hint_str += "今天有些事情要做\n"
            return hint_str + "💡 可以简单分享心情，但不要过于私人\n"
        return ""

    def _format_life_context_for_private(self, context: str, sharing_type: SharingType) -> str:
        """格式化私聊生活上下文"""
        if sharing_type == SharingType.GREETING:
            return f"\n\n【你的真实状态】\n{context}\n\n💡 可以结合上面的真实状态（天气、穿搭、今日计划）来打招呼\n"
        elif sharing_type == SharingType.MOOD:
            return f"\n\n【你现在的状态】\n{context}\n\n💡 可以结合当前的穿搭、天气、心情、约会等分享感受\n"
        elif sharing_type == SharingType.NEWS:
            lines = [l for l in context.split('\n') if '天气' in l or '穿搭' in l or '约会' in l]
            if lines:
                return f"\n\n【你当前在做什么】\n{chr(10).join(lines[:3])}\n\n💡 可以说明你在什么场景下看到这个新闻\n"
            return ""
        elif sharing_type in (SharingType.KNOWLEDGE, SharingType.RECOMMENDATION):
            lines = [l for l in context.split('\n') if '天气' in l or '时段' in l]
            if lines:
                return f"\n\n【当前场景】\n{chr(10).join(lines[:2])}\n\n💡 可以简单提一下当前场景\n"
            return ""
        return ""

    # ==================== 聊天历史 (Memos) ====================

    async def get_history_data(self, target_umo: str, is_group: bool = None) -> Dict[str, Any]:
        """获取聊天历史 (统一入口)"""
        # 1. 检查配置
        if not self.config.get("enable_chat_history", True):
            return {}
            
        # 自动判断是否为群聊 (如果调用方没传 is_group)
        if is_group is None:
            is_group = self._is_group_chat(target_umo)

        # 2. 获取插件
        memos = self._get_memos_plugin()
        if not memos or not hasattr(memos, 'memory_manager'):
            return {}

        # 3. 计算 limit
        default_limit = 10
        conf_limit = self.config.get("chat_history_count", default_limit)
        limit = min(self.config.get("group_chat_history_count", conf_limit * 2), 25) if is_group else conf_limit

        try:
            logger.info(f"[DailySharing] Fetching history for {target_umo} (limit={limit})...")
            # 4. 调用 API
            memories = await memos.memory_manager.retrieve_relevant_memories(
                query="最近的对话", 
                user_id=target_umo, 
                conversation_id="", 
                limit=limit
            )

            if not memories: 
                return {}

            # 5. 格式转换
            messages = []
            for mem in memories:
                m_type = mem.get("type", "fact")
                role = mem.get("role")
                
                if m_type == "preference":
                    role = "system"
                elif not role:
                    role = "assistant"
                
                # Memos 可能会把用户消息标记在 content 里
                content = mem.get("content", "")
                if content.startswith("User:") or content.startswith("用户:"):
                    role = "user"
                
                messages.append({
                    "role": role,
                    "content": content,
                    "timestamp": mem.get("timestamp", ""),
                    "user_id": mem.get("user_id", "")
                })

            result = {"messages": messages, "is_group": is_group}
            if is_group:
                result["group_info"] = self._analyze_group_chat(messages)
            
            return result

        except Exception as e:
            logger.error(f"[DailySharing] History API error: {e}")
            return {}

    def _analyze_group_chat(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """分析群聊历史"""
        if not messages: return {}
        try:
            user_count = {}
            topics = []
            timestamps = []
            
            for msg in messages:
                if msg.get("role") == "user":
                    uid = msg.get("user_id", "unknown")
                    user_count[uid] = user_count.get(uid, 0) + 1
                
                content = msg.get("content", "")
                if len(content) > 5: topics.append(content[:50])
                if msg.get("timestamp"): timestamps.append(msg.get("timestamp"))
            
            # 活跃用户
            active_users = sorted(user_count.items(), key=lambda x: x[1], reverse=True)[:3]
            
            # 聊天热度
            cnt = len(messages)
            if cnt > 10: intensity = "high"
            elif cnt > 5: intensity = "medium"
            else: intensity = "low"
            
            # 是否正在讨论 (5分钟内)
            is_discussing = False
            if timestamps:
                try:
                    last_ts = timestamps[-1]
                    if isinstance(last_ts, str): last = datetime.datetime.fromisoformat(last_ts)
                    else: last = last_ts
                    
                    if (datetime.datetime.now() - last).total_seconds() < 300:
                        is_discussing = True
                except: pass
            
            return {
                "recent_topics": topics[-5:],
                "active_users": [u for u, c in active_users],
                "chat_intensity": intensity,
                "message_count": cnt,
                "is_discussing": is_discussing,
            }
        except Exception as e:
            logger.warning(f"[DailySharing] Analyze group error: {e}")
            return {}

    def format_history_prompt(self, history_data: Dict, sharing_type: SharingType) -> str:
        """格式化 Prompt 文本 (统一入口)"""
        if not history_data or not history_data.get("messages"): return ""
        
        is_group = history_data.get("is_group", False)
        messages = history_data["messages"]
        
        if is_group:
            return self._format_group_chat_for_prompt(messages, history_data.get("group_info", {}), sharing_type)
        else:
            return self._format_private_chat_for_prompt(messages, sharing_type)

    def _format_group_chat_for_prompt(self, messages: List[Dict], group_info: Dict, sharing_type: SharingType) -> str:
        """格式化群聊 Prompt"""
        intensity = group_info.get("chat_intensity", "low")
        discussing = group_info.get("is_discussing", False)
        topics = group_info.get("recent_topics", [])
        
        # 提示词策略
        if sharing_type == SharingType.GREETING:
            if discussing:
                hint = "💡 群里正在热烈讨论，简短打个招呼即可"
            else:
                hint = "💡 可以活跃一下气氛"
        elif sharing_type == SharingType.NEWS:
            hint = "💡 选择可能引起群内讨论的新闻"
        elif sharing_type == SharingType.MOOD:
            hint = "💡 可以简单分享心情，但不要过于私人"
        else:
            hint = ""
        
        txt = f"\n\n【群聊状态】\n聊天热度: {intensity}\n消息数: {group_info.get('message_count', 0)} 条\n"
        if discussing: txt += "⚠️ 群里正在热烈讨论中！\n"
        if topics:
            txt += "\n【最近话题】\n" + "\n".join([f"• {t}..." for t in topics[-3:]])
        
        return txt + f"\n{hint}\n"

    def _format_private_chat_for_prompt(self, messages: List[Dict], sharing_type: SharingType) -> str:
        """格式化私聊 Prompt"""
        max_length = 500
        
        if sharing_type == SharingType.GREETING: hint = "💡 可以根据最近的对话内容打招呼"
        elif sharing_type == SharingType.MOOD: hint = "💡 可以延续最近的话题或感受"
        elif sharing_type == SharingType.NEWS: hint = "💡 可以根据对方的兴趣选择新闻"
        else: hint = "💡 可以自然地延续最近的对话"
        
        lines = []
        total_len = 0
        for m in reversed(messages[-5:]): # 默认取最近5条
            role = "用户" if m["role"] == "user" else "你"
            content = m["content"]
            if len(content) > 100: content = content[:100] + "..."
            
            line = f"{role}: {content}"
            if total_len + len(line) > max_length: break
            
            lines.insert(0, line)
            total_len += len(line)
        
        return "\n\n【最近的对话】\n" + "\n".join(lines) + f"\n\n{hint}\n"

    # ==================== 策略检查 ====================

    def check_group_strategy(self, group_info: Dict) -> bool:
        """检查群聊是否允许发送"""
        if not group_info: return True
        
        strategy = self.config.get("group_share_strategy", "cautious")
        is_discussing = group_info.get("is_discussing", False)
        intensity = group_info.get("chat_intensity", "low")

        if strategy == "cautious":
            # 谨慎模式：群里正在热烈讨论时，不打断
            if is_discussing and intensity == "high": return False
        elif strategy == "minimal":
            # 最小模式：只有没人说话时才发
            if is_discussing or intensity != "low": return False
        return True

    # ==================== 记忆记录 ====================

    async def record_to_memos(self, target_umo: str, content: str, image_desc: str = None):
        """记录发送内容到 Memos """
        if not self.config.get("record_sharing_to_memory", True): return
        
        memos = self._get_memos_plugin()
        if memos:
            try:
                full_text = content
                if image_desc: 
                    tag = f"[配图: {image_desc}]" if self.config.get("record_image_description", True) else "[已发送配图]"
                    full_text += f"\n{tag}"
                elif image_desc is not None:
                    full_text += "\n[已发送配图]"

                cid = await self.context.conversation_manager.get_curr_conversation_id(target_umo)
                if not cid: cid = await self.context.conversation_manager.new_conversation(target_umo)

                await memos.memory_manager.add_message(
                    messages=[{"role": "assistant", "content": full_text}],
                    user_id=target_umo, conversation_id=cid
                )
                logger.info(f"[Context] Recorded to Memos for {target_umo}")
            except Exception as e: 
                logger.warning(f"[Context] Record error: {e}")
