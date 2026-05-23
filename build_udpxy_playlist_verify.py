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
    "output_dir_single": "output/single",
    "default_max_servers": 6,           # 默认目标服务器数量
    "default_server_sources": ['good', 'precise', 'quick'],
    "verify": True,  # 默认启用实时验证
    "save_category": False,
    "verbose": False,
    "local_first": True,
    "ip_dir": "ip",
    "template_dir": "template",
    "rtp_dir": "rtp",
    "verify_timeout": 3,
    "verify_workers": 20,
    "verify_retry": 1,
}

QUALITY_SUFFIXES = ['HD', '-HD', 'hd', '-hd', '高清', '-高清', 'H264', 'H265', 'HEVC']

# ==================== 省份排序顺序 ====================
REGIONS = [
    "安徽", "北京", "重庆", "福建", "甘肃", "广东", "广西", "贵州", "海南", "河北",
    "河南", "黑龙江", "湖北", "湖南", "吉林", "江苏", "江西", "辽宁", "内蒙古", "宁夏",
    "青海", "山东", "山西", "陕西", "上海", "四川", "天津", "西藏", "新疆", "云南",
    "浙江", "台湾", "香港", "澳门"
]


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


# ==================== 城市列表获取 ====================
def get_cities_from_template_dir(template_dir: str = "template") -> List[str]:
    """从 template 目录读取城市列表（根据模板文件名）"""
    cities = []
    if not os.path.exists(template_dir):
        print(f"警告：模板目录不存在 - {template_dir}")
        return cities
    
    for filename in os.listdir(template_dir):
        if not filename.endswith('.txt'):
            continue
        
        if filename.startswith('template_'):
            city_name = filename.replace('template_', '').replace('.txt', '')
            if city_name:
                cities.append(city_name)
    
    return cities


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
        return "未找到任何模板文件（template/template_*.txt）"
    
    cities = sort_cities(cities, sort_mode)
    
    lines = []
    cols = 5
    for i in range(0, len(cities), cols):
        row = cities[i:i+cols]
        row_text = ""
        for j, city in enumerate(row):
            idx = i + j + 1
            template_file = os.path.join(CONFIG["template_dir"], f"template_{city}.txt")
            no_template = "*" if not os.path.exists(template_file) else " "
            row_text += f"{idx:2d}. {city}{no_template}\t"
        lines.append(row_text)
    lines.append("  (标记 * 表示缺少模板文件，无法生成播放列表)")
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


def verify_servers(servers: List[str], stream: str, verbose: bool = False, 
                   max_valid: int = None) -> List[str]:
    if not servers or not stream:
        return []
    
    if verbose:
        if max_valid:
            print(f"    正在验证服务器（目标 {max_valid} 个）...", end=" ", flush=True)
        else:
            print(f"    正在验证 {len(servers)} 个服务器...", end=" ", flush=True)
    
    valid_servers = []
    
    with ThreadPoolExecutor(max_workers=CONFIG["verify_workers"]) as executor:
        future_to_server = {executor.submit(verify_server, server, stream, CONFIG["verify_timeout"]): server 
                           for server in servers}
        
        for future in as_completed(future_to_server):
            server = future_to_server[future]
            try:
                is_valid = future.result()
                if is_valid:
                    valid_servers.append(server)
                    if max_valid and len(valid_servers) >= max_valid:
                        for f in future_to_server:
                            if not f.done():
                                f.cancel()
                        break
            except Exception:
                pass
    
    if verbose:
        print(f"有效 {len(valid_servers)}/{len(servers)}")
    
    return valid_servers


# ==================== 速度解析函数 ====================
def parse_speed_value(speed_str: str, source_type: str) -> float:
    if speed_str == "[X]":
        return 0
    
    if source_type == 'quick':
        match = re.match(r"([\d.]+)([MkB])", speed_str)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            if unit == "M":
                return value * 1024 * 1024
            elif unit == "k":
                return value * 1024
            elif unit == "B":
                return value
    else:
        match = re.match(r"([\d.]+)\s+([KM]B/s)", speed_str)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            if unit == "MB/s":
                return value * 1024 * 1024
            elif unit == "KB/s":
                return value * 1024
    return 0


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


# ==================== 智能服务器选择（带域名解析去重） ====================
def read_qualified_servers(city: str, ip_dir: str = "ip") -> List[Tuple[str, float]]:
    """
    读取达标服务器（精确测试和快速测试）
    对域名进行解析，按解析后的IP去重
    返回 [(原始服务器地址, 速度KB/s), ...] 按速度降序排序
    """
    servers_dict = {}  # 使用解析后的IP作为key，存储 (原始地址, 速度)
    
    # 读取精确测试结果
    precise_file = Path(ip_dir) / f"{city}_ip_precise.txt"
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
                    # 解析域名获取去重键
                    resolved_key = get_resolved_key(server)
                    # 保留速度较高的那个
                    if resolved_key not in servers_dict or servers_dict[resolved_key][1] < speed_kbps:
                        servers_dict[resolved_key] = (server, speed_kbps)
    
    # 读取快速测试结果
    quick_file = Path(ip_dir) / f"{city}_ip_quick.txt"
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
    
    if CONFIG['verbose']:
        domain_count = sum(1 for s, _ in servers if is_domain_format(s))
        ip_count = len(servers) - domain_count
        print(f"  达标服务器: {len(servers)} 个 (域名:{domain_count}, IP:{ip_count})")
    
    return servers


def read_slow_servers(city: str, ip_dir: str = "ip") -> List[Tuple[str, float]]:
    """
    读取 slow 目录下的低速服务器
    对域名进行解析，按解析后的IP去重
    返回 [(原始服务器地址, 速度KB/s), ...] 按速度降序排序
    """
    servers_dict = {}
    slow_dir = Path(ip_dir) / "slow"
    
    # 读取精确测试低速结果
    precise_slow_file = slow_dir / f"{city}_ip_precise_slow.txt"
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
    quick_slow_file = slow_dir / f"{city}_ip_quick_slow.txt"
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
    servers = [(original, speed) for original, speed in servers_dict.values()]
    servers.sort(key=lambda x: x[1], reverse=True)
    
    if CONFIG['verbose']:
        domain_count = sum(1 for s, _ in servers if is_domain_format(s))
        ip_count = len(servers) - domain_count
        print(f"  低速服务器: {len(servers)} 个 (域名:{domain_count}, IP:{ip_count})")
    
    return servers


def get_optimal_servers(city: str, ip_dir: str = "ip", 
                        target_count: int = 3) -> List[str]:
    """
    获取最优服务器列表（智能补充策略）
    目标：至少 target_count 个服务器
    对域名进行解析，按解析后的IP去重
    返回原始格式的服务器地址列表
    
    优先级：
    1. 达标服务器（精确测试 > 快速测试）
    2. 低速服务器（精确低速 > 快速低速）
    """
    # 1. 读取达标服务器（已按解析后IP去重）
    qualified_servers = read_qualified_servers(city, ip_dir)
    qualified_count = len(qualified_servers)
    
    # 2. 读取低速服务器（已按解析后IP去重）
    slow_servers = read_slow_servers(city, ip_dir)
    
    if CONFIG['verbose']:
        print(f"  服务器统计: 达标 {qualified_count} 个, 低速 {len(slow_servers)} 个")
    
    # 3. 计算需要补充的数量
    if qualified_count >= target_count:
        result = [server for server, _ in qualified_servers[:target_count]]
        if CONFIG['verbose']:
            print(f"  达标服务器充足，使用 {len(result)} 个")
        return result
    
    need_count = target_count - qualified_count
    supplement_count = min(need_count, len(slow_servers))
    
    # 4. 构建结果列表
    result = [server for server, _ in qualified_servers]
    
    if supplement_count > 0:
        supplement_servers = [server for server, _ in slow_servers[:supplement_count]]
        result.extend(supplement_servers)
        if CONFIG['verbose']:
            print(f"  补充策略: 达标 {qualified_count} 个，补充 {supplement_count} 个低速")
    else:
        if CONFIG['verbose'] and qualified_count > 0:
            print(f"  补充策略: 达标 {qualified_count} 个，无低速可用")
        elif CONFIG['verbose'] and qualified_count == 0:
            print(f"  ⚠ 警告: 无任何可用服务器")
    
    return result


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


def normalize_channel_name(channel_name: str) -> str:
    name = channel_name.strip()
    for suffix in QUALITY_SUFFIXES:
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
            if name.endswith(('-', '_')):
                name = name[:-1].strip()
    return name.replace(' ', '')


def find_matched_channel(channel_name: str, channel_index: Dict) -> Optional[Dict]:
    normalized = normalize_channel_name(channel_name)
    if normalized in channel_index:
        return channel_index[normalized]
    if channel_name in channel_index:
        return channel_index[channel_name]
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


def is_channel_from_local_region(channel_name: str, local_city_name: str) -> bool:
    if not local_city_name:
        return False
    city_clean = local_city_name
    for op in ["电信", "移动", "联通"]:
        if city_clean.endswith(op):
            city_clean = city_clean[:-len(op)]
            break
    city_prefix = city_clean[:2] if len(city_clean) >= 2 else city_clean
    return channel_name.startswith(city_prefix)


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


def get_city_exclusion(city_name: str) -> Tuple[Set[str], bool, Set[str]]:
    config_file = Path("config/city_config.json")
    if not config_file.exists():
        return set(), True, set()
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cities_data = data.get("cities", data)
        for key, cfg in cities_data.items():
            if cfg.get("city") == city_name:
                exclude_prefixes = set()
                prefixes_str = cfg.get("exclude_prefixes", "")
                if prefixes_str:
                    for part in prefixes_str.split('|'):
                        if part.strip():
                            exclude_prefixes.add(part.strip())
                
                raw_keep = cfg.get("keep_unmatched", True)
                if isinstance(raw_keep, bool):
                    keep_unmatched = raw_keep
                elif isinstance(raw_keep, str):
                    keep_unmatched = raw_keep.lower() in ("true", "1", "yes")
                else:
                    keep_unmatched = True
                
                keep_keywords = set()
                raw_keywords = cfg.get("keep_keywords", [])
                if isinstance(raw_keywords, str):
                    for kw in raw_keywords.split('|'):
                        if kw.strip():
                            keep_keywords.add(kw.strip())
                elif isinstance(raw_keywords, list):
                    for kw in raw_keywords:
                        if isinstance(kw, str):
                            keep_keywords.add(kw)
                return exclude_prefixes, keep_unmatched, keep_keywords
    except Exception as e:
        if CONFIG['verbose']:
            print(f"  警告：读取城市排除配置失败: {e}")
    return set(), True, set()


def classify_channels(channels: List[Dict], city: str, channel_index: Dict,
                      region_index: Dict, local_first: bool,
                      exclude_prefixes: Set[str], keep_unmatched: bool, keep_keywords: Set[str],
                      name_style: str = "full") -> Tuple[List, List, List, List, List]:
    local_satellite = []
    local_other = []
    categorized = []
    region_based = []
    unmatched = []
    excluded_count = 0

    for channel in channels:
        channel_name = channel["name"]
        info = find_matched_channel(channel_name, channel_index)

        if info:
            idx = info["index"]
            excluded = False
            for prefix in exclude_prefixes:
                if idx.startswith(prefix):
                    excluded = True
                    break
            if excluded:
                excluded_count += 1
                continue

            is_local = is_channel_from_local_region(channel_name, city)
            is_satellite = "卫视" in channel_name
            display_name = info["full_name"] if name_style == "full" else info["short_name"]
            if local_first:
                if is_local and is_satellite:
                    local_satellite.append({**channel, "display_name": display_name})
                elif is_local:
                    local_other.append({**channel, "display_name": display_name})
                else:
                    categorized.append({**channel, "display_name": display_name, "sort_key": (info["index"], channel_name)})
            else:
                categorized.append({**channel, "display_name": display_name, "sort_key": (info["index"], channel_name)})
            continue

        region_code = get_region_index(channel_name, region_index)
        if region_code:
            region_based.append({**channel, "display_name": channel_name, "sort_key": (region_code, channel_name)})
            continue

        if keep_unmatched:
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
        print(f"  共排除 {excluded_count} 个频道")

    local_satellite.sort(key=lambda x: natural_sort_key(x["name"]))
    local_other.sort(key=lambda x: natural_sort_key(x["name"]))
    categorized.sort(key=lambda x: x["sort_key"])
    region_based.sort(key=lambda x: natural_sort_key(x["display_name"]))
    unmatched.sort(key=lambda x: natural_sort_key(x["display_name"]))

    return local_satellite, local_other, categorized, region_based, unmatched


def generate_playlist_for_city(city: str, channel_index: Dict, region_index: Dict,
                               local_first: bool, output_dir: str, max_servers: int = None,
                               use_all_ips: bool = False,
                               name_style: str = "full", skip_excluded: bool = True,
                               server_sources: List[str] = None,
                               verify: bool = True) -> Tuple[bool, int]:
    """
    生成单个城市的播放列表
    使用智能服务器选择策略，确保至少有 max_servers 个服务器
    """
    # 获取城市配置，获取组播地址用于验证
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
    
    # 目标服务器数量
    target_count = max_servers if max_servers and max_servers > 0 else CONFIG["default_max_servers"]
    
    # 智能获取最优服务器（返回原始格式）
    optimal_servers = get_optimal_servers(city, CONFIG["ip_dir"], target_count)
    
    if not optimal_servers:
        if CONFIG['verbose']:
            print(f"  {city}: 没有可用的服务器")
        return False, 0
    
    # 实时验证服务器可用性（直接使用原始格式）
    if verify and stream:
        valid_ips = verify_servers(optimal_servers, stream, CONFIG['verbose'], target_count)
        if not valid_ips:
            if CONFIG['verbose']:
                print(f"  {city}: 验证后无可用服务器")
            return False, 0
        top_ips = valid_ips
    else:
        top_ips = optimal_servers[:target_count]
    
    template_file = os.path.join(CONFIG["template_dir"], f"template_{city}.txt")
    if not os.path.exists(template_file):
        return False, 0
    
    channels = load_template_channels(template_file)
    if not channels:
        return False, 0

    if skip_excluded:
        exclude_prefixes, keep_unmatched, keep_keywords = get_city_exclusion(city)
        if CONFIG['verbose'] and (exclude_prefixes or (not keep_unmatched and keep_keywords)):
            print(f"  {city}: 排除索引前缀 {exclude_prefixes if exclude_prefixes else '无'}")
    else:
        exclude_prefixes = set()
        keep_unmatched = True
        keep_keywords = set()
        if CONFIG['verbose']:
            print(f"  {city}: 全量模式，使用全部频道")

    local_satellite, local_other, categorized, region_based, unmatched = classify_channels(
        channels, city, channel_index, region_index, local_first,
        exclude_prefixes, keep_unmatched, keep_keywords, name_style
    )

    output_lines = []
    for i, ip in enumerate(top_ips, 1):
        if i > 1:
            output_lines.append("")
        output_lines.append(f"{city}-组播{i},#genre#")
        for ch in local_satellite:
            output_lines.append(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}")
        if local_satellite and local_other:
            output_lines.append("")
        for ch in local_other:
            output_lines.append(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}")
        if (local_satellite or local_other) and categorized:
            output_lines.append("")
        for ch in categorized:
            output_lines.append(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}")
        if categorized and region_based:
            output_lines.append("")
        for ch in region_based:
            output_lines.append(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}")
        if region_based and unmatched:
            output_lines.append("")
        for ch in unmatched:
            output_lines.append(f"{ch['display_name']},{ch['url'].replace('ipipip', ip)}")

    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, f"{city}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines))
    
    if CONFIG['verbose']:
        print(f"  {city}: 使用 {len(top_ips)} 个服务器")
    
    return True, len(top_ips)


def generate_all_playlists(max_servers: int, local_first: bool, 
                           name_style: str = "full", sort_mode: str = "city_first",
                           server_sources: List[str] = None,
                           verify: bool = True) -> None:
    """为所有城市生成播放列表并汇总"""
    
    # 清理历史文件
    limited_dir = Path(CONFIG["output_dir_limited"])
    if limited_dir.exists():
        print(f"清理历史文件: {CONFIG['output_dir_limited']}/")
        for file in limited_dir.glob("*.txt"):
            file.unlink()
    else:
        limited_dir.mkdir(parents=True, exist_ok=True)
    
    all_dir = Path(CONFIG["output_dir_all"])
    if all_dir.exists():
        print(f"清理历史文件: {CONFIG['output_dir_all']}/")
        for file in all_dir.glob("*.txt"):
            file.unlink()
    else:
        all_dir.mkdir(parents=True, exist_ok=True)
    
    print("\n正在加载分类索引和地区编码...")
    channel_index = load_category_index()
    region_index = load_region_code()
    print(f"  加载分类: {len(channel_index)} 条")
    print(f"  加载地区编码: {len(region_index)} 条")
    
    sort_mode_name = "先城市后运营商" if sort_mode == "city_first" else "先运营商后城市"
    print(f"排序模式: {sort_mode_name}")
    
    # 显示服务器源配置
    source_names = []
    if 'good' in server_sources:
        source_names.append("优先服务器(good)")
    if 'precise' in server_sources:
        source_names.append("精确测试(precise)")
    if 'quick' in server_sources:
        source_names.append("快速测试(quick)")
    print(f"服务器源: {', '.join(source_names)}")
    print(f"实时验证: {'启用' if verify else '禁用'}")
    print(f"目标服务器数: {max_servers}")
    
    # 从 template 目录获取城市列表
    all_cities = get_cities_from_template_dir()
    if not all_cities:
        print("错误：未找到任何模板文件，请检查 template 目录下的 template_*.txt 文件")
        return
    
    cities = sort_cities(all_cities, sort_mode)
    print(f"\n共 {len(cities)} 个城市待处理")
    
    zubo_cities_set = load_zubo_cities()
    has_zubo_filter = len(zubo_cities_set) > 0
    if has_zubo_filter:
        print(f"汇总模式: 仅以下 {len(zubo_cities_set)} 个城市汇总到 zubo.txt")
        if CONFIG['verbose']:
            for city in sorted(zubo_cities_set):
                print(f"  - {city}")
    else:
        print("汇总模式: 所有城市汇总到 zubo.txt")
    
    # ==================== 模式1: 定制模式 ====================
    print("\n" + "=" * 60)
    print(f"模式1: 定制模式 (最多{max_servers}个IP/城市, 跳过排除频道)")
    print("=" * 60)
    success_count = 0
    for idx, city in enumerate(cities, 1):
        print(f"  [{idx}/{len(cities)}] 处理 {city}...", end=" ", flush=True)
        success, server_count = generate_playlist_for_city(
            city, channel_index, region_index, local_first,
            CONFIG["output_dir_limited"], max_servers,
            use_all_ips=False, name_style=name_style, skip_excluded=True,
            server_sources=server_sources, verify=verify
        )
        if success:
            success_count += 1
            print(f"✓ ({server_count}个IP)")
        else:
            print("✗")
    print(f"  模式1完成: {success_count}/{len(cities)} 个城市成功")
    
    # ==================== 模式2: 全量模式 ====================
    print("\n" + "=" * 60)
    print(f"模式2: 全量模式 (使用全部有效IP, 不跳过任何频道)")
    print("=" * 60)
    success_count = 0
    for idx, city in enumerate(cities, 1):
        print(f"  [{idx}/{len(cities)}] 处理 {city}...", end=" ", flush=True)
        success, server_count = generate_playlist_for_city(
            city, channel_index, region_index, local_first,
            CONFIG["output_dir_all"], max_servers,
            use_all_ips=True, name_style=name_style, skip_excluded=False,
            server_sources=server_sources, verify=verify
        )
        if success:
            success_count += 1
            print(f"✓ ({server_count}个IP)")
        else:
            print("✗")
    print(f"  模式2完成: {success_count}/{len(cities)} 个城市成功")
    
    # ==================== 合并所有组播源 ====================
    print("\n" + "=" * 60)
    print("合并所有组播源")
    print("=" * 60)
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    current_time = now.strftime("%Y/%m/%d %H:%M")
    os.makedirs("output", exist_ok=True)
    
    # 生成 zubo.txt (定制模式)
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
        print(f"  ✓ zubo.txt 已生成 (包含 {len(limited_contents)} 个城市)")
    else:
        print("  ✗ 未生成 zubo.txt (无有效内容)")
    
    # 生成 zubo_all.txt (全量模式)
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
        print(f"  ✓ zubo_all.txt 已生成 (包含 {len(all_contents)} 个城市)")
    else:
        print("  ✗ 未生成 zubo_all.txt (无有效内容)")
    
    print("\n✅ 所有播放列表生成完成！")


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
    parser.add_argument('--verify', action='store_true', dest='force_verify',
                        help='强制启用实时验证（覆盖配置文件）')
    args = parser.parse_args()
    
    if args.verbose:
        CONFIG['verbose'] = True
    local_first = args.local_first
    name_style = args.name_style
    sort_mode = args.sort_mode
    
    # 验证开关：命令行优先，然后配置文件，默认 True
    if args.force_verify:
        verify = True
    elif args.no_verify:
        verify = False
    else:
        verify = CONFIG.get('verify', True)

    sort_mode_name = "先城市后运营商" if sort_mode == "city_first" else "先运营商后城市"
    print("IPTVZ 播放列表生成工具（带实时验证版）")
    print("=" * 60)
    print(f"目标服务器数: {args.num}")
    print(f"本地频道优先: {'是' if local_first else '否'}")
    print(f"频道名称样式: {'简称' if name_style == 'short' else '全称'}")
    print(f"排序模式: {sort_mode_name}")
    print(f"详细模式: {'是' if CONFIG['verbose'] else '否'}")
    print(f"实时验证: {'启用' if verify else '禁用'}")
    
    source_names = []
    if 'good' in args.server_sources:
        source_names.append("优先服务器(good)")
    if 'precise' in args.server_sources:
        source_names.append("精确测试(precise)")
    if 'quick' in args.server_sources:
        source_names.append("快速测试(quick)")
    print(f"服务器源: {', '.join(source_names)}")

    cities = get_cities_from_template_dir()
    
    if not cities:
        print("错误：未找到任何模板文件，请检查 template 目录下的 template_*.txt 文件")
        return   

    if args.all:
        print(f"\n开始为全部 {len(cities)} 个城市生成播放列表...")
        generate_all_playlists(args.num, local_first, name_style, sort_mode,
                               server_sources=args.server_sources, verify=verify)
        return

    if args.city is not None:
        if 1 <= args.city <= len(cities):
            city_name = cities[args.city - 1]
            print(f"\n处理城市: {city_name}")
            channel_index = load_category_index()
            region_index = load_region_code()
            success, _ = generate_playlist_for_city(city_name, channel_index, region_index, local_first,
                                                    CONFIG["output_dir_single"], args.num,
                                                    use_all_ips=False, name_style=name_style, skip_excluded=True,
                                                    server_sources=args.server_sources, verify=verify)
            if success:
                print(f"✅ {city_name} 播放列表生成完成！")
            else:
                print(f"❌ {city_name} 播放列表生成失败")
        else:
            print(f"错误：无效的城市编号 {args.city}")
            print(f"请输入 1-{len(cities)} 之间的数字")
        return

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
                print(f"\n开始为全部 {len(cities)} 个城市生成播放列表...")
                generate_all_playlists(args.num, local_first, name_style, sort_mode,
                                       server_sources=args.server_sources, verify=verify)
                return
            else:
                city_num = int(choice)
                if 1 <= city_num <= len(cities):
                    city_name = cities[city_num - 1]
                    print(f"\n处理城市: {city_name}")
                    channel_index = load_category_index()
                    region_index = load_region_code()
                    success, _ = generate_playlist_for_city(city_name, channel_index, region_index, local_first,
                                                            CONFIG["output_dir_single"], args.num,
                                                            use_all_ips=False, name_style=name_style, skip_excluded=True,
                                                            server_sources=args.server_sources, verify=verify)
                    if success:
                        print(f"✅ {city_name} 播放列表生成完成！")
                    else:
                        print(f"❌ {city_name} 播放列表生成失败")
                    
                    print()
                    continue_choice = input("是否继续处理其他城市？(y/N): ").strip().lower()
                    if continue_choice != 'y':
                        print("退出程序")
                        return
                    print()
                else:
                    print(f"无效选择，请输入 1-{len(cities)} 之间的数字")
        except ValueError:
            print("请输入有效的数字或直接回车")
        except KeyboardInterrupt:
            print("\n用户中断")
            return


if __name__ == "__main__":
    main()