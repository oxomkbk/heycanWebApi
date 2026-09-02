#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
heycan.com 表情包搜索 Web API

网页/接口传关键词，服务端用浏览器搜索并临时下载表情包到本地，
返回可直接访问的图片 URL（基于请求的 IP 或域名自动拼接）。
图片 24 小时定时清理一次，无需常驻监控。

性能/稳定性:
    - BrowserPool 复用浏览器实例，池大小即并发搜索上限
    - ThreadPoolExecutor 并发执行搜索任务（同步接口也支持并发）
    - 随机 User-Agent 轮换降低风控概率
    - 图片并发下载
    - 支持异步模式: /search?async=1 立即返回 task_id，/result/<id> 轮询

接口:
    GET /search?q=猫&count=10&type=2
        搜索关键词并返回随机挑选的图片 URL 列表（同步，等待约 5~8 秒）
        q      关键词（必填）
        count  每词返回张数，默认 10，最大 50
        type   素材类型: 2=贴纸/表情(默认) 0=全部 1=花字 3=视频/音效 4=其他
        async=1 时不等待，立即返回 {"task_id": "..."}，用 /result 轮询
    GET /result/<task_id>
        查询异步任务状态: {"status": "pending|running|done|error", "result": {...}}
    GET /downloads/<相对路径>    访问已下载的图片
    GET /health                 健康检查

Linux 部署:
    pip install -r requirements.txt
    apt install chromium chromium-driver        # 或安装 google-chrome
    python app.py --host 0.0.0.0 --port 8000    # root 用户已自动加 --no-sandbox
    生产建议: pip install waitress && waitress-serve --listen=0.0.0.0:8000 app:app
    环境变量: HEYCAN_POOL_SIZE（并发搜索数，默认 2）、HEYCAN_TEMP_DIR（图片目录）
"""

import argparse
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from search_heycan_urls import (
    TYPE_NAMES,
    BrowserPool,
    safe_name,
    search_and_download,
    search_keyword,
)

# 图片临时存放目录；可通过环境变量 HEYCAN_TEMP_DIR 覆盖
TEMP_DIR = os.environ.get('HEYCAN_TEMP_DIR', 'downloads')
# 图片保留时长 / 清理周期：24 小时
CLEANUP_INTERVAL = 24 * 3600
MAX_AGE = 24 * 3600
# 异步任务结果保留时长：1 小时
TASK_TTL = 3600

app = Flask(__name__)
# 支持 Nginx 反代：用 X-Forwarded-* 头还原真实域名/IP
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# 浏览器池 + 搜索线程池：并发度 = 池大小，控制风控风险
_pool_size = int(os.environ.get('HEYCAN_POOL_SIZE', '2'))
_pool = BrowserPool(size=_pool_size)
_executor = ThreadPoolExecutor(max_workers=_pool_size)

# 异步任务表 {task_id: {'status', 'result', 'created', 'error'}}
_tasks = {}
_tasks_lock = threading.Lock()


def cleanup_expired():
    """删除超过 24 小时的临时图片及空目录，返回删除数量。"""
    if not os.path.isdir(TEMP_DIR):
        return 0
    now = time.time()
    removed = 0
    for root, dirs, files in os.walk(TEMP_DIR, topdown=False):
        for f in files:
            p = os.path.join(root, f)
            try:
                if now - os.path.getmtime(p) > MAX_AGE:
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
        try:
            os.rmdir(root)  # 删除空目录
        except OSError:
            pass
    return removed


def cleanup_tasks():
    """清理过期的异步任务结果。"""
    now = time.time()
    with _tasks_lock:
        expired = [tid for tid, t in _tasks.items() if now - t['created'] > TASK_TTL]
        for tid in expired:
            _tasks.pop(tid, None)
    return len(expired)


def cleanup_worker():
    """后台守护线程：每 24 小时清理图片，顺带清理过期任务记录。"""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            n = cleanup_expired()
            m = cleanup_tasks()
            print(f'[cleanup] 清理图片 {n} 个、任务记录 {m} 条', flush=True)
        except Exception as e:
            print(f'[cleanup] 出错: {e}', flush=True)


def do_search(keyword, item_type, count):
    """执行一次完整搜索：取浏览器 -> 搜索 -> 随机挑 -> 并发下载 -> 返回结果。

    返回的 items 中 url 是相对路径（由请求方拼接域名），path 是本地文件路径。
    """
    driver = _pool.acquire()
    try:
        result_pool = search_keyword(driver, keyword, item_type, timeout=25)
    except Exception:
        _pool.discard(driver)
        raise
    else:
        _pool.release(driver)

    if not result_pool:
        return None

    dest_dir = os.path.join(TEMP_DIR, safe_name(keyword))
    results = search_and_download(result_pool, count, dest_dir)
    items = []
    for item, path in results:
        if not path:
            continue
        rel = os.path.relpath(path, start=TEMP_DIR).replace(os.sep, '/')
        items.append({
            'title': item['title'],
            'url': f'/downloads/{urllib.parse.quote(rel)}',
            'filename': os.path.basename(path),
            'size': os.path.getsize(path),
            'item_type': item['item_type'],
            'favorite_num': item['favorite_num'],
        })
    return items


def parse_args():
    """解析并校验 q/count/type 参数，出错抛 ValueError。"""
    q = (request.args.get('q') or '').strip()
    if not q:
        raise ValueError('缺少关键词参数 q')
    try:
        count = int(request.args.get('count', 10))
    except ValueError:
        count = 10
    count = max(1, min(count, 50))
    try:
        item_type = int(request.args.get('type', 2))
    except ValueError:
        item_type = 2
    if item_type not in TYPE_NAMES:
        item_type = 2
    return q, item_type, count


def build_response(keyword, items, base):
    """把相对 url 拼成完整 url。"""
    out = []
    for it in items:
        it = dict(it)
        it['url'] = base + it['url']
        out.append(it)
    return {'keyword': keyword, 'count': len(out), 'items': out}


@app.route('/health')
def health():
    return jsonify(ok=True, time=time.strftime('%Y-%m-%d %H:%M:%S'),
                   pool_size=_pool_size)


@app.route('/result/<task_id>')
def result(task_id):
    """查询异步任务状态。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return jsonify(error='任务不存在或已过期'), 404
    resp = {'status': task['status']}
    if task['status'] == 'done':
        resp['result'] = task['result']
    elif task['status'] == 'error':
        resp['error'] = task['error']
    return jsonify(resp)


@app.route('/search')
def search():
    try:
        q, item_type, count = parse_args()
    except ValueError as e:
        return jsonify(error=str(e)), 400

    base = request.host_url.rstrip('/')

    # 异步模式：立即返回 task_id，后台搜索，前端轮询 /result/<id>
    if request.args.get('async') == '1':
        task_id = uuid.uuid4().hex
        with _tasks_lock:
            _tasks[task_id] = {'status': 'pending', 'created': time.time()}
            if len(_tasks) > 200:  # 防内存膨胀
                cleanup_tasks()

        def run():
            with _tasks_lock:
                _tasks[task_id]['status'] = 'running'
            try:
                items = do_search(q, item_type, count)
                if items is None:
                    with _tasks_lock:
                        _tasks[task_id] = {'status': 'done', 'created': time.time(),
                                           'result': build_response(q, [], base)}
                else:
                    with _tasks_lock:
                        _tasks[task_id] = {'status': 'done', 'created': time.time(),
                                           'result': build_response(q, items, base)}
            except Exception as e:
                with _tasks_lock:
                    _tasks[task_id] = {'status': 'error', 'created': time.time(),
                                       'error': str(e)}

        _executor.submit(run)
        return jsonify(task_id=task_id, status='pending',
                       result_url=f'{base}/result/{task_id}')

    # 同步模式：等待结果（并发请求会并行执行，池满则排队）
    try:
        items = do_search(q, item_type, count)
    except TimeoutError as e:
        return jsonify(error=str(e)), 503
    except Exception as e:
        print(f'[search] 失败 q={q}: {type(e).__name__}: {e}', flush=True)
        # 浏览器启动失败等属于瞬时故障，返回 502 便于前端重试
        return jsonify(error=f'搜索失败: {e}'), 502

    if items is None:
        return jsonify(keyword=q, count=0, items=[],
                       message='未获取到结果（可能被风控，稍后重试）')
    return jsonify(build_response(q, items, base))


@app.route('/downloads/<path:filename>')
def serve_file(filename):
    """提供已下载图片的访问（返回的 url 指向这里）。"""
    if not os.path.isfile(os.path.join(TEMP_DIR, filename)):
        abort(404)
    return send_from_directory(TEMP_DIR, filename)


def main():
    parser = argparse.ArgumentParser(description='heycan.com 表情包搜索 Web API')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址，默认 0.0.0.0')
    parser.add_argument('--port', type=int, default=8000, help='监听端口，默认 8000')
    args = parser.parse_args()

    os.makedirs(TEMP_DIR, exist_ok=True)
    # 启动时清理一次历史残留
    n = cleanup_expired()
    if n:
        print(f'[startup] 已清理 {n} 个过期文件', flush=True)

    threading.Thread(target=cleanup_worker, daemon=True).start()

    print(f'heycan 表情包搜索 API 已启动: http://{args.host}:{args.port}')
    print(f'图片临时目录: {os.path.abspath(TEMP_DIR)}（24 小时自动清理）')
    print(f'并发搜索数: {_pool_size}（环境变量 HEYCAN_POOL_SIZE 可调）')
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
