# -*- coding: utf-8 -*-
"""
heycan.com 表情包接口 JS 逆向分析报告
分析 httpdata.json 抓包数据，找出 sign 参数生成机制
"""

import re
import json

def analyze_heycan_requests(file_path='httpdata.json'):
    """分析 heycan.com 的 HTTP 请求数据"""
    
    print("="*70)
    print("heycan.com 表情包接口签名分析")
    print("="*70)
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # 1. 提取所有 searchMaterial 相关 URL
        print("\n[1] 搜索接口 URL 模式:")
        urls = re.findall(r'"url"\s*:\s*"([^"]*searchMaterial[^"]*)"', content, re.IGNORECASE)
        
        unique_urls = set()
        for url in urls:
            unique_urls.add(url[:200])  # 取前 200 字符
        
        for i, url in enumerate(list(unique_urls)[:5], 1):
            print(f"\n   {i}. {url}")
            
    except Exception as e:
        print(f"读取文件失败：{e}")
        return
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # 2. 查找参数特征
        print("\n\n[2] 关键参数检测:")
        
        params_found = {}
        
        # msToken
        ms_token_matches = re.findall(r'"msToken"\s*:\s*"([^"]+)"', content)
        if ms_token_matches:
            params_found['msToken'] = len(ms_token_matches)
            print(f"   OK: msToken - 发现 {len(set(ms_token_matches))} 个不同的 Token")
            print(f"     示例：{ms_token_matches[0][:60]}...")
        
        # X-Bogus
        bogus_matches = re.findall(r'"X-Bogus"\s*:\s*"([^"]+)"', content)
        if bogus_matches:
            params_found['X-Bogus'] = len(bogus_matches)
            print(f"   OK: X-Bogus - 发现 {len(set(bogus_matches))} 个不同的签名")
            print(f"     示例：{bogus_matches[0][:60]}...")
        
        # _signature
        sig_matches = re.findall(r'"_signature"\s*:\s*"([^"]+)"', content)
        if sig_matches:
            params_found['_signature'] = len(sig_matches)
            print(f"   OK: _signature - 发现 {len(set(sig_matches))} 个不同的签名")
        
        # query/search 关键词
        query_matches = re.findall(r'query=([^&"\']+)', content)
        if query_matches:
            keywords = set(query_matches)
            print(f"   OK: query 参数 - 发现关键词 {len(keywords)} 个")
            print(f"     示例：{list(keywords)[:10]}")
        
        # item_type
        item_type_matches = re.findall(r'item_type=(\d+)', content)
        if item_type_matches:
            item_types = set(item_type_matches)
            print(f"   OK: item_type - {item_types} (可能的类型)")
        
        print("\n" + "="*70)
        print("参数统计汇总:")
        for param, count in params_found.items():
            print(f"   {param}: {count} 次出现")
        print("="*70)
        
        # 3. 尝试理解签名格式
        print("\n\n[3] 签名算法初步分析:")
        
        if bogus_matches:
            sample_bogus = bogus_matches[0]
            print(f"\n   X-Bogus 签名结构分析:")
            print(f"     原始值：{sample_bogus}")
            print(f"     长度：{len(sample_bogus)}")
            
            # 检查是否有固定前缀/后缀
            if sample_bogus.startswith('DFSzsw'):
                print(f"     ✨ 检测到固定前缀：DFSzsw")
                print(f"       可能是：加密后的随机字符串或哈希值")
            
            # 分析结构特征
            parts = re.split(r'[= &]', sample_bogus)[:5]
            print(f"     分段观察：{len(parts)} 段")
        
        if sig_matches:
            sample_sig = sig_matches[0]
            print(f"\n   _signature 签名结构分析:")
            print(f"     原始值：{sample_sig[:100]}...")
            
            # 检查是否有特殊结构
            if '/_' in sample_sig or '_02B4Z6w' in sample_sig:
                print(f"     ✨ 包含特定前缀/_/组合，可能为指纹特征")
        
        # 4. 提取实际的成功案例
        print("\n\n[4] 成功案例提取:")
        
        # 查找完整请求体
        request_patterns = re.findall(
            r'"request"\s*:\s*\{[^}]*"content"\s*:\s*\[.*?"content"\s*:\s*"([^"]+)}"[^}]*\}',
            content, 
            re.DOTALL
        )
        
        if request_patterns:
            print(f"   找到 {len(request_patterns)} 个请求样本")
            
            # 显示第一个请求的部分内容
            first_req = request_patterns[0]
            print(f"\n   第一个请求示例（JSON 格式）:")
            try:
                # 尝试解析 JSON 字符串
                decoded_req = json.loads(first_req)
                print(f"   事件类型：{decoded_req.get('events', [{}])[0].get('event')}")
                print(f"   用户 ID: {decoded_req.get('user', {}).get('user_unique_id')}")
            except:
                print(f"   (内容过长或格式复杂，无法直接解析)")
                print(f"   前 500 字符：{first_req[:500]}")
                
        else:
            print("   未找到完整的请求体，可能需要进一步分析")
            
        # 5. 给出结论和建议
        print("\n" + "="*70)
        print("逆向分析与建议:")
        
        print("\n   核心发现:")
        print("   - heycan.com 使用多重签名机制保护接口")
        print("   - 主要签名参数：msToken + X-Bogus + _signature")
        print("   - 每个参数都随时间/会话变化，具有动态性")
        
        print("\n   可能方案:")
        print("   A. Hook 浏览器环境:")
        print("      - 使用 Camoufox MCP 或浏览器自动化工具")
        print("      - Hook document.cookie 或 window.webkitRequest")
        print("      - 追踪 X-Bogus 生成函数的调用链")
        
        print("   B. 反编译 JavaScript:")
        print("      - 定位 generateX-Bogus 或 createSignature 函数")
        print("      - 还原加密算法实现")
        print("      - 尝试在 Node.js 或 Python 中复现")
        
        print("   C. 行为模拟:")
        print("      - 使用真实浏览器发起请求")
        print("      - 维持 Cookie 状态")
        print("      - 定期刷新 msToken")
        
        print("\n   🟢 推荐步骤:")
        print("   1. ✅ 安装 Camoufox MCP 工具 (如果可用)")
        print("   2. ✅ 启动浏览器并访问 https://www.heycan.com/")
        print("   3. ✅ 在 DevTools 中使用 Debugger 设置断点")
        print("   4. ✅ 触发表情包搜索功能")
        print("   5. ✅ 查看哪个函数生成了 X-Bogus 签名")
        print("   6. ✅ 逐步追踪该函数的完整逻辑")
        print("   7. ✅ 提取关键的加密算法代码片段")
        print("   8. ✅ 编写对应的 Node.js 或 Python 实现")
        
        print("\n" + "="*70)
        
    except Exception as e:
        print(f"分析过程中出错：{e}")


if __name__ == '__main__':
    import sys
    file_path = sys.argv[1] if len(sys.argv) > 1 else 'httpdata.json'
    analyze_heycan_requests(file_path)
