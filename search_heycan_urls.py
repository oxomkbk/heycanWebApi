#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
heycan.com 表情包搜索核心模块

原理:
    heycan.com 的 searchMaterial 接口有动态签名 (X-Bogus / _signature / msToken)，
    无法用纯 requests 伪造。本模块用真实浏览器 (系统 Chrome/Chromium + selenium)
    打开搜索页，让站点自己的 JS 生成签名并发起请求，再拦截 searchMaterial 响应。

性能/稳定性优化:
    - UA 随机轮换: 每次建浏览器随机选一个 UA，降低被风控概率
    - BrowserPool: 浏览器实例池复用，避免反复启停 Chrome（省 2~4 秒/次）
    - 并发下载: 图片用线程池并行下载

命令行用法:
    python search_heycan_urls.py 猫
    python search_heycan_urls.py 猫 狗狗 kfc --count 6 --visible --timeout 30

参数:
    --visible   显示浏览器窗口（默认无头模式）
    --timeout N 单个关键词抓取结果池的超时秒数，默认 25
    --type T    素材类型: 0=全部(默认) 1=花字 2=贴纸/表情 3=视频/音效 4=其他
    --count N   每个关键词随机保存多少张，默认 10
    --save-dir S 图片保存目录，默认 downloads
"""

import argparse
import collections
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# 注入到每个页面的脚本：拦截带 searchMaterial 的 XHR/fetch 响应
# 注意: 浏览器池会复用 driver，每次导航都会重新执行本脚本，
# 所以每次先把 __cap 清空，避免读到上一次搜索的旧数据
CAPTURE_JS = """
(function(){
  window.__cap = [];
  if (window.__capInstalled) return;
  window.__capInstalled = true;
  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url){
    this.__url = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(){
    var self = this;
    this.addEventListener('load', function(){
      if (String(self.__url||'').indexOf('searchMaterial') !== -1) {
        try { window.__cap.push({url:self.__url, body:self.responseText}); } catch(e){}
      }
    });
    return origSend.apply(this, arguments);
  };
  var origFetch = window.fetch;
  window.fetch = function(){
    var args = arguments;
    return origFetch.apply(this, arguments).then(function(resp){
      var url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
      if (url.indexOf('searchMaterial') !== -1) {
        resp.clone().text().then(function(t){ window.__cap.push({url:url, body:t}); });
      }
      return resp;
    });
  };
})();
"""

TYPE_NAMES = {0: '全部', 1: '花字', 2: '贴纸/表情', 3: '视频/音效', 4: '其他'}

# 搜索页分类锚点：#paster 只请求贴纸，#all 请求全部类型
# （页面只会发所选分类对应的 searchMaterial 请求）
TAB_MAP = {0: 'all', 2: 'paster'}

# 随机 UA 池：不同系统 × 不同 Chrome 版本，每次建浏览器随机选一个
UA_POOL = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
]


def random_ua():
    """随机返回一个 User-Agent。"""
    return random.choice(UA_POOL)


def find_chrome():
    """定位系统 Chrome/Chromium（跨平台），找不到返回 None（交给 selenium 自动查找）。

    可用环境变量 HEYCAN_CHROME 指定浏览器路径（Linux 服务器常用）。
    """
    env = os.environ.get('HEYCAN_CHROME')
    if env and os.path.exists(env):
        return env
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/headless_shell",
        "/snap/bin/chromium",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def build_driver(visible=False, ua=None):
    opts = Options()
    chrome_bin = find_chrome()
    if chrome_bin:
        opts.binary_location = chrome_bin
    if not visible:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")  # Linux 服务器常以 root 运行，必须禁用沙箱
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=zh-CN")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"user-agent={ua or random_ua()}")
    driver = webdriver.Chrome(options=opts)
    # 页面加载超时：被风控挑战时页面可能一直转圈，避免 get() 无限卡死
    driver.set_page_load_timeout(25)
    # 注册一次，之后所有页面导航都会自动注入拦截脚本
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': CAPTURE_JS})
    return driver


class BrowserPool:
    """浏览器实例池：复用 Chrome 避免反复启停，支持并发（池大小即并发上限）。

    acquire() 拿空闲实例或新建（不超过上限）；池满时阻塞等待归还；
    release() 归还；实例崩溃时自动重建。
    """

    def __init__(self, size=2, visible=False):
        self.size = max(1, size)
        self.visible = visible
        self._idle = collections.deque()
        self._lock = threading.Lock()
        self._total = 0

    def acquire(self, timeout=60):
        deadline = time.time() + timeout
        while True:
            with self._lock:
                if self._idle:
                    d = self._idle.popleft()
                    if self._is_alive(d):
                        return d
                    # 死实例：丢弃并让位新建
                    self._total -= 1
                    try:
                        d.quit()
                    except Exception:
                        pass
                    if self._total < self.size:
                        self._total += 1
                        break
                elif self._total < self.size:
                    self._total += 1
                    break
            if time.time() > deadline:
                raise TimeoutError('浏览器池忙，请稍后重试')
            time.sleep(0.1)
        # 池外创建（不占锁，避免阻塞其他归还）
        try:
            return build_driver(visible=self.visible)
        except Exception:
            with self._lock:
                self._total -= 1
            raise

    @staticmethod
    def _is_alive(driver):
        """健康检查：空闲的 Chrome 可能悄悄退出，用一次轻量调用探测。"""
        try:
            driver.current_url
            return True
        except Exception:
            return False

    def release(self, driver):
        try:
            driver.quit()
        except Exception:
            pass
        with self._lock:
            self._idle.append(driver)

    def discard(self, driver):
        """实例异常时丢弃并重建。"""
        try:
            driver.quit()
        except Exception:
            pass
        with self._lock:
            self._total -= 1

    def close_all(self):
        with self._lock:
            while self._idle:
                try:
                    self._idle.popleft().quit()
                except Exception:
                    pass
            self._total = 0


def parse_captured(captured):
    """把拦截到的 searchMaterial 响应解析成结果条目列表（不去重）。"""
    results = []
    for c in captured:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(c['url']).query)
        if 'item_type' not in q:
            continue
        try:
            item_type = int(q['item_type'][0])
            data = json.loads(c['body'])
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        for item in data.get('item_list', []) or []:
            cd = item.get('common_data') or {}
            icon = cd.get('icon') or {}
            url = icon.get('image_url') or icon.get('static_image_url') or ''
            if not url:
                continue
            results.append({
                'keyword': None,  # 稍后填充
                'item_type': item_type,
                'item_id': item.get('item_id'),
                'title': cd.get('title', ''),
                'url': url,
                'favorite_num': (item.get('statistics') or {}).get('favorite_num', ''),
            })
    return results


def search_keyword(driver, keyword, want_type, timeout, min_pool=40, max_scrolls=10):
    """用给定 driver 搜索关键词，累计去重后的结果池（driver 由调用方提供，可复用）。

    want_type=2（贴纸）时用 #paster 锚点，页面只请求贴纸数据，更快更稳；
    want_type=0（全部）时用 #all。
    """
    tab = TAB_MAP.get(want_type, 'all')
    url = (f'https://www.heycan.com/material?from=input&query='
           f'{urllib.parse.quote(keyword)}#{tab}')
    driver.get(url)

    pool, seen = [], set()
    deadline = time.time() + timeout
    scrolls = 0
    while time.time() < deadline:
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(1.0)
        captured = driver.execute_script('return window.__cap || []')
        for r in parse_captured(captured):
            if want_type != 0 and r['item_type'] != want_type:
                continue
            key = (r['item_type'], r['item_id'])
            if key in seen:
                continue
            seen.add(key)
            r['keyword'] = keyword
            pool.append(r)
        scrolls += 1
        if len(pool) >= min_pool or scrolls >= max_scrolls:
            break
    return pool


def pick_random(pool, count):
    """随机抽取 count 个，保证不重复；池子不够就全取。"""
    pool = list(pool)
    random.shuffle(pool)
    return pool[:count]


def safe_name(text, max_len=20):
    """去掉文件名里的非法字符。"""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', text or '')
    return text[:max_len] or 'untitled'


def guess_ext(url, content_type=''):
    """从 URL 或 Content-Type 推断图片扩展名。"""
    m = re.search(r'resize:\d+:\d+\.(\w+)', url)
    if m:
        return m.group(1).lower()
    if 'gif' in content_type:
        return 'gif'
    if 'png' in content_type:
        return 'png'
    if 'webp' in content_type:
        return 'webp'
    return 'jpg'


def download_one(item, dest_dir):
    """下载单张图片，返回 (item, 保存路径或 None)。"""
    headers = {
        'User-Agent': random_ua(),
        'Referer': 'https://www.heycan.com/',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
    }
    try:
        r = requests.get(item['url'], headers=headers, timeout=15)
        r.raise_for_status()
        ext = guess_ext(item['url'], r.headers.get('content-type', ''))
        name = f"{uuid.uuid4().hex[:6]}_{safe_name(item['title'])}.{ext}"
        path = os.path.join(dest_dir, name)
        with open(path, 'wb') as f:
            f.write(r.content)
        return item, path
    except Exception:
        return item, None


def download_many(items, dest_dir, workers=4):
    """并发下载多张图片，返回 [(item, 保存路径或 None)]。"""
    os.makedirs(dest_dir, exist_ok=True)
    if workers <= 1:
        return [download_one(it, dest_dir) for it in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda it: download_one(it, dest_dir), items))


def search_and_download(pool, count, dest_dir):
    """从结果池随机抽 count 张并发下载，返回 [(item, path 或 None)]。"""
    picked = pick_random(pool, count)
    return download_many(picked, dest_dir)


def main():
    parser = argparse.ArgumentParser(description='heycan.com 表情包搜索：随机挑选并下载保存')
    parser.add_argument('keywords', nargs='+', help='搜索关键词，可多个')
    parser.add_argument('--visible', action='store_true', help='显示浏览器窗口（默认无头模式）')
    parser.add_argument('--timeout', type=int, default=25, help='每个关键词抓取超时秒数')
    parser.add_argument('--type', type=int, default=2, choices=[0, 1, 2, 3, 4],
                        help='素材类型: 2=贴纸/表情(默认) 0=全部 1=花字 3=视频/音效 4=其他')
    parser.add_argument('--count', type=int, default=10, help='每个关键词随机保存多少张')
    parser.add_argument('--save-dir', default='downloads', help='图片保存目录')
    args = parser.parse_args()

    pool = BrowserPool(size=1, visible=args.visible)
    print('=' * 70)
    print(f'heycan.com 表情包搜索（随机下载版，类型: {TYPE_NAMES[args.type]}）')
    print('=' * 70)

    all_picked = []
    try:
        for i, kw in enumerate(args.keywords, 1):
            print(f'\n[{i}/{len(args.keywords)}] 正在搜索: {kw}')
            driver = pool.acquire()
            try:
                result_pool = search_keyword(driver, kw, args.type, args.timeout)
            except Exception:
                pool.discard(driver)
                raise
            else:
                pool.release(driver)

            if not result_pool:
                print('  !! 未获取到结果。若反复失败，可加 --visible 用有头模式重试（可能需过验证码）')
                continue

            dest_dir = os.path.join(args.save_dir, safe_name(kw))
            results = search_and_download(result_pool, args.count, dest_dir)
            saved = sum(1 for _, p in results if p)
            print(f'  结果池 {len(result_pool)} 张，本次随机抽出 {len(results)} 张，成功 {saved}:')
            for j, (item, path) in enumerate(results, 1):
                if path:
                    print(f'    [{j}] {item["title"][:20]:<20} -> {path}')
            print(f'  ✅ 已保存 {saved}/{len(results)} 张到: {dest_dir}')
            all_picked.extend(item for item, p in results if p)
    finally:
        pool.close_all()

    if not all_picked:
        print('\n没有获取到任何结果。')
        sys.exit(1)

    with open('search_heycan_urls.json', 'w', encoding='utf-8') as f:
        json.dump(all_picked, f, ensure_ascii=False, indent=2)
    with open('urls.txt', 'w', encoding='utf-8') as f:
        for r in all_picked:
            f.write(f'{r["url"]}\n')

    print('\n' + '=' * 70)
    print(f'完成! 本次共挑选保存 {len(all_picked)} 张')
    print(f'  图片保存在 downloads/<关键词>/ 目录')
    print(f'  结构化结果: search_heycan_urls.json')
    print(f'  纯 URL 列表: urls.txt')
    print('=' * 70)


if __name__ == '__main__':
    main()
