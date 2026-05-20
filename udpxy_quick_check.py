#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
udpxy 快速检测工具
功能：快速检测udpxy服务器可用性和性能
支持从 ip/{城市}_ip.txt 读取服务器列表
支持结果输出到 ip/{城市}_ip_quick.txt
"""

import os
import sys
import re
import socket
import time
import requests
import argparse
import json
import locale
from datetime import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置参数 ====================
CONFIG = {
    "download_size": 32 * 1024,     # 下载32KB
    "chunk_size": 8192,             # 8KB块
    "timeout_connect": 3,           # 连接超时（秒）
    "timeout_read": 10,             # 读取超时（秒）
    "max_workers_connect": 200,      # 端口检测并发数
    "max_workers_speed": 100,        # 测速并发数
    "retry_times": 1,               # 重试次数
    "city_config_file": "config/city_config.json",
    "ip_dir": "ip",
    "logs_dir": "logs",
    "max_servers": 0,               # 最大测试服务器数（0表示全部）
}

# 尝试导入 locale
try:
    import locale
    locale.setlocale(locale.LC_COLLATE, 'zh_CN.UTF-8')
except:
    pass

# ==================== 城市配置加载 ====================
CITY_CONFIG = {}


def load_city_config():
    """加载 config/city_config.json"""
    global CITY_CONFIG
    config_file = CONFIG["city_config_file"]
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        CITY_CONFIG = data.get("cities", data)


def get_all_cities():
    """从 ip 目录获取所有城市列表（返回城市名称列表）"""
    ip_dir = CONFIG["ip_dir"]
    cities = []
    if os.path.exists(ip_dir):
        for filename in os.listdir(ip_dir):
            if filename.endswith("_ip.txt"):
                city_name = filename[:-7]
                skip_patterns = ["存档", "template", "ipresu", "ipgo"]
                if not any(city_name.startswith(p) for p in skip_patterns):
                    cities.append(city_name)
    try:
        cities = sorted(cities, key=locale.strxfrm)
    except:
        cities = sorted(cities)
    return cities


def get_city_by_name(city_name):
    """根据城市名称获取城市信息"""
    for key, cfg in CITY_CONFIG.items():
        if cfg.get("city") == city_name:
            return {"city": city_name, "stream": cfg.get("stream")}
    return {"city": city_name, "stream": None}


def print_city_list():
    """动态打印城市选择列表"""
    cities = get_all_cities()
    if not cities:
        return "未找到任何城市文件"
    
    lines = []
    cols = 5
    for i in range(0, len(cities), cols):
        row = cities[i:i+cols]
        row_text = ""
        for j, city in enumerate(row):
            idx = i + j + 1
            result_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_quick.txt")
            has_result = "✓" if os.path.exists(result_file) else " "
            row_text += f"{idx:2d}.{has_result}{city}\t"
        lines.append(row_text)
    
    lines.append("  (标记 ✓ 表示已有快速测试结果)")
    return "\n".join(lines)


# ==================== 工具函数 ====================
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


def quick_speed_test(ip_port, stream, retry=1):
    url = f"http://{ip_port}/rtp/{stream}"
    for attempt in range(retry):
        try:
            start = time.time()
            resp = requests.get(url, timeout=(CONFIG["timeout_connect"], CONFIG["timeout_read"]), stream=True)
            resp.raise_for_status()
            
            downloaded = 0
            target = CONFIG["download_size"]
            max_chunks = target // CONFIG["chunk_size"]
            
            for _ in range(max_chunks):
                chunk = resp.raw.read(CONFIG["chunk_size"])
                if not chunk:
                    break
                downloaded += len(chunk)
            
            elapsed = time.time() - start
            if elapsed > 0 and downloaded > 0:
                speed_bps = downloaded / elapsed
                if speed_bps >= 1024 * 1024:
                    speed_str = f"{speed_bps / (1024 * 1024):.1f}M"
                elif speed_bps >= 1024:
                    speed_str = f"{speed_bps / 1024:.1f}k"
                else:
                    speed_str = f"{speed_bps:.0f}B"
                return speed_str
            else:
                return "[X]"
        except Exception:
            if attempt == retry - 1:
                return "[X]"
            time.sleep(1)
    return "[X]"


def parse_speed_value(speed_str):
    if speed_str == "[X]":
        return 0
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


def read_existing_history(city):
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


def update_history(existing, results):
    for server, speed in results:
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


def save_results(city, results, existing):
    current_time = datetime.now().strftime('%Y%m%d_%H%M')
    
    result_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_quick.txt")
    valid_servers = [(s, sp) for s, sp in results if sp != "[X]"]
    
    if len(valid_servers) == 0:
        if os.path.exists(result_file):
            os.remove(result_file)
            print(f"  没有有效服务器，已删除 {result_file}")
    else:
        with open(result_file, 'w', encoding='utf-8') as f:
            f.write(f"# {current_time}_quick\n")
            for server, speed in valid_servers:
                if not server.startswith('http'):
                    server = f"http://{server}"
                f.write(f"{server}\t{speed}\n")
    
    history_file = os.path.join(CONFIG["ip_dir"], f"{city}_ip_history.txt")
    with open(history_file, 'w', encoding='utf-8') as f:
        f.write(f"# {current_time}_quick_history\n")
        f.write("# 服务器地址\t状态\t测试有效次数\t测试无效次数\n")
        for server_norm, data in existing.items():
            f.write(f"{server_norm}\t{data['status']}\t{data['success']}\t{data['fail']}\n")
    
    return len(valid_servers)


def process_city(city_name, max_servers=0):
    city_info = get_city_by_name(city_name)
    if not city_info:
        print(f"错误：无效城市 {city_name}")
        return False
    
    stream = city_info["stream"]
    
    matched_stream = None
    for key, cfg in CITY_CONFIG.items():
        if cfg.get("city") == city_name:
            matched_stream = cfg.get("stream")
            break
    
    if not matched_stream:
        print(f"  ✗ 未匹配到组播地址，跳过检测")
        return False
    
    print(f"  ✓ 匹配到组播地址：{matched_stream}")
    
    ip_list, ip_mapping = parse_servers(city_name, max_servers)
    if not ip_list:
        print(f"  ✗ 没有可用的IP地址")
        return False
    
    print(f"  有效IP数量：{len(ip_list)}")
    
    print("  正在检测端口连通性...")
    good_ips = set()
    with ThreadPoolExecutor(max_workers=CONFIG["max_workers_connect"]) as ex:
        futures = {ex.submit(test_port_connect, ip): ip for ip in ip_list}
        for f in as_completed(futures):
            if f.result():
                good_ips.add(futures[f])
    
    print(f"  端口可用：{len(good_ips)} 个")
    
    existing = read_existing_history(city_name)
    results = []
    
    if not good_ips:
        print(f"  ✗ 没有可用的端口，跳过测速，但记录所有IP为无效")
        for ip in ip_list:
            orig = ip_mapping.get(ip, ip)
            results.append((orig, "[X]"))
    else:
        print(f"  正在快速测速（下载64KB）...")
        test_list = [(ip, ip_mapping.get(ip, ip)) for ip in ip_list if ip in good_ips]
        with ThreadPoolExecutor(max_workers=CONFIG["max_workers_speed"]) as ex:
            futures = {}
            for ip, orig in test_list:
                futures[ex.submit(quick_speed_test, orig, stream, CONFIG["retry_times"])] = (ip, orig)
            
            for f in as_completed(futures):
                ip, orig = futures[f]
                speed = f.result()
                results.append((orig, speed))
                print(f"    {orig}\t{speed}")
        
        for ip in ip_list:
            if ip not in good_ips:
                orig = ip_mapping.get(ip, ip)
                results.append((orig, "[X]"))
    
    results.sort(key=lambda x: parse_speed_value(x[1]), reverse=True)
    existing = update_history(existing, results)
    valid_count = save_results(city_name, results, existing)
    
    print(f"\n  ✓ 快速检测完成！成功: {valid_count}/{len(results)}")
    return True


def process_all_cities(max_servers=0):
    cities = get_all_cities()
    
    print(f"\n开始快速测试 - 共 {len(cities)} 个城市")
    print("=" * 60)
    
    success_count = 0
    skipped_cities = []
    
    for i, city in enumerate(cities, 1):
        print(f"\n[{i}/{len(cities)}] 处理城市：{city}")
        if process_city(city, max_servers):
            success_count += 1
        else:
            skipped_cities.append(city)
    
    print("\n" + "=" * 60)
    print("快速测试完成")
    print(f"成功: {success_count}/{len(cities)} 个城市")
    
    if skipped_cities:
        print(f"跳过（无组播配置）: {len(skipped_cities)} 个")
        log_dir = CONFIG["logs_dir"]
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "no_multicast_config_quick.txt")
        with open(log_file, 'w', encoding='utf-8-sig') as f:
            f.write(f"# 未配置组播地址的城市列表（快速测试跳过）\n")
            f.write(f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            for city in skipped_cities:
                f.write(f"{city}\n")


def main():
    parser = argparse.ArgumentParser(description='udpxy 服务器快速检测工具')
    parser.add_argument('--auto', action='store_true', help='自动模式（无交互，用于CI/CD）')
    parser.add_argument('--cities', nargs='+', help='指定检测的城市列表（如：--cities 上海电信 北京移动）')
    parser.add_argument('--max-servers', type=int, default=CONFIG['default_max_servers'], 
                        help=f'最多检测的服务器数量，默认{CONFIG["default_max_servers"]}个')
    args = parser.parse_args()
    
    # 设置配置
    if args.auto:
        CONFIG['verbose'] = False  # 自动模式减少输出
        CONFIG['auto_mode'] = True
    else:
        CONFIG['verbose'] = True
    
    CONFIG['default_max_servers'] = args.max_servers
    
    print("=" * 60)
    print("udpxy 服务器快速检测工具")
    print("=" * 60)
    print(f"自动模式: {'是' if args.auto else '否'}")
    print(f"最大检测数: {args.max_servers}")
    if args.cities:
        print(f"指定城市: {', '.join(args.cities)}")
    print("=" * 60)
    
    # 获取城市列表
    cities = get_cities_from_rtp_dir()
    
    if not cities:
        print("错误：未找到任何城市文件，请检查 rtp 目录")
        return
    
    # 筛选指定城市
    if args.cities:
        cities = [c for c in cities if c in args.cities]
        if not cities:
            print(f"错误：未找到指定城市 {', '.join(args.cities)}")
            return
        print(f"\n将检测 {len(cities)} 个指定城市: {', '.join(cities)}")
    else:
        print(f"\n将检测全部 {len(cities)} 个城市")
    
    print()
    
    # 统计信息
    success_cities = []
    failed_cities = []
    total_servers_found = 0
    
    for idx, city in enumerate(cities, 1):
        print(f"[{idx}/{len(cities)}] 检测城市: {city}")
        
        success, server_count = check_city(city, max_servers=args.max_servers)
        
        if success:
            success_cities.append(city)
            total_servers_found += server_count
            print(f"  ✓ 完成，检测到 {server_count} 个有效服务器")
        else:
            failed_cities.append(city)
            print(f"  ✗ 失败")
        
        print()
        
        # 非自动模式下询问是否继续
        if not args.auto and idx < len(cities):
            choice = input("是否继续检测下一个城市？(y/n，默认y): ").strip().lower()
            if choice not in ('y', 'yes', ''):
                print("用户中断检测")
                break
    
    # 打印汇总
    print("=" * 60)
    print("检测完成")
    print("=" * 60)
    print(f"总城市数: {len(cities)}")
    print(f"成功: {len(success_cities)} 个")
    print(f"失败: {len(failed_cities)} 个")
    print(f"有效服务器总数: {total_servers_found}")
    
    if success_cities:
        print(f"成功城市: {', '.join(success_cities)}")
    if failed_cities:
        print(f"失败城市: {', '.join(failed_cities)}")
    
    print("=" * 60)
    
    # 自动模式下，如果有失败的城市，返回非零退出码
    if args.auto and failed_cities:
        sys.exit(1)


if __name__ == "__main__":
    main()