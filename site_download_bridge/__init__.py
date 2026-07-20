import asyncio
import re
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Dict, Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.context import Context
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType
from app.utils.http import RequestUtils


class SiteDownloadBridge(_PluginBase):
    """
    二次跳转种子下载桥接插件

    支持那些搜索结果返回的是种子详情页（而非直接下载链接）的站点。
    插件会在资源选择阶段自动抓取详情页，智能识别页面中的磁力链接(magnet:)
    和种子文件(.torrent)，无需手动指定CSS选择器即可工作。

    两种工作模式：
      1. 自动模式（推荐）：不填 download_selector，自动扫描页面所有链接
      2. 精确模式：指定 CSS 选择器精确定位下载元素

    自动模式会按优先级匹配：磁力链接 > .torrent文件 > 含download关键字的链接

    配置格式 (YAML):
      sites:
        - name: "FileMood"
          domains:
            - "filemood.com"
          # download_selector 可选，留空则自动检测
          need_cookie: true
          need_proxy: true

        - name: "复杂站点"  # 如需精确匹配
          domains:
            - "complex-site.com"
          download_selector: "a.dl-btn"   # 可选：CSS选择器
          download_attr: "href"
          url_regex: "\\.torrent$"          # 可选：URL正则过滤
          need_cookie: false
          need_proxy: false
    """

    plugin_name = "站点下载桥接"
    plugin_desc = "支持二次跳转站点的种子下载：自动从详情页提取真实种子/磁力链接。使用YAML配置，可读性强，支持多站点。"
    plugin_icon = "download.png"
    plugin_version = "1.0"
    plugin_author = "KurosakiRei"
    author_url = "https://github.com/KurosakiRei"
    plugin_config_prefix = "sitedownloadbridge_"
    plugin_order = 22
    auth_level = 1

    _enabled = False
    _config_yaml = ""
    _sites_config = []
    _fetch_timeout = 10
    _max_workers = 3

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._config_yaml = config.get("config_yaml", "")
            self._fetch_timeout = int(config.get("fetch_timeout", 10))
            self._max_workers = int(config.get("max_workers", 3))
            self._parse_config()

    def _parse_config(self):
        """解析 YAML 配置"""
        self._sites_config = []
        if not self._config_yaml:
            return
        try:
            data = yaml.safe_load(self._config_yaml)
            if isinstance(data, dict) and "sites" in data:
                for site in data["sites"]:
                    if not isinstance(site, dict):
                        continue
                    name = site.get("name", "")
                    domains = site.get("domains", [])
                    if isinstance(domains, str):
                        domains = [domains]
                    # download_selector 可选：不填则自动扫描页面所有磁链/torrent
                    download_selector = site.get("download_selector", "")
                    download_attr = site.get("download_attr", "href")
                    need_cookie = site.get("need_cookie", False)
                    need_proxy = site.get("need_proxy", False)
                    encoding = site.get("encoding", "utf-8")
                    url_regex = site.get("url_regex", "")

                    if not name or not domains:
                        logger.warn(f"[SiteDownloadBridge] 站点配置不完整（缺少名称或域名），跳过: {name}")
                        continue

                    self._sites_config.append({
                        "name": name,
                        "domains": [d.lower().strip() for d in domains],
                        "download_selector": download_selector,
                        "download_attr": download_attr,
                        "need_cookie": need_cookie,
                        "need_proxy": need_proxy,
                        "encoding": encoding,
                        "url_regex": url_regex,
                    })
                    logger.info(f"[SiteDownloadBridge] 已加载站点: {name} → {domains}")
        except Exception as e:
            logger.error(f"[SiteDownloadBridge] YAML 配置解析失败: {e}")

    def get_state(self) -> bool:
        return self._enabled

    def _match_site(self, page_url: str) -> Optional[Dict]:
        """根据 URL 匹配对应的站点配置"""
        if not page_url:
            return None
        try:
            hostname = urlparse(page_url).hostname or ""
            hostname = hostname.lower()
        except Exception:
            return None

        for site in self._sites_config:
            for domain in site["domains"]:
                if hostname == domain or hostname.endswith("." + domain):
                    return site
        return None

    def _resolve_download_url(self, page_url: str, site_config: Dict, torrent_ctx) -> Optional[str]:
        """
        从详情页提取真实下载链接。

        策略（按优先级）：
          1. 如果配置了 download_selector，用 CSS 选择器精确提取
          2. 否则自动扫描页面上所有 <a> 标签，匹配 magnet: 或 .torrent 链接
          3. 自动扫描也支持可选的 url_regex 过滤

        返回: 真实的种子/磁力 URL，失败返回 None
        """
        try:
            cookie = getattr(torrent_ctx, "site_cookie", None) if torrent_ctx else None
            ua = getattr(torrent_ctx, "site_ua", None) if torrent_ctx else None
            use_proxy = site_config.get("need_proxy", False)

            req = RequestUtils(
                ua=ua,
                cookies=cookie,
                proxies=settings.PROXY if use_proxy else None,
            ).get_res(url=page_url, timeout=self._fetch_timeout)

            if req is None or req.status_code != 200:
                logger.warn(f"[SiteDownloadBridge] 获取详情页失败: {page_url} status={req.status_code if req else 'None'}")
                return None

            encoding = site_config.get("encoding", "utf-8")
            if encoding and req.apparent_encoding:
                try:
                    req.encoding = encoding
                except Exception:
                    req.encoding = req.apparent_encoding

            html = req.text
            soup = BeautifulSoup(html, "html.parser")
            selector = site_config.get("download_selector", "")
            attr = site_config.get("download_attr", "href")
            url_regex = site_config.get("url_regex", "")

            candidates = []

            # ---- 策略 1：CSS 选择器精确提取（如果配置了）----
            if selector:
                elements = soup.select(selector)
                if elements:
                    for el in elements:
                        url = (el.get(attr) or "").strip()
                        if url:
                            if not url.startswith("http") and not url.startswith("magnet:"):
                                url = urljoin(page_url, url)
                            candidates.append(url)
                    if candidates:
                        logger.debug(f"[SiteDownloadBridge] CSS选择器匹配到 {len(candidates)} 个候选链接")

            # ---- 策略 2：自动扫描所有 <a> 标签 ----
            if not candidates or not selector:
                logger.debug(f"[SiteDownloadBridge] 使用自动扫描模式（{'选择器未配置' if not selector else '选择器无结果'}）")
                for a_tag in soup.find_all("a", href=True):
                    url = (a_tag.get("href") or "").strip()
                    if not url:
                        continue

                    # 转为绝对 URL
                    if not url.startswith("http") and not url.startswith("magnet:"):
                        url = urljoin(page_url, url)

                    url_lower = url.lower()
                    # 匹配磁力链接、.torrent 文件、或包含 download/torrent 关键字的链接
                    if url.startswith("magnet:") or ".torrent" in url_lower or \
                            any(kw in url_lower for kw in ["download", "torrent", "getfile"]):
                        if url not in candidates:
                            candidates.append(url)

                logger.debug(f"[SiteDownloadBridge] 自动扫描发现 {len(candidates)} 个候选链接")

            if not candidates:
                logger.warn(f"[SiteDownloadBridge] 未找到任何下载链接 @ {page_url}")
                return None

            # ---- 过滤与选择最佳候选 ----
            for url in candidates:
                if url_regex and not re.search(url_regex, url):
                    continue
                # 优先级：磁力链接 > .torrent 直链 > 含 download/torrent 关键字的链接
                if url.startswith("magnet:"):
                    logger.info(f"[SiteDownloadBridge] ✅ {site_config['name']}: 自动检测到磁力链接")
                    return url

            for url in candidates:
                if url_regex and not re.search(url_regex, url):
                    continue
                if url.lower().endswith(".torrent") or ".torrent" in url.lower():
                    logger.info(f"[SiteDownloadBridge] ✅ {site_config['name']}: 自动检测到种子文件 {url[:80]}...")
                    return url

            # 兜底：返回第一个通过正则的候选
            for url in candidates:
                if not url_regex or re.search(url_regex, url):
                    logger.info(f"[SiteDownloadBridge] ⚠ {site_config['name']}: 使用候选链接 {url[:80]}...")
                    return url

            logger.warn(f"[SiteDownloadBridge] 所有候选链接被 url_regex 过滤: {url_regex}")
            return None

        except Exception as e:
            logger.error(f"[SiteDownloadBridge] 提取下载链接异常: {e}", exc_info=True)
            return None

    @eventmanager.register(ChainEventType.ResourceSelection)
    def _on_resource_selection(self, event: Event):
        """在资源选择阶段，预解析二次跳转站点的真实下载链接"""
        if not self._enabled or not self._sites_config:
            return

        event_data = getattr(event, "event_data", None)
        if not event_data:
            return

        contexts = getattr(event_data, "contexts", None)
        if not contexts:
            return

        # 找出需要桥接的 torrent
        bridge_tasks = []  # (index, context, site_config)
        for idx, ctx in enumerate(contexts):
            torrent = getattr(ctx, "torrent_info", None)
            if not torrent:
                continue

            # 优先使用 enclosure，其次 page_url
            page_url = getattr(torrent, "page_url", None) or getattr(torrent, "enclosure", None)
            if not page_url:
                continue

            # 跳过已经是磁力链接或 .torrent 直链的
            if str(page_url).startswith("magnet:") or str(page_url).endswith(".torrent"):
                continue

            site_config = self._match_site(str(page_url))
            if site_config:
                bridge_tasks.append((idx, ctx, site_config))

        if not bridge_tasks:
            return

        logger.info(f"[SiteDownloadBridge] 发现 {len(bridge_tasks)} 个需要桥接的资源，开始并行解析...")

        # 并行抓取详情页、提取下载链接
        resolved_count = 0
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {}
            for idx, ctx, site_config in bridge_tasks:
                torrent = getattr(ctx, "torrent_info", None)
                page_url = getattr(torrent, "page_url", None) or getattr(torrent, "enclosure", None)
                future = executor.submit(self._resolve_download_url, page_url, site_config, torrent)
                futures[future] = (idx, ctx, site_config, page_url)

            for future in as_completed(futures):
                idx, ctx, site_config, page_url = futures[future]
                try:
                    real_url = future.result(timeout=self._fetch_timeout + 5)
                    if real_url:
                        torrent = getattr(ctx, "torrent_info", None)
                        if torrent:
                            # 设置 enclosure 为真实下载链接（MoviePilot 下载时优先读此字段）
                            try:
                                torrent.enclosure = real_url
                                resolved_count += 1
                                logger.info(f"[SiteDownloadBridge] ✅ {site_config['name']}: {page_url[:60]}... → {real_url[:60]}...")
                            except Exception as e:
                                logger.warn(f"[SiteDownloadBridge] 设置 enclosure 失败: {e}")
                except Exception as e:
                    logger.warn(f"[SiteDownloadBridge] 解析失败 {site_config['name']}: {e}")

        if resolved_count > 0:
            event_data.updated = True
            event_data.updated_contexts = contexts
            event_data.source = "SiteDownloadBridge"
            logger.info(f"[SiteDownloadBridge] 解析完成: {resolved_count}/{len(bridge_tasks)} 个成功")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        default_yaml = """# 二次跳转站点配置 (YAML格式)
# 支持两种模式：
#
# 【自动模式】不填 download_selector，自动扫描页面所有磁链和种子
# sites:
#   - name: "FileMood"
#     domains: ["filemood.com"]
#     need_cookie: true
#     need_proxy: true
#
# 【精确模式】指定CSS选择器精确定位（高级用户）
#   download_selector: CSS选择器（可选，不填则自动检测）
#   download_attr:      提取属性（默认 href）
#   url_regex:          URL正则过滤（可选）
#   need_cookie:        是否需要站点Cookie
#   need_proxy:         是否使用代理(FlareSolverr)
#   encoding:           页面编码（默认 utf-8）
#
sites:
  - name: "FileMood"
    domains:
      - "filemood.com"
    need_cookie: true
    need_proxy: true
"""
        return [
            {
                "component": "VForm",
                "content": [
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
                                            "model": "enabled",
                                            "label": "启用插件",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "fetch_timeout",
                                            "label": "抓取超时(秒)",
                                            "placeholder": "10",
                                            "type": "number",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_workers",
                                            "label": "并行线程数",
                                            "placeholder": "3",
                                            "type": "number",
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
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "config_yaml",
                                            "label": "站点桥接配置 (YAML)",
                                            "rows": 18,
                                            "placeholder": "在此粘贴 YAML 格式的站点配置...",
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
                                            "text": "工作原理：搜索返回结果后，自动抓取详情页。优先使用CSS选择器（如配置），否则自动扫描页面所有 magnet: 和 .torrent 链接。支持并行解析。需依赖：beautifulsoup4, lxml, pyyaml"
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
            "config_yaml": default_yaml,
            "fetch_timeout": 10,
            "max_workers": 3,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        pass
