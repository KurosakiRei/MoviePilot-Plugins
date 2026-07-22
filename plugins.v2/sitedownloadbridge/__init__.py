import json
import re
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Dict, Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType
from app.utils.http import RequestUtils


class SiteDownloadBridge(_PluginBase):
    """
    站点索引 + 下载桥接 + 按钮触发（CustomIndexer 增强版）
    
    ====== 三大核心功能 ======
    
    1. indexer — 索引器注册：添加自定义站点搜索，获取标题/大小/做种者等元数据
    2. bridge  — 下载桥接：自动扫描或 CSS 选择器提取页面中的 magnet:/torrent 链接
    3. trigger — 按钮触发：模拟点击 JS 按钮，解析 AJAX 请求获取下载链接（FileMood 类站点）
    
    详见同目录下的 YAML_GUIDE.md
    """

    plugin_name = "站点下载桥接"
    plugin_desc = "索引器+桥接+按钮触发：支持自定义站点搜索、二次跳转下载、JS按钮触发的AJAX下载。YAML配置，自动检测磁链/种子。"
    plugin_icon = "download.png"
    plugin_version = "1.2"
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

    # ---------- 生命周期 ----------

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._config_yaml = config.get("config_yaml", "")
            self._fetch_timeout = int(config.get("fetch_timeout", 10))
            self._max_workers = int(config.get("max_workers", 3))
            self._parse_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
        pass

    # ---------- 配置解析 ----------

    def _parse_config(self):
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

                # 域名
                domains = site.get("domains", [])
                if isinstance(domains, str):
                    domains = [domains]
                indexer_cfg = site.get("indexer")
                if isinstance(indexer_cfg, dict):
                    idx_domain = indexer_cfg.get("domain", "")
                    if idx_domain and idx_domain not in domains:
                        domains.append(idx_domain)
                domains = [d.lower().strip() for d in domains if d]
                if not domains:
                    logger.warn(f"[SiteDownloadBridge] '{name}' 缺少域名，跳过")
                    continue

                # 注册索引器
                if isinstance(indexer_cfg, dict) and indexer_cfg.get("domain"):
                    try:
                        domain_key = indexer_cfg["domain"]
                        idx_json = {k: v for k, v in indexer_cfg.items() if k != "bridge"}
                        SitesHelper().add_indexer(domain_key, idx_json)
                        registered_count += 1
                        logger.info(f"[SiteDownloadBridge] ✅ 索引器已注册: {name} ({domain_key})")
                    except Exception as e:
                        logger.error(f"[SiteDownloadBridge] 注册索引器失败 {name}: {e}")

                # 桥接规则
                bridge_cfg = site.get("bridge")
                entry = self._parse_bridge_config(name, domains, bridge_cfg, site)
                if entry:
                    self._sites_config.append(entry)
                    bridge_count += 1
                    logger.info(f"[SiteDownloadBridge] 桥接规则已加载: {name} → {domains}")

            logger.info(f"[SiteDownloadBridge] 配置完成: {registered_count} 索引器, {bridge_count} 桥接")

        except Exception as e:
            logger.error(f"[SiteDownloadBridge] YAML 解析失败: {e}")

    def _parse_bridge_config(self, name: str, domains: list, bridge_cfg, site: dict) -> Optional[Dict]:
        """解析 bridge 配置（兼容新旧格式）"""
        entry = {
            "name": name, "domains": domains,
            "download_selector": "", "download_attr": "href",
            "need_cookie": False, "need_proxy": False,
            "encoding": "utf-8", "url_regex": "",
            "trigger": None,
        }
        if isinstance(bridge_cfg, dict):
            entry["need_cookie"] = bridge_cfg.get("need_cookie", False)
            entry["need_proxy"] = bridge_cfg.get("need_proxy", False)
            entry["download_selector"] = bridge_cfg.get("download_selector", "")
            entry["download_attr"] = bridge_cfg.get("download_attr", "href")
            entry["encoding"] = bridge_cfg.get("encoding", "utf-8")
            entry["url_regex"] = bridge_cfg.get("url_regex", "")
            # trigger 子配置
            trigger_cfg = bridge_cfg.get("trigger")
            if isinstance(trigger_cfg, dict) and trigger_cfg.get("selector"):
                entry["trigger"] = {
                    "selector": trigger_cfg["selector"],
                    "attribute": trigger_cfg.get("attribute", ""),
                    "mode": trigger_cfg.get("mode", "auto"),
                    "form_selector": trigger_cfg.get("form_selector", ""),
                    "script_url_regex": trigger_cfg.get("script_url_regex", ""),
                    "script_data_field": trigger_cfg.get("script_data_field", ""),
                    "ajax_method": trigger_cfg.get("ajax_method", ""),
                }
        elif isinstance(bridge_cfg, bool) and bridge_cfg:
            pass  # bare bridge: true
        elif site.get("need_cookie") is not None or site.get("need_proxy") is not None:
            # 兼容旧扁平格式
            entry["need_cookie"] = site.get("need_cookie", False)
            entry["need_proxy"] = site.get("need_proxy", False)
            entry["download_selector"] = site.get("download_selector", "")
        return entry

    # ---------- 站点匹配 ----------

    def _match_site(self, page_url: str) -> Optional[Dict]:
        if not page_url:
            return None
        try:
            hostname = (urlparse(page_url).hostname or "").lower()
        except Exception:
            return None
        for site in self._sites_config:
            for domain in site["domains"]:
                if hostname == domain or hostname.endswith("." + domain):
                    return site
        return None

    # ---------- 下载链接解析 ----------

    def _fetch_page(self, page_url: str, site_config: Dict, torrent_ctx) -> Optional[str]:
        """抓取页面 HTML"""
        cookie = getattr(torrent_ctx, "site_cookie", None) if torrent_ctx else None
        ua = getattr(torrent_ctx, "site_ua", None) if torrent_ctx else None
        use_proxy = site_config.get("need_proxy", False)

        req = RequestUtils(
            ua=ua, cookies=cookie,
            referer=page_url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            proxies=settings.PROXY if use_proxy else None,
        ).get_res(url=page_url, timeout=self._fetch_timeout)

        if req is None or req.status_code != 200:
            logger.warn(f"[SiteDownloadBridge] 获取页面失败: {page_url} status={req.status_code if req else 'None'}")
            return None

        logger.debug(f"[SiteDownloadBridge] 页面获取成功: len={len(req.text)}, preview={req.text[:150].strip()}")
        encoding = site_config.get("encoding", "utf-8")
        try:
            req.encoding = encoding
        except Exception:
            if req.apparent_encoding:
                req.encoding = req.apparent_encoding
        return req.text

    def _extract_url_from_element(self, el, attr: str, page_url: str, candidates: list):
        """从元素属性提取 URL"""
        url = (el.get(attr) or "").strip()
        if url:
            if not url.startswith("http") and not url.startswith("magnet:"):
                url = urljoin(page_url, url)
            if url not in candidates:
                candidates.append(url)

    def _extract_url_from_onclick(self, onclick: str, page_url: str, candidates: list):
        """从 onclick 属性中提取 URL"""
        patterns = [
            r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
            r"window\.open\s*\(\s*['\"]([^'\"]+)['\"]",
            r"['\"](https?://[^'\"]+)['\"]",
            r"['\"](/[^'\"]+)['\"]",
        ]
        for pat in patterns:
            for m in re.finditer(pat, onclick):
                url = m.group(1).strip()
                if url and not url.startswith("http") and not url.startswith("magnet:"):
                    url = urljoin(page_url, url)
                if url and url not in candidates:
                    candidates.append(url)

    def _extract_from_scripts(self, html: str, page_url: str, site_config: Dict, candidates: list):
        """
        从页面 <script> 标签中提取 AJAX 下载信息并重放请求。
        支持 $.post, $.get, $.ajax, fetch, XMLHttpRequest 等模式。
        """
        soup = BeautifulSoup(html, "html.parser")
        trigger_cfg = site_config.get("trigger") or {}
        script_url_regex = trigger_cfg.get("script_url_regex", "")
        ajax_method = trigger_cfg.get("ajax_method", "")

        # 合并页面所有内联脚本
        scripts_text = " ".join(
            s.text for s in soup.find_all("script")
            if s.text and ("$.post" in s.text or "$.get" in s.text or "$.ajax" in s.text or
                           "fetch(" in s.text or "link/index" in s.text or "download" in s.text.lower())
        )
        if not scripts_text:
            return

        logger.debug(f"[SiteDownloadBridge] 开始解析页面脚本 ({len(scripts_text)} 字符)")

        # --- 模式 A: $.post / $.get / $.ajax ---
        ajax_patterns = [
            # $.post('url', {data}, callback)
            r'\$\.(post|get)\s*\(\s*[\'\"]([^\'\"]+)[\'\"],\s*(\{[^}]+\})',
            # $.ajax({url:'url', data:{...}, type:'POST'})
            r'\$\.ajax\s*\(\s*(\{[^}]+\})\)',
        ]
        found_ajax_url = None
        found_ajax_data = None
        found_ajax_type = "POST"

        for pat in ajax_patterns:
            for m in re.finditer(pat, scripts_text, re.DOTALL):
                if m.group(1) in ("post", "get"):
                    found_ajax_url = m.group(2)
                    found_ajax_data = m.group(3)
                    found_ajax_type = m.group(1).upper()
                else:
                    # $.ajax({...}) — parse JSON-like object
                    ajax_obj = m.group(1)
                    url_m = re.search(r"url\s*:\s*['\"]([^'\"]+)['\"]", ajax_obj)
                    type_m = re.search(r"type\s*:\s*['\"]([^'\"]+)['\"]", ajax_obj)
                    data_m = re.search(r"data\s*:\s*(\{[^}]+\})", ajax_obj)
                    if url_m:
                        found_ajax_url = url_m.group(1)
                        found_ajax_data = data_m.group(1) if data_m else None
                        found_ajax_type = type_m.group(1).upper() if type_m else "POST"
                if found_ajax_url:
                    break
            if found_ajax_url:
                break

        # --- 模式 B: fetch() ---
        if not found_ajax_url:
            fm = re.search(r"fetch\s*\(\s*['\"]([^'\"]+)['\"]", scripts_text)
            if fm:
                found_ajax_url = fm.group(1)
                found_ajax_type = "GET"

        if not found_ajax_url:
            return

        # 补全相对 URL
        if not found_ajax_url.startswith("http"):
            found_ajax_url = urljoin(page_url, found_ajax_url)

        logger.info(f"[SiteDownloadBridge] 发现 AJAX 请求: {found_ajax_type} {found_ajax_url}")

        # --- 解析 POST data ---
        post_data = None
        if found_ajax_data and found_ajax_type == "POST":
            # 尝试从页面源码中提取 data 字段的实际值（可能是变量引用）
            data_field = trigger_cfg.get("script_data_field", "data")
            # 先尝试从脚本中直接提取 data 字段的值
            data_val_m = re.search(
                r"data\s*:\s*['\"]([^'\"]+)['\"]",
                scripts_text
            )
            if data_val_m:
                post_data = {data_field: data_val_m.group(1), "source": "1"}
            else:
                # 作为 JSON 解析
                try:
                    post_data = json.loads(found_ajax_data)
                except json.JSONDecodeError:
                    # 尝试用单引号替换双引号
                    try:
                        post_data = json.loads(found_ajax_data.replace("'", '"'))
                    except json.JSONDecodeError:
                        post_data = None

        # --- 自定义正则提取 ---
        if script_url_regex and not found_ajax_url:
            srm = re.search(script_url_regex, scripts_text)
            if srm:
                found_ajax_url = srm.group(1) if srm.lastindex else srm.group(0)
                if not found_ajax_url.startswith("http"):
                    found_ajax_url = urljoin(page_url, found_ajax_url)

        # --- 重放 AJAX 请求 ---
        try:
            # AJAX 请求不经过代理、不携带 cookie（已验证直接 POST 即可成功）

            # 构建 AJAX 请求所需的 headers（模拟浏览器）
            ajax_headers = {
                "X-Requested-With": "XMLHttpRequest",
            }

            if found_ajax_type == "POST":
                req = RequestUtils(
                    referer=page_url,
                    headers=ajax_headers,
                ).post_res(url=found_ajax_url, data=post_data, timeout=self._fetch_timeout,
                           allow_redirects=True)
            else:
                req = RequestUtils(
                    referer=page_url,
                    headers=ajax_headers,
                ).get_res(url=found_ajax_url, timeout=self._fetch_timeout,
                          allow_redirects=True)

            if req is not None:
                resp_text = (req.text or "").strip()
                logger.debug(f"[SiteDownloadBridge] AJAX 响应: status={req.status_code}, len={len(resp_text)}, text={resp_text[:200]}")

                if req.status_code in (200, 302, 301):
                    # 检查是否为下载链接
                    if resp_text.startswith("magnet:") or "magnet:" in resp_text:
                        magnet_m = re.search(r'(magnet:\?[^\s\"\'<>]+)', resp_text)
                        url = magnet_m.group(1) if magnet_m else resp_text
                        candidates.append(url)
                        logger.info(f"[SiteDownloadBridge] AJAX 响应包含磁力链接")
                    elif resp_text.startswith("http") and ".torrent" in resp_text.lower():
                        candidates.append(resp_text)
                        logger.info(f"[SiteDownloadBridge] AJAX 响应包含种子链接")
                    elif resp_text:
                        # 尝试 JSON
                        try:
                            j = json.loads(resp_text)
                            for key in ("url", "download", "link", "magnet", "torrent"):
                                if key in j and isinstance(j[key], str) and j[key].strip():
                                    candidates.append(j[key])
                                    logger.info(f"[SiteDownloadBridge] AJAX JSON 提取: {key}={j[key][:80]}")
                                    break
                        except json.JSONDecodeError:
                            # 可能响应就是纯文本 URL
                            if resp_text.startswith("http") or resp_text.startswith("magnet:"):
                                candidates.append(resp_text)
                                logger.info(f"[SiteDownloadBridge] AJAX 纯文本响应: {resp_text[:80]}")
                else:
                    logger.warn(f"[SiteDownloadBridge] AJAX 请求返回异常状态码: {req.status_code}")
            else:
                logger.warn(f"[SiteDownloadBridge] AJAX 请求失败：无响应")
        except Exception as e:
            logger.warn(f"[SiteDownloadBridge] AJAX 请求异常: {e}")

    def _resolve_download_url(self, page_url: str, site_config: Dict, torrent_ctx) -> Optional[str]:
        """
        多策略解析下载链接：
          1. 自动扫描 <a> 标签（magnet: / .torrent）
          2. CSS 选择器精确提取
          3. 按钮触发：属性提取 → 表单提交 → 脚本 AJAX 重放
        """
        html = self._fetch_page(page_url, site_config, torrent_ctx)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        trigger_cfg = site_config.get("trigger") or {}
        url_regex_str = site_config.get("url_regex", "")

        # ====== 策略 1 + 2: 扫描 <a> 标签 + CSS 选择器 ======
        selector = site_config.get("download_selector", "")
        attr = site_config.get("download_attr", "href")

        if selector:
            for el in soup.select(selector):
                self._extract_url_from_element(el, attr, page_url, candidates)

        # 自动扫描所有 <a> 标签
        if not candidates:
            for a_tag in soup.find_all("a", href=True):
                url = (a_tag.get("href") or "").strip()
                if url:
                    if not url.startswith("http") and not url.startswith("magnet:"):
                        url = urljoin(page_url, url)
                    url_lower = url.lower()
                    if url.startswith("magnet:") or ".torrent" in url_lower or \
                            any(kw in url_lower for kw in ["download", "torrent", "getfile"]):
                        if url not in candidates:
                            candidates.append(url)

        # ====== 策略 3: 按钮触发 ======
        if not candidates and trigger_cfg:
            trigger_sel = trigger_cfg.get("selector", "")
            if trigger_sel:
                logger.info(f"[SiteDownloadBridge] 尝试按钮触发: {trigger_sel}")
                btn = soup.select_one(trigger_sel)
                if btn:
                    # 3a. 检查按钮属性
                    for attr_name in ("data-url", "data-href", "data-download", "data-link",
                                      "href", "data-torrent", "data-magnet"):
                        self._extract_url_from_element(btn, attr_name, page_url, candidates)

                    # 3b. onclick 解析
                    onclick = btn.get("onclick") or ""
                    if onclick and not candidates:
                        self._extract_url_from_onclick(onclick, page_url, candidates)

                    # 3c. 表单提交
                    if not candidates:
                        form = btn.find_parent("form") if hasattr(btn, "find_parent") else None
                        if not form and trigger_cfg.get("form_selector"):
                            form = soup.select_one(trigger_cfg["form_selector"])
                        if form:
                            action = form.get("action") or ""
                            if action:
                                form_url = urljoin(page_url, action)
                                method = (form.get("method") or "GET").upper()
                                inputs = {}
                                for inp in form.find_all("input"):
                                    name = inp.get("name")
                                    value = inp.get("value") or ""
                                    if name:
                                        inputs[name] = value
                                # 重放表单请求
                                try:
                                    use_proxy = site_config.get("need_proxy", False)
                                    if method == "POST":
                                        req = RequestUtils(
                                            proxies=settings.PROXY if use_proxy else None,
                                        ).post_res(url=form_url, data=inputs, timeout=self._fetch_timeout,
                                                   allow_redirects=True)
                                    else:
                                        req = RequestUtils(
                                            proxies=settings.PROXY if use_proxy else None,
                                        ).get_res(url=form_url, params=inputs, timeout=self._fetch_timeout,
                                                  allow_redirects=True)
                                    if req and req.status_code == 200:
                                        resp_text = req.text
                                        magnet_m = re.search(r'(magnet:\?[^\s\"\'<>]+)', resp_text)
                                        if magnet_m:
                                            candidates.append(magnet_m.group(1))
                                        elif ".torrent" in resp_text.lower():
                                            candidates.append(resp_text.strip()[:500])
                                except Exception as e:
                                    logger.warn(f"[SiteDownloadBridge] 表单提交失败: {e}")

                    # 3d. 脚本 AJAX 解析
                    if not candidates:
                        old_trigger = site_config.get("trigger")
                        # 临时注入 cookie 以便 _extract_from_scripts 使用
                        if torrent_ctx:
                            site_config["_cookie"] = getattr(torrent_ctx, "site_cookie", None)
                        self._extract_from_scripts(html, page_url, site_config, candidates)

                else:
                    logger.warn(f"[SiteDownloadBridge] 未找到触发按钮: {trigger_sel}")

        # ====== 过滤 & 选择最佳候选 ======
        url_regex = re.compile(url_regex_str) if url_regex_str else None

        # 优先：磁力链接
        for url in candidates:
            if url_regex and not url_regex.search(url):
                continue
            if url.startswith("magnet:"):
                logger.info(f"[SiteDownloadBridge] ✅ {site_config['name']}: 磁力链接")
                return url
        # 其次：.torrent
        for url in candidates:
            if url_regex and not url_regex.search(url):
                continue
            if ".torrent" in url.lower():
                logger.info(f"[SiteDownloadBridge] ✅ {site_config['name']}: 种子文件 {url[:80]}")
                return url
        # 兜底
        for url in candidates:
            if not url_regex or url_regex.search(url):
                logger.info(f"[SiteDownloadBridge] ⚠ {site_config['name']}: 候选 {url[:80]}")
                return url

        return None

    # ---------- ResourceSelection 拦截 ----------

    @eventmanager.register(ChainEventType.ResourceSelection)
    def _on_resource_selection(self, event: Event):
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

        logger.info(f"[SiteDownloadBridge] 发现 {len(bridge_tasks)} 个需桥接资源")
        resolved = 0
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {}
            for idx, ctx, sc in bridge_tasks:
                torrent = getattr(ctx, "torrent_info", None)
                pu = getattr(torrent, "page_url", None) or getattr(torrent, "enclosure", None)
                futures[executor.submit(self._resolve_download_url, pu, sc, torrent)] = (idx, ctx, sc, pu)
            for future in as_completed(futures):
                idx, ctx, sc, pu = futures[future]
                try:
                    real_url = future.result(timeout=self._fetch_timeout + 10)
                    if real_url:
                        torrent = getattr(ctx, "torrent_info", None)
                        if torrent:
                            torrent.enclosure = real_url
                            resolved += 1
                            logger.info(f"[SiteDownloadBridge] ✅ {sc['name']}: {pu[:60]}... → {real_url[:80]}")
                except Exception as e:
                    logger.warn(f"[SiteDownloadBridge] 失败 {sc['name']}: {e}")

        if resolved > 0:
            event_data.updated = True
            event_data.updated_contexts = contexts
            event_data.source = "SiteDownloadBridge"
            logger.info(f"[SiteDownloadBridge] 完成: {resolved}/{len(bridge_tasks)}")

    @eventmanager.register(ChainEventType.ResourceDownload)
    def _on_resource_download(self, event: Event):
        """
        拦截单次下载（download_single 路径），在下载前解析二次跳转 URL。
        ResourceSelection 只覆盖批量下载，单个下载走 ResourceDownload。
        """
        if not self._enabled or not self._sites_config:
            return

        event_data = getattr(event, "event_data", None)
        if not event_data:
            return

        ctx = getattr(event_data, "context", None)
        if not ctx:
            return

        torrent = getattr(ctx, "torrent_info", None)
        if not torrent:
            return

        page_url = getattr(torrent, "page_url", None) or getattr(torrent, "enclosure", None)
        if not page_url:
            return

        if str(page_url).startswith("magnet:") or str(page_url).endswith(".torrent"):
            return

        site_config = self._match_site(str(page_url))
        if not site_config:
            return

        logger.info(f"[SiteDownloadBridge] 单次下载拦截: {site_config['name']} → {page_url[:80]}...")

        try:
            real_url = self._resolve_download_url(str(page_url), site_config, torrent)
            if real_url:
                torrent.enclosure = real_url
                # 同时更新 event_data 中的 context（确保下载链使用更新后的值）
                if hasattr(event_data, 'context'):
                    ctx2 = event_data.context
                    if ctx2:
                        t2 = getattr(ctx2, "torrent_info", None)
                        if t2:
                            t2.enclosure = real_url
                logger.info(f"[SiteDownloadBridge] ✅ 单次下载: {site_config['name']}: {page_url[:60]}... → {real_url[:80]}")
            else:
                logger.warn(f"[SiteDownloadBridge] 单次下载解析失败: {site_config['name']} → {page_url[:80]}")
        except Exception as e:
            logger.error(f"[SiteDownloadBridge] 单次下载异常: {e}", exc_info=True)

    # ---------- UI ----------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        default_yaml = """# ====== 站点索引 + 下载桥接 + 按钮触发 ======
# 详细说明见同目录 YAML_GUIDE.md
#
sites:
  # --- 仅桥接（站点已在索引器中）---
  - name: "FileMood"
    domains: ["filemood.com"]
    bridge:
      need_cookie: true
      need_proxy: true
      trigger:
        selector: "#download-btn"
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
                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "fetch_timeout", "label": "抓取超时(秒)", "placeholder": "10", "type": "number"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VTextField", "props": {"model": "max_workers", "label": "并行线程数", "placeholder": "3", "type": "number"}}]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{"component": "VTextarea", "props": {"model": "config_yaml", "label": "站点配置 (YAML)", "rows": 20, "placeholder": "粘贴 YAML ..."}}]
                        }]
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "三大功能：indexer(索引器) / bridge(下载桥接) / trigger(按钮触发AJAX)。详细格式见 YAML_GUIDE.md。依赖: beautifulsoup4, lxml, pyyaml"}}]
                        }]
                    }
                ]
            }
        ], {"enabled": False, "config_yaml": default_yaml, "fetch_timeout": 10, "max_workers": 3}

    def get_page(self) -> List[dict]:
        pass
