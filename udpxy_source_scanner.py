#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
udpxy 代理服务器扫描工具
功能：扫描指定IP段，检测可用的 udpxy 代理服务器，支持存档更新
支持自动模式（用于 CI/CD）
"""

import os
import sys
import time
import glob
import threading
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Set

import requests

# ==================== 配置常量 ====================
DEFAULT_WORKERS = 500          # 默认并发线程数
DEFAULT_TIMEOUT = 2            # 请求超时时间（秒）


# ==================== 进度条函数 ====================
def print_progress_bar(current: int, total: int, prefix: str = "", suffix: str = "", bar_length: int = 40):
    """打印进度条"""
    if total == 0:
        return
    percent = current / total
    arrow = '=' * int(round(percent * bar_length))
    spaces = ' ' * (bar_length - len(arrow))
    sys.stdout.write(f"\r{prefix} [{arrow}{spaces}] {percent*100:.1f}% ({current}/{total}){suffix}")
    sys.stdout.flush()


# ==================== 工具函数 ====================
def read_config(config_file: str) -> List[Tuple[str, str, int, str]]:
    """
    读取配置文件，返回配置列表
    每行格式：起始IP:端口,选项
    选项含义：
        - 0/10: 固定C段扫描 (同C段所有IP)
        - 2/12: C段范围扫描 (C段范围1-255)
        - 其他: 全网段扫描 (B段所有IP)
    """
    print(f"[配置读取] 正在读取: {config_file}")
    ip_configs = []
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            print(f"[配置读取] 文件共 {len(lines)} 行")

            for line_num, line in enumerate(lines, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "," not in line:
                    print(f"[配置读取] 第{line_num}行：格式错误，跳过")
                    continue

                parts = line.split(',')
                if len(parts) < 2:
                    print(f"[配置读取] 第{line_num}行：参数不足，跳过")
                    continue

                try:
                    ip_part, port = parts[0].strip().split(':')
                    a, b, c, d = ip_part.split('.')
                    option = int(parts[1])
                    # 修正：option >= 10 使用 /status，否则使用 /stat
                    url_end = "/status" if option >= 10 else "/stat"
                    
                    # 根据选项计算起始IP
                    if option % 2 == 0:
                        ip = f"{a}.{b}.{c}.1"
                    else:
                        ip = f"{a}.{b}.1.1"
                    
                    ip_configs.append((ip, port, option, url_end))
                    print(f"[配置读取] 第{line_num}行：http://{ip}:{port}{url_end} 添加成功")
                except Exception as e:
                    print(f"[配置读取] 第{line_num}行：解析错误 - {e}，跳过")

        print(f"[配置读取] 完成，共 {len(ip_configs)} 条配置")
        return ip_configs
    except Exception as e:
        print(f"[配置读取] 错误: {e}")
        return []


def generate_ip_ports(ip: str, port: str, option: int) -> List[str]:
    """根据选项生成待扫描的 IP 端口列表"""
    a, b, c, d = ip.split('.')

    if option == 2 or option == 12:
        # C段范围扫描
        c_extent = c.split('-')
        c_first = int(c_extent[0]) if len(c_extent) == 2 else int(c)
        c_last = int(c_extent[1]) + 1 if len(c_extent) == 2 else int(c) + 8
        ip_list = [f"{a}.{b}.{x}.{y}:{port}" for x in range(c_first, c_last) for y in range(1, 256)]
        print(f"[IP生成] 模式: C段范围扫描, IP数量: {len(ip_list)}")
        return ip_list
    elif option == 0 or option == 10:
        # 固定C段扫描
        ip_list = [f"{a}.{b}.{c}.{y}:{port}" for y in range(1, 256)]
        print(f"[IP生成] 模式: 固定C段扫描, IP数量: {len(ip_list)}")
        return ip_list
    else:
        # 全网段扫描
        ip_list = [f"{a}.{b}.{x}.{y}:{port}" for x in range(256) for y in range(1, 256)]
        print(f"[IP生成] 模式: 全网段扫描, IP数量: {len(ip_list)}")
        return ip_list


def check_ip_port(ip_port: str, url_end: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """检测单个 IP 端口是否为 udpxy 服务，成功返回 ip_port，失败返回 None"""
    try:
        url = f"http://{ip_port}{url_end}"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        # 检测 udpxy 特征
        text_lower = resp.text.lower()
        if "multi stream daemon" in text_lower or "udpxy status" in text_lower:
            return ip_port
    except Exception:
        pass
    return None


def scan_ip_ports(ip_ports: List[str], url_end: str, workers: int = DEFAULT_WORKERS,
                  timeout: int = DEFAULT_TIMEOUT, verbose: bool = False) -> List[str]:
    """多线程扫描 IP 端口列表，返回有效的 IP:端口 列表"""
    if not ip_ports:
        return []

    if verbose:
        print(f"[线程配置] 启动 {workers} 个线程，共 {len(ip_ports)} 个目标")
    
    start_time = time.time()
    valid_results = []
    completed = 0
    total = len(ip_ports)

    if verbose:
        print_progress_bar(0, total, prefix="扫描进度:", suffix=" 有效:0")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(check_ip_port, ip_port, url_end, timeout): ip_port for ip_port in ip_ports}

        for future in as_completed(futures):
            result = future.result()
            if result:
                valid_results.append(result)

            completed += 1
            if verbose:
                print_progress_bar(completed, total, prefix="扫描进度:", suffix=f" 有效:{len(valid_results)}")

    if verbose:
        print()
    
    elapsed = time.time() - start_time
    if verbose:
        print(f"[扫描完成] 耗时: {elapsed:.2f}秒, 有效IP: {len(valid_results)}个")

    return valid_results


def save_results(province: str, new_ips: List[str]) -> Tuple[int, int, int]:
    """保存扫描结果到 ip/{province}_ip.txt 和 ip/存档/存档_{province}_ip.txt"""
    # 确保目录存在
    os.makedirs("ip", exist_ok=True)
    os.makedirs("ip/存档", exist_ok=True)

    # 主文件路径
    main_file = f"ip/{province}_ip.txt"
    archive_file = f"ip/存档/存档_{province}_ip.txt"

    # 读取现有主文件
    existing_ips = []
    if os.path.exists(main_file):
        with open(main_file, 'r', encoding='utf-8') as f:
            existing_ips = [line.strip() for line in f if line.strip()]

    # 合并去重
    all_ips = sorted(set(existing_ips + new_ips))
    added_count = len(all_ips) - len(existing_ips)

    with open(main_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(all_ips))

    print(f"[文件保存] 结果已保存: {main_file}")
    print(f"[文件保存] 原记录: {len(existing_ips)}, 新记录: {len(all_ips)}, 新增: {added_count}")

    # 处理存档文件（存储网关IP）
    if new_ips:
        # 提取网关IP（将IP末尾改为.1）
        gateway_ips = set()
        for ip_port in new_ips:
            try:
                ip, port = ip_port.split(":")
                parts = ip.split(".")
                gateway_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.1:{port}"
                gateway_ips.add(gateway_ip)
            except ValueError:
                continue

        # 读取现有存档
        archive_ips = []
        if os.path.exists(archive_file):
            with open(archive_file, 'r', encoding='utf-8') as f:
                archive_ips = [line.strip() for line in f if line.strip()]

        # 合并去重
        all_archive = sorted(set(archive_ips + list(gateway_ips)))
        archive_added = len(all_archive) - len(archive_ips)

        with open(archive_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(all_archive))

        print(f"[存档更新] 存档文件已更新: {archive_file}")
        print(f"[存档更新] 原记录: {len(archive_ips)}, 新记录: {len(all_archive)}, 新增: {archive_added}")

    return len(existing_ips), len(all_ips), added_count


def scan_province(config_file: str, workers: int = DEFAULT_WORKERS, verbose: bool = True) -> bool:
    """扫描单个省份的 udpxy 服务器，返回是否发现有效服务"""
    filename = os.path.basename(config_file)
    province = filename.split('_')[0]

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"[开始扫描] 省份: {province}")
        print(f"{'=' * 60}")

    configs = sorted(set(read_config(config_file)))
    if not configs:
        print(f"[扫描结果] 未读取到有效配置，跳过 {province}")
        return False

    if verbose:
        print(f"[扫描配置] 共 {len(configs)} 组配置")

    all_ip_ports = []
    for idx, (ip, port, option, url_end) in enumerate(configs, 1):
        if verbose:
            print(f"\n[扫描组 {idx}/{len(configs)}] http://{ip}:{port}{url_end}, 选项: {option}")
        
        ip_ports = generate_ip_ports(ip, port, option)
        results = scan_ip_ports(ip_ports, url_end, workers, DEFAULT_TIMEOUT, verbose)
        all_ip_ports.extend(results)

    if all_ip_ports:
        all_ip_ports = sorted(set(all_ip_ports))
        if verbose:
            print(f"\n{'=' * 60}")
            print(f"[扫描结果] {province} 发现 {len(all_ip_ports)} 个有效服务")
        
        save_results(province, all_ip_ports)
        return True
    else:
        if verbose:
            print(f"\n[扫描结果] {province} 未发现有效服务")
        return False


def select_config_files(config_files: List[str], auto: bool = False) -> List[str]:
    """选择要扫描的配置文件，自动模式返回全部"""
    if auto:
        print(f"[模式] 自动模式，扫描全部 {len(config_files)} 个城市")
        return config_files

    print("\n[配置列表] 可用城市列表:")
    print("-" * 60)
    for idx, config_file in enumerate(config_files, 1):
        filename = os.path.basename(config_file)
        province = filename.split('_')[0]
        file_size = os.path.getsize(config_file)
        print(f"  {idx:2d}. {province:<12} | 文件: {filename} | 大小: {file_size}字节")

    print("\n" + "-" * 60)
    print("[操作提示] 输入城市编号选择扫描对象")
    print("[操作提示] 输入 0 或 直接回车 扫描全部城市")
    print("[操作提示] 输入 q 退出程序")

    while True:
        try:
            choice = input("\n请输入城市编号: ").strip().lower()
            
            if choice == '':
                print("[用户选择] 扫描全部城市")
                return config_files
            if choice == 'q':
                print("[用户选择] 退出程序")
                return []
            if choice == '0':
                print("[用户选择] 扫描全部城市")
                return config_files
            
            idx = int(choice)
            if 1 <= idx <= len(config_files):
                selected = [config_files[idx - 1]]
                province = os.path.basename(selected[0]).split('_')[0]
                print(f"[用户选择] 开始扫描: {province}")
                return selected
            else:
                print(f"[错误] 无效编号 {idx}，请输入 1-{len(config_files)} 或 0 或 直接回车 或 q")
        except ValueError:
            print("[错误] 请输入有效的数字")


def main():
    parser = argparse.ArgumentParser(description='udpxy 代理服务器扫描工具')
    parser.add_argument('-w', '--workers', type=int, default=DEFAULT_WORKERS,
                        help=f'并发线程数（默认: {DEFAULT_WORKERS}）')
    parser.add_argument('-t', '--timeout', type=int, default=DEFAULT_TIMEOUT,
                        help=f'请求超时时间（秒，默认: {DEFAULT_TIMEOUT}）')
    parser.add_argument('-c', '--city', type=int, default=None,
                        help='指定城市编号（不指定则交互选择）')
    parser.add_argument('--auto', action='store_true',
                        help='自动模式（用于CI/CD，无交互，扫描全部）')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='静默模式，减少输出')
    args = parser.parse_args()

    verbose = not args.quiet

    print(f"\n{'=' * 60}")
    print("          udpxy 代理服务器扫描工具 v2.0")
    print(f"{'=' * 60}")
    print(f"[配置] 并发线程数: {args.workers}")
    print(f"[配置] 请求超时: {args.timeout}秒")
    print(f"[配置] 自动模式: {'是' if args.auto else '否'}")
    print(f"[配置] 静默模式: {'是' if args.quiet else '否'}")

    start_time = time.time()

    config_files = glob.glob(os.path.join('ip', '*_config.txt'))
    if not config_files:
        print("[错误] 未找到配置文件，请确保 ip 目录下有 *_config.txt 文件")
        sys.exit(1)

    print(f"\n[初始化] 找到 {len(config_files)} 个配置文件")
    config_files = sorted(config_files)

    # 选择要扫描的配置文件
    selected_configs = select_config_files(config_files, auto=args.auto)
    
    if not selected_configs:
        print("[退出] 用户取消")
        sys.exit(0)

    # 执行扫描
    success_count = 0
    for idx, config_file in enumerate(selected_configs, 1):
        province = os.path.basename(config_file).split('_')[0]
        
        if verbose and len(selected_configs) > 1:
            print(f"\n{'=' * 60}")
            print(f"[进度] {idx}/{len(selected_configs)} - {province}")
            print(f"{'=' * 60}")
        
        if scan_province(config_file, args.workers, verbose):
            success_count += 1

    elapsed = time.time() - start_time
    
    print(f"\n{'=' * 60}")
    print("[全部完成] 扫描任务结束")
    print(f"{'=' * 60}")
    print(f"[统计] 总耗时: {elapsed:.2f}秒")
    print(f"[统计] 扫描城市: {len(selected_configs)}")
    print(f"[统计] 有效城市: {success_count}")
    print(f"[统计] 结果目录: ip/")
    
    # 自动模式下，如果有失败的城市，返回非零退出码
    if args.auto and success_count < len(selected_configs):
        sys.exit(1)


if __name__ == "__main__":
    main()