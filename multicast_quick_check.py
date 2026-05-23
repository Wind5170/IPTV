#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组播地址健康检查工具
功能：检测组播地址的有效性，支持批量测试和接续测试
输出文件：
  - 快速测试：{城市}_quick.txt
  - 流信息探测：{城市}_probe.txt
"""

import requests
import re
import time
import sys
import os
import argparse
import locale
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# 设置中文环境用于拼音排序
try:
    locale.setlocale(locale.LC_COLLATE, 'Chinese_People\'s Republic of China.936')
except:
    try:
        locale.setlocale(locale.LC_COLLATE, 'zh_CN.UTF-8')
    except:
        pass

# ==================== 配置参数 ====================
MAX_SERVERS = 50
FORCE_TEST_ALL = True
SERVER_SOURCE = "quick"  # 可选: quick, precise
OUTPUT_MODE = "simple"   # 可选: detailed（详细模式）, simple（简洁模式）
MAX_PER_SERVER = 10      # 单服务器最大并发数

# ==================== 辅助函数 ====================
def test_multicast_url(multicast_url, udpxy_server, retries=2):
    """测试单个组播地址"""
    for attempt in range(retries + 1):
        try:
            match = re.search(r'(rtp|udp)://([\d.]+):(\d+)', multicast_url)
            if not match:
                return multicast_url, False, "格式错误", ""

            protocol, ip, port = match.groups()
            server_clean = udpxy_server.replace('http://', '').replace('https://', '')
            udpxy_url = f"http://{server_clean}/{protocol}/{ip}:{port}"

            start_time = time.time()
            with requests.get(udpxy_url, stream=True, timeout=(5, 15)) as resp:
                if resp.status_code == 200:
                    downloaded = 0
                    for chunk in resp.iter_content(chunk_size=4096):
                        if chunk:
                            downloaded += len(chunk)
                        if downloaded >= 16384:
                            end_time = time.time()
                            speed = end_time - start_time
                            return multicast_url, True, f"耗时: {speed:.2f}s", udpxy_url
                    if downloaded > 0:
                        end_time = time.time()
                        speed = end_time - start_time
                        return multicast_url, True, f"耗时: {speed:.2f}s", udpxy_url
                    end_time = time.time()
                    return multicast_url, False, f"无数据", udpxy_url
                else:
                    return multicast_url, False, f"状态码: {resp.status_code}", udpxy_url
        except requests.exceptions.ConnectTimeout:
            if attempt < retries:
                time.sleep(0.5)
                continue
            return multicast_url, False, "连接超时", udpxy_url
        except requests.exceptions.ReadTimeout:
            if attempt < retries:
                time.sleep(0.5)
                continue
            return multicast_url, False, "读取超时", udpxy_url
        except Exception as e:
            if attempt < retries:
                time.sleep(0.5)
                continue
            return multicast_url, False, "错误", udpxy_url


def extract_region_from_filename(file_path):
    """从文件名提取城市名称（完整文件名，不含扩展名）"""
    basename = os.path.basename(file_path)
    city_name = os.path.splitext(basename)[0]
    return city_name, True


def extract_main_city_name(filename):
    """
    从文件名提取主城市名（去除 _extracted、_source、_checked 等后缀）
    例如：
        "江苏电信" -> "江苏电信"
        "江苏电信_extracted" -> "江苏电信"
        "江苏电信_checked" -> "江苏电信"
    """
    suffixes = ['_extracted', '_source', '_checked', '_result', '_precise', '_history', '_quick', '_probe']
    for suffix in suffixes:
        if filename.endswith(suffix):
            return filename[:-len(suffix)]
    return filename


def find_multicast_files(source_dir):
    """扫描目录下的组播地址文件，跳过特殊后缀的文件"""
    if not os.path.exists(source_dir):
        return []
    files = []
    # 需要跳过的后缀（这些是结果文件，不是源文件）
    skip_suffixes = ['_source', '_checked', '_quick', '_probe', '_result', '_precise', '_history']
    
    for filename in os.listdir(source_dir):
        if not filename.endswith('.txt'):
            continue
        # 跳过结果文件
        should_skip = False
        for suffix in skip_suffixes:
            if filename.endswith(f"{suffix}.txt"):
                should_skip = True
                break
        if should_skip:
            continue
        
        file_path = os.path.join(source_dir, filename)
        city_name, _ = extract_region_from_filename(file_path)
        if city_name:
            files.append(file_path)
    
    return sorted(files, key=lambda x: locale.strxfrm(os.path.basename(x)))


def print_city_list(multicast_files):
    """动态打印城市选择列表（多列对齐）"""
    if not multicast_files:
        return "未找到任何城市文件"
    
    ip_dir = "ip"
    cities = [os.path.splitext(os.path.basename(f))[0] for f in multicast_files]
    
    max_len = max(len(c) for c in cities) + 2
    cols = 4
    lines = []
    
    for i in range(0, len(cities), cols):
        row = cities[i:i+cols]
        row_text = ""
        for j, city in enumerate(row):
            idx = i + j + 1
            # 检查快速测试结果文件（新格式 _quick.txt）
            result_file = os.path.join(ip_dir, f"{extract_main_city_name(city)}_ip_quick.txt")
            has_result = "✓" if os.path.exists(result_file) else " "
            row_text += f"{idx:2d}.{has_result}{city:<{max_len}}"
        lines.append(row_text)
    
    lines.append("  (标记 ✓ 表示已有快速测试结果)")
    return "\n".join(lines)


def is_valid_multicast_addr(addr):
    """判断是否为有效的组播地址"""
    if not addr:
        return False
    if addr.startswith('#'):
        return False
    if addr.startswith(('rtp://', 'udp://')):
        return True
    if ':' in addr and addr.count('.') >= 2:
        return True
    return False


def normalize_addr(addr):
    """规范化组播地址，确保统一格式"""
    if not addr:
        return addr
    addr = addr.strip()
    if addr.startswith(('rtp://', 'udp://')):
        return addr
    if ':' in addr and addr.count('.') >= 2:
        return 'rtp://' + addr
    return addr


def parse_multicast_file(file_path, default_region=None):
    """解析组播文件，返回分类和频道列表"""
    items = {}
    current_category = default_region if default_region else "未分类"

    encodings = ['utf-8', 'gbk', 'gb2312', 'latin1']
    lines = None
    detected_encoding = None
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                lines = f.readlines()
            detected_encoding = encoding
            break
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return None, None, str(e)

    if lines is None:
        return None, None, "无法读取文件"

    for line in lines:
        line = line.rstrip('\n')
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith('#'):
            continue
        elif ',' in stripped:
            parts = stripped.split(',', 3)
            tag = ""

            if len(parts) >= 4:
                category_part = parts[0].strip()
                name = parts[1].strip()
                addr = parts[2].strip()
                tag = parts[3].strip()
            elif len(parts) == 3:
                category_part = parts[0].strip()
                name = parts[1].strip()
                addr = parts[2].strip()
            elif len(parts) == 2:
                second_part = parts[1].strip()
                if second_part == '#genre#' or second_part.startswith('#genre'):
                    current_category = parts[0].strip() or current_category
                    continue
                elif is_valid_multicast_addr(second_part):
                    category_part = current_category
                    name = parts[0].strip()
                    addr = second_part
                else:
                    category_part = current_category
                    name = parts[0].strip()
                    addr = second_part
            else:
                continue

            if not is_valid_multicast_addr(addr):
                continue

            if not category_part:
                category_part = default_region if default_region else "未分类"

            if category_part not in items:
                items[category_part] = {"active": [], "failed": []}

            addr = normalize_addr(addr)

            if addr.startswith(('rtp://', 'udp://')):
                if tag == "无效":
                    items[category_part]["failed"].append({"name": name, "addr": addr})
                else:
                    items[category_part]["active"].append({"name": name, "addr": addr})
        elif is_valid_multicast_addr(stripped):
            category_part = default_region if default_region else "未分类"
            if category_part not in items:
                items[category_part] = {"active": [], "failed": []}
            addr = normalize_addr(stripped)
            items[category_part]["active"].append({"name": "", "addr": addr})

    return items, detected_encoding, False


def parse_servers_by_region(ip_dir, main_city_name, server_source="quick"):
    """
    解析服务器列表（使用主城市名）
    新格式：{main_city_name}_ip_quick.txt 或 {main_city_name}_ip_precise.txt
    """
    servers = []
    if server_source == "quick":
        server_file = os.path.join(ip_dir, f"{main_city_name}_ip_quick.txt")
        # 兼容旧格式
        if not os.path.exists(server_file):
            server_file = os.path.join(ip_dir, f"{main_city_name}_ip_result.txt")
    else:
        server_file = os.path.join(ip_dir, f"{main_city_name}_ip_precise.txt")

    if os.path.exists(server_file):
        with open(server_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '\t' in line:
                parts = line.split('\t')
                if len(parts) >= 1:
                    server = parts[0].strip()
                    server_clean = server.replace('http://', '').replace('https://', '')
                    if ':' in server_clean:
                        servers.append(server_clean)
            elif ':' in line:
                servers.append(line.strip())
    else:
        print(f"  警告: 未找到服务器文件 {server_file}")
    return servers


def read_checked_file(checked_file_path):
    """读取_checked文件，返回有效的频道列表和无效的频道列表"""
    valid_channels = []
    invalid_channels = []
    all_checked_addrs = set()
    if not os.path.exists(checked_file_path):
        return valid_channels, invalid_channels, all_checked_addrs

    encodings = ['utf-8', 'gbk', 'gb2312', 'latin1']
    lines = None
    for encoding in encodings:
        try:
            with open(checked_file_path, 'r', encoding=encoding) as f:
                lines = f.readlines()
            break
        except UnicodeDecodeError:
            continue
    if lines is None:
        return valid_channels, invalid_channels, all_checked_addrs

    for line in lines:
        line = line.rstrip('\n').rstrip('\t')
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        parts = stripped.split('\t')
        if len(parts) < 2:
            continue
        addr = parts[1].strip() if len(parts) >= 2 else parts[0].strip()
        name = parts[0].strip() if len(parts) > 1 else ""
        status = parts[2].strip() if len(parts) >= 3 else ""
        if not is_valid_multicast_addr(addr):
            continue
        normalized_addr = normalize_addr(addr)
        all_checked_addrs.add(normalized_addr)
        if status == "无效" or "无效" in line:
            invalid_channels.append({"name": name, "addr": normalized_addr})
        else:
            valid_channels.append({"name": name, "addr": normalized_addr})
    return valid_channels, invalid_channels, all_checked_addrs


def print_progress_bar(current, total, bar_length=30):
    """打印进度条"""
    percent = current / total
    arrow = '=' * int(round(percent * bar_length))
    spaces = ' ' * (bar_length - len(arrow))
    sys.stdout.write(f"\r  进度: [{arrow}{spaces}] {percent*100:.1f}% ({current}/{total})")
    sys.stdout.flush()


def process_single_file(multicast_file, ip_dir, max_servers, force_test_all, 
                        retest_mode=None, server_source="quick", output_mode="detailed", max_per_server=1):
    """处理单个组播地址文件"""
    rel_dir = os.path.dirname(multicast_file)
    full_basename = os.path.splitext(os.path.basename(multicast_file))[0]
    # 输出文件使用新格式 _quick.txt
    checked_file = os.path.join(rel_dir, f"{full_basename}_quick.txt")

    # 提取主城市名（用于读取IP文件）
    main_city_name = extract_main_city_name(full_basename)
    city_name = full_basename

    if not city_name:
        print(f"  [跳过] {full_basename}: 无法识别城市名称")
        return False

    source_name = "快速测试" if server_source == "quick" else "精确测试"
    print(f"\n处理文件: {full_basename}")
    print(f"  城市: {city_name}")
    print(f"  服务器来源: {source_name}")
    print(f"  输出模式: {'详细' if output_mode == 'detailed' else '简洁'}")

    valid_existing = []
    invalid_existing = []
    all_checked_addrs = set()
    has_checked_file = os.path.exists(checked_file)

    if retest_mode == 'invalid_only':
        if has_checked_file:
            valid_existing, invalid_existing, all_checked_addrs = read_checked_file(checked_file)
            total_checked = len(valid_existing) + len(invalid_existing)
            print(f"  模式: 接续测试 - 已测试频道: {total_checked}个，有效: {len(valid_existing)}，待重测无效: {len(invalid_existing)}")
        else:
            print(f"  模式: 接续测试 - 未找到检查结果文件，进行全新测试")
    else:
        print(f"  模式: 全部重新测试")

    items, detected_encoding, _ = parse_multicast_file(multicast_file, city_name)
    if items is None:
        print(f"  [错误] {full_basename}: {detected_encoding}")
        return False
    if not items:
        print(f"  [错误] {full_basename}: 没有找到组播地址")
        return False

    region_servers = parse_servers_by_region(ip_dir, main_city_name, server_source)
    if not region_servers:
        print(f"  [跳过] {full_basename}: 服务器列表为空")
        return True

    region_servers = region_servers[:max_servers]
    num_servers = len(region_servers)
    print(f"  可用服务器数: {num_servers}")

    total_active = sum(len(r["active"]) for r in items.values())
    total_failed = sum(len(r["failed"]) for r in items.values())
    print(f"  分类数: {len(items)}, 地址: {total_active}, 前次无效: {total_failed}")

    # 确定需要测试的项
    all_test_items = []
    new_data_items = []

    if retest_mode == 'invalid_only':
        invalid_addrs = {item["addr"] for item in invalid_existing}
        for category, data in items.items():
            for item in data["active"]:
                if item["addr"] not in all_checked_addrs:
                    new_data_items.append(item)
                    item["category"] = category
                    all_test_items.append(item)
                elif item["addr"] in invalid_addrs:
                    item["category"] = category
                    all_test_items.append(item)
            for item in data["failed"]:
                if item["addr"] not in all_checked_addrs:
                    new_data_items.append(item)
                item["category"] = category
                all_test_items.append(item)
        if new_data_items:
            print(f"  新增测试: {len(new_data_items)} 个")
        if len(all_test_items) > len(new_data_items):
            print(f"  重测无效: {len(all_test_items) - len(new_data_items)} 个")
    else:
        for category, data in items.items():
            test_items = data["active"].copy()
            if force_test_all:
                test_items.extend(data["failed"])
            for item in test_items:
                item["category"] = category
            all_test_items.extend(test_items)

    num_items = len(all_test_items)
    if num_items == 0:
        if retest_mode == 'invalid_only':
            print(f"  [完成] {full_basename}: 所有频道已有效")
            return True
        print(f"  [错误] {full_basename}: 没有要测试的地址")
        return False

    # 并发分配逻辑
    max_total_concurrency = min(num_servers * max_per_server, max_servers)
    items_per_concurrency = math.ceil(num_items / max_total_concurrency)

    server_concurrency = [0] * num_servers
    remaining = max_total_concurrency
    for i in range(num_servers):
        if remaining <= 0:
            break
        can_assign = min(max_per_server, remaining)
        server_concurrency[i] = can_assign
        remaining -= can_assign

    actual_total = sum(server_concurrency)
    if actual_total < max_total_concurrency:
        for i in range(num_servers):
            if actual_total >= max_total_concurrency:
                break
            if server_concurrency[i] < max_per_server:
                inc = min(max_per_server - server_concurrency[i], max_total_concurrency - actual_total)
                server_concurrency[i] += inc
                actual_total += inc

    total_threads = sum(server_concurrency)
    print(f"  总测试项: {num_items}, 总并发线程数: {total_threads}, 每个线程处理: {items_per_concurrency} 个地址")

    # 切分批次
    batches = []
    for i in range(total_threads):
        start = i * items_per_concurrency
        end = min(start + items_per_concurrency, num_items)
        if start < end:
            batches.append(all_test_items[start:end])

    # 构建服务器池
    server_pool = []
    for i, con in enumerate(server_concurrency):
        server_pool.extend([region_servers[i]] * con)
    server_pool = server_pool[:len(batches)]

    # 开始测试
    valid_results = []
    invalid_results = []
    print_lock = threading.Lock()
    completed_count = 0
    total_count = num_items

    print(f"  开始测试 {num_items} 个地址...")

    def test_batch(batch, server):
        nonlocal completed_count
        local_valid = []
        local_invalid = []
        for item in batch:
            addr = item["addr"]
            name = item.get("name", "")
            result = test_multicast_url(addr, server)
            _, is_valid, status, _ = result
            display_name = name if name else addr
            with print_lock:
                if output_mode == "detailed":
                    if is_valid:
                        print(f"    [√] {display_name} - {status}")
                    else:
                        print(f"    [×] {display_name} - {status}")
                else:
                    completed_count += 1
                    print_progress_bar(completed_count, total_count)
            if is_valid:
                try:
                    speed = float(status.replace("耗时:", "").replace("s", "").strip()) if "耗时:" in status else 9999.0
                except:
                    speed = 9999.0
                result_item = item.copy()
                result_item["speed"] = speed
                local_valid.append(result_item)
            else:
                local_invalid.append(item)
        return local_valid, local_invalid

    with ThreadPoolExecutor(max_workers=total_threads) as executor:
        futures = [executor.submit(test_batch, batch, server) for batch, server in zip(batches, server_pool)]
        for future in futures:
            valid_batch, invalid_batch = future.result()
            valid_results.extend(valid_batch)
            invalid_results.extend(invalid_batch)

    if output_mode == "simple" and total_count > 0:
        print()

    all_valid_addrs = set()
    if retest_mode == 'invalid_only':
        all_valid_addrs = {item["addr"] for item in valid_existing}
    all_valid_addrs.update({item["addr"] for item in valid_results})

    print(f"\n  写入结果到 {full_basename}_quick.txt")
    try:
        with open(checked_file, 'w', encoding='utf-8') as f:
            f.write(f"# {time.strftime('%Y%m%d_%H%M%S')}_quick\n")
            for category, data in items.items():
                for item in data["active"]:
                    addr = item["addr"].replace('rtp://', '').replace('udp://', '')
                    status = "有效" if item["addr"] in all_valid_addrs else "无效"
                    f.write(f"{item['name']}\t{addr}\t{status}\n")
                if force_test_all or retest_mode == 'invalid_only':
                    for item in data["failed"]:
                        addr = item["addr"].replace('rtp://', '').replace('udp://', '')
                        status = "有效" if item["addr"] in all_valid_addrs else "无效"
                        f.write(f"{item['name']}\t{addr}\t{status}\n")
        print(f"  [完成] {full_basename}: 有效 {len(valid_results)}, 无效 {len(invalid_results)}")
        return True
    except Exception as e:
        print(f"  [错误] 写入文件失败: {e}")
        return False


def main():
    global MAX_SERVERS, FORCE_TEST_ALL, SERVER_SOURCE, OUTPUT_MODE, MAX_PER_SERVER

    parser = argparse.ArgumentParser(description='组播地址健康检查工具')
    parser.add_argument('-d', '--dir', help='组播地址目录路径')
    parser.add_argument('-s', '--servers', type=int, default=MAX_SERVERS,
                        help=f'最大总并发数（默认: {MAX_SERVERS}）')
    parser.add_argument('-a', '--all', action='store_true', default=FORCE_TEST_ALL,
                        help=f'是否测试所有地址包括前次失败（默认: {FORCE_TEST_ALL}）')
    parser.add_argument('--source', choices=['quick', 'precise'], default=SERVER_SOURCE,
                        help=f'服务器来源: quick(快速测试) 或 precise(精确测试) （默认: {SERVER_SOURCE}）')
    parser.add_argument('--mode', choices=['detailed', 'simple'], default=OUTPUT_MODE,
                        help=f'输出模式: detailed(详细模式) 或 simple(简洁模式) （默认: {OUTPUT_MODE}）')
    parser.add_argument('--per-server', type=int, default=MAX_PER_SERVER,
                        help=f'每个服务器的最大并发数（默认: {MAX_PER_SERVER}），设为1则为串行模式')
    args = parser.parse_args()

    max_servers = args.servers
    server_source = args.source
    output_mode = args.mode
    max_per_server = args.per_server

    source_dir = "rtp"
    source_name = "快速测试" if server_source == "quick" else "精确测试"
    mode_name = "详细模式" if output_mode == "detailed" else "简洁模式"
    print("=" * 60)
    print("组播地址健康检查工具")
    print("功能：检测组播地址的有效性，支持批量测试和接续测试")
    print("=" * 60)
    print(f"使用目录: {source_dir}")
    print(f"服务器来源: {source_name}")
    print(f"输出模式: {mode_name}")
    print(f"最大总并发数: {max_servers}")
    print(f"单服务器最大并发: {max_per_server}")

    if not os.path.exists(source_dir):
        print(f"目录不存在: {source_dir}")
        return

    multicast_files = find_multicast_files(source_dir)
    if not multicast_files:
        print(f"在目录 {source_dir} 中没有找到符合规范的组播地址文件")
        return

    print(f"\n找到 {len(multicast_files)} 个待处理文件:")
    print(print_city_list(multicast_files))

    # 选择要处理的城市
    selected_files = []
    if len(multicast_files) > 1:
        while True:
            choice = input("\n请选择要处理的城市（输入数字，按回车处理全部，输入 q 退出）: ").strip().lower()
            if choice == 'q':
                print("已取消操作，退出程序")
                return
            elif not choice:
                selected_files = multicast_files
                break
            else:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(multicast_files):
                        selected_files = [multicast_files[idx]]
                        break
                    else:
                        print(f"请输入 1-{len(multicast_files)} 之间的数字")
                except ValueError:
                    print("请输入有效的数字或 q")
    else:
        selected_files = multicast_files

    while True:
        test_mode = input("\n请选择测试方式：\n  1. 全部重新测试\n  2. 接续测试\n\n请输入选择(1-2) [默认2]: ").strip()
        if test_mode == '1' or test_mode == '2' or not test_mode:
            if not test_mode:
                test_mode = '2'
            break
        print("无效选择，请重新输入")

    ip_dir = "ip"
    success_count = 0
    fail_count = 0

    for multicast_file in selected_files:
        try:
            full_basename = os.path.splitext(os.path.basename(multicast_file))[0]
            checked_file = os.path.join(source_dir, f"{full_basename}_quick.txt")
            has_checked = os.path.exists(checked_file)
            if test_mode == '1':
                if process_single_file(multicast_file, ip_dir, max_servers, True, None, server_source, output_mode, max_per_server):
                    success_count += 1
                else:
                    fail_count += 1
            else:
                if has_checked:
                    if process_single_file(multicast_file, ip_dir, max_servers, False, 'invalid_only', server_source, output_mode, max_per_server):
                        success_count += 1
                    else:
                        fail_count += 1
                else:
                    if process_single_file(multicast_file, ip_dir, max_servers, True, None, server_source, output_mode, max_per_server):
                        success_count += 1
                    else:
                        fail_count += 1
        except Exception as e:
            print(f"处理文件时出错 {multicast_file}: {e}")
            fail_count += 1

    print("\n" + "=" * 60)
    print("批量处理完成")
    print(f"成功: {success_count} 个文件")
    if fail_count > 0:
        print(f"失败: {fail_count} 个文件")
    print("=" * 60)


if __name__ == "__main__":
    main()