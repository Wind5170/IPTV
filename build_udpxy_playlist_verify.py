#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
udpxy 播放列表生成工具（带实时验证版）
功能：
1. 从测速结果生成可用的组播源播放列表，并在使用前实时验证服务器可用性
2. 智能服务器选择策略：优先使用达标服务器，不足时从低速服务器中补充
3. 目标：确保每个城市至少有3个可用服务器（达标服务器 + 低速服务器补充）
4. 支持定制模式和全量模式，支持实时验证和频道过滤
5. 读取服务器数据时，对域名进行解析，按解析后的IP去重后使用
6. 支持自动模式（--auto），用于 CI/CD 环境
7. _ip_good.txt 中的服务器单独验证连通性，通过后优先使用
8. 统一流程：获取所有优质服务器 -> 验证 -> 补充低速 -> 生成列表
9. 支持关键字索引匹配（IPTV、百事通、广播等）
"""

import os
import sys
import argparse
import datetime
import json
import re
import shutil
import time
import socket
import requests
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed


# ==================== 配置参数 ====================
CONFIG = {
    "output_dir_limited": "output/limited",
    "output_dir_all": "output/all",
    "default_max_servers": 6,           # 默认目标服务器数量（定制版）
    "default_server_sources": ['good', 'precise', 'quick'],
    "save_category": False,
    "verbose": False,
    "local_first": True,
    "ip_dir": "ip",
    "template_dir": "template",
    "template_export_dir": "template/export",  # 优先使用的模板目录
    "rtp_dir": "rtp",
    "verify_timeout": 3,
    "verify_workers": 20,
    "verify_retry": 1,
    "verify": True,                      # 默认启用实时验证
    "auto_mode": False,                  # 默认非自动模式
    
    # ==================== 频道过滤规则开关 ====================
    "enable_exclude_prefixes": True,     # 是否启用排除索引前缀规则
    "enable_keep_keywords": True,        # 是否启用保留关键词规则
    "enable_keep_unmatched": True,       # 是否保留未匹配频道
    
    # ==================== 排序规则开关 ====================
    "enable_city_sort": True,            # 是否启用城市排序（按省份+运营商排序）
    "enable_channel_natural_sort": True, # 是否启用频道自然排序（数字排序）
    "local_satellite_top": True,         # 本地卫视是否置顶（排在本地其他频道之前）
    "local_other_top": True,             # 本地其他频道是否置顶（排在已分类频道之前）
    "protect_local_satellite": True,     # 是否保护本城市卫视（不被排除规则影响）
    
    # ==================== 本地卫视名称映射 ====================
    "local_satellite_names": {
        "上海": ["上海卫视", "东方卫视"],
        "广东": ["广东卫视", "大湾区卫视"],
        # 其他城市使用默认规则：{城市名}卫视
    },
}

QUALITY_SUFFIXES = ['HD', '-HD', 'hd', '-hd', '高清', '-高清', 'H264', 'H265', 'HEVC']

# ==================== 省份排序顺序 ====================
REGIONS = [
    "四川", "贵州",
    "北京", "上海", "广东", "浙江", "江苏", "重庆", "天津",
    "安徽", "福建", "甘肃", "广西", "海南", "河北", "河南",
    "黑龙江", "湖北", "湖南", "吉林", "江西", "辽宁", "内蒙古", "宁夏",
    "青海", "山东", "山西", "陕西", "云南", "西藏", "新疆", "台湾", "香港", "澳门"
]

'''
REGIONS = [
    "北京", "上海", "广东", "浙江", "江苏", "四川", "重庆", "天津",
    "安徽", "福建", "甘肃", "广西", "贵州", "海南", "河北", "河南",
    "黑龙江", "湖北", "湖南", "吉林", "江西", "辽宁", "内蒙古", "宁夏",
    "青海", "山东", "山西", "陕西", "云南", "西藏", "新疆", "台湾", "香港", "澳门"
]
'''

# ==================== 服务器解析辅助函数 ====================
def is_domain_format(server):
    """判断服务器地址是否为域名格式"""
    if not server:
        return False
    if ':' in server:
        host, _ = server.rsplit(':', 1)
        ip_pattern = re.compile(r'^\d+\.\d+\.\d+\.\d+$')
        return not ip_pattern.match(host)
    ip_pattern = re.compile(r'^\d+\.\d+\.\d+\.\d+$')
    return not ip_pattern.match(server)


def resolve_server_to_ip(server):
    """
    将域名解析为IP地址（用于去重）
    返回: IP:端口 或 None（解析失败时）
    """
    if not server or ':' not in server:
        return server
    
    host, port = server.rsplit(':', 1)
    
    # 如果已经是IP地址，直接返回
    ip_pattern = re.compile(r'^\d+\.\d+\.\d+\.\d+$')
    if ip_pattern.match(host):
        return server
    
    # 域名，解析为IP
    try:
        ip = socket.gethostbyname(host)
        result = f"{ip}:{port}"
        return result
    except (socket.gaierror, ValueError):
        return None


def get_resolved_key(server):
    """
    获取解析后的IP作为去重键
    返回: IP:端口（解析后的值），解析失败则返回原值
    """
    if is_domain_format(server):
        resolved = resolve_server_to_ip(server)
        return resolved if resolved else server
    return server


def extract_main_city_name(filename):
    """从文件名提取主城市名（去除后缀）"""
    suffixes = ['_extracted', '_source', '_checked', '_result', '_precise', '_history', '_quick', '_probe']
    for suffix in suffixes:
        if filename.endswith(suffix):
            return filename[:-len(suffix)]
    return filename


# ==================== 城市列表获取（优先使用 export 目录） ====================
def get_cities_from_template_dir(template_dir: str = "template") -> List[str]:
    """
    从 template 目录读取城市列表（根据模板文件名）
    优先使用 template/export 目录下的模板，如果没有则使用 template 目录
    返回所有模板文件对应的城市（去重）
    """
    cities = set()
    
    # 1. 优先从 export 目录读取
    export_dir = CONFIG.get("template_export_dir", "template/export")
    if os.path.exists(export_dir):
        for filename in os.listdir(export_dir):
            if not filename.endswith('.txt'):
                continue
            if filename.startswith('template_'):
                city_name = filename.replace('template_', '').replace('.txt', '')
                if city_name:
                    cities.add(city_name)
        if CONFIG['verbose']:
            print(f"  从 export 目录读取到 {len(cities)} 个城市")
    
    # 2. 从原 template 目录读取（补全遗漏的城市）
    original_count = len(cities)
    if os.path.exists(template_dir):
        for filename in os.listdir(template_dir):
            if not filename.endswith('.txt'):
                continue
            if filename.startswith('template_'):
                city_name = filename.replace('template_', '').replace('.txt', '')
                if city_name:
                    cities.add(city_name)
        if CONFIG['verbose'] and len(cities) > original_count:
            print(f"  从 template 目录补全 {len(cities) - original_count} 个城市")
    
    return sorted(list(cities))


def get_template_file_path(city: str, template_dir: str = "template") -> str:
    """
    获取模板文件路径
    优先使用 template/export 目录下的模板，如果没有则使用 template 目录
    """
    export_dir = CONFIG.get("template_export_dir", "template/export")
    
    # 优先检查 export 目录
    export_file = os.path.join(export_dir, f"template_{city}.txt")
    if os.path.exists(export_file):
        return export_file
    
    # 回退到原 template 目录
    fallback_file = os.path.join(template_dir, f"template_{city}.txt")
    return fallback_file


def get_city_sort_key(city_name: str, sort_mode: str = "city_first") -> tuple:
    operator_order = {"电信": 1, "联通": 2, "移动": 3}
    province = city_name
    operator = ""
    
    for op in operator_order.keys():
        if city_name.endswith(op):
            province = city_name[:-len(op)]
            operator = op
            break
    
    region_order = {region: idx for idx, region in enumerate(REGIONS)}
    province_index = region_order.get(province, 999)
    operator_index = operator_order.get(operator, 99)
    
    if sort_mode == "city_first":
        return (province_index, operator_index)
    else:
        return (operator_index, province_index)


def sort_cities(cities: List[str], sort_mode: str = "city_first") -> List[str]:
    if not CONFIG.get("enable_city_sort", True):
        return cities
    return sorted(cities, key=lambda x: get_city_sort_key(x, sort_mode))


def load_zubo_cities(zubo_cities_file: str = "config/zubo_cities.txt") -> Set[str]:
    if not os.path.exists(zubo_cities_file):
        return set()
    
    cities = set()
    
    for encoding in ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'latin-1']:
        try:
            with open(zubo_cities_file, 'r', encoding=encoding) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        line = line.strip('\ufeff')
                        if line:
                            cities.add(line)
            if cities and CONFIG['verbose']:
                print(f"  加载 zubo_cities: {len(cities)} 个城市 (编码: {encoding})")
            return cities
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:
            continue
    
    return set()


def print_city_list(sort_mode: str = "city_first") -> str:
    cities = get_cities_from_template_dir()
    if not cities:
        return "未找到任何模板文件（template/export/template_*.txt 或 template/template_*.txt）"
    
    cities = sort_cities(cities, sort_mode)
    
    lines = []
    cols = 5
    for i in range(0, len(cities), cols):
        row = cities[i:i+cols]
        row_text = ""
        for j, city in enumerate(row):
            idx = i + j + 1
            template_file = get_template_file_path(city)
            # 检查是否使用 export 目录模板
            export_dir = CONFIG.get("template_export_dir", "template/export")
            is_export = export_dir in template_file
            no_template = "*" if not os.path.exists(template_file) else " "
            marker = "📁" if is_export else " "
            row_text += f"{idx:2d}.{marker}{city}{no_template}\t"
        lines.append(row_text)
    lines.append("  (标记 📁 表示使用 export 目录模板，* 表示缺少模板文件)")
    return "\n".join(lines)


# ==================== 服务器实时验证 ====================
def verify_server(server: str, stream: str, timeout: int = 3) -> bool:
    """
    验证服务器可用性
    server 可以是 IP:端口 或 域名:端口
    直接使用域名请求，不预先解析（requests会自动解析）
    """
    url = f"http://{server}/rtp/{stream}"
    
    for attempt in range(CONFIG["verify_retry"] + 1):
        try:
            start = time.time()
            resp = requests.get(url, timeout=(timeout, timeout), stream=True)
            resp.raise_for_status()
            
            downloaded = 0
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    downloaded += len(chunk)
                if downloaded >= 16384:
                    return True
            return downloaded > 0
        except Exception:
            if attempt == CONFIG["verify_retry"]:
                return False
            time.sleep(0.5)
    
    return False


# ==================== 速度解析函数 ====================
def parse_speed_to_kbps(speed_str: str, mode: str = 'precise') -> float:
    """将速度字符串转换为 KB/s"""
    if speed_str == "[X]":
        return 0
    
    if mode == 'quick':
        match = re.match(r"([\d.]+)([MkB])", speed_str)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            if unit == "M":
                return value * 1024
            elif unit == "k":
                return value
            elif unit == "B":
                return value / 1024
    else:
        match = re.match(r"([\d.]+)\s+([KM]B/s)", speed_str)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            if unit == "MB/s":
                return value * 1024
            elif unit == "KB/s":
                return value
    return 0


# ==================== 服务器获取函数 ====================
def get_good_servers(city: str, stream: str, verify: bool, verbose: bool = True) -> List[str]:
    """
    获取并验证 _ip_good.txt 中的服务器
    返回验证通过的服务器列表
    """
    good_file = Path(CONFIG["ip_dir"]) / f"{city}_ip_good.txt"
    if not good_file.exists():
        return []
    
    good_servers_raw = []
    with open(good_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('http://'):
                line = line[7:]
            if line.startswith('https://'):
                line = line[8:]
            if ':' in line:
                good_servers_raw.append(line)
    
    if not good_servers_raw:
        return []
    
    if verbose and CONFIG['verbose'] and not CONFIG['auto_mode']:
        print(f"  读取优质服务器: {len(good_servers_raw)} 个")
    
    # 验证 good 服务器的连通性
    valid_good = []
    if verify and stream:
        for server in good_servers_raw:
            if verify_server(server, stream, CONFIG["verify_timeout"]):
                valid_good.append(server)
                if verbose and CONFIG['verbose'] and not CONFIG['auto_mode']:
                    print(f"    ✓ {server} (优质服务器) 验证通过")
            else:
                if verbose and CONFIG['verbose'] and not CONFIG['auto_mode']:
                    print(f"    ✗ {server} (优质服务器) 验证失败")
    else:
        valid_good = good_servers_raw
    
    if verbose and CONFIG['verbose'] and not CONFIG['auto_mode']:
        print(f"  优质服务器验证通过 {len(valid_good)}/{len(good_servers_raw)} 个")
    
    return valid_good


def get_test_servers(city: str, ip_dir: str = "ip", verbose: bool = True) -> List[Tuple[str, float]]:
    """
    获取测试达标服务器（precise/quick 测试结果）
    返回 [(原始服务器地址, 速度KB/s), ...] 按速度降序排序
    """
    servers_dict = {}
    main_city_name = extract_main_city_name(city)
    
    # 读取精确测试结果
    precise_file = Path(ip_dir) / f"{main_city_name}_ip_precise.txt"
    if precise_file.exists():
        with open(precise_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 2:
                    server = parts[0].strip()
                    speed_str = parts[1].strip()
                    speed_kbps = parse_speed_to_kbps(speed_str, 'precise')
                    resolved_key = get_resolved_key(server)
                    if resolved_key not in servers_dict or servers_dict[resolved_key][1] < speed_kbps:
                        servers_dict[resolved_key] = (server, speed_kbps)
    
    # 读取快速测试结果
    quick_file = Path(ip_dir) / f"{main_city_name}_ip_quick.txt"
    if quick_file.exists():
        with open(quick_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 2:
                    server = parts[0].strip()
                    speed_str = parts[1].strip()
                    speed_kbps = parse_speed_to_kbps(speed_str, 'quick')
                    resolved_key = get_resolved_key(server)
                    if resolved_key not in servers_dict or servers_dict[resolved_key][1] < speed_kbps:
                        servers_dict[resolved_key] = (server, speed_kbps)
    
    # 转换为列表并按速度降序排序
    servers = [(original, speed) for original, speed in servers_dict.values()]
    servers.sort(key=lambda x: x[1], reverse=True)
    
    if verbose and CONFIG['verbose'] and not CONFIG['auto_mode']:
        domain_count = sum(1 for s, _ in servers if is_domain_format(s))
        ip_count = len(servers) - domain_count
        print(f"  测试达标服务器: {len(servers)} 个 (域名:{domain_count}, IP:{ip_count})")
    
    return servers


def get_slow_servers_for_city(city: str, ip_dir: str = "ip", verbose: bool = True) -> List[str]:
    """
    获取城市的低速服务器列表（用于补充）
    从 slow 目录下的 _slow.txt 文件读取
    返回原始格式的服务器地址列表（按速度排序）
    """
    servers_dict = {}
    slow_dir = Path(ip_dir) / "slow"
    main_city_name = extract_main_city_name(city)
    
    # 读取精确测试低速结果
    precise_slow_file = slow_dir / f"{main_city_name}_ip_precise_slow.txt"
    if precise_slow_file.exists():
        with open(precise_slow_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 2:
                    server = parts[0].strip()
                    speed_str = parts[1].strip()
                    speed_kbps = parse_speed_to_kbps(speed_str, 'precise')
                    resolved_key = get_resolved_key(server)
                    if resolved_key not in servers_dict or servers_dict[resolved_key][1] < speed_kbps:
                        servers_dict[resolved_key] = (server, speed_kbps)
    
    # 读取快速测试低速结果
    quick_slow_file = slow_dir / f"{main_city_name}_ip_quick_slow.txt"
    if quick_slow_file.exists():
        with open(quick_slow_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 2:
                    server = parts[0].strip()
                    speed_str = parts[1].strip()
                    speed_kbps = parse_speed_to_kbps(speed_str, 'quick')
                    resolved_key = get_resolved_key(server)
                    if resolved_key not in servers_dict or servers_dict[resolved_key][1] < speed_kbps:
                        servers_dict[resolved_key] = (server, speed_kbps)
    
    # 转换为列表并按速度降序排序
    servers = [original for original, _ in sorted(servers_dict.values(), key=lambda x: x[1], reverse=True)]
    
    if verbose and CONFIG['verbose'] and not CONFIG['auto_mode'] and servers:
        print(f"  低速服务器: {len(servers)} 个可用于补充")
    
    return servers


def get_valid_servers(city: str, stream: str, verify: bool, target_count: int, auto_mode: bool) -> Tuple[List[str], List[str]]:
    """
    获取并验证服务器
    流程：
    1. 获取 good 服务器并验证连通性
    2. 获取 precise/quick 服务器并验证连通性
    3. 合并去重（good 服务器优先）
    4. 如果不足，从低速服务器补充
    返回: (所有验证通过的服务器列表, 优先级排序后的服务器列表)
    """
    verbose = not auto_mode
    
    # 1. 获取 good 服务器并验证
    good_servers = get_good_servers(city, stream, verify, verbose=verbose)
    
    # 2. 获取测试达标服务器（precise/quick）
    test_servers_with_speed = get_test_servers(city, CONFIG["ip_dir"], verbose=verbose)
    test_servers = [server for server, _ in test_servers_with_speed]
    
    if verbose and CONFIG['verbose']:
        print(f"  测试达标服务器: {len(test_servers)} 个")
    
    # 3. 验证测试达标服务器
    valid_test = []
    if verify and stream and test_servers:
        if verbose and CONFIG['verbose']:
            print(f"  正在验证 {len(test_servers)} 个测试达标服务器...")
        
        for server in test_servers:
            if verify_server(server, stream, CONFIG["verify_timeout"]):
                valid_test.append(server)
                if verbose and CONFIG['verbose']:
                    print(f"    ✓ {server} 验证通过 ({len(valid_test)}/{len(test_servers)})")
            else:
                if verbose and CONFIG['verbose']:
                    print(f"    ✗ {server} 验证失败")
        
        if verbose and CONFIG['verbose']:
            print(f"  测试达标服务器验证通过 {len(valid_test)}/{len(test_servers)} 个")
    else:
        valid_test = test_servers
        if verbose and CONFIG['verbose']:
            print(f"  跳过验证，使用全部 {len(valid_test)} 个测试达标服务器")
    
    # 4. 合并 good 服务器和测试达标服务器（去重）
    # 使用解析后的IP作为去重键
    good_keys = {get_resolved_key(s) for s in good_servers}
    
    # 先添加 good 服务器
    all_servers = good_servers.copy()
    
    # 再添加不在 good 中的测试达标服务器
    for server in valid_test:
        resolved_key = get_resolved_key(server)
        if resolved_key not in good_keys:
            all_servers.append(server)
            good_keys.add(resolved_key)
    
    if verbose and CONFIG['verbose']:
        print(f"  合并后共 {len(all_servers)} 个服务器 (优质: {len(good_servers)}, 测试达标: {len(valid_test)})")
    
    # 5. 如果服务器不足，从低速服务器补充（仅定制版需要）
    if len(all_servers) < target_count:
        slow_servers = get_slow_servers_for_city(city, CONFIG["ip_dir"], verbose=verbose)
        if slow_servers and verbose and CONFIG['verbose']:
            print(f"  服务器不足 ({len(all_servers)}/{target_count})，尝试补充低速服务器...")
        
        valid_slow = []
        if verify and stream:
            for server in slow_servers:
                if len(all_servers) + len(valid_slow) >= target_count:
                    break
                if verify_server(server, stream, CONFIG["verify_timeout"]):
                    valid_slow.append(server)
                    if verbose and CONFIG['verbose']:
                        print(f"    ✓ {server} 验证通过 (低速补充)")
                else:
                    if verbose and CONFIG['verbose']:
                        print(f"    ✗ {server} 验证失败")
        else:
            valid_slow = slow_servers[:target_count - len(all_servers)]
        
        if valid_slow:
            all_servers.extend(valid_slow)
            if verbose and CONFIG['verbose']:
                print(f"  补充低速服务器 {len(valid_slow)} 个，总计 {len(all_servers)} 个")
    
    if not all_servers:
        return [], []
    
    return all_servers, all_servers


# ==================== 频道分类相关 ====================
def load_category_index(category_file: str = "config/iptv_category.txt") -> Dict:
    channel_index = {}
    if not os.path.exists(category_file):
        return channel_index
    with open(category_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) >= 5:
                name_group = parts[0].strip()
                short_name = parts[1].strip()
                full_name = parts[2].strip()
                group = parts[3].strip()
                idx = parts[4].strip()
                for name in name_group.split('|'):
                    name = name.strip()
                    if name:
                        channel_index[name] = {
                            "index": idx,
                            "short_name": short_name,
                            "full_name": full_name,
                            "group": group
                        }
    return channel_index


def load_region_code(region_file: str = "config/region_code.txt") -> Dict:
    region_index = {}
    if not os.path.exists(region_file):
        return region_index
    with open(region_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                region_index[parts[0].strip()] = parts[1].strip()
    return region_index


def load_keyword_index(keyword_file: str = "config/keyword_index.txt") -> Dict:
    """
    加载关键字索引配置
    返回: {关键字: 索引号}
    用于匹配频道名中包含该关键字的频道
    """
    keyword_index = {}
    if not os.path.exists(keyword_file):
        return keyword_index
    
    try:
        with open(keyword_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 2:
                    keyword = parts[0].strip()
                    idx = parts[1].strip()
                    if keyword and idx:
                        keyword_index[keyword] = idx
    except Exception as e:
        if CONFIG['verbose']:
            print(f"  警告：读取关键字索引文件失败 {keyword_file}: {e}")
    
    return keyword_index


def normalize_channel_name(channel_name: str) -> str:
    """
    规范化频道名称
    1. 去除尾部特殊字符（空格、下划线、横线）
    2. 去除高清/标清等质量后缀
    3. 去除特殊符号（-、_、空格等）
    """
    name = channel_name.strip()
    
    # 去除质量后缀
    for suffix in QUALITY_SUFFIXES:
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
            if name.endswith(('-', '_')):
                name = name[:-1].strip()
            break
    
    # 去除括号内容
    name = re.sub(r'[（(].*?[）)]', '', name)
    
    # 去除横线、下划线、空格
    name = re.sub(r'[-_\s]+', '', name)
    
    return name


def find_matched_channel(channel_name: str, channel_index: Dict, keyword_index: Dict = None) -> Optional[Dict]:
    """
    查找频道匹配的分类信息
    优先级：
    1. 规范化后匹配（去除特殊字符、高清后缀等）
    2. 原始频道名匹配（兜底）
    3. 关键字匹配（包含关键字）
    """
    # 1. 规范化后匹配
    normalized = normalize_channel_name(channel_name)
    if normalized in channel_index:
        if CONFIG['verbose']:
            print(f"    规范化匹配: {channel_name} -> {normalized}")
        return channel_index[normalized]
    
    # 2. 原始频道名匹配
    if channel_name in channel_index:
        if CONFIG['verbose']:
            print(f"    精确匹配: {channel_name}")
        return channel_index[channel_name]
    
    # 3. 关键字匹配
    if keyword_index:
        for keyword, idx in keyword_index.items():
            if keyword in channel_name:
                if CONFIG['verbose']:
                    print(f"    关键字匹配: {channel_name} 匹配关键字 '{keyword}' -> 索引 {idx}")
                return {
                    "index": idx,
                    "short_name": channel_name,
                    "full_name": channel_name,
                    "group": "关键字匹配"
                }
    
    return None


def get_region_index(channel_name: str, region_index: Dict) -> Optional[str]:
    if len(channel_name) >= 2:
        prefix2 = channel_name[:2]
        if prefix2 in region_index:
            return region_index[prefix2]
    if len(channel_name) >= 3:
        prefix3 = channel_name[:3]
        if prefix3 in region_index:
            return region_index[prefix3]
    return None


def natural_sort_key(name: str) -> tuple:
    parts = re.split(r'(\d+)', name)
    result = []
    for p in parts:
        if p.isdigit():
            result.append(int(p))
        else:
            result.append(p)
    return tuple(result)


def load_template_channels(template_file: str) -> List[Dict]:
    channels = []
    if not os.path.exists(template_file):
        return channels
    with open(template_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if "," in line:
                parts = line.split(",", 1)
                if len(parts) >= 2:
                    name = parts[0].strip()
                    url = parts[1].strip()
                    if url != '#genre#':
                        channels.append({"name": name, "url": url})
    return channels


def get_city_exclusion(city_name: str) -> Tuple[Set[str], bool, Set[str], bool, bool]:
    """
    读取城市排除配置
    返回: (exclude_prefixes, keep_unmatched, keep_keywords, enable_exclude, enable_keep)
    """
    config_file = Path("config/city_config.json")
    default_exclude_prefixes = set()
    default_keep_unmatched = CONFIG.get("enable_keep_unmatched", True)
    default_keep_keywords = set()
    enable_exclude = CONFIG.get("enable_exclude_prefixes", True)
    enable_keep = CONFIG.get("enable_keep_keywords", True)
    
    if not config_file.exists():
        return default_exclude_prefixes, default_keep_unmatched, default_keep_keywords, enable_exclude, enable_keep
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cities_data = data.get("cities", data)
        for key, cfg in cities_data.items():
            if cfg.get("city") == city_name:
                exclude_prefixes = set()
                prefixes_str = cfg.get("exclude_prefixes", "")
                if prefixes_str and enable_exclude:
                    for part in prefixes_str.split('|'):
                        if part.strip():
                            exclude_prefixes.add(part.strip())
                
                raw_keep = cfg.get("keep_unmatched", default_keep_unmatched)
                if isinstance(raw_keep, bool):
                    keep_unmatched = raw_keep
                elif isinstance(raw_keep, str):
                    keep_unmatched = raw_keep.lower() in ("true", "1", "yes")
                else:
                    keep_unmatched = default_keep_unmatched
                
                keep_keywords = set()
                raw_keywords = cfg.get("keep_keywords", [])
                if enable_keep:
                    if isinstance(raw_keywords, str):
                        for kw in raw_keywords.split('|'):
                            if kw.strip():
                                keep_keywords.add(kw.strip())
                    elif isinstance(raw_keywords, list):
                        for kw in raw_keywords:
                            if isinstance(kw, str):
                                keep_keywords.add(kw)
                
                return exclude_prefixes, keep_unmatched, keep_keywords, enable_exclude, enable_keep
    except Exception as e:
        if CONFIG['verbose']:
            print(f"  警告：读取城市排除配置失败: {e}")
    
    return default_exclude_prefixes, default_keep_unmatched, default_keep_keywords, enable_exclude, enable_keep


def extract_city_name_from_city(city: str) -> str:
    """从城市名中提取城市名称（去除运营商后缀）"""
    city_clean = city
    for op in ["电信", "移动", "联通"]:
        if city_clean.endswith(op):
            city_clean = city_clean[:-len(op)]
            break
    return city_clean


def get_local_satellite_order(channel_name: str, city: str) -> int:
    """
    获取本地卫视的排序权重（数值越小越靠前）
    根据 local_satellite_names 配置的顺序决定
    """
    city_clean = extract_city_name_from_city(city)
    local_satellite_names = CONFIG.get("local_satellite_names", {})
    satellite_list = local_satellite_names.get(city_clean, [f"{city_clean}卫视"])
    
    for idx, sat_name in enumerate(satellite_list):
        if sat_name in channel_name or channel_name == sat_name:
            return idx
    
    return len(satellite_list)


def is_local_satellite(channel_name: str, city: str) -> bool:
    """
    判断频道是否为当前城市的卫视频道
    支持自定义卫视名称映射
    """
    city_clean = extract_city_name_from_city(city)
    
    # 获取自定义卫视名称映射
    local_satellite_names = CONFIG.get("local_satellite_names", {})
    satellite_keywords = local_satellite_names.get(city_clean, [f"{city_clean}卫视"])
    
    # 检查频道名是否匹配任何卫视关键词
    for keyword in satellite_keywords:
        if keyword in channel_name:
            return True
    
    # 检查"卫视"结尾且以城市名开头
    if channel_name.endswith("卫视"):
        sat_name = channel_name[:-2]
        if sat_name == city_clean:
            return True
    
    return False


def is_local_other_channel(channel_name: str, city: str) -> bool:
    """
    判断频道是否为本地其他频道（以城市名开头，但不是卫视）
    例如：城市为"江苏"，则"江苏体育休闲"、"江苏城市频道"等为本地其他频道
    """
    city_clean = extract_city_name_from_city(city)
    
    # 排除卫视频道
    if is_local_satellite(channel_name, city):
        return False
    
    # 检查是否以城市名开头
    if channel_name.startswith(city_clean):
        return True
    
    return False


def classify_channels(channels: List[Dict], city: str, channel_index: Dict,
                      region_index: Dict, keyword_index: Dict,
                      local_first: bool,
                      exclude_prefixes: Set[str], keep_unmatched: bool, keep_keywords: Set[str],
                      name_style: str = "full",
                      enable_exclude: bool = True,
                      enable_natural_sort: bool = True,
                      protect_local_satellite: bool = True,
                      local_satellite_top: bool = True,
                      local_other_top: bool = False) -> Tuple[List, List, List, List, List]:
    """
    分类频道
    
    分类优先级：
    1. 本地卫视（自定义映射或城市名+卫视）
    2. 本地其他频道（以城市名开头的非卫视频道）
    3. 已分类频道（有分类索引或关键字匹配）
    4. 地区分类频道（有地区编码，且不是本地频道）
    5. 未匹配频道
    """
    local_satellite = []
    local_other = []
    categorized = []
    region_based = []
    unmatched = []
    excluded_count = 0
    kept_local_satellite_count = 0

    for channel in channels:
        channel_name = channel["name"]
        info = find_matched_channel(channel_name, channel_index, keyword_index)

        if info:
            idx = info["index"]
            # 检查是否需要排除
            should_exclude = False
            if enable_exclude and exclude_prefixes:
                for prefix in exclude_prefixes:
                    if idx.startswith(prefix):
                        # 检查是否为本城市卫视频道（启用保护时）
                        if protect_local_satellite and is_local_satellite(channel_name, city):
                            kept_local_satellite_count += 1
                            should_exclude = False
                            break
                        else:
                            should_exclude = True
                            break
            
            if should_exclude:
                excluded_count += 1
                continue

            # 分类：本地卫视、本地其他、已分类
            if is_local_satellite(channel_name, city):
                display_name = info["full_name"] if name_style == "full" else info["short_name"]
                local_satellite.append({**channel, "display_name": display_name})
            elif is_local_other_channel(channel_name, city):
                display_name = info["full_name"] if name_style == "full" else info["short_name"]
                local_other.append({**channel, "display_name": display_name})
            else:
                display_name = info["full_name"] if name_style == "full" else info["short_name"]
                categorized.append({**channel, "display_name": display_name, "sort_key": (idx, channel_name)})
            continue

        # 没有分类索引的频道
        # 优先判断是否为本地频道（卫视或其他）
        if is_local_satellite(channel_name, city):
            local_satellite.append({**channel, "display_name": channel_name})
        elif is_local_other_channel(channel_name, city):
            local_other.append({**channel, "display_name": channel_name})
        else:
            # 再判断地区编码
            region_code = get_region_index(channel_name, region_index)
            if region_code:
                region_based.append({**channel, "display_name": channel_name, "sort_key": region_code})
            elif keep_unmatched:
                unmatched.append({**channel, "display_name": channel_name, "sort_key": ("99999999", channel_name)})
            else:
                keep = False
                for kw in keep_keywords:
                    if kw in channel_name:
                        keep = True
                        break
                if keep:
                    unmatched.append({**channel, "display_name": channel_name, "sort_key": ("99999999", channel_name)})
                else:
                    excluded_count += 1

    if excluded_count > 0 and CONFIG['verbose']:
        print(f"  共排除 {excluded_count} 个频道（保留 {kept_local_satellite_count} 个本城市卫视）")

    # ==================== 排序 ====================
    
    # 1. 本地卫视：按自定义顺序排序
    local_satellite.sort(key=lambda x: get_local_satellite_order(x["name"], city))
    
    # 2. 本地其他频道：按频道名自然排序
    if enable_natural_sort:
        local_other.sort(key=lambda x: natural_sort_key(x["name"]))
    else:
        local_other.sort(key=lambda x: x["name"])
    
    # 3. 已分类频道：按索引号排序
    categorized.sort(key=lambda x: x["sort_key"])
    
    # 4. 地区分类频道：按地区编码排序
    region_based.sort(key=lambda x: (x["sort_key"], x["display_name"]))
    
    # 5. 未匹配频道：按显示名自然排序
    if enable_natural_sort:
        unmatched.sort(key=lambda x: natural_sort_key(x["display_name"]))
    else:
        unmatched.sort(key=lambda x: x["display_name"])

    return local_satellite, local_other, categorized, region_based, unmatched


# ==================== 核心生成函数 ====================
def generate_city_playlist(city: str, channel_index: Dict, region_index: Dict, keyword_index: Dict,
                           local_first: bool, max_servers: int,
                           verify: bool, auto_mode: bool,
                           valid_servers: List[str] = None,
                           prioritized_servers: List[str] = None) -> Tuple[bool, bool]:
    """
    生成单个城市的播放列表（定制版和完整版）
    如果传入 valid_servers 和 prioritized_servers，则跳过服务器获取验证步骤
    返回: (定制版是否成功, 完整版是否成功)
    """
    # 如果没有传入服务器列表，则获取并验证
    if valid_servers is None or prioritized_servers is None:
        # 获取流地址用于验证
        stream = None
        config_file = Path("config/city_config.json")
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                cities_data = data.get("cities", data)
                for key, cfg in cities_data.items():
                    if cfg.get("city") == city:
                        stream = cfg.get("stream")
                        break
            except Exception:
                pass
        
        valid_servers, prioritized_servers = get_valid_servers(city, stream, verify, max_servers, auto_mode)
        
        if not valid_servers:
            if CONFIG['verbose'] and not auto_mode:
                print(f"  {city}: 没有可用的服务器，跳过")
            return False, False
    
    # 定制版服务器：取前 max_servers 个
    limited_servers = prioritized_servers[:max_servers]
    # 完整版服务器：使用全部
    all_servers = valid_servers
    
    # 加载频道模板（优先使用 export 目录）
    template_file = get_template_file_path(city, CONFIG["template_dir"])
    if not os.path.exists(template_file):
        if CONFIG['verbose'] and not auto_mode:
            print(f"  {city}: 模板文件不存在，跳过")
        return False, False
    
    channels = load_template_channels(template_file)
    if not channels:
        if CONFIG['verbose'] and not auto_mode:
            print(f"  {city}: 没有找到组播地址")
        return False, False
    
    # ==================== 定制版（跳过排除频道） ====================
    exclude_prefixes, keep_unmatched, keep_keywords, enable_exclude, enable_keep = get_city_exclusion(city)
    
    local_satellite, local_other, categorized, region_based, unmatched = classify_channels(
        channels, city, channel_index, region_index, keyword_index, local_first,
        exclude_prefixes, keep_unmatched, keep_keywords, name_style="full",
        enable_exclude=enable_exclude,
        enable_natural_sort=CONFIG.get("enable_channel_natural_sort", True),
        protect_local_satellite=CONFIG.get("protect_local_satellite", True),
        local_satellite_top=CONFIG.get("local_satellite_top", True),
        local_other_top=CONFIG.get("local_other_top", False)
    )
    
    # 写入定制版
    limited_file = os.path.join(CONFIG["output_dir_limited"], f"{city}.txt")
    os.makedirs(CONFIG["output_dir_limited"], exist_ok=True)
    
    with open(limited_file, "w", encoding="utf-8") as f:
        for i, ip in enumerate(limited_servers, 1):
            if i > 1:
                f.write("\n")
            f.write(f"{city}-组播{i},#genre#\n")
            
            # 1. 本地卫视（如果启用置顶）
            if CONFIG.get("local_satellite_top", True):
                for ch in local_satellite:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if local_satellite and (local_other or categorized or region_based or unmatched):
                    f.write("\n")
            
            # 2. 判断是否启用本地其他频道置顶
            if CONFIG.get("local_other_top", False):
                # 置顶模式：本地其他频道放在本地卫视之后
                for ch in local_other:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if local_other and (categorized or region_based or unmatched):
                    f.write("\n")
                # 3. 已分类频道
                for ch in categorized:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if categorized and (region_based or unmatched):
                    f.write("\n")
                # 4. 地区分类频道
                for ch in region_based:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if region_based and unmatched:
                    f.write("\n")
                # 5. 未匹配频道
                for ch in unmatched:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
            else:
                # 非置顶模式：已分类频道在前，本地其他频道在后
                # 3. 已分类频道
                for ch in categorized:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if categorized and (region_based or local_other or unmatched):
                    f.write("\n")
                # 4. 地区分类频道
                for ch in region_based:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if region_based and (local_other or unmatched):
                    f.write("\n")
                # 5. 本地其他频道（不置顶）
                for ch in local_other:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if local_other and unmatched:
                    f.write("\n")
                # 6. 未匹配频道
                for ch in unmatched:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
    
    # ==================== 完整版（不跳过排除频道） ====================
    local_satellite_all, local_other_all, categorized_all, region_based_all, unmatched_all = classify_channels(
        channels, city, channel_index, region_index, keyword_index, local_first,
        set(), True, set(), name_style="full",
        enable_exclude=False,
        enable_natural_sort=CONFIG.get("enable_channel_natural_sort", True),
        protect_local_satellite=CONFIG.get("protect_local_satellite", True),
        local_satellite_top=CONFIG.get("local_satellite_top", True),
        local_other_top=CONFIG.get("local_other_top", False)
    )
    
    # 写入完整版
    all_file = os.path.join(CONFIG["output_dir_all"], f"{city}.txt")
    os.makedirs(CONFIG["output_dir_all"], exist_ok=True)
    
    with open(all_file, "w", encoding="utf-8") as f:
        for i, ip in enumerate(all_servers, 1):
            if i > 1:
                f.write("\n")
            f.write(f"{city}-组播{i},#genre#\n")
            
            # 1. 本地卫视（如果启用置顶）
            if CONFIG.get("local_satellite_top", True):
                for ch in local_satellite_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if local_satellite_all and (local_other_all or categorized_all or region_based_all or unmatched_all):
                    f.write("\n")
            
            # 2. 判断是否启用本地其他频道置顶
            if CONFIG.get("local_other_top", False):
                # 置顶模式：本地其他频道放在本地卫视之后
                for ch in local_other_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if local_other_all and (categorized_all or region_based_all or unmatched_all):
                    f.write("\n")
                # 3. 已分类频道
                for ch in categorized_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if categorized_all and (region_based_all or unmatched_all):
                    f.write("\n")
                # 4. 地区分类频道
                for ch in region_based_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if region_based_all and unmatched_all:
                    f.write("\n")
                # 5. 未匹配频道
                for ch in unmatched_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
            else:
                # 非置顶模式：已分类频道在前，本地其他频道在后
                # 3. 已分类频道
                for ch in categorized_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if categorized_all and (region_based_all or local_other_all or unmatched_all):
                    f.write("\n")
                # 4. 地区分类频道
                for ch in region_based_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if region_based_all and (local_other_all or unmatched_all):
                    f.write("\n")
                # 5. 本地其他频道（不置顶）
                for ch in local_other_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
                if local_other_all and unmatched_all:
                    f.write("\n")
                # 6. 未匹配频道
                for ch in unmatched_all:
                    f.write(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}\n")
    
    if CONFIG['verbose'] and not auto_mode:
        print(f"  定制版: {len(limited_servers)} 个服务器")
        print(f"  完整版: {len(all_servers)} 个服务器")
    
    return True, True


def merge_all_playlists(cities: List[str], has_zubo_filter: bool, zubo_cities_set: Set[str], auto_mode: bool) -> None:
    """合并所有城市的播放列表"""
    if not auto_mode:
        print("\n" + "=" * 60)
        print("合并所有组播源")
        print("=" * 60)
    
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    current_time = now.strftime("%Y/%m/%d %H:%M")
    os.makedirs("output", exist_ok=True)
    
    # 生成 zubo.txt (定制模式)
    if not auto_mode:
        print("📝 生成 zubo.txt (定制模式)")
    limited_contents = []
    for city in cities:
        if has_zubo_filter and city not in zubo_cities_set:
            continue
        file_path = f"{CONFIG['output_dir_limited']}/{city}.txt"
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding="utf-8") as f:
                limited_contents.append(f.read())
    
    if limited_contents:
        with open("output/zubo.txt", "w", encoding="utf-8") as f:
            f.write(f"{current_time}精选更新,#genre#\n")
            f.write(f"浙江卫视,http://ali-m-l.cztv.com/channels/lantian/channel001/1080p.m3u8\n\n")
            f.write('\n'.join(limited_contents))
        txt_to_m3u("output/zubo.txt", "output/zubo.m3u")
        if not auto_mode:
            print(f"  ✓ zubo.txt 已生成 (包含 {len(limited_contents)} 个城市)")
    else:
        if not auto_mode:
            print("  ✗ 未生成 zubo.txt (无有效内容)")
    
    # 生成 zubo_all.txt (全量模式)
    if not auto_mode:
        print("📝 生成 zubo_all.txt (全量模式)")
    all_contents = []
    for city in cities:
        file_path = f"{CONFIG['output_dir_all']}/{city}.txt"
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding="utf-8") as f:
                all_contents.append(f.read())
    
    if all_contents:
        with open("output/zubo_all.txt", "w", encoding="utf-8") as f:
            f.write(f"{current_time}更新,#genre#\n")
            f.write(f"浙江卫视,http://ali-m-l.cztv.com/channels/lantian/channel001/1080p.m3u8\n\n")
            f.write('\n'.join(all_contents))
        txt_to_m3u("output/zubo_all.txt", "output/zubo_all.m3u")
        if not auto_mode:
            print(f"  ✓ zubo_all.txt 已生成 (包含 {len(all_contents)} 个城市)")
    else:
        if not auto_mode:
            print("  ✗ 未生成 zubo_all.txt (无有效内容)")


def txt_to_m3u(input_file: str, output_file: str) -> None:
    if not os.path.exists(input_file):
        return
    with open(input_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        genre = ''
        for line in lines:
            line = line.strip()
            if line:
                if "," in line:
                    parts = line.split(',', 1)
                    if len(parts) >= 2:
                        channel_name, channel_url = parts[0], parts[1]
                        if channel_url == '#genre#':
                            genre = channel_name
                        else:
                            f.write(f'#EXTINF:-1 group-title="{genre}",{channel_name}\n')
                            f.write(f'{channel_url}\n')


def main():
    parser = argparse.ArgumentParser(description='udpxy 播放列表生成工具（带实时验证版）')
    parser.add_argument('city', type=int, nargs='?', default=None,
                        help='城市编号（不指定则显示列表选择）')
    parser.add_argument('-n', '--num', type=int, default=CONFIG['default_max_servers'],
                        help=f'目标服务器数量（默认: {CONFIG["default_max_servers"]}）')
    parser.add_argument('--local-first', dest='local_first', action='store_true', default=True,
                        help='本地频道优先（默认启用）')
    parser.add_argument('--no-local-first', dest='local_first', action='store_false',
                        help='不启用本地频道优先')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='显示详细处理信息')
    parser.add_argument('--all', action='store_true',
                        help='为所有城市生成播放列表')
    parser.add_argument('--name-style', choices=['short', 'full'], default='full',
                        help='频道名称样式: short(简称) 或 full(全称)，默认 full')
    parser.add_argument('--sort-mode', choices=['city_first', 'operator_first'], default='city_first',
                        help='城市排序模式: city_first(先城市后运营商) 或 operator_first(先运营商后城市)，默认 city_first')
    parser.add_argument('--server-sources', nargs='+', choices=['good', 'precise', 'quick'], 
                        default=CONFIG.get('default_server_sources', ['good', 'precise', 'quick']),
                        help='服务器来源: good(_ip_good.txt), precise(_ip_precise.txt), quick(_ip_quick.txt)，可组合使用')
    parser.add_argument('--no-verify', action='store_true',
                        help='禁用实时验证（默认启用）')
    parser.add_argument('--auto', action='store_true', default=False,
                        help='自动模式（用于CI/CD，不显示详细进度和交互）')
    args = parser.parse_args()
    
    if args.verbose:
        CONFIG['verbose'] = True
    
    # 设置自动模式
    auto_mode = args.auto
    if auto_mode:
        CONFIG['verbose'] = False
    
    # 验证开关
    if args.no_verify:
        verify = False
    else:
        verify = CONFIG.get('verify', True)
    
    local_first = args.local_first
    name_style = args.name_style
    sort_mode = args.sort_mode

    sort_mode_name = "先城市后运营商" if sort_mode == "city_first" else "先运营商后城市"
    
    print("=" * 60)
    print("IPTVZ 播放列表生成工具")
    print("=" * 60)
    print(f"目标服务器数: {args.num}")
    print(f"本地频道优先: {'是' if local_first else '否'}")
    print(f"频道名称样式: {'简称' if name_style == 'short' else '全称'}")
    print(f"排序模式: {sort_mode_name}")
    print(f"实时验证: {'启用' if verify else '禁用'}")
    print(f"自动模式: {'是' if auto_mode else '否'}")
    
    source_names = []
    if 'good' in args.server_sources:
        source_names.append("优质服务器(good)")
    if 'precise' in args.server_sources:
        source_names.append("精确测试(precise)")
    if 'quick' in args.server_sources:
        source_names.append("快速测试(quick)")
    print(f"服务器源: {', '.join(source_names)}")
    print("=" * 60)

    # 获取并排序城市列表
    raw_cities = get_cities_from_template_dir()
    if not raw_cities:
        print("错误：未找到任何模板文件，请检查 template/export/template_*.txt 或 template/template_*.txt 文件")
        return
    
    cities = sort_cities(raw_cities, sort_mode)
    
    # 确定要处理的城市列表
    selected_cities = []
    is_single_mode = False
    
    if args.city is not None:
        if 1 <= args.city <= len(cities):
            selected_cities = [cities[args.city - 1]]
            is_single_mode = True
        else:
            print(f"错误：无效的城市编号 {args.city}")
            print(f"请输入 1-{len(cities)} 之间的数字")
            return
    elif args.all or auto_mode:
        selected_cities = cities
        if not auto_mode:
            print(f"\n开始为全部 {len(cities)} 个城市生成播放列表...")
    else:
        # 交互模式：显示城市列表
        print("\n可用的城市列表:")
        print(print_city_list(sort_mode))
        print()
        
        while True:
            try:
                choice = input("请选择城市编号（直接回车全部，输入 q 退出）: ").strip()
                if choice.lower() == 'q':
                    print("退出程序")
                    return
                elif choice == '':
                    selected_cities = cities
                    break
                else:
                    city_num = int(choice)
                    if 1 <= city_num <= len(cities):
                        selected_cities = [cities[city_num - 1]]
                        is_single_mode = True
                        break
                    else:
                        print(f"请输入 1-{len(cities)} 之间的数字")
            except ValueError:
                print("请输入有效的数字或直接回车")
    
    if not selected_cities:
        print("未选择任何城市")
        return
    
    # 加载分类索引和地区编码
    if not auto_mode:
        print("\n正在加载分类索引和地区编码...")
    channel_index = load_category_index()
    region_index = load_region_code()
    keyword_index = load_keyword_index()
    if not auto_mode:
        print(f"  加载分类: {len(channel_index)} 条")
        print(f"  加载地区编码: {len(region_index)} 条")
        print(f"  加载关键字索引: {len(keyword_index)} 条")
    
    # 加载 zubo_cities 配置（仅汇总时需要）
    zubo_cities_set = load_zubo_cities()
    has_zubo_filter = len(zubo_cities_set) > 0 and len(selected_cities) > 1
    
    # 初始化统计
    limited_success = 0
    all_success = 0
    
    # 处理每个城市
    for idx, city in enumerate(selected_cities, 1):
        if not auto_mode and len(selected_cities) > 1:
            print(f"\n[{idx}/{len(selected_cities)}] 处理 {city}...")
        elif not auto_mode:
            print(f"\n处理城市: {city}")
        
        # 获取流地址用于验证
        stream = None
        config_file = Path("config/city_config.json")
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                cities_data = data.get("cities", data)
                for key, cfg in cities_data.items():
                    if cfg.get("city") == city:
                        stream = cfg.get("stream")
                        break
            except Exception:
                pass
        
        # 获取并验证服务器
        valid_servers, prioritized_servers = get_valid_servers(city, stream, verify, args.num, auto_mode)
        
        if not valid_servers:
            if CONFIG['verbose'] and not auto_mode:
                print(f"  {city}: 没有可用的服务器，跳过")
            continue
        
        # 生成播放列表
        success_limited, success_all = generate_city_playlist(
            city, channel_index, region_index, keyword_index, local_first, args.num,
            verify, auto_mode, valid_servers, prioritized_servers
        )
        
        if success_limited:
            limited_success += 1
        if success_all:
            all_success += 1
        
        # 单城市模式显示结果
        if is_single_mode and not auto_mode:
            print(f"\n✅ {city} 播放列表生成完成！")
            print(f"  定制版: {CONFIG['output_dir_limited']}/{city}.txt ({len(prioritized_servers[:args.num])}个IP)")
            print(f"  完整版: {CONFIG['output_dir_all']}/{city}.txt ({len(valid_servers)}个IP)")
    
    # 多城市模式，汇总所有播放列表
    if len(selected_cities) > 1:
        merge_all_playlists(selected_cities, has_zubo_filter, zubo_cities_set, auto_mode)
        
        if not auto_mode:
            print(f"\n✅ 批量处理完成！")
            print(f"  定制版: {limited_success}/{len(selected_cities)} 个城市成功")
            print(f"  完整版: {all_success}/{len(selected_cities)} 个城市成功")


if __name__ == "__main__":
    main()