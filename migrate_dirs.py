#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迁移脚本：将旧目录结构迁移到新目录结构
- 存档文件：ip/存档/存档_城市_ip.txt -> ip/gateway/城市_gateway.txt
- 历史文件：ip/城市_ip_history.txt -> ip/history/城市_ip_history.txt（智能合并）
- 低速文件：已存在 ip/slow/ 目录，无需迁移
"""

import os
import shutil
from pathlib import Path

# 获取脚本所在目录
SCRIPT_DIR = Path(__file__).parent.resolve()
ip_dir = SCRIPT_DIR / "ip"


def merge_history_files(old_file, new_file):
    """
    合并历史文件
    规则：按服务器地址合并，累加有效次数和无效次数
    """
    # 解析现有文件
    existing_data = {}
    existing_header = []
    
    if new_file.exists():
        with open(new_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        in_data = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                existing_header.append(line)
                if '服务器地址' in line:
                    in_data = True
                continue
            if in_data:
                parts = line.split('\t')
                if len(parts) >= 4:
                    server = parts[0].strip()
                    status = parts[1].strip()
                    success = int(parts[2]) if parts[2].isdigit() else 0
                    fail = int(parts[3]) if parts[3].isdigit() else 0
                    existing_data[server] = {
                        'status': status,
                        'success': success,
                        'fail': fail
                    }
    
    # 解析旧文件
    old_data = {}
    old_header = []
    
    # 读取旧文件（尝试多种编码）
    content = None
    for encoding in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
        try:
            with open(old_file, 'r', encoding=encoding) as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue
    
    if content is None:
        print(f"    无法读取 {old_file.name}，跳过")
        return 0
    
    lines = content.split('\n')
    in_data = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('#'):
            old_header.append(line)
            if '服务器地址' in line:
                in_data = True
            continue
        if in_data:
            parts = line.split('\t')
            if len(parts) >= 4:
                server = parts[0].strip()
                status = parts[1].strip()
                success = int(parts[2]) if parts[2].isdigit() else 0
                fail = int(parts[3]) if parts[3].isdigit() else 0
                old_data[server] = {
                    'status': status,
                    'success': success,
                    'fail': fail
                }
    
    # 合并数据
    merged_data = existing_data.copy()
    for server, data in old_data.items():
        if server in merged_data:
            # 累加次数
            merged_data[server]['success'] += data['success']
            merged_data[server]['fail'] += data['fail']
            # 状态判断：如果任一次有效则标记为有效
            if data['status'] == '有效' or merged_data[server]['status'] == '有效':
                merged_data[server]['status'] = '有效'
            else:
                merged_data[server]['status'] = '无效'
        else:
            merged_data[server] = data
    
    # 使用新文件的表头（或旧文件的表头）
    header = existing_header if existing_header else old_header
    
    # 写入合并后的文件
    with open(new_file, 'w', encoding='utf-8') as f:
        for line in header:
            f.write(line + '\n')
        # 按服务器地址排序
        for server in sorted(merged_data.keys()):
            data = merged_data[server]
            f.write(f"{server}\t{data['status']}\t{data['success']}\t{data['fail']}\n")
    
    return len(merged_data)


def migrate_gateway_files():
    """迁移网关文件：ip/存档/存档_城市_ip.txt -> ip/gateway/城市_gateway.txt"""
    old_archive_dir = ip_dir / "存档"
    
    if not old_archive_dir.exists():
        print("未找到 ip/存档 目录，跳过网关文件迁移")
        return
    
    gateway_dir = ip_dir / "gateway"
    gateway_dir.mkdir(exist_ok=True)
    
    migrated = 0
    for old_file in old_archive_dir.glob("存档_*_ip.txt"):
        city = old_file.name.replace("存档_", "").replace("_ip.txt", "")
        new_file = gateway_dir / f"{city}_gateway.txt"
        
        print(f"  处理: {old_file.name} -> {new_file.name}")
        
        try:
            # 读取内容
            content = None
            for encoding in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
                try:
                    with open(old_file, 'r', encoding=encoding) as src:
                        content = src.read()
                    break
                except UnicodeDecodeError:
                    continue
            
            if content is None:
                print(f"    无法读取，跳过")
                continue
            
            if new_file.exists():
                # 合并网关文件（简单合并去重）
                with open(new_file, 'r', encoding='utf-8') as existing:
                    existing_content = existing.read()
                lines = set(existing_content.splitlines() + content.splitlines())
                merged_content = '\n'.join(sorted(lines))
                with open(new_file, 'w', encoding='utf-8') as dst:
                    dst.write(merged_content)
                print(f"    合并完成")
            else:
                with open(new_file, 'w', encoding='utf-8') as dst:
                    dst.write(content)
                print(f"    迁移完成")
            
            migrated += 1
            old_file.unlink()
        except Exception as e:
            print(f"    错误: {e}")
    
    if old_archive_dir.exists() and not list(old_archive_dir.iterdir()):
        old_archive_dir.rmdir()
        print("删除空目录: ip/存档")
    
    print(f"网关文件迁移完成，共迁移 {migrated} 个文件")


def migrate_history_files():
    """迁移历史文件：ip/城市_ip_history.txt -> ip/history/城市_ip_history.txt"""
    history_dir = ip_dir / "history"
    history_dir.mkdir(exist_ok=True)
    
    all_history_files = list(ip_dir.glob("*_ip_history.txt"))
    print(f"发现 {len(all_history_files)} 个历史文件:")
    for f in all_history_files:
        print(f"  - {f.name}")
    
    migrated = 0
    for old_file in all_history_files:
        if old_file.parent == history_dir:
            print(f"  跳过: {old_file.name} (已在 history 目录)")
            continue
        
        new_file = history_dir / old_file.name
        print(f"  处理: {old_file.name}")
        
        try:
            if new_file.exists():
                merged_count = merge_history_files(old_file, new_file)
                print(f"    合并完成，共 {merged_count} 条记录")
            else:
                # 读取并直接写入
                content = None
                for encoding in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
                    try:
                        with open(old_file, 'r', encoding=encoding) as src:
                            content = src.read()
                        break
                    except UnicodeDecodeError:
                        continue
                
                if content is None:
                    print(f"    无法读取，跳过")
                    continue
                
                with open(new_file, 'w', encoding='utf-8') as dst:
                    dst.write(content)
                print(f"    迁移完成")
            
            migrated += 1
            old_file.unlink()
            print(f"    已删除原文件")
        except Exception as e:
            print(f"    错误: {e}")
    
    print(f"历史文件迁移完成，共迁移 {migrated} 个文件")


def ensure_slow_directory():
    """确保 slow 目录存在"""
    slow_dir = ip_dir / "slow"
    if slow_dir.exists():
        files = list(slow_dir.glob("*.txt"))
        print(f"低速文件目录已存在: slow/ ({len(files)} 个文件)")
    else:
        slow_dir.mkdir(exist_ok=True)
        print("创建 slow 目录")


def main():
    print("=" * 60)
    print("IPTV 目录结构迁移工具")
    print("=" * 60)
    print(f"脚本目录: {SCRIPT_DIR}")
    print(f"IP目录: {ip_dir}")
    print()
    
    if not ip_dir.exists():
        print("错误：ip 目录不存在")
        return
    
    print("\n--- 迁移网关文件 ---")
    migrate_gateway_files()
    
    print("\n--- 迁移历史文件 ---")
    migrate_history_files()
    
    print("\n--- 确认低速文件目录 ---")
    ensure_slow_directory()
    
    print("\n" + "=" * 60)
    print("迁移完成！")
    print("新目录结构:")
    print("  ip/")
    print("  ├── *_config.txt")
    print("  ├── *_ip.txt")
    print("  ├── *_ip_precise.txt")
    print("  ├── *_ip_quick.txt")
    print("  ├── gateway/")
    print("  │   └── *_gateway.txt")
    print("  ├── history/")
    print("  │   └── *_ip_history.txt")
    print("  └── slow/")
    print("      ├── *_ip_precise_slow.txt")
    print("      └── *_ip_quick_slow.txt")
    print("=" * 60)


if __name__ == "__main__":
    main()