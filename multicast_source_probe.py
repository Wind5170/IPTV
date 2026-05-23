#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组播源探测工具 - 获取视频流分辨率、编码等信息
输出文件：{城市}_probe.txt
"""

import os
import sys
import subprocess
import json
import re
import time
import locale
import argparse
import threading
import math
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置参数（集中调整） ====================
CONFIG = {
    # 服务器来源: 'quick' 使用 _quick.txt（快速测试），'precise' 使用 _precise.txt（精确测试）
    "server_source": "quick",
    
    # 测试方法: 'ffprobe' 或 'opencv'
    "test_method": "ffprobe",
    
    # 最大总并发数（默认50，控制同时运行的测试线程数）
    "max_concurrency": 50,
    
    # 每个服务器最大并发数（默认5，避免单服务器过载）
    "max_per_server": 10,
    
    # 接续模式: True 只测试无效或新增的组播源，False 全部重新测试
    "retry_failed": False,
    
    # ffprobe 超时时间（秒）
    "ffprobe_timeout": 15,
    
    # 目录配置
    "rtp_dir": "rtp",
    "ip_dir": "ip",
}

# 设置中文环境用于拼音排序
try:
    locale.setlocale(locale.LC_COLLATE, 'Chinese_People\'s Republic of China.936')
except:
    try:
        locale.setlocale(locale.LC_COLLATE, 'zh_CN.UTF-8')
    except:
        pass

# 尝试导入 OpenCV
try:
    import cv2
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False


def find_ffprobe():
    """查找 ffprobe 可执行文件的位置（仅依赖系统 PATH）"""
    try:
        result = subprocess.run(['ffprobe', '-version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return 'ffprobe'
    except:
        pass
    return None


FFPROBE_PATH = find_ffprobe()


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


def get_source_files(rtp_dir='rtp'):
    """获取组播源文件列表"""
    source_files = []
    if not os.path.exists(rtp_dir):
        print(f"目录 {rtp_dir} 不存在")
        return source_files
    
    for filename in os.listdir(rtp_dir):
        if not filename.endswith('.txt'):
            continue
        # 跳过结果文件
        skip_patterns = ['_result', '_precise', '_history', '_checked', '_source', '_quick', '_probe', 'ip_', 'template_']
        if any(x in filename for x in skip_patterns):
            continue
        
        file_path = os.path.join(rtp_dir, filename)
        city_name = filename.replace('.txt', '')
        source_files.append((city_name, file_path))
    
    source_files.sort(key=lambda x: locale.strxfrm(x[0]))
    return source_files


def parse_server_file(city, ip_dir='ip', server_source='quick', max_servers=10):
    """
    解析服务器文件，支持快速测试和精确测试结果
    新格式：{city}_ip_quick.txt / {city}_ip_precise.txt
    兼容旧格式：{city}_ip_result.txt / {city}_ip_precise.txt
    返回: 服务器地址列表（按速度排序，快的在前）
    """
    servers = []
    main_city_name = extract_main_city_name(city)
    
    if server_source == 'quick':
        server_file = os.path.join(ip_dir, f"{main_city_name}_ip_quick.txt")
        if not os.path.exists(server_file):
            server_file = os.path.join(ip_dir, f"{main_city_name}_ip_result.txt")
    else:
        server_file = os.path.join(ip_dir, f"{main_city_name}_ip_precise.txt")
    
    if not os.path.exists(server_file):
        return []
    
    try:
        with open(server_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                # 格式: 服务器地址\t速度
                if '\t' in line:
                    parts = line.split('\t')
                    if len(parts) >= 1:
                        server = parts[0].strip()
                        server = server.replace('http://', '').replace('https://', '')
                        if ':' in server:
                            servers.append(server)
                elif ':' in line:
                    servers.append(line.strip())
        
        # 限制服务器数量
        if max_servers > 0 and len(servers) > max_servers:
            servers = servers[:max_servers]
        
    except Exception as e:
        print(f"  读取服务器文件失败: {e}")
    
    return servers


def normalize_multicast_addr(addr):
    """规范化组播地址格式为 rtp/ip:port"""
    addr = addr.strip()
    if addr.startswith('rtp/') or addr.startswith('udp/'):
        return addr
    if addr.startswith('rtp://'):
        return addr.replace('rtp://', 'rtp/')
    if addr.startswith('udp://'):
        return addr.replace('udp://', 'udp/')
    if ':' in addr:
        parts = addr.split(':')[0]
        if parts.count('.') == 3:
            return f'rtp/{addr}'
    return addr


def parse_source_file(file_path):
    """解析组播源文件，返回 (频道名, 地址, 分类) 列表"""
    sources = []
    current_category = "未分类"
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                if '#genre#' in line:
                    parts = line.split(',')
                    if parts:
                        current_category = parts[0].strip()
                    continue
                
                # 支持 Tab 或逗号分隔
                if '\t' in line:
                    parts = line.split('\t')
                else:
                    parts = line.split(',')
                
                if len(parts) >= 2:
                    channel_name = parts[0].strip()
                    multicast_addr = parts[1].strip()
                    multicast_addr = normalize_multicast_addr(multicast_addr)
                    sources.append((channel_name, multicast_addr, current_category))
                    
    except Exception as e:
        print(f"  读取源文件失败: {e}")
    
    return sources


def test_with_ffprobe(server, multicast_addr, timeout=15):
    """使用 ffprobe 测试组播源，获取详细信息"""
    video_url = f"http://{server}/{multicast_addr}"
    
    try:
        cmd = [
            FFPROBE_PATH,
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-show_format',
            video_url
        ]
        
        start_time = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        response_time = (time.time() - start_time) * 1000
        
        if result.returncode != 0 or not result.stdout:
            return False, "", "", 0, response_time
        
        info = json.loads(result.stdout)
        
        width, height = 0, 0
        codec_name = ""
        bit_rate = 0
        has_video = False
        has_audio = False
        
        for stream in info.get('streams', []):
            codec_type = stream.get('codec_type', '')
            if codec_type == 'video':
                has_video = True
                width = stream.get('width', 0) or stream.get('coded_width', 0)
                height = stream.get('height', 0) or stream.get('coded_height', 0)
                codec_name = stream.get('codec_name', '')
                
                bit_rate_str = stream.get('bit_rate', '')
                if not bit_rate_str:
                    bit_rate_str = info.get('format', {}).get('bit_rate', '')
                if bit_rate_str:
                    try:
                        bit_rate = int(bit_rate_str) // 1000
                    except:
                        pass
            elif codec_type == 'audio':
                has_audio = True
        
        resolution = f"{width}x{height}" if width > 0 and height > 0 else ""
        is_valid = has_video or has_audio
        
        return is_valid, resolution, codec_name, bit_rate, response_time
        
    except subprocess.TimeoutExpired:
        return False, "", "", 0, 99999
    except Exception:
        return False, "", "", 0, 99999


def test_with_opencv(server, multicast_addr, timeout=15):
    """使用 OpenCV 测试组播源，获取分辨率信息"""
    video_url = f"http://{server}/{multicast_addr}"
    
    try:
        start_time = time.time()
        cap = cv2.VideoCapture(video_url)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout * 1000)
        
        # 等待第一帧
        ret, frame = cap.read()
        response_time = (time.time() - start_time) * 1000
        
        if ret:
            height, width = frame.shape[:2]
            cap.release()
            return True, f"{width}x{height}", "", 0, response_time
        else:
            cap.release()
            return False, "", "", 0, response_time
            
    except Exception:
        return False, "", "", 0, 99999


def process_city(city, source_file, config):
    """处理单个城市的组播源探测 - 按总并发数分配任务"""
    
    print(f"\n处理城市: {city}")
    
    # 读取服务器列表
    servers = parse_server_file(city, config['ip_dir'], config['server_source'], config['max_concurrency'])
    if not servers:
        print(f"  跳过: 没有可用的服务器")
        return False
    
    # 读取组播源列表
    sources = parse_source_file(source_file)
    if not sources:
        print(f"  跳过: 没有找到组播源")
        return False
    
    num_servers = len(servers)
    num_sources = len(sources)
    
    print(f"  组播源数量: {num_sources}")
    print(f"  服务器数量: {num_servers}")
    
    # 选择测试函数
    if config['test_method'] == 'opencv' and HAS_OPENCV:
        test_func = test_with_opencv
        timeout = 15
    else:
        test_func = test_with_ffprobe
        timeout = config.get('ffprobe_timeout', 15)
    
    # 确定需要测试的源（接续测试模式）
    result_file = os.path.join(config['rtp_dir'], f"{city}_probe.txt")
    
    # 读取已有完整结果（保留详细信息）
    existing_results = {}  # key: addr, value: dict with all fields
    sources_to_test = []
    
    if config.get('retry_failed', False) and os.path.exists(result_file):
        with open(result_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 3:
                    name = parts[0].strip()
                    addr_raw = parts[1].strip()
                    addr = normalize_multicast_addr(addr_raw)
                    status = parts[2].strip()
                    resolution = parts[3].strip() if len(parts) > 3 else ""
                    codec = parts[4].strip() if len(parts) > 4 else ""
                    response = parts[5].strip() if len(parts) > 5 else "0"
                    server = parts[6].strip() if len(parts) > 6 else ""
                    
                    existing_results[addr] = {
                        "name": name,
                        "addr": addr,
                        "addr_raw": addr_raw,
                        "status": status,
                        "resolution": resolution,
                        "codec": codec,
                        "response": response,
                        "server": server
                    }
        
        # 筛选需要测试的源（新增 或 状态为无效）
        for name, addr, cat in sources:
            if addr not in existing_results:
                sources_to_test.append((name, addr, cat))
            elif existing_results[addr]["status"] != "有效":
                sources_to_test.append((name, addr, cat))
        
        print(f"  接续模式: 需要测试 {len(sources_to_test)} 个源")
    else:
        sources_to_test = sources
    
    if not sources_to_test:
        print(f"  所有组播源已完成测试")
        return True
    
    num_items = len(sources_to_test)
    max_total_concurrency = min(num_servers * config['max_per_server'], config['max_concurrency'])
    
    # 每个并发单元负责的组播数
    items_per_concurrency = math.ceil(num_items / max_total_concurrency) if max_total_concurrency > 0 else num_items
    
    # 给每个服务器分配并发数（每个服务器不超过 max_per_server）
    server_concurrency = [0] * num_servers
    remaining = max_total_concurrency
    for i in range(num_servers):
        if remaining <= 0:
            break
        can_assign = min(config['max_per_server'], remaining)
        server_concurrency[i] = can_assign
        remaining -= can_assign
    # 调整使总和等于 max_total_concurrency
    actual_total = sum(server_concurrency)
    if actual_total < max_total_concurrency:
        for i in range(num_servers):
            if actual_total >= max_total_concurrency:
                break
            if server_concurrency[i] < config['max_per_server']:
                inc = min(config['max_per_server'] - server_concurrency[i], max_total_concurrency - actual_total)
                server_concurrency[i] += inc
                actual_total += inc
    
    total_threads = sum(server_concurrency)
    # 构建服务器池
    server_pool = []
    for i, con in enumerate(server_concurrency):
        server_pool.extend([servers[i]] * con)
    # 实际使用的线程数 = min(total_threads, ceil(num_items / items_per_concurrency))
    num_batches = math.ceil(num_items / items_per_concurrency)
    effective_threads = min(total_threads, num_batches)
    server_pool = server_pool[:effective_threads]
    
    # 切分批次
    batches = []
    for i in range(effective_threads):
        start = i * items_per_concurrency
        end = min(start + items_per_concurrency, num_items)
        if start < end:
            batches.append(sources_to_test[start:end])
    
    print(f"  总测试次数: {num_items}")
    print(f"  总并发线程数: {len(batches)} (最大并发 {max_total_concurrency})")
    print(f"  每个线程处理: {items_per_concurrency} 个地址")
    
    # 并发测试
    new_results = []
    results_lock = threading.Lock()
    completed = 0
    valid_so_far = 0
    
    print(f"\n  开始测试...")
    
    def test_batch(batch, server):
        nonlocal completed, valid_so_far
        local_results = []
        for name, addr, cat in batch:
            try:
                is_valid, resolution, codec, bitrate, response = test_func(server, addr, timeout)
            except Exception:
                is_valid, resolution, codec, bitrate, response = False, "", "", 0, 99999
            
            with results_lock:
                completed += 1
                if is_valid:
                    valid_so_far += 1
                percent = completed * 100 // num_items if num_items > 0 else 0
                sys.stdout.write(f"\r  进度: {completed}/{num_items} ({percent}%) 有效: {valid_so_far}")
                sys.stdout.flush()
            
            local_results.append({
                "name": name,
                "addr": addr,
                "addr_raw": addr.replace('rtp/', '').replace('udp/', ''),
                "status": "有效" if is_valid else "无效",
                "resolution": resolution,
                "codec": codec,
                "response": str(int(response)),
                "server": server
            })
        return local_results
    
    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        futures = []
        for batch, server in zip(batches, server_pool):
            futures.append(executor.submit(test_batch, batch, server))
        for future in as_completed(futures):
            try:
                new_results.extend(future.result())
            except Exception as e:
                print(f"\n  批次测试异常: {e}")
    
    print()  # 换行
    
    # ========== 合并结果：保留原有有效数据的详细信息 ==========
    final_results = []
    
    for name, addr, cat in sources:
        # 优先使用新测试结果
        new_item = None
        for item in new_results:
            if item["addr"] == addr:
                new_item = item
                break
        
        if new_item:
            final_results.append(new_item)
        elif addr in existing_results:
            existing = existing_results[addr]
            final_results.append({
                "name": existing["name"],
                "addr": addr,
                "addr_raw": existing["addr_raw"],
                "status": existing["status"],
                "resolution": existing["resolution"],
                "codec": existing["codec"],
                "response": existing["response"],
                "server": existing.get("server", "")
            })
        else:
            # 兜底
            final_results.append({
                "name": name,
                "addr": addr,
                "addr_raw": addr.replace('rtp/', '').replace('udp/', ''),
                "status": "未知",
                "resolution": "",
                "codec": "",
                "response": "0",
                "server": ""
            })
    
    # 保存结果
    try:
        with open(result_file, 'w', encoding='utf-8') as f:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            f.write(f"# {timestamp}_probe\n")
            f.write("# 频道名\t组播地址\t状态\t分辨率\t编码\t响应时间(ms)\n")
            
            valid_count = 0
            for item in final_results:
                status = item["status"]
                if status == "有效":
                    valid_count += 1
                f.write(f"{item['name']}\t{item['addr_raw']}\t{status}\t{item['resolution']}\t{item['codec']}\t{item['response']}\n")
        
        print(f"\n  结果保存到: {result_file}")
        print(f"  统计: 有效 {valid_count}/{len(final_results)}")
        
    except Exception as e:
        print(f"  保存结果失败: {e}")
        return False
    
    return True


def print_city_list(source_files):
    """动态打印城市选择列表（多列对齐）"""
    if not source_files:
        return "未找到任何城市文件"
    
    cities = [city for city, _ in source_files]
    max_len = max(len(c) for c in cities) + 2
    cols = 4
    lines = []
    
    for i in range(0, len(cities), cols):
        row = cities[i:i+cols]
        row_text = ""
        for j, city in enumerate(row):
            idx = i + j + 1
            # 检查是否有探测结果文件（新格式 _probe.txt）
            result_file = os.path.join(CONFIG["rtp_dir"], f"{city}_probe.txt")
            has_result = "✓" if os.path.exists(result_file) else " "
            row_text += f"{idx:2d}.{has_result}{city:<{max_len}}"
        lines.append(row_text)
    
    lines.append("  (标记 ✓ 表示已有探测结果)")
    return "\n".join(lines)


def main():
    # 解析命令行参数（覆盖配置文件）
    parser = argparse.ArgumentParser(description='组播源探测工具 - 获取视频流分辨率、编码信息')
    parser.add_argument('--source', choices=['quick', 'precise'], default=CONFIG['server_source'],
                        help=f'服务器来源: quick(快速测试) 或 precise(精确测试) (默认: {CONFIG["server_source"]})')
    parser.add_argument('--method', choices=['ffprobe', 'opencv'], default=CONFIG['test_method'],
                        help=f'测试方法: ffprobe(推荐) 或 opencv (默认: {CONFIG["test_method"]})')
    parser.add_argument('--servers', '-s', type=int, default=CONFIG['max_concurrency'],
                        help=f'最大总并发数 (默认: {CONFIG["max_concurrency"]})')
    parser.add_argument('--per-server', type=int, default=CONFIG['max_per_server'],
                        help=f'每个服务器最大并发数 (默认: {CONFIG["max_per_server"]})')
    parser.add_argument('--retry', '-r', action='store_true', default=CONFIG['retry_failed'],
                        help=f'接续模式: 只测试无效或新增的组播源')
    parser.add_argument('--city', '-c', help='指定城市名称（可选）')
    parser.add_argument('--timeout', '-t', type=int, default=CONFIG.get('ffprobe_timeout', 15),
                        help=f'测试超时时间（秒） (默认: 15)')
    
    args = parser.parse_args()
    
    # 合并配置
    config = CONFIG.copy()
    config['server_source'] = args.source
    config['test_method'] = args.method
    config['max_concurrency'] = args.servers
    config['max_per_server'] = args.per_server
    config['retry_failed'] = args.retry
    config['ffprobe_timeout'] = args.timeout
    
    # 检查依赖
    if config['test_method'] == 'ffprobe' and FFPROBE_PATH is None:
        print("警告: ffprobe 未找到，尝试切换到 OpenCV...")
        if HAS_OPENCV:
            config['test_method'] = 'opencv'
            print("已切换到 OpenCV")
        else:
            print("错误: ffprobe 和 OpenCV 均不可用")
            print("请安装 FFmpeg 并确保 ffprobe 在 PATH 中，或运行: pip install opencv-python")
            sys.exit(1)
    
    if config['test_method'] == 'opencv' and not HAS_OPENCV:
        print("警告: OpenCV 未安装，切换到 ffprobe...")
        config['test_method'] = 'ffprobe'
        if FFPROBE_PATH is None:
            print("错误: ffprobe 也不可用")
            sys.exit(1)
    
    print("=" * 60)
    print("组播源探测工具 (Multicast Source Probe)")
    print("功能: 探测组播源，获取分辨率、编码等信息")
    print("=" * 60)
    print(f"服务器来源: {'快速测试' if config['server_source'] == 'quick' else '精确测试'}")
    print(f"测试方法: {config['test_method'].upper()}")
    print(f"最大总并发数: {config['max_concurrency']}")
    print(f"单服务器最大并发: {config['max_per_server']}")
    print(f"接续模式: {'是' if config['retry_failed'] else '否'}")
    print(f"超时时间: {config['ffprobe_timeout']}秒")
    print("=" * 60)
    
    # 获取组播源文件
    source_files = get_source_files(config['rtp_dir'])
    if not source_files:
        print("未找到组播源文件")
        print(f"请确保 {config['rtp_dir']} 目录下有组播源文件（如：北京电信.txt）")
        return
    
    print(f"\n找到 {len(source_files)} 个组播源文件:")
    print(print_city_list(source_files))
    
    # 选择城市
    if args.city:
        selected_cities = [args.city]
        if not any(city == args.city for city, _ in source_files):
            print(f"错误: 未找到城市 '{args.city}'")
            return
    elif len(source_files) > 1:
        try:
            choice = input(f"\n请选择城市 (1-{len(source_files)}，回车全部): ").strip()
            if choice:
                idx = int(choice) - 1
                if 0 <= idx < len(source_files):
                    selected_cities = [source_files[idx][0]]
                else:
                    print("无效选择")
                    return
            else:
                selected_cities = [city for city, _ in source_files]
        except (ValueError, KeyboardInterrupt):
            print("\n已取消")
            return
    else:
        selected_cities = [source_files[0][0]]
    
    # 处理选中的城市
    for city in selected_cities:
        source_file = os.path.join(config['rtp_dir'], f"{city}.txt")
        if not os.path.exists(source_file):
            print(f"文件不存在: {source_file}")
            continue
        process_city(city, source_file, config)
    
    print("\n" + "=" * 60)
    print("处理完成")
    print("=" * 60)


if __name__ == "__main__":
    main()