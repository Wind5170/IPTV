#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组播源探测工具 - 获取视频流分辨率、编码等信息
输出文件：{城市}_probe.txt

服务器读取策略：
1. 优先读取达标服务器文件（_quick.txt 或 _precise.txt）
2. 只有当没有达标服务器时，才从低速服务器中补充（slow/目录）
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
import signal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置参数（集中调整） ====================
CONFIG = {
    # 服务器来源: 'quick' 使用 _quick.txt（快速测试），'precise' 使用 _precise.txt（精确测试）
    "server_source": "precise",
    
    # 测试方法: 'ffprobe' 或 'opencv'
    "test_method": "ffprobe",
    
    # 测试模式: 
    #   "full" - 全部重新测试（测试所有组播地址）
    #   "incremental" - 接续测试（只测试无效或新增的组播源）
    "test_mode": "incremental",
    
    # 最大总并发数（控制同时运行的测试线程数）
    "max_concurrency": 100,
    
    # 每个服务器最大并发数（避免单服务器过载）
    "max_per_server": 10,
    
    # 是否启用低速服务器备用（仅当没有达标服务器时使用）
    "use_slow_servers": False,
    
    # 低速服务器最大数量（当没有达标服务器时补充）
    "max_slow_servers": 5,
    
    # ffprobe 超时时间（秒）
    "ffprobe_timeout": 20,
    
    # 调试模式
    "debug": False,
    
    # 目录配置
    "rtp_dir": "rtp",
    "ip_dir": "ip",
    
    # 自动模式（用于CI/CD，不显示进度条）
    "auto_mode": False,
}

# ==================== 全局退出标志 ====================
should_exit = False

def signal_handler(signum, frame):
    """处理 Ctrl+C 信号"""
    global should_exit
    print("\n\n⚠ 收到中断信号，正在优雅退出...")
    should_exit = True

# 注册信号处理器
signal.signal(signal.SIGINT, signal_handler)

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


def parse_server_file(city, ip_dir='ip', server_source='quick', max_servers=10, use_slow_servers=True, max_slow_servers=20, verbose=True):
    """
    解析服务器文件，支持快速测试和精确测试结果
    新格式：{city}_ip_quick.txt / {city}_ip_precise.txt
    低速服务器：{city}_ip_quick_slow.txt / {city}_ip_precise_slow.txt (在 slow 目录下)
    
    读取策略：
    1. 优先读取达标服务器文件（_quick.txt 或 _precise.txt）
    2. 只有当没有达标服务器时，才从低速服务器中补充
    """
    servers = []
    slow_servers = []
    main_city_name = extract_main_city_name(city)
    
    source_name = "快速测试" if server_source == 'quick' else "精确测试"
    source_file_suffix = "quick" if server_source == 'quick' else "precise"
    
    # 1. 读取达标服务器 - 尝试多种文件格式
    possible_files = [
        os.path.join(ip_dir, f"{main_city_name}_ip_{source_file_suffix}.txt"),
        os.path.join(ip_dir, f"{main_city_name}_ip.txt"),
        os.path.join(ip_dir, f"{main_city_name}_ip_result.txt"),
    ]
    
    server_file = None
    for pf in possible_files:
        if os.path.exists(pf):
            server_file = pf
            break
    
    # 读取达标服务器
    if server_file:
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
            if verbose:
                print(f"  读取{source_name}服务器: {os.path.basename(server_file)} ({len(servers)} 个)")
        except Exception as e:
            if verbose:
                print(f"  读取达标服务器文件失败: {e}")
    else:
        if verbose:
            print(f"  未找到{source_name}达标服务器文件: {main_city_name}_ip_*.txt")
    
    # 记录达标服务器数量
    current_count = len(servers)
    
    # 2. 只有当没有达标服务器时，才从低速服务器中补充
    if use_slow_servers and current_count == 0:
        slow_dir = os.path.join(ip_dir, "slow")
        possible_slow_files = [
            os.path.join(slow_dir, f"{main_city_name}_ip_{source_file_suffix}_slow.txt"),
            os.path.join(slow_dir, f"{main_city_name}_ip_slow.txt"),
        ]
        
        slow_file = None
        for psf in possible_slow_files:
            if os.path.exists(psf):
                slow_file = psf
                break
        
        if slow_file:
            try:
                with open(slow_file, 'r', encoding='utf-8') as f:
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
                                    slow_servers.append(server)
                        elif ':' in line:
                            slow_servers.append(line.strip())
            except Exception as e:
                if verbose:
                    print(f"  读取低速服务器文件失败: {e}")
            
            # 补充低速服务器（最多 max_slow_servers 个）
            supplement_count = min(len(slow_servers), max_slow_servers)
            
            if supplement_count > 0:
                servers.extend(slow_servers[:supplement_count])
                if verbose:
                    print(f"  {source_name}达标服务器: 0 个，使用低速服务器: {supplement_count} 个")
            elif verbose:
                print(f"  未找到{source_name}低速服务器")
        elif verbose:
            print(f"  未找到{source_name}低速服务器")
    elif use_slow_servers and current_count > 0 and verbose:
        print(f"  {source_name}达标服务器: {current_count} 个（充足，不使用低速服务器）")
    
    # 限制服务器数量
    if max_servers > 0 and len(servers) > max_servers:
        servers = servers[:max_servers]
    
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
        sample_rate = 0
        
        for stream in info.get('streams', []):
            codec_type = stream.get('codec_type', '')
            if codec_type == 'video':
                has_video = True
                width = stream.get('width', 0) or stream.get('coded_width', 0)
                height = stream.get('height', 0) or stream.get('coded_height', 0)
                codec_name = stream.get('codec_name', '')
                
                bit_rate_str = stream.get('bit_rate', '')
                if bit_rate_str:
                    try:
                        bit_rate = int(bit_rate_str) // 1000
                    except:
                        pass
            elif codec_type == 'audio':
                has_audio = True
                if not has_video and not codec_name:
                    codec_name = stream.get('codec_name', '')
                sample_rate = stream.get('sample_rate', 0)
        
        # 如果没有视频流码率，尝试从格式中获取
        if bit_rate == 0:
            format_bit_rate = info.get('format', {}).get('bit_rate', '')
            if format_bit_rate:
                try:
                    bit_rate = int(format_bit_rate) // 1000
                except:
                    pass
        
        # 构建分辨率字符串
        if width > 0 and height > 0:
            resolution = f"{width}x{height}"
        elif height > 0:
            # 只有高度，推算宽度（假设16:9）
            if height == 1080:
                resolution = "1920x1080"
            elif height == 720:
                resolution = "1280x720"
            elif height == 576:
                resolution = "720x576"
            elif height == 480:
                resolution = "720x480"
            else:
                resolution = f"{height}p"
        else:
            resolution = ""
        
        # 音频专用：显示采样率
        if not has_video and has_audio and sample_rate > 0:
            codec_name = f"{codec_name} ({sample_rate//1000}kHz)" if codec_name else f"音频 {sample_rate//1000}kHz"
        
        is_valid = has_video or has_audio
        
        # 调试输出
        if CONFIG.get('debug', False):
            print(f"    ffprobe 结果: 有效={is_valid}, 分辨率={resolution}, 编码={codec_name}, 码率={bit_rate}kbps")
        
        return is_valid, resolution, codec_name, bit_rate, response_time
        
    except subprocess.TimeoutExpired:
        return False, "", "", 0, 99999
    except Exception as e:
        if CONFIG.get('debug', False):
            print(f"    ffprobe 异常: {e}")
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


def process_city(city, source_file, config, auto_mode=False):
    """处理单个城市的组播源探测 - 按总并发数分配任务"""
    global should_exit
    should_exit = False
    
    # 确定服务器来源名称
    source_name = "快速测试" if config['server_source'] == 'quick' else "精确测试"
    source_file_suffix = "quick" if config['server_source'] == 'quick' else "precise"
    
    # auto 模式下也显示开始信息
    if auto_mode:
        print(f"\n[{city}] 开始处理...", flush=True)
    else:
        print(f"\n处理城市: {city}")
    
    # 读取服务器列表
    servers = parse_server_file(
        city, 
        config['ip_dir'], 
        config['server_source'], 
        config['max_concurrency'],
        config.get('use_slow_servers', True),
        config.get('max_slow_servers', 20),
        verbose=not auto_mode
    )
    
    if not servers:
        if auto_mode:
            print(f"[{city}] ✗ 跳过: 无{source_name}服务器", flush=True)
        else:
            print(f"  跳过: 没有可用的{source_name}服务器")
            print(f"  提示: 请确保 {city}_ip_{source_file_suffix}.txt 文件存在")
        return False
    
    # 读取组播源列表
    sources = parse_source_file(source_file)
    if not sources:
        if auto_mode:
            print(f"[{city}] ✗ 跳过: 无组播源", flush=True)
        else:
            print(f"  跳过: 没有找到组播源")
        return False
    
    num_servers = len(servers)
    num_sources = len(sources)
    
    if auto_mode:
        print(f"[{city}] 组播源: {num_sources}, 服务器: {num_servers}", flush=True)
    else:
        print(f"  组播源数量: {num_sources}")
        print(f"  {source_name}服务器数量: {num_servers}")
    
    # 选择测试函数
    if config['test_method'] == 'opencv' and HAS_OPENCV:
        test_func = test_with_opencv
        timeout = 15
    else:
        test_func = test_with_ffprobe
        timeout = config.get('ffprobe_timeout', 15)
    
    # 确定需要测试的源
    result_file = os.path.join(config['rtp_dir'], f"{city}_probe.txt")
    
    # 读取已有完整结果（保留详细信息）
    existing_results = {}  # key: addr, value: dict with all fields
    sources_to_test = []
    
    # 两种模式都读取已有结果
    if os.path.exists(result_file):
        try:
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
                        
                        existing_results[addr] = {
                            "name": name,
                            "addr": addr,
                            "addr_raw": addr_raw,
                            "status": status,
                            "resolution": resolution,
                            "codec": codec,
                            "response": response
                        }
        except Exception as e:
            if not auto_mode:
                print(f"  读取已有结果文件失败: {e}")
    
    # 根据测试模式决定需要测试的源
    if config.get('test_mode') == 'incremental':
        # 增量模式：只测试无效或新增的组播源
        for name, addr, cat in sources:
            if addr not in existing_results:
                sources_to_test.append((name, addr, cat))
            elif existing_results[addr]["status"] != "有效":
                sources_to_test.append((name, addr, cat))
        
        if auto_mode:
            print(f"[{city}] 接续模式: 需测试 {len(sources_to_test)}/{num_sources}", flush=True)
        elif not auto_mode:
            print(f"  接续模式: 需要测试 {len(sources_to_test)} 个源")
    else:
        # 全量模式：测试全部组播源
        sources_to_test = sources
        if auto_mode:
            print(f"[{city}] 全量模式: 测试全部 {len(sources_to_test)} 个", flush=True)
        elif not auto_mode:
            print(f"  全量模式: 测试全部 {len(sources_to_test)} 个源")
    
    if not sources_to_test:
        if auto_mode:
            print(f"[{city}] ✓ 全部已完成", flush=True)
        else:
            print(f"  所有组播源已完成测试")
        return True
    
    num_items = len(sources_to_test)
    max_total_concurrency = min(num_servers * config['max_per_server'], config['max_concurrency'])
    
    items_per_concurrency = math.ceil(num_items / max_total_concurrency) if max_total_concurrency > 0 else num_items
    
    # 分配服务器并发
    server_concurrency = [0] * num_servers
    remaining = max_total_concurrency
    for i in range(num_servers):
        if remaining <= 0:
            break
        can_assign = min(config['max_per_server'], remaining)
        server_concurrency[i] = can_assign
        remaining -= can_assign
    
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
    server_pool = []
    for i, con in enumerate(server_concurrency):
        server_pool.extend([servers[i]] * con)
    
    num_batches = math.ceil(num_items / items_per_concurrency)
    effective_threads = min(total_threads, num_batches)
    server_pool = server_pool[:effective_threads]
    
    batches = []
    for i in range(effective_threads):
        start = i * items_per_concurrency
        end = min(start + items_per_concurrency, num_items)
        if start < end:
            batches.append(sources_to_test[start:end])
    
    if not auto_mode:
        print(f"  总测试次数: {num_items}")
        print(f"  总并发线程数: {len(batches)} (最大并发 {max_total_concurrency})")
        print(f"  每个线程处理: {items_per_concurrency} 个地址")
    
    # 开始测试
    if auto_mode:
        print(f"[{city}] 测试中... ({num_items}个地址)", flush=True)
    else:
        print(f"\n  开始测试...")
        print("  (按 Ctrl+C 可安全中断)\n")
    
    new_results = []
    results_lock = threading.Lock()
    completed = 0
    valid_so_far = 0
    
    def test_batch(batch, server):
        nonlocal completed, valid_so_far
        local_results = []
        for name, addr, cat in batch:
            if should_exit:
                break
            
            try:
                is_valid, resolution, codec, bitrate, response = test_func(server, addr, timeout)
            except Exception:
                is_valid, resolution, codec, bitrate, response = False, "", "", 0, 99999
            
            with results_lock:
                completed += 1
                if is_valid:
                    valid_so_far += 1
                if not auto_mode:
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
                "response": str(int(response))
            })
        return local_results
    
    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        futures = []
        for batch, server in zip(batches, server_pool):
            futures.append(executor.submit(test_batch, batch, server))
        
        try:
            for future in as_completed(futures):
                if should_exit:
                    for f in futures:
                        f.cancel()
                    break
                try:
                    new_results.extend(future.result())
                except Exception as e:
                    if not auto_mode:
                        print(f"\n  批次测试异常: {e}")
        except KeyboardInterrupt:
            if not auto_mode:
                print("\n\n⚠ 用户中断，正在保存已完成的测试结果...")
            for future in futures:
                future.cancel()
    
    if not auto_mode:
        print()
    
    # ==================== 合并结果（全量和增量逻辑统一） ====================
    final_results = []
    
    for name, addr, cat in sources:
        # 优先使用新测试结果
        new_item = None
        for item in new_results:
            if item["addr"] == addr:
                new_item = item
                break
        
        if new_item:
            # 新测试结果，包含完整信息
            final_results.append(new_item)
        elif addr in existing_results:
            # 保留已有结果（保留原有的分辨率、编码等信息）
            existing = existing_results[addr]
            final_results.append({
                "name": existing["name"],
                "addr": addr,
                "addr_raw": existing["addr_raw"],
                "status": existing["status"],
                "resolution": existing.get("resolution", ""),
                "codec": existing.get("codec", ""),
                "response": existing.get("response", "0")
            })
        else:
            # 兜底（理论上不会进入）
            final_results.append({
                "name": name,
                "addr": addr,
                "addr_raw": addr.replace('rtp/', '').replace('udp/', ''),
                "status": "未知",
                "resolution": "",
                "codec": "",
                "response": "0"
            })
    
    # 保存结果
    try:
        with open(result_file, 'w', encoding='utf-8') as f:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            f.write(f"# {timestamp}_probe\n")
            if should_exit:
                f.write("# 注意: 测试被中断，结果不完整\n")
            f.write("# 频道名\t组播地址\t状态\t分辨率\t编码\t响应时间(ms)\n")
            
            valid_count = 0
            for item in final_results:
                status = item["status"]
                if status == "有效":
                    valid_count += 1
                resolution = item.get("resolution", "")
                codec = item.get("codec", "")
                response = item.get("response", "0")
                f.write(f"{item['name']}\t{item['addr_raw']}\t{status}\t{resolution}\t{codec}\t{response}\n")
        
        # auto 模式显示结果
        if auto_mode:
            print(f"[{city}] ✓ 完成: 有效 {valid_count}/{len(final_results)}", flush=True)
        else:
            print(f"\n  结果保存到: {result_file}")
            print(f"  统计: 有效 {valid_count}/{len(final_results)}")
        
    except Exception as e:
        if auto_mode:
            print(f"[{city}] ✗ 保存失败: {e}", flush=True)
        else:
            print(f"  保存结果失败: {e}")
        return False
    
    return not should_exit


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
    parser.add_argument('--test-mode', choices=['full', 'incremental'], default=CONFIG['test_mode'],
                        help=f'测试模式: full(全部重新测试) 或 incremental(接续测试) (默认: {CONFIG["test_mode"]})')
    parser.add_argument('--city', '-c', help='指定城市名称（可选）')
    parser.add_argument('--timeout', '-t', type=int, default=CONFIG.get('ffprobe_timeout', 15),
                        help=f'测试超时时间（秒） (默认: 15)')
    parser.add_argument('--no-slow', action='store_true', default=False,
                        help=f'禁用低速服务器备用功能')
    parser.add_argument('--max-slow', type=int, default=CONFIG.get('max_slow_servers', 20),
                        help=f'低速服务器最大数量 (默认: 20)')
    parser.add_argument('--auto', action='store_true', default=False,
                        help='自动模式（用于CI/CD，不显示进度条和交互）')
    parser.add_argument('--debug', action='store_true', default=False,
                        help='调试模式（显示ffprobe详细信息）')
    
    args = parser.parse_args()
    
    # 合并配置
    config = CONFIG.copy()
    config['server_source'] = args.source
    config['test_method'] = args.method
    config['max_concurrency'] = args.servers
    config['max_per_server'] = args.per_server
    config['test_mode'] = args.test_mode
    config['ffprobe_timeout'] = args.timeout
    config['use_slow_servers'] = not args.no_slow
    config['max_slow_servers'] = args.max_slow
    config['debug'] = args.debug
    auto_mode = args.auto
    
    # 测试模式描述
    test_mode_desc = "全部重新测试" if config['test_mode'] == 'full' else "接续测试（只测试无效或新增）"
    
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
    print(f"测试模式: {test_mode_desc}")
    print(f"服务器来源: {'快速测试' if config['server_source'] == 'quick' else '精确测试'}")
    print(f"测试方法: {config['test_method'].upper()}")
    print(f"最大总并发数: {config['max_concurrency']}")
    print(f"单服务器最大并发: {config['max_per_server']}")
    print(f"超时时间: {config['ffprobe_timeout']}秒")
    print(f"低速服务器备用: {'是' if config['use_slow_servers'] else '否'}")
    if config['use_slow_servers']:
        print(f"  规则: 仅当没有达标服务器时使用低速服务器")
        print(f"  最大数量: {config['max_slow_servers']}")
    print(f"调试模式: {'是' if config['debug'] else '否'}")
    print(f"自动模式: {'是' if auto_mode else '否'}")
    print("=" * 60)
    
    # 获取组播源文件
    source_files = get_source_files(config['rtp_dir'])
    if not source_files:
        print("未找到组播源文件")
        print(f"请确保 {config['rtp_dir']} 目录下有组播源文件（如：北京电信.txt）")
        return
    
    if not auto_mode and not args.city:
        print(f"\n找到 {len(source_files)} 个组播源文件:")
        print(print_city_list(source_files))
    
    # 选择城市
    if args.city:
        # 指定城市模式
        selected_cities = [args.city]
        if not any(city == args.city for city, _ in source_files):
            print(f"错误: 未找到城市 '{args.city}'")
            return
        # 如果指定了 auto_mode，在指定城市模式下也启用自动模式的特性（不显示进度条）
        if auto_mode:
            print(f"\n自动模式: 处理指定城市 {args.city}")
    elif auto_mode:
        # 自动模式：处理全部城市
        selected_cities = [city for city, _ in source_files]
        print(f"\n自动模式: 处理全部 {len(selected_cities)} 个城市")
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
    success_count = 0
    fail_count = 0
    
    for city in selected_cities:
        source_file = os.path.join(config['rtp_dir'], f"{city}.txt")
        if not os.path.exists(source_file):
            print(f"文件不存在: {source_file}")
            fail_count += 1
            continue
        if process_city(city, source_file, config, auto_mode):
            success_count += 1
        else:
            fail_count += 1
    
    print("\n" + "=" * 60)
    print("处理完成")
    print(f"成功: {success_count} 个城市")
    if fail_count > 0:
        print(f"失败: {fail_count} 个城市")
    print("=" * 60)


if __name__ == "__main__":
    main()