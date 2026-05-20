#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
udpxy 播放列表生成工具
功能：从测速结果生成可用的组播源播放列表
"""

import os
import sys
import argparse
import datetime
import json
import re
import shutil
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Set


# ==================== 配置参数 ====================
CONFIG = {
    "output_dir_limited": "output/limited",
    "output_dir_all": "output/all",
    "output_dir_single": "output/single",
    "default_max_servers": 8,
    "default_server_sources": ['good', 'precise', 'quick'],  # 默认服务器源
    "save_category": False,
    "verbose": False,
    "local_first": True,
    "ip_dir": "ip",
    "template_dir": "template",
    "rtp_dir": "rtp",
}

QUALITY_SUFFIXES = ['HD', '-HD', 'hd', '-hd', '高清', '-高清', 'H264', 'H265', 'HEVC']

# ==================== 省份排序顺序（按拼音） ====================
REGIONS = [
    "安徽", "北京", "重庆", "福建", "甘肃", "广东", "广西", "贵州", "海南", "河北",
    "河南", "黑龙江", "湖北", "湖南", "吉林", "江苏", "江西", "辽宁", "内蒙古", "宁夏",
    "青海", "山东", "山西", "陕西", "上海", "四川", "天津", "西藏", "新疆", "云南",
    "浙江", "台湾", "香港", "澳门"
]


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
    
    # 注意：不在这里排序，由调用方使用 sort_cities()
    return cities


def get_city_sort_key(city_name: str, sort_mode: str = "city_first") -> tuple:
    """
    获取城市排序键
    sort_mode: city_first(先城市后运营商) 或 operator_first(先运营商后城市)
    """
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
    """对城市列表进行排序"""
    return sorted(cities, key=lambda x: get_city_sort_key(x, sort_mode))


def load_zubo_cities(zubo_cities_file: str = "config/zubo_cities.txt") -> Set[str]:
    """加载需要汇总的城市列表，支持多种编码"""
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
    """打印城市列表（从模板文件获取，使用自定义排序）"""
    cities = get_cities_from_template_dir()
    if not cities:
        return "未找到任何模板文件（template/template_*.txt）"
    
    # 使用自定义排序
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


# ==================== 速度解析函数 ====================
def parse_speed_value(speed_str: str, source_type: str) -> float:
    """
    将速度字符串转换为数值（字节/秒）
    quick 格式: "1.5M", "256k", "512B"
    precise 格式: "2.35 MB/s", "128.50 KB/s"
    """
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
    else:  # precise
        match = re.match(r"([\d.]+)\s+([KM]B/s)", speed_str)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            if unit == "MB/s":
                return value * 1024 * 1024
            elif unit == "KB/s":
                return value * 1024
    return 0


# ==================== 服务器解析 ====================
def parse_result_ips(city: str, max_servers: int = 0,
                     server_sources: List[str] = None) -> Tuple[List[str], int]:
    """解析可用IP，按优先级和速度排序"""
    if server_sources is None:
        server_sources = CONFIG.get('default_server_sources', ['good', 'precise', 'quick'])
    
    servers_with_speed = []
    seen = set()
    
    source_priority = {
        "good": 1,
        "precise": 2,
        "quick": 3,
    }
    
    # 1. 读取 _ip_good.txt（最高优先级）
    if 'good' in server_sources:
        good_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_good.txt")
        if os.path.exists(good_file):
            with open(good_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and ':' in line and not line.startswith('#'):
                        server = line.replace('http://', '').replace('https://', '')
                        if server not in seen:
                            seen.add(server)
                            servers_with_speed.append((server, float('inf'), source_priority["good"]))
    
    # 2. 读取精确测试结果（_ip_precise.txt）
    if 'precise' in server_sources:
        precise_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_precise.txt")
        if os.path.exists(precise_file):
            with open(precise_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '\t' in line:
                        parts = line.split('\t')
                        if len(parts) >= 2:
                            server = parts[0].strip().replace('http://', '').replace('https://', '')
                            speed_str = parts[1].strip()
                            if ':' in server and server not in seen:
                                seen.add(server)
                                speed_value = parse_speed_value(speed_str, 'precise')
                                servers_with_speed.append((server, speed_value, source_priority["precise"]))
    
    # 3. 读取快速测试结果（_ip_quick.txt）
    if 'quick' in server_sources:
        quick_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_quick.txt")
        if not os.path.exists(quick_file):
            quick_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_result.txt")
        
        if os.path.exists(quick_file):
            with open(quick_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '\t' in line:
                        parts = line.split('\t')
                        if len(parts) >= 2:
                            server = parts[0].strip().replace('http://', '').replace('https://', '')
                            speed_str = parts[1].strip()
                            if ':' in server and server not in seen:
                                seen.add(server)
                                speed_value = parse_speed_value(speed_str, 'quick')
                                servers_with_speed.append((server, speed_value, source_priority["quick"]))
    
    # 排序：先按优先级，再按速度（速度高的在前）
    servers_with_speed.sort(key=lambda x: (x[2], -x[1]))
    
    combined = [s for s, _, _ in servers_with_speed]
    total = len(combined)
    
    if max_servers > 0 and total > max_servers:
        combined = combined[:max_servers]
    
    return combined, total


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
                               server_sources: List[str] = None) -> Tuple[bool, int]:
    """
    生成单个城市的播放列表
    返回: (是否成功, 生成的服务器数量)
    """
    if use_all_ips:
        top_ips, total_available = parse_result_ips(city, 0, server_sources)
    else:
        top_ips, total_available = parse_result_ips(city, max_servers, server_sources)
    
    if not top_ips:
        return False, 0
    
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
        source_names = []
        if 'good' in server_sources:
            source_names.append("good")
        if 'precise' in server_sources:
            source_names.append("precise")
        if 'quick' in server_sources:
            source_names.append("quick")
        print(f"  {city}: 使用 {len(top_ips)} 个IP (共 {total_available} 个可用, 来源: {', '.join(source_names)})")
    return True, len(top_ips)


def generate_all_playlists(max_servers: int, local_first: bool, 
                           name_style: str = "full", sort_mode: str = "city_first",
                           server_sources: List[str] = None) -> None:
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
            server_sources=server_sources
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
            CONFIG["output_dir_all"], None,
            use_all_ips=True, name_style=name_style, skip_excluded=False,
            server_sources=server_sources
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
    """将 txt 格式转换为 m3u 格式"""
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
    parser = argparse.ArgumentParser(description='udpxy 播放列表生成工具')
    parser.add_argument('city', type=int, nargs='?', default=None,
                        help='城市编号（不指定则显示列表选择）')
    parser.add_argument('-n', '--num', type=int, default=CONFIG['default_max_servers'],
                        help=f'最多使用的服务器数量，默认{CONFIG["default_max_servers"]}个')
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
    args = parser.parse_args()
    
    # 设置配置
    if args.verbose:
        CONFIG['verbose'] = True
    local_first = args.local_first
    name_style = args.name_style
    sort_mode = args.sort_mode

    # 打印配置信息
    sort_mode_name = "先城市后运营商" if sort_mode == "city_first" else "先运营商后城市"
    print("IPTVZ 播放列表生成工具")
    print("=" * 60)
    print(f"最大服务器数: {args.num}")
    print(f"本地频道优先: {'是' if local_first else '否'}")
    print(f"频道名称样式: {'简称' if name_style == 'short' else '全称'}")
    print(f"排序模式: {sort_mode_name}")
    print(f"详细模式: {'是' if CONFIG['verbose'] else '否'}")
    
    # 显示服务器源配置
    source_names = []
    if 'good' in args.server_sources:
        source_names.append("优先服务器(good)")
    if 'precise' in args.server_sources:
        source_names.append("精确测试(precise)")
    if 'quick' in args.server_sources:
        source_names.append("快速测试(quick)")
    print(f"服务器源: {', '.join(source_names)}")

     # 获取城市列表（从 template 目录）
    cities = get_cities_from_template_dir()
    
    if not cities:
        print("错误：未找到任何模板文件，请检查 template 目录下的 template_*.txt 文件")
        return   

    # 情况1: 指定 --all 参数
    if args.all:
        print(f"\n开始为全部 {len(cities)} 个城市生成播放列表...")
        generate_all_playlists(args.num, local_first, name_style, sort_mode,
                               server_sources=args.server_sources)
        return

    # 情况2: 命令行指定城市编号
    if args.city is not None:
        if 1 <= args.city <= len(cities):
            city_name = cities[args.city - 1]
            print(f"\n处理城市: {city_name}")
            channel_index = load_category_index()
            region_index = load_region_code()
            success, _ = generate_playlist_for_city(city_name, channel_index, region_index, local_first,
                                                    CONFIG["output_dir_single"], args.num,
                                                    use_all_ips=False, name_style=name_style, skip_excluded=True,
                                                    server_sources=args.server_sources)
            if success:
                print(f"✅ {city_name} 播放列表生成完成！")
            else:
                print(f"❌ {city_name} 播放列表生成失败")
        else:
            print(f"错误：无效的城市编号 {args.city}")
            print(f"请输入 1-{len(cities)} 之间的数字")
        return

    # 情况3: 交互模式
    print("\n可用的城市列表:")
    print(print_city_list(sort_mode))  # 传入排序模式
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
                                       server_sources=args.server_sources)
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
                                                            server_sources=args.server_sources)
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