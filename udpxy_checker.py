#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
udpxy 服务器统一检测工具
功能：
1. 快速测试：下载64KB测速，快速筛选可用服务器
2. 精确测试：下载1MB测速，获取详细性能数据
支持从 ip/{城市}_ip.txt 读取服务器列表
支持结果输出到 ip/{城市}_ip_quick.txt（快速）或 ip/{城市}_ip_precise.txt（精确）
支持自动模式（用于CI/CD）
分级保存：有效服务器保存到主目录，低速服务器保存到 slow/ 子目录
"""

import os
import sys
import re
import socket
import time
import glob
import json
from datetime import datetime
import threading
import argparse
from typing import List, Tuple, Dict, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ==================== 配置参数 ====================
CONFIG = {
    "test_mode": "precise", # 快速测试或精确测试 （默认精确测试 precise ， 快速测试 quick ）
    "quick": {
        "download_size": 64 * 1024,
        "chunk_size": 8192,
        "timeout_connect": 3,
        "timeout_read": 10,
        "max_workers_connect": 100,
        "max_workers_speed": 50,
        "retry_times": 1,
        "output_suffix": "quick",
        "output_name": "快速测试",
        "min_speed_kbps": 200,      # 最低速度要求 200KB/s（约1.6Mbps）
    },
    "precise": {
        "download_size": 1024 * 1024,
        "chunk_size": 8192,
        "timeout_connect": 5,
        "timeout_read": 15,
        "max_workers_connect": 100,
        "max_workers_speed": 50,
        "retry_times": 2,
        "output_suffix": "precise",
        "output_name": "精确测试",
        "min_speed_kbps": 500,      # 最低速度要求 500KB/s（约4Mbps）
    },
    "city_config_file": "config/city_config.json",
    "ip_dir": "ip",
    "slow_dir": "ip/slow",          # 低速服务器保存目录
    "logs_dir": "logs",
    "max_servers": 0,
    "auto_mode": False,
    "verbose": True,
}


CITY_CONFIG = {}


def load_city_config():
    global CITY_CONFIG
    config_file = CONFIG["city_config_file"]
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        CITY_CONFIG = data.get("cities", data)

# ==================== 省份排序顺序（按拼音） ====================
REGIONS = [
    "安徽", "北京", "重庆", "福建", "甘肃", "广东", "广西", "贵州", "海南", "河北",
    "河南", "黑龙江", "湖北", "湖南", "吉林", "江苏", "江西", "辽宁", "内蒙古", "宁夏",
    "青海", "山东", "山西", "陕西", "上海", "四川", "天津", "西藏", "新疆", "云南",
    "浙江", "台湾", "香港", "澳门"
]

def get_all_cities(ip_dir: str = "ip") -> List[str]:
    """获取所有有 ip.txt 文件的城市（按 REGIONS 顺序排序）"""
    cities = set()
    
    for file_path in glob.glob(os.path.join(ip_dir, "*_ip.txt")):
        filename = os.path.basename(file_path)
        city = filename.replace("_ip.txt", "")
        skip_patterns = ["存档", "template", "ipresu", "ipgo"]
        if city not in skip_patterns and not city.startswith("存档"):
            cities.add(city)
    
    # 按省份+运营商排序
    operator_order = {"电信": 1, "联通": 2, "移动": 3}
    region_order = {region: idx for idx, region in enumerate(REGIONS)}
    
    def sort_key(city: str) -> tuple:
        province = city
        operator = ""
        for op in operator_order.keys():
            if city.endswith(op):
                province = city[:-len(op)]
                operator = op
                break
        province_index = region_order.get(province, 999)
        operator_index = operator_order.get(operator, 99)
        return (province_index, operator_index)
    
    return sorted(cities, key=sort_key)


def print_city_list(cities: List[str]) -> None:
    """打印格式化的城市列表"""
    if not cities:
        print("未找到任何城市")
        return
    
    print("\n可用的城市列表:")
    print("-" * 60)
    
    cols = 5
    for i in range(0, len(cities), cols):
        row = cities[i:i+cols]
        row_parts = []
        for j, city in enumerate(row):
            idx = i + j + 1
            row_parts.append(f"{idx:2d}. {city:<14}")
        print(' '.join(row_parts))
    
    print("-" * 60)
    print(f"共 {len(cities)} 个城市")


def print_city_list_with_status(mode):
    """打印带状态标记的城市列表"""
    cities = get_all_cities()
    if not cities:
        return "未找到任何城市文件"
    
    config = CONFIG[mode]
    lines = []
    cols = 5
    for i in range(0, len(cities), cols):
        row = cities[i:i+cols]
        row_text = ""
        for j, city in enumerate(row):
            idx = i + j + 1
            result_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_{config['output_suffix']}.txt")
            has_result = "✓" if os.path.exists(result_file) else " "
            row_text += f"{idx:2d}.{has_result}{city}\t"
        lines.append(row_text)
    
    lines.append(f"  (标记 ✓ 表示已有{config['output_name']}结果)")
    return "\n".join(lines)


def get_city_by_name(city_name):
    for key, cfg in CITY_CONFIG.items():
        if cfg.get("city") == city_name:
            return {"city": city_name, "stream": cfg.get("stream")}
    return {"city": city_name, "stream": None}


def resolve_host_to_ip(host_port):
    try:
        if ':' not in host_port:
            return host_port, False
        host, port = host_port.rsplit(':', 1)
        port = int(port)
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            return host_port, True
        ip = socket.gethostbyname(host)
        return f"{ip}:{port}", True
    except:
        return host_port, False


def test_port_connect(ip_port, timeout=2):
    resolved, ok = resolve_host_to_ip(ip_port)
    if not ok:
        return False
    ip, port = resolved.split(":")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, int(port))) == 0
    except:
        return False


def speed_test(ip_port, stream, mode='quick'):
    config = CONFIG[mode]
    url = f"http://{ip_port}/rtp/{stream}"
    retry = config["retry_times"]
    
    for attempt in range(retry + 1):
        try:
            start = time.time()
            resp = requests.get(url, timeout=(config["timeout_connect"], config["timeout_read"]), stream=True)
            resp.raise_for_status()
            
            downloaded = 0
            target = config["download_size"]
            
            if mode == 'quick':
                max_chunks = target // config["chunk_size"]
                for _ in range(max_chunks):
                    chunk = resp.raw.read(config["chunk_size"])
                    if not chunk:
                        break
                    downloaded += len(chunk)
            else:
                for chunk in resp.iter_content(chunk_size=config["chunk_size"]):
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded >= target:
                        break
            
            elapsed = time.time() - start
            if elapsed > 0 and downloaded > 0:
                speed_bps = downloaded / elapsed
                if mode == 'quick':
                    if speed_bps >= 1024 * 1024:
                        speed_str = f"{speed_bps / (1024 * 1024):.1f}M"
                    elif speed_bps >= 1024:
                        speed_str = f"{speed_bps / 1024:.1f}k"
                    else:
                        speed_str = f"{speed_bps:.0f}B"
                else:
                    if speed_bps >= 1024 * 1024:
                        speed_str = f"{speed_bps / (1024 * 1024):.2f} MB/s"
                    else:
                        speed_str = f"{speed_bps / 1024:.2f} KB/s"
                return speed_str
            else:
                return "[X]"
        except Exception:
            if attempt == retry:
                return "[X]"
            time.sleep(1)
    return "[X]"


def parse_speed_value(speed_str, mode='quick'):
    if speed_str == "[X]":
        return 0
    
    if mode == 'quick':
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


def parse_speed_to_kbps(speed_str: str, mode: str = 'quick') -> float:
    """将速度字符串转换为 KB/s"""
    if speed_str == "[X]":
        return 0
    
    if mode == 'quick':
        match = re.match(r"([\d.]+)([MkB])", speed_str)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            if unit == "M":
                return value * 1024  # MB/s -> KB/s
            elif unit == "k":
                return value         # KB/s
            elif unit == "B":
                return value / 1024  # B/s -> KB/s
    else:
        match = re.match(r"([\d.]+)\s+([KM]B/s)", speed_str)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            if unit == "MB/s":
                return value * 1024  # MB/s -> KB/s
            elif unit == "KB/s":
                return value         # KB/s
    return 0


def parse_servers(city, max_servers=0):
    ip_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip.txt")
    if not os.path.exists(ip_file):
        return [], {}
    
    with open(ip_file, 'r', encoding='utf-8') as f:
        raw_ips = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    
    if not raw_ips:
        return [], {}
    
    ip_mapping = {}
    ip_list = []
    for ip_port in raw_ips:
        resolved, ok = resolve_host_to_ip(ip_port)
        if ok:
            ip_list.append(resolved)
            ip_mapping[resolved] = ip_port
        else:
            ip_list.append(ip_port)
            ip_mapping[ip_port] = ip_port
    
    ip_list = sorted(set(ip_list))
    if max_servers > 0 and len(ip_list) > max_servers:
        ip_list = ip_list[:max_servers]
    
    return ip_list, ip_mapping


def read_existing_history(city, mode):
    history_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_history.txt")
    existing = {}
    
    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 4:
                    server = parts[0]
                    server_norm = server.replace('http://', '').replace('https://', '')
                    status = parts[1]
                    success = int(parts[2])
                    fail = int(parts[3])
                    existing[server_norm] = {"status": status, "success": success, "fail": fail}
    return existing


def update_history(existing, results, mode):
    """更新历史记录：只要通就算有效，不管速度"""
    for server, speed in results:
        # 只去除 http:// 用于内部比较，不修改原始数据
        server_norm = server.replace('http://', '').replace('https://', '')
        if server_norm not in existing:
            existing[server_norm] = {"status": "", "success": 0, "fail": 0}
        
        if speed != "[X]":
            existing[server_norm]["status"] = "有效"
            existing[server_norm]["success"] += 1
        else:
            existing[server_norm]["fail"] += 1
            if existing[server_norm]["success"] == 0:
                existing[server_norm]["status"] = "无效"
    
    return existing


def save_results(city, results, existing, mode):
    """分级保存测试结果（智能阈值策略）"""
    config = CONFIG[mode]
    current_time = datetime.now().strftime('%Y%m%d_%H%M')
    min_speed_kbps = config.get("min_speed_kbps", 0)
    
    # 确保目录存在
    os.makedirs(CONFIG["ip_dir"], exist_ok=True)
    os.makedirs(CONFIG["slow_dir"], exist_ok=True)
    
    # 分类服务器（按速度）
    high_speed_servers = []   # 速度达标
    low_speed_servers = []    # 速度未达标但有数据
    
    for server, speed in results:
        if speed == "[X]":
            continue
        
        speed_kbps = parse_speed_to_kbps(speed, mode)
        if min_speed_kbps > 0 and speed_kbps < min_speed_kbps:
            low_speed_servers.append((server, speed, speed_kbps))
        else:
            high_speed_servers.append((server, speed, speed_kbps))
    
    # 按速度排序
    high_speed_servers.sort(key=lambda x: x[2], reverse=True)
    low_speed_servers.sort(key=lambda x: x[2], reverse=True)
    
    # 智能阈值策略：
    # 1. 有达标服务器：保留所有达标服务器
    #    - 如果达标服务器只有1个，再保留1个最好的低速服务器（作为备用）
    #    - 如果达标服务器 >= 2个，只保留达标服务器
    # 2. 没有达标服务器：保留2个最好的低速服务器
    valid_servers = []
    
    if high_speed_servers:
        # 有达标的服务器，保留所有达标的
        valid_servers = high_speed_servers.copy()
        
        # 如果只有1个达标服务器，再保留1个最好的低速服务器
        if len(high_speed_servers) == 1 and low_speed_servers:
            best_low = low_speed_servers[0]
            valid_servers.append(best_low)
            if CONFIG['verbose']:
                print(f"    达标服务器仅1个，额外保留1个最佳低速服务器: {best_low[1]} ({best_low[2]:.1f} KB/s)")
    elif low_speed_servers:
        # 没有达标的，但有低速服务器，保留2个最好的低速服务器
        keep_count = min(2, len(low_speed_servers))
        valid_servers = low_speed_servers[:keep_count]
        if CONFIG['verbose']:
            print(f"    没有速度达标的服务器，保留 {keep_count} 个最佳低速服务器")
            for sv in valid_servers:
                print(f"      - {sv[1]} ({sv[2]:.1f} KB/s)")
    
    # 1. 保存有效服务器
    result_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_{config['output_suffix']}.txt")
    
    if valid_servers:
        # 按速度排序
        valid_servers.sort(key=lambda x: x[2], reverse=True)
        
        with open(result_file, 'w', encoding='utf-8') as f:
            f.write(f"# {current_time}_{config['output_suffix']}\n")
            f.write("# 服务器地址\t速度\n")
            for server, speed, _ in valid_servers:
                f.write(f"{server}\t{speed}\n")
    else:
        if os.path.exists(result_file):
            os.remove(result_file)
            if CONFIG['verbose']:
                print(f"    没有可用服务器，已删除 {os.path.basename(result_file)}")
    
    # 2. 保存低速服务器到 slow/ 子目录（供分析参考）
    # 确定哪些低速服务器需要保存到 slow 目录
    valid_low_set = set(valid_servers)
    slow_for_save = [s for s in low_speed_servers if s not in valid_low_set]
    
    if slow_for_save:
        slow_file = os.path.join(CONFIG["slow_dir"], f"{city}_ip_{config['output_suffix']}_slow.txt")
        
        with open(slow_file, 'w', encoding='utf-8') as f:
            f.write(f"# {current_time}_{config['output_suffix']}_slow\n")
            f.write("# 服务器地址\t速度\n")
            for server, speed, _ in slow_for_save:
                f.write(f"{server}\t{speed}\n")
        
        if CONFIG['verbose']:
            print(f"    低速服务器: {len(slow_for_save)} 个 (已保存到 slow/{city}_ip_{config['output_suffix']}_slow.txt)")
    elif low_speed_servers and CONFIG['verbose']:
        print(f"    所有低速服务器均已作为有效服务器保留")
    
    # 3. 更新历史记录（不管速度，只要通就算有效）
    existing = update_history(existing, results, mode)
    
    history_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_history.txt")
    with open(history_file, 'w', encoding='utf-8') as f:
        f.write(f"# {current_time}_{config['output_suffix']}_history\n")
        f.write("# 服务器地址\t状态\t有效次数\t无效次数\n")
        for server_norm, data in existing.items():
            f.write(f"{server_norm}\t{data['status']}\t{data['success']}\t{data['fail']}\n")
    
    return len(valid_servers)


def process_city(city_name, mode, max_servers=0):
    """处理单个城市，返回 (是否成功, 有效服务器数量)"""
    config = CONFIG[mode]
    city_info = get_city_by_name(city_name)
    if not city_info:
        if CONFIG['verbose']:
            print(f"错误：无效城市 {city_name}")
        return False, 0
    
    stream = city_info["stream"]
    
    matched_stream = None
    for key, cfg in CITY_CONFIG.items():
        if cfg.get("city") == city_name:
            matched_stream = cfg.get("stream")
            break
    
    if not matched_stream:
        if CONFIG['verbose']:
            print(f"  ✗ 未匹配到组播地址，跳过检测")
        return False, 0
    
    if CONFIG['verbose']:
        print(f"  ✓ 匹配到组播地址：{matched_stream}")
    
    ip_list, ip_mapping = parse_servers(city_name, max_servers)
    if not ip_list:
        if CONFIG['verbose']:
            print(f"  ✗ 没有可用的IP地址")
        return False, 0
    
    if CONFIG['verbose']:
        print(f"  待测IP数量：{len(ip_list)}")
        print("  正在检测端口连通性...")
    
    good_ips = set()
    with ThreadPoolExecutor(max_workers=config["max_workers_connect"]) as ex:
        futures = {ex.submit(test_port_connect, ip): ip for ip in ip_list}
        for f in as_completed(futures):
            if f.result():
                good_ips.add(futures[f])
    
    if CONFIG['verbose']:
        print(f"  端口可用：{len(good_ips)} 个")
    
    existing = read_existing_history(city_name, mode)
    results = []
    
    if not good_ips:
        if CONFIG['verbose']:
            print(f"  ✗ 没有可用的端口，跳过测速，但记录所有IP为无效")
        for ip in ip_list:
            orig = ip_mapping.get(ip, ip)
            results.append((orig, "[X]"))
    else:
        if CONFIG['verbose']:
            print(f"  正在{config['output_name']}...")
        test_list = [(ip, ip_mapping.get(ip, ip)) for ip in ip_list if ip in good_ips]
        with ThreadPoolExecutor(max_workers=config["max_workers_speed"]) as ex:
            futures = {}
            for ip, orig in test_list:
                futures[ex.submit(speed_test, orig, stream, mode)] = (ip, orig)
            
            for f in as_completed(futures):
                ip, orig = futures[f]
                speed = f.result()
                results.append((orig, speed))
                if CONFIG['verbose']:
                    print(f"    {orig}\t{speed}")
        
        for ip in ip_list:
            if ip not in good_ips:
                orig = ip_mapping.get(ip, ip)
                results.append((orig, "[X]"))
    
    results.sort(key=lambda x: parse_speed_value(x[1], mode), reverse=True)
    valid_count = save_results(city_name, results, existing, mode)
    
    if CONFIG['verbose']:
        total = len(results)
        invalid = len([r for r in results if r[1] == "[X]"])
        slow = total - valid_count - invalid
        print(f"\n  ✓ {config['output_name']}完成！")
        print(f"    总服务器: {total}")
        print(f"    有效(速度达标): {valid_count}")
        if slow > 0:
            print(f"    低速(未达标): {slow}")
        if invalid > 0:
            print(f"    无效: {invalid}")
    
    return True, valid_count


def process_all_cities(mode, max_servers=0, cities_filter=None):
    """处理所有城市，返回 (成功城市数, 总有效服务器数)"""
    cities = get_all_cities()
    
    # 筛选指定城市
    if cities_filter:
        cities = [c for c in cities if c in cities_filter]
        if not cities:
            print(f"错误：未找到指定城市 {', '.join(cities_filter)}")
            return 0, 0
    
    config = CONFIG[mode]
    
    if CONFIG['verbose']:
        print(f"\n开始{config['output_name']} - 共 {len(cities)} 个城市")
        print("=" * 60)
    
    success_count = 0
    total_servers = 0
    failed_cities = []
    
    for i, city in enumerate(cities, 1):
        if CONFIG['verbose']:
            print(f"\n[{i}/{len(cities)}] 处理城市：{city}")
        
        success, server_count = process_city(city, mode, max_servers)
        
        if success:
            success_count += 1
            total_servers += server_count
        else:
            failed_cities.append(city)
    
    if CONFIG['verbose']:
        print("\n" + "=" * 60)
        print(f"{config['output_name']}完成")
        print(f"成功: {success_count}/{len(cities)} 个城市")
        print(f"有效服务器总数: {total_servers}")
        
        if failed_cities:
            print(f"失败: {len(failed_cities)} 个")
            log_dir = CONFIG["logs_dir"]
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"failed_cities_{mode}.txt")
            with open(log_file, 'w', encoding='utf-8-sig') as f:
                for city in failed_cities:
                    f.write(f"{city}\n")
    
    return success_count, total_servers


def single_city_mode(mode, max_servers=0):
    """交互模式（支持连续处理多个城市）"""
    config = CONFIG[mode]
    cities = get_all_cities()
    
    while True:
        print("\n" + "-" * 60)
        print("可用的城市列表:")
        print(print_city_list_with_status(mode))
        print("-" * 60)
        
        try:
            choice = input(f"\n请选择城市编号（直接回车=全部测试，q=退出）: ").strip()
            
            if choice.lower() == 'q':
                print("\n退出程序")
                break
            
            if choice == '':
                # 直接回车：全部测试
                process_all_cities(mode, max_servers)
                continue
            
            # 处理单个或多个城市
            city_num = int(choice)
            if 1 <= city_num <= len(cities):
                city_name = cities[city_num - 1]
                print(f"\n处理城市: {city_name}")
                print("-" * 40)
                process_city(city_name, mode, max_servers)
                print(f"\n[完成] {city_name} 处理完成，返回城市列表")
            else:
                print(f"无效选择，请输入 1-{len(cities)} 之间的数字")
                
        except ValueError:
            # 输入的不是数字，检查是否为多个编号
            if ',' in choice or '-' in choice:
                # 解析多个编号
                selected_cities = parse_city_selection(choice, cities)
                if selected_cities:
                    for city_name in selected_cities:
                        print(f"\n处理城市: {city_name}")
                        print("-" * 40)
                        process_city(city_name, mode, max_servers)
                    print(f"\n[完成] 所有选中城市处理完成")
                else:
                    print("请输入有效的城市编号（如：1,3,5 或 1-5）")
            else:
                print("请输入有效的数字，或直接回车全部测试")
        except KeyboardInterrupt:
            print("\n\n退出程序")
            break


def parse_city_selection(choice: str, cities: List[str]) -> List[str]:
    """解析城市选择输入，支持单个、多个、范围"""
    selected = []
    parts = choice.split(',')
    
    for part in parts:
        part = part.strip()
        if '-' in part:
            try:
                start, end = part.split('-')
                start_idx = int(start) - 1
                end_idx = int(end) - 1
                if 0 <= start_idx < len(cities) and 0 <= end_idx < len(cities):
                    for idx in range(start_idx, end_idx + 1):
                        selected.append(cities[idx])
                else:
                    print(f"范围 {part} 超出范围")
            except ValueError:
                print(f"无效范围: {part}")
        elif part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(cities):
                selected.append(cities[idx])
            else:
                print(f"无效编号: {part}")
    
    return selected


def main():
    parser = argparse.ArgumentParser(description='udpxy 服务器统一检测工具')
    parser.add_argument('-m', '--mode', choices=['quick', 'precise'], 
                        default=CONFIG["test_mode"],
                        help=f'测试模式: quick(快速测试) 或 precise(精确测试) (默认: {CONFIG["test_mode"]})')
    parser.add_argument('-c', '--city', type=int, default=None,
                        help='城市编号（不指定则进入交互模式）')
    parser.add_argument('-n', '--num', type=int, default=CONFIG["max_servers"],
                        help=f'最大服务器数量 (默认: {CONFIG["max_servers"]}, 0表示全部)')
    parser.add_argument('-a', '--all', action='store_true',
                        help='为所有城市生成测试结果（非交互模式）')
    parser.add_argument('--auto', action='store_true',
                        help='自动模式（用于CI/CD，无交互）')
    parser.add_argument('--cities', nargs='+',
                        help='指定检测的城市列表（如：--cities 上海电信 北京移动）')
    parser.add_argument('--min-speed', type=int, default=None,
                        help='最低速度要求（KB/s），覆盖配置文件中的默认值')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='静默模式，减少输出')
    args = parser.parse_args()
    
    # 设置配置
    CONFIG["test_mode"] = args.mode
    CONFIG["max_servers"] = args.num
    CONFIG["auto_mode"] = args.auto
    if args.quiet:
        CONFIG["verbose"] = False
    else:
        CONFIG["verbose"] = True
    
    config = CONFIG[args.mode]
    
    print("=" * 60)
    print(f"udpxy {config['output_name']}工具")
    print(f"功能：{config['output_name']}检测udpxy服务器可用性和性能")
    print("=" * 60)
    print(f"测试模式: {config['output_name']}")
    print(f"自动模式: {'是' if args.auto else '否'}")
    print(f"最大检测数: {args.num if args.num > 0 else '全部'}")
    if args.cities:
        print(f"指定城市: {', '.join(args.cities)}")
    print(f"最低速度要求: {config.get('min_speed_kbps', 0)} KB/s")
    print("=" * 60)
    
    if args.min_speed is not None:
        CONFIG['quick']['min_speed_kbps'] = args.min_speed
        CONFIG['precise']['min_speed_kbps'] = args.min_speed
        if CONFIG['verbose']:
            print(f"最低速度要求: {args.min_speed} KB/s (命令行指定)")

    load_city_config()
    cities = get_all_cities()
    
    if not cities:
        print("错误：未找到任何城市IP文件")
        print(f"请确保 {CONFIG['ip_dir']} 目录下有 {{城市}}_ip.txt 文件")
        sys.exit(1)
    
    # 自动模式
    if args.auto:
        print("\n自动模式运行...")
        success_count, total_servers = process_all_cities(args.mode, args.num if args.num > 0 else 0, args.cities)
        print(f"\n自动模式完成: {success_count}/{len(cities)} 个城市成功, {total_servers} 个有效服务器")
        return
    
    # 全部城市模式
    if args.all:
        process_all_cities(args.mode, args.num if args.num > 0 else 0, args.cities)
        return
    
    # 命令行指定城市编号
    if args.city is not None:
        if 1 <= args.city <= len(cities):
            city_name = cities[args.city - 1]
            print(f"\n处理城市: {city_name}")
            process_city(city_name, args.mode, args.num if args.num > 0 else 0)
        else:
            print(f"错误：无效的城市编号 {args.city}")
            print(f"请输入 1-{len(cities)} 之间的数字")
        return
    
    # 交互模式
    single_city_mode(args.mode, args.num if args.num > 0 else 0)


if __name__ == "__main__":
    main()