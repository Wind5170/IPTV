#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络IP提取工具
功能：从网络URL或本地文件提取服务器IP地址
支持格式：M3U、TXT、汇总格式、纯IP列表
输出到 ip/ 目录，只保留 REGIONS+运营商 格式的城市
支持自动模式（用于CI/CD）
支持文件去重验证（基于URL+MD5）
"""

import os
import re
import socket
import urllib.request
import hashlib
import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import logging
import sys
import argparse

# ==================== 配置参数 ====================
CONFIG = {
    "url_config_file": "config/iptv_playlist_url.txt",
    "proxy_heads": [ "https://gh-proxy.org/", "https://ghfast.top/", "https://gh-proxy.com/", "https://github.moeyy.xyz/", ""],
    "network_timeout": 10,
    "ip_dir": "ip",
    "verbose": False,           # 默认关闭详细输出
    "debug": False,             # 调试模式（显示所有细节）
    "skip_strings": ["ipipip", "localhost", "127.0.0.1"],
    "global_deduplicate": True,
    "records_file": "config/processed_records.json",
}

# ==================== 省份排序顺序 ====================
REGIONS = [
    "安徽", "北京", "重庆", "福建", "甘肃", "广东", "广西", "贵州", "海南", "河北",
    "河南", "黑龙江", "湖北", "湖南", "吉林", "江苏", "江西", "辽宁", "内蒙古", "宁夏",
    "青海", "山东", "山西", "陕西", "上海", "四川", "天津", "西藏", "新疆", "云南",
    "浙江", "台湾", "香港", "澳门"
]

OPERATORS = ["电信", "联通", "移动"]

REGION_ALIAS = {
    "北京市": "北京", "上海市": "上海", "天津市": "天津", "重庆市": "重庆",
    "江苏省": "江苏", "浙江省": "浙江", "广东省": "广东", "福建省": "福建",
    "安徽省": "安徽", "江西省": "江西", "山东省": "山东", "河南省": "河南",
    "湖北省": "湖北", "湖南省": "湖南", "四川省": "四川", "贵州省": "贵州",
    "云南省": "云南", "陕西省": "陕西", "甘肃省": "甘肃", "青海省": "青海",
    "辽宁省": "辽宁", "吉林省": "吉林", "黑龙江省": "黑龙江", "海南省": "海南",
    "河北省": "河北", "山西省": "山西", "内蒙古": "内蒙古", "广西": "广西",
    "西藏": "西藏", "宁夏": "宁夏", "新疆": "新疆", "台湾": "台湾",
    "香港": "香港", "澳门": "澳门",
}

# ==================== 正则表达式模式 ====================
PATTERNS = {
    'city_operator': re.compile(r'([\u4e00-\u9fa5]+(?:电信|联通|移动))'),
    'city_operator_separator': re.compile(r'([\u4e00-\u9fa5]+)[-_](电信|联通|移动)'),
    'ipv4': re.compile(r'^\d+\.\d+\.\d+\.\d+$'),
    'domain': re.compile(r'^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'),
    'ip_with_port': re.compile(r'^(\d+\.\d+\.\d+\.\d+):(\d+)$'),
    'domain_with_port': re.compile(r'^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}:(\d+)$'),
    'url_with_server': re.compile(r'^https?://([^/]+)'),
    'ip_data': re.compile(r'^([\u4e00-\u9fa5]+(?:电信|联通|移动)),(?:https?://)?([\w.-]+:\d+)$'),
    'udpxy_http': re.compile(r'https?://([^/]+)/(?:rtp|udp)/(.+)$'),
    'udpxy_relative': re.compile(r'//([^/]+)/(?:rtp|udp)/(.+)$'),
}

# ==================== 日志设置 ====================
def setup_logging(verbose=False, debug=False):
    logger = logging.getLogger('network_ip_extractor')
    if verbose:
        logger.setLevel(logging.INFO)
    elif debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.WARNING)
    
    if not logger.handlers:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()


# ==================== 文件处理记录 ====================
def load_processed_records():
    records_file = Path(CONFIG["records_file"])
    if records_file.exists():
        try:
            with open(records_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_processed_records(records):
    records_file = Path(CONFIG["records_file"])
    os.makedirs(records_file.parent, exist_ok=True)
    with open(records_file, 'w', encoding='utf-8') as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def is_already_processed(url, content_md5):
    """检查文件是否已经处理过"""
    records = load_processed_records()
    
    # 1. 全局 MD5 去重（不同 URL 相同内容）
    for stored_url, info in records.items():
        if info.get("md5") == content_md5:
            if CONFIG['verbose']:
                logger.info(f"  内容与 {stored_url} 重复 (MD5: {content_md5[:8]}...)，跳过")
            return True
    
    # 2. 同一 URL 的 MD5 检查
    if url in records:
        if records[url].get("md5") == content_md5:
            if CONFIG['verbose']:
                logger.info(f"  URL 未变化，跳过")
            return True
    
    return False


def update_processed_record(url, content_md5, filename, added_servers=0, total_servers=0):
    records = load_processed_records()
    records[url] = {
        "md5": content_md5,
        "filename": filename,
        "last_processed": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "added_servers": added_servers,
        "total_servers": total_servers
    }
    save_processed_records(records)


# ==================== 工具函数 ====================
def get_content_md5(content):
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def is_valid_city(city_name):
    for region in REGIONS:
        for operator in OPERATORS:
            if city_name == f"{region}{operator}":
                return True
    return False


def normalize_city_name(city_name):
    match = PATTERNS['city_operator_separator'].search(city_name)
    if match:
        province = match.group(1)
        operator = match.group(2)
        if province in REGION_ALIAS:
            province = REGION_ALIAS[province]
        if province in REGIONS and operator in OPERATORS:
            return True, f"{province}{operator}"
    
    for op in OPERATORS:
        if city_name.endswith(op):
            province = city_name[:-len(op)]
            if province in REGION_ALIAS:
                province = REGION_ALIAS[province]
            province = province.rstrip('省市区县')
            if province in REGIONS:
                return True, f"{province}{op}"
            break
    
    if is_valid_city(city_name):
        return True, city_name
    
    return False, city_name


def extract_city_from_filename(file_path):
    filename = Path(file_path).stem
    suffixes = ['_ip', '_servers', '_list', '_iptv', '_server',
                '_source', '_checked', '_result', '_precise', '_history', '_quick', '_probe',
                '_config', '_good', '_slow']
    for suffix in suffixes:
        if filename.endswith(suffix):
            filename = filename[:-len(suffix)]
            break
    
    match = PATTERNS['city_operator_separator'].search(filename)
    if match:
        province = match.group(1)
        operator = match.group(2)
        if province in REGION_ALIAS:
            province = REGION_ALIAS[province]
        city_name = f"{province}{operator}"
    else:
        match = PATTERNS['city_operator'].search(filename)
        if match:
            city_name = match.group(1)
        else:
            city_name = filename
    
    is_valid, normalized = normalize_city_name(city_name)
    if is_valid:
        return normalized, True
    
    return filename, False


def resolve_host_to_ip(host_port, verbose=False):
    if ':' not in host_port:
        return host_port
    
    host, port = host_port.rsplit(':', 1)
    
    if PATTERNS['ipv4'].match(host):
        return host_port
    
    try:
        ip = socket.gethostbyname(host)
        result = f"{ip}:{port}"
        if verbose:
            logger.debug(f"域名解析: {host_port} -> {result}")
        return result
    except (socket.gaierror, ValueError):
        if verbose:
            logger.debug(f"域名解析失败: {host_port}")
        return host_port


def parse_url_to_server(url_str):
    """从URL中解析出服务器地址（IP:端口 或 域名:端口）"""
    if not url_str:
        return None
    
    match = PATTERNS['url_with_server'].search(url_str)
    if match:
        server = match.group(1)
        if ':' in server:
            return server
    return None


def fetch_url_with_proxy(url, proxy_heads, timeout):
    for proxy in proxy_heads:
        try:
            full_url = proxy + url if proxy else url
            req = urllib.request.Request(full_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8'), True
        except Exception:
            continue
    return None, False


def detect_file_type(content_preview):
    if not content_preview:
        return 'unknown'
    
    lines = content_preview.strip().split('\n')
    first_line = lines[0].strip() if lines else ""
    
    if first_line.startswith('#EXTM3U'):
        return 'm3u'
    
    if '#genre#' in content_preview:
        return 'summary'
    
    ip_list_count = 0
    non_comment_count = 0
    for line in lines[:20]:
        line = line.strip()
        if line and not line.startswith('#'):
            non_comment_count += 1
            if PATTERNS['ip_with_port'].match(line) or PATTERNS['domain_with_port'].match(line):
                ip_list_count += 1
            elif PATTERNS['ipv4'].match(line):
                ip_list_count += 1
    
    if non_comment_count >= 1 and ip_list_count / non_comment_count >= 0.5:
        return 'ip_list'
    
    ip_data_count = 0
    for line in lines[:10]:
        if PATTERNS['ip_data'].match(line.strip()):
            ip_data_count += 1
    if ip_data_count >= 3:
        return 'ip_data'
    
    return 'channel_list'


def extract_servers_from_content(content, file_type, file_path=None):
    """从内容中提取服务器地址，按分组归属"""
    city_servers = defaultdict(set)
    lines = content.split('\n')
    
    current_city = None
    
    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        
        if '#genre#' in line:
            parts = line.split(',')
            if parts:
                genre = parts[0].strip()
                match = re.match(r'^(.*?)(?:-组播\d+)?$', genre)
                if match:
                    group_name = match.group(1)
                    is_valid, normalized = normalize_city_name(group_name)
                    if is_valid:
                        current_city = normalized
                        if CONFIG['debug']:
                            logger.debug(f"  识别分组: {group_name} -> {current_city}")
            continue
        
        if line_num <= 2:
            continue
        
        if not current_city:
            continue
        
        if ',' in line:
            parts = line.split(',', 1)
            if len(parts) >= 2:
                url = parts[1].strip()
                
                if url.startswith(('http://', 'https://')):
                    server = parse_url_to_server(url)
                    if server and ':' in server:
                        ip_addr = resolve_host_to_ip(server, CONFIG['debug'])
                        if ip_addr and ':' in ip_addr:
                            city_servers[current_city].add(ip_addr)
    
    return dict(city_servers)


def save_servers(city_name, servers, ip_dir="ip"):
    if not servers:
        return 0
    
    os.makedirs(ip_dir, exist_ok=True)
    ip_file = os.path.join(ip_dir, f"{city_name}_ip.txt")
    
    existing_servers = set()
    if os.path.exists(ip_file):
        with open(ip_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    existing_servers.add(line)
    
    new_servers = servers - existing_servers
    added_count = len(new_servers)
    
    if added_count == 0:
        return 0
    
    all_servers = sorted(existing_servers | servers)
    
    with open(ip_file, 'w', encoding='utf-8') as f:
        f.write(f"# 自动生成于 {datetime.now().strftime('%Y/%m/%d %H:%M')}\n")
        for server in all_servers:
            f.write(f"{server}\n")
    
    return added_count


def process_network_file(url, filename, content):
    """处理单个网络文件，返回新增服务器数量"""
    content_md5 = get_content_md5(content)
    
    file_type = detect_file_type(content[:500])
    
    # 统计分组信息
    lines = content.split('\n')
    groups = []
    for line in lines:
        if '#genre#' in line and ',' in line:
            parts = line.split(',')
            if parts:
                genre = parts[0].strip()
                match = re.match(r'^(.*?)(?:-组播\d+)?$', genre)
                if match:
                    group_name = match.group(1)
                    is_valid, normalized = normalize_city_name(group_name)
                    if is_valid:
                        groups.append(normalized)
    
    if CONFIG['verbose'] and groups:
        unique_groups = list(set(groups))
        logger.info(f"  识别到 {len(unique_groups)} 个城市分组")
    
    # 提取各城市的服务器
    city_servers = extract_servers_from_content(content, file_type, filename)
    
    if not city_servers:
        if CONFIG['verbose']:
            logger.warning(f"  未提取到有效服务器")
        # 记录无新增
        update_processed_record(url, content_md5, filename, 0, 0)
        return 0
    
    total_servers = sum(len(s) for s in city_servers.values())
    if CONFIG['verbose']:
        logger.info(f"  提取到 {total_servers} 个服务器地址，分布在 {len(city_servers)} 个城市")
    
    # 保存各城市的服务器
    total_added = 0
    added_cities = []
    
    for city_name, servers in city_servers.items():
        if not is_valid_city(city_name):
            continue
        added = save_servers(city_name, servers, CONFIG["ip_dir"])
        if added > 0:
            total_added += added
            added_cities.append((city_name, added))
    
    # 打印新增汇总
    if CONFIG['verbose'] and added_cities:
        logger.info(f"  新增服务器汇总:")
        for city_name, added in added_cities:
            logger.info(f"    {city_name}: +{added}")
    
    # 记录处理结果（包含新增数量）
    update_processed_record(url, content_md5, filename, total_added, total_servers)
    
    return total_added


def process_network_files():
    """处理网络文件列表"""
    urls = []
    url_file = Path(CONFIG["url_config_file"])
    if url_file.exists():
        with open(url_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    urls.append(line)
    else:
        logger.warning(f"配置文件不存在: {url_file}")
        return
    
    if not urls:
        logger.warning("没有配置任何URL")
        return
    
    logger.info("=" * 60)
    logger.info("网络IP提取工具")
    logger.info("=" * 60)
    
    total_added = 0
    processed_count = 0
    skipped_count = 0
    failed_count = 0
    
    # 本次运行的内容 MD5 缓存（用于同批次去重）
    session_md5_cache = set()
    
    # 记录有新增的 URL
    urls_with_added = []
    
    for idx, url in enumerate(urls, 1):
        if CONFIG['verbose']:
            logger.info(f"\n[{idx}/{len(urls)}] {url}")
        
        content, ok = fetch_url_with_proxy(url, CONFIG["proxy_heads"], CONFIG["network_timeout"])
        if ok:
            if CONFIG['verbose']:
                logger.info("  ✓ 获取成功")
            filename = Path(url).name or "playlist"
            
            content_md5 = get_content_md5(content)
            
            # 检查是否已处理过
            should_skip, skip_reason = is_already_processed_in_session(url, content_md5, session_md5_cache)
            
            if should_skip:
                if CONFIG['verbose']:
                    logger.info(f"  → {skip_reason}，跳过")
                skipped_count += 1
                continue
            
            added = process_network_file(url, filename, content)
            total_added += added
            processed_count += 1
            
            if added > 0:
                urls_with_added.append((url, added))
            
            # 记录本次运行的 MD5
            session_md5_cache.add(content_md5)
            
            if CONFIG['verbose'] and added == 0:
                logger.info("  → 无新增服务器")
        else:
            if CONFIG['verbose']:
                logger.info("  ✗ 获取失败")
            failed_count += 1
    
    # 打印有新增的 URL 汇总
    if urls_with_added and CONFIG['verbose']:
        logger.info("\n" + "=" * 60)
        logger.info("有新增服务器的来源:")
        for url, added in urls_with_added:
            logger.info(f"  +{added}: {url}")
        logger.info("=" * 60)
    
    logger.info("\n" + "=" * 60)
    logger.info("处理完成")
    logger.info(f"  ✓ 成功处理: {processed_count} 个")
    if skipped_count > 0:
        logger.info(f"  → 跳过(重复内容): {skipped_count} 个")
    if failed_count > 0:
        logger.info(f"  ✗ 失败: {failed_count} 个")
    logger.info(f"  📊 共新增 {total_added} 个服务器")
    logger.info("=" * 60)


def is_md5_globally_processed(content_md5):
    """检查历史记录中是否已有相同 MD5 的内容"""
    records = load_processed_records()
    for stored_url, info in records.items():
        if info.get("md5") == content_md5:
            return True
    return False


def is_already_processed_in_session(url, content_md5, session_md5_cache):
    """
    检查是否已处理过
    session_md5_cache: 本次运行已处理的 MD5 集合
    返回: (是否跳过, 跳过原因)
    """
    # 1. 本次运行相同 MD5
    if content_md5 in session_md5_cache:
        return True, "本次运行已处理相同内容"
    
    # 2. 历史记录相同 MD5
    records = load_processed_records()
    for stored_url, info in records.items():
        if info.get("md5") == content_md5:
            return True, f"历史记录中已有相同内容"
    
    # 3. 同一 URL 相同 MD5
    if url in records and records[url].get("md5") == content_md5:
        return True, "URL 未变化"
    
    return False, None


def main():
    parser = argparse.ArgumentParser(description='网络IP提取工具')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='显示详细信息')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='调试模式（显示所有细节）')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='静默模式，只显示错误')
    parser.add_argument('--urls', nargs='+',
                        help='直接指定URL列表（覆盖配置文件）')
    parser.add_argument('--force', action='store_true',
                        help='强制重新处理所有URL（忽略MD5缓存）')
    args = parser.parse_args()
    
    # 设置输出级别
    if args.quiet:
        CONFIG['verbose'] = False
        CONFIG['debug'] = False
    elif args.debug:
        CONFIG['verbose'] = True
        CONFIG['debug'] = True
    elif args.verbose:
        CONFIG['verbose'] = True
        CONFIG['debug'] = False
    else:
        CONFIG['verbose'] = True  # 默认显示关键信息
        CONFIG['debug'] = False
    
    # 重新设置日志
    global logger
    logger = setup_logging(CONFIG['verbose'], CONFIG['debug'])
    
    if args.force:
        records_file = Path(CONFIG["records_file"])
        if records_file.exists():
            records_file.unlink()
            logger.info("强制模式：已清空处理记录")
    
    if args.urls:
        os.makedirs("config", exist_ok=True)
        with open(CONFIG["url_config_file"], 'w', encoding='utf-8') as f:
            for url in args.urls:
                f.write(f"{url}\n")
        
        process_network_files()
    else:
        process_network_files()


if __name__ == "__main__":
    main()