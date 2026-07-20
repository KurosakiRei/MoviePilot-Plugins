import asyncio
import json
import re
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Dict, Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.context import Context
from app.core.event import eventmanager, Event
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType
from app.utils.http import RequestUtils


class SiteDownloadBridge(_PluginBase):
    """
    站点索引 + 下载桥接插件（CustomIndexer 增强版）

    ====== 两大核心功能 ======

    1. 索引器注册（indexer）：
       为 MoviePilot 添加自定义站点的搜索能力，自动获取标题、大小、
       做种者、下载链接等元数据。格式兼容官方索引器配置，但无需 base64。

    2. 下载桥接（bridge）：
       对搜索结果返回的是种子详情页（而非直接下载链接）的站点，
       自动抓取详情页，识别磁力链接(magnet:) 或种子文件(.torrent)。

    ====== 配置格式 (YAML) ======

    sites:
      # ---- 仅下载桥接（站点已在 TorrentKitty 等索引器中）----
      - name: "FileMood"
        domains:
          - "filemood.com"
        bridge:
          need_cookie: true
          need_proxy: true

      # ---- 索引器 + 下载桥接（完整配置）----
      - name: "T-Baozi"
        domains:
          - "p.t-baozi.cc"
        indexer:
          domain: "p.t-baozi.cc"
          public: false
          search:
            paths:
              - path: "/torrents.php?search={keyword}&search_area=0"
          torrents:
            list:
              selector: "table.torrents > tbody > tr:not(.table2_title)"
            fields:
              title:
                selector: "td:nth-child(2) table.torrentname a b"
              download:
                selector: "td:nth-child(2) a[href^='download.php']"
                attribute: "href"
              size:
                selector: "td:nth-child(5)"
              seeders:
                selector: "td:nth-child(6)"
        bridge:
          need_cookie: true
          need_proxy: false
          # download_selector 可选，不填则自动检测
    """

    plugin_name = "站点下载桥接"
    plugin_desc = "索引器注册 + 二次跳转下载：支持自定义站点搜索（获取标题/大小/做种者等）并自动从详情页提取真实磁链/种子。YAML配置，无需base64。"
    plugin_icon = "download.png"
    plugin_version = "1.1"
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
        """解析 YAML 配置，注册索引器并加载桥接规则"""
        self._sites_config = []
        if not self._config_yaml:
            return
        try:
            data = yaml.safe_load(self._config_yaml)
            if not isinstance(data, dict) or "sites" not in data:
                return

            registered_count = 0
            bridge_count = 0

            for site in data["sites"]:
                if not isinstance(site, dict):
                    continue

                name = site.get("name", "")
                if not name:
                    continue

                # ---- 解析域名 ----
                domains = site.get("domains", [])
                if isinstance(domains, str):
                    domains = [domains]
                # 也从 indexer.domain 提取
                indexer_cfg = site.get("indexer")
                if isinstance(indexer_cfg, dict):
                    idx_domain = indexer_cfg.get("domain", "")
                    if idx_domain and idx_domain not in domains:
                        domains.append(idx_domain)
                domains = [d.lower().strip() for d in domains if d]

                if not domains:
                    logger.warn(f"[SiteDownloadBridge] 站点 '{name}' 缺少域名，跳过")
                    continue

                # ---- 注册索引器 ----
                if isinstance(indexer_cfg, dict) and indexer_cfg.get("domain"):
                    try:
                        domain_key = indexer_cfg.get("domain", "")
                        # 构建标准索引器配置（移除 bridge 无关字段，保留纯索引器 JSON）
                        idx_json = {k: v for k, v in indexer_cfg.items()
                                    if k not in ("bridge",)}
                        SitesHelper().add_indexer(domain_key, idx_json)
                        registered_count += 1
                        logger.info(f"[SiteDownloadBridge] ✅ 已注册索引器: {name} ({domain_key})")
                    except Exception as e:
                        logger.error(f"[SiteDownloadBridge] 注册索引器失败 {name}: {e}")

                # ---- 加载桥接规则 ----
                bridge_cfg = site.get("bridge")
                bridge_enabled = False
                need_cookie = False
                need_proxy = False
                download_selector = ""
                download_attr = "href"
                encoding = "utf-8"
                url_regex = ""

                if isinstance(bridge_cfg, dict):
                    bridge_enabled = bridge_cfg.get("enabled", True)
                    need_cookie = bridge_cfg.get("need_cookie", False)
                    need_proxy = bridge_cfg.get("need_proxy", False)
                    download_selector = bridge_cfg.get("download_selector", "")
                    download_attr = bridge_cfg.get("download_attr", "href")
                    encoding = bridge_cfg.get("encoding", "utf-8")
                    url_regex = bridge_cfg.get("url_regex", "")
                elif isinstance(bridge_cfg, bool) and bridge_cfg:
                    bridge_enabled = True
                # 兼容旧格式：直接在 site 层级配置 bridge 参数
                elif site.get("need_cookie") is not None or site.get("need_proxy") is not None:
                    bridge_enabled = True
                    need_cookie = site.get("need_cookie", False)
                    need_proxy = site.get("need_proxy", False)
                    download_selector = site.get("download_selector", "")

                if bridge_enabled:
                    self._sites_config.append({
                        "name": name,
                        "domains": domains,
                        "download_selector": download_selector,
                        "download_attr": download_attr,
                        "need_cookie": need_cookie,
                        "need_proxy": need_proxy,
                        "encoding": encoding,
                        "url_regex": url_regex,
                    })
                    bridge_count += 1
                    logger.info(f"[SiteDownloadBridge] 已加载桥接规则: {name} → {domains}")

            logger.info(f"[SiteDownloadBridge] 配置解析完成: {registered_count} 个索引器, {bridge_count} 个桥接规则")

        except Exception as e:
            logger.error(f"[SiteDownloadBridge] YAML 配置解析失败: {e}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def _match_site(self, page_url: str) -> Optional[Dict]:
        """根据 URL 匹配对应的站点桥接配置"""
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

            # ---- 策略 1：CSS 选择器精确提取 ----
            if selector:
                elements = soup.select(selector)
                if elements:
                    for el in elements:
                        url = (el.get(attr) or "").strip()
                        if url:
                            if not url.startswith("http") and not url.startswith("magnet:"):
                                url = urljoin(page_url, url)
                            candidates.append(url)

            # ---- 策略 2：自动扫描所有 <a> 标签 ----
            if not candidates:
                for a_tag in soup.find_all("a", href=True):
                    url = (a_tag.get("href") or "").strip()
                    if not url:
                        continue
                    if not url.startswith("http") and not url.startswith("magnet:"):
                        url = urljoin(page_url, url)
                    url_lower = url.lower()
                    if url.startswith("magnet:") or ".torrent" in url_lower or \
                            any(kw in url_lower for kw in ["download", "torrent", "getfile"]):
                        if url not in candidates:
                            candidates.append(url)

            if not candidates:
                logger.warn(f"[SiteDownloadBridge] 未找到任何下载链接 @ {page_url}")
                return None

            # ---- 按优先级选择：磁力链接 > .torrent ----
            for url in candidates:
                if url_regex and not re.search(url_regex, url):
                    continue
                if url.startswith("magnet:"):
                    logger.info(f"[SiteDownloadBridge] ✅ {site_config['name']}: 自动检测到磁力链接")
                    return url

            for url in candidates:
                if url_regex and not re.search(url_regex, url):
                    continue
                if ".torrent" in url.lower():
                    logger.info(f"[SiteDownloadBridge] ✅ {site_config['name']}: 自动检测到种子文件 {url[:80]}...")
                    return url

            # 兜底
            for url in candidates:
                if not url_regex or re.search(url_regex, url):
                    logger.info(f"[SiteDownloadBridge] ⚠ {site_config['name']}: 使用候选链接 {url[:80]}...")
                    return url

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

        bridge_tasks = []
        for idx, ctx in enumerate(contexts):
            torrent = getattr(ctx, "torrent_info", None)
            if not torrent:
                continue

            page_url = getattr(torrent, "page_url", None) or getattr(torrent, "enclosure", None)
            if not page_url:
                continue

            if str(page_url).startswith("magnet:") or str(page_url).endswith(".torrent"):
                continue

            site_config = self._match_site(str(page_url))
            if site_config:
                bridge_tasks.append((idx, ctx, site_config))

        if not bridge_tasks:
            return

        logger.info(f"[SiteDownloadBridge] 发现 {len(bridge_tasks)} 个需要桥接的资源，开始并行解析...")

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
        default_yaml = """# ====== 站点索引 + 下载桥接配置 (YAML) ======
#
# 两种配置方式可单独使用或组合使用:
#
# 【仅下载桥接】站点已在 TorrentKitty 等索引器中，只需处理二次跳转:
#   sites:
#     - name: "FileMood"
#       domains: ["filemood.com"]
#       bridge:
#         need_cookie: true
#         need_proxy: true
#
# 【索引器 + 下载桥接】完整配置新站点:
#   sites:
#     - name: "MySite"
#       domains: ["mysite.com"]
#       indexer:
#         domain: "mysite.com"
#         public: false
#         search:
#           paths:
#             - path: "/torrents.php?search={keyword}"
#         torrents:
#           list:
#             selector: "table.torrents > tbody > tr"
#           fields:
#             title:
#               selector: "td:nth-child(2) a"
#             download:
#               selector: "td:nth-child(3) a"
#               attribute: "href"
#             size:
#               selector: "td:nth-child(5)"
#             seeders:
#               selector: "td:nth-child(6)"
#       bridge:
#         need_cookie: true
#         need_proxy: false
#
sites:
  - name: "FileMood"
    domains:
      - "filemood.com"
    bridge:
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
                                            "label": "站点配置 (YAML)",
                                            "rows": 20,
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
                                            "text": "两大功能：1) indexer 注册索引器（获取标题/大小/做种者等元数据）；2) bridge 处理二次跳转下载。indexer 配置格式兼容官方，但无需 base64 编码。bridge 自动检测 magnet: 和 .torrent 链接。需依赖：beautifulsoup4, lxml, pyyaml"
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
