# services/news.py
import random
import aiohttp
import asyncio
from typing import Optional, List, Dict, Any 
from astrbot.api import logger
from ..config import NEWS_SOURCE_MAP, NEWS_TIME_PREFERENCES, TimePeriod

class NewsService:
    def __init__(self, config: dict):
        self.config = config

    def _get_current_period(self) -> TimePeriod:
        from datetime import datetime
        hour = datetime.now().hour
        if 0 <= hour < 6: return TimePeriod.DAWN
        elif 6 <= hour < 11: return TimePeriod.MORNING
        elif 11 <= hour < 17: return TimePeriod.AFTERNOON
        elif 17 <= hour < 20: return TimePeriod.EVENING
        else: return TimePeriod.NIGHT

    def select_news_source(self) -> str:
        """选择主新闻源"""
        mode = self.config.get("news_random_mode", "config")
        
        if mode == "fixed": 
            source = self.config.get("news_api_source", "zhihu")
            logger.debug(f"[News] 固定模式: {source}")
            return source
        elif mode == "random": 
            source = random.choice(list(NEWS_SOURCE_MAP.keys()))
            logger.info(f"[News] 🎲 完全随机: {NEWS_SOURCE_MAP[source]['name']}")
            return source
        elif mode == "config":
            c = self.config.get("news_random_sources", ["zhihu", "weibo"])
            valid = [s for s in c if s in NEWS_SOURCE_MAP]
            if not valid: valid = ["zhihu"] # 兜底
            source = random.choice(valid)
            logger.info(f"[News] 🎲 配置列表随机: {NEWS_SOURCE_MAP[source]['name']}")
            return source
        elif mode == "time_based": 
            return self._select_by_time()
        
        return "zhihu"

    def _select_by_time(self) -> str:
        """基于时间的智能选择"""
        period = self._get_current_period()
        # 获取偏好，默认为早晨配置
        prefs = NEWS_TIME_PREFERENCES.get(period, NEWS_TIME_PREFERENCES[TimePeriod.MORNING])
        
        # 获取用户配置的源列表
        conf = self.config.get("news_random_sources", None)
        
        selected = "zhihu"
        if conf:
            # 如果配置了限制列表，取交集
            valid = [s for s in conf if s in prefs]
            if valid:
                # 重新计算权重
                total = sum(prefs[s] for s in valid)
                weights = [prefs[s]/total for s in valid]
                selected = random.choices(valid, weights=weights, k=1)[0]
            else:
                # 没交集则从配置里随机
                selected = random.choice(conf)
        else:
            # 默认使用所有偏好
            selected = random.choices(list(prefs.keys()), weights=list(prefs.values()), k=1)[0]
            
        period_label = {
            TimePeriod.DAWN: "凌晨", TimePeriod.MORNING: "早晨",
            TimePeriod.AFTERNOON: "下午", TimePeriod.EVENING: "傍晚", TimePeriod.NIGHT: "深夜"
        }.get(period, "现在")
        
        logger.info(f"[News] 🎲 {period_label}智能选择: {NEWS_SOURCE_MAP[selected]['name']}")
        return selected

    async def get_hot_news(self) -> Optional[tuple]:
        """获取热搜 (包含降级重试逻辑)"""
        # 0. 检查开关和Key
        if not self.config.get("enable_news_api", True): return None
        
        key = self.config.get("nycnm_api_key", "").strip()
        if not key: 
            logger.error("[News] ❌ 未配置柠柚API密钥！")
            return None

        # 1. 尝试主要源
        pri_source = self.select_news_source()
        res = await self._fetch_news(pri_source, key)
        if res: 
            return (res, pri_source)

        # 2. 失败降级逻辑
        logger.warning(f"[News] 主要源 {pri_source} 失败，尝试备用源...")
        
        mode = self.config.get("news_random_mode", "config")
        
        # 确定备选池范围
        if mode in ["config", "time_based"]:
            # 只从用户配置的列表中找
            configured = self.config.get("news_random_sources", ["zhihu", "weibo"])
            pool = [s for s in configured if s in NEWS_SOURCE_MAP]
        else:
            # 从所有可用源中找
            pool = list(NEWS_SOURCE_MAP.keys())
            
        # 排除刚才失败的源
        fallback_pool = [s for s in pool if s != pri_source]
        
        if not fallback_pool: 
            logger.warning("[News] 没有可用的备用源")
            return None
        
        back_source = random.choice(fallback_pool)
        logger.info(f"[News] 尝试备用源: {NEWS_SOURCE_MAP[back_source]['name']}")
        
        res = await self._fetch_news(back_source, key)
        if res:
            logger.info(f"[News] ✅ 备用源成功")
            return (res, back_source)
        
        logger.warning(f"[News] 所有新闻源均失败")
        return None

    async def _fetch_news(self, source: str, key: str) -> Optional[List[Dict]]:
        """执行 HTTP 请求 (带完整错误处理)"""
        if source not in NEWS_SOURCE_MAP: return None
        
        source_name = NEWS_SOURCE_MAP[source]['name']
        url = NEWS_SOURCE_MAP[source]['url']
        full_url = f"{url}?format=json&apikey={key}"
        
        timeout = self.config.get("news_api_timeout", 15)
        
        logger.info(f"[News] 获取新闻: {source_name}")
        logger.debug(f"[News] 请求URL: {url}?format=json&apikey=***")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, timeout=timeout) as resp:
                    if resp.status != 200: 
                        logger.warning(f"[News] API返回状态码: {resp.status}")
                        if resp.status in (401, 403):
                            logger.error("[News] ❌ API密钥无效或已过期！")
                        return None
                    
                    data = await resp.json(content_type=None)
                    parsed = self._parse_response(data)
                    
                    if parsed:
                        logger.info(f"[News] ✅ 成功获取 {len(parsed)} 条{source_name}")
                        return parsed
                    else:
                        logger.warning(f"[News] ⚠️ 未能解析到新闻内容")
                        logger.debug(f"[News] 原始数据: {str(data)[:300]}...")
                        return None
                        
        except asyncio.TimeoutError:
            logger.error(f"[News] ⏱️ 请求超时: {source_name}")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"[News] 🌐 网络请求失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[News] ❌ 解析新闻失败: {e}", exc_info=True)
            return None

    def _parse_response(self, data: Any) -> Optional[List[Dict]]:
        """
        解析响应数据
        支持多层级 JSON 和多种字段名 (hot/heat/hotValue)
        """
        items = []
        
        # 1. 定位列表数据位置 (兼容多种API返回格式)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # 尝试常见的数据包裹字段
            for k in ["data", "list", "items", "result"]:
                if k in data:
                    val = data[k]
                    if isinstance(val, list): 
                        items = val
                        break
                    elif isinstance(val, dict):
                        # 处理嵌套情况 data: { list: [] }
                        for sub_k in ["list", "items"]:
                            if sub_k in val and isinstance(val[sub_k], list): 
                                items = val[sub_k]
                                break
        
        if not items: return None

        # 2. 提取字段 (title, hot, url)
        res = []
        for i in items[:15]: # 限制前15条
            if not isinstance(i, dict): continue
            
            # 标题提取 (兼容多种字段名)
            title = i.get("title") or i.get("name") or i.get("query") or i.get("word")
            if not title: continue
            
            # 热度提取 (兼容多种字段名)
            hot = i.get("hot") or i.get("hotValue") or i.get("heat") or i.get("hotScore") or ""
            
            # URL 提取 (兼容多种字段名)
            url_link = i.get("url") or i.get("link") or i.get("mobileUrl") or ""
            
            res.append({
                "title": str(title).strip(),
                "hot": str(hot).strip() if hot else "",
                "url": str(url_link).strip() if url_link else ""
            })
            
        return res if res else None
