import re
from typing import List, Tuple, Dict, Any

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType


class SearchEnhancer(_PluginBase):
    """搜索结果增强插件：过滤占位符、年份宽松提示、中文标题包含匹配"""

    plugin_name = "搜索增强"
    plugin_desc = "增强搜索结果匹配：过滤No result占位符、年份不匹配标记为警告而非丢弃、中文标题包含匹配。"
    plugin_icon = "search.png"
    plugin_version = "1.0"
    plugin_author = "KurosakiRei"
    author_url = "https://github.com/KurosakiRei"
    plugin_config_prefix = "searchenhancer_"
    plugin_order = 21
    auth_level = 1

    _enabled = False
    _filter_noresult = True
    _relax_year = True
    _fuzzy_cn_match = True

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._filter_noresult = config.get("filter_noresult", True)
            self._relax_year = config.get("relax_year", True)
            self._fuzzy_cn_match = config.get("fuzzy_cn_match", True)

    def get_state(self) -> bool:
        return self._enabled

    @eventmanager.register(ChainEventType.ResourceSelection)
    def _on_resource_selection(self, event: Event):
        """拦截资源选择，过滤占位符并标记年份不匹配"""
        if not self._enabled:
            return

        event_data = getattr(event, "event_data", None)
        if not event_data:
            return

        contexts = getattr(event_data, "contexts", None)
        if not contexts:
            return

        updated = False
        filtered = []
        for ctx in contexts:
            torrent = getattr(ctx, "torrent_info", None)
            meta = getattr(ctx, "meta_info", None)
            if not torrent or not meta:
                filtered.append(ctx)
                continue

            title = getattr(torrent, "title", "") or ""
            org_string = getattr(meta, "org_string", "") or title

            # 1. 过滤 "No result" 占位符
            if self._filter_noresult and org_string.strip().lower().startswith("no result"):
                logger.info(f"[SearchEnhancer] 过滤占位符: {title}")
                updated = True
                continue

            # 2. 年份宽松：仅记录警告，不丢弃（文件补丁已处理，此处作为安全网）
            # 如果 meta.year 存在但与媒体年份不匹配，在 torrent 描述中添加标记
            if self._relax_year:
                media = getattr(ctx, "media_info", None)
                if media and meta.year:
                    media_year = getattr(media, "year", None)
                    if media_year and str(meta.year) != str(media_year):
                        desc = getattr(torrent, "description", "") or ""
                        year_tag = f"[⚠年份:{meta.year}≠{media_year}]"
                        if year_tag not in (desc or ""):
                            try:
                                torrent.description = f"{desc} {year_tag}".strip()
                            except Exception:
                                pass

            filtered.append(ctx)

        if updated and filtered != contexts:
            event_data.updated = True
            event_data.updated_contexts = filtered
            event_data.source = "SearchEnhancer"
            logger.info(f"[SearchEnhancer] 过滤完成: {len(contexts)} → {len(filtered)} 条结果")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "filter_noresult",
                                            "label": "过滤 No result 占位符",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "relax_year",
                                            "label": "年份不匹配标警告",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "fuzzy_cn_match",
                                            "label": "中文标题包含匹配",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "核心匹配改进（中文包含匹配、年份宽松不丢弃）通过文件补丁实现。本插件作为安全网提供二次过滤。"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "filter_noresult": True,
            "relax_year": True,
            "fuzzy_cn_match": True,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        pass
