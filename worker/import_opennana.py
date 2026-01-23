#!/usr/bin/env python3
"""
OpenNana Prompt Gallery 导入脚本

工作流程:
1. 从 opennana.com 获取 prompts.json
2. 解析出有 Twitter/X 来源的 prompts
3. 尝试通过 main.py 的 process_twitter_url 流程处理
4. 如果 Twitter 处理失败，直接使用原始 JSON 数据入库

环境变量:
  DATABASE_URL - PostgreSQL 连接字符串 (必需)
  AI_MODEL     - AI 模型 (默认: openai)

用法:
  python import_opennana.py                    # 导入所有有 X 来源的 prompts
  python import_opennana.py --limit 10         # 限制导入数量
  python import_opennana.py --skip-twitter     # 跳过 Twitter 处理，直接用原始数据
  python import_opennana.py --dry-run          # 预览模式，不写入数据库
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# 加载环境变量
try:
    from dotenv import load_dotenv

    root_dir = Path(__file__).parent.parent
    env_local = root_dir / ".env.local"
    env_file = root_dir / ".env"
    
    if env_local.exists():
        load_dotenv(env_local)
        print(f"✓ 已加载: {env_local}")
    elif env_file.exists():
        load_dotenv(env_file)
        print(f"✓ 已加载: {env_file}")
except ImportError:
    pass

# 导入主模块的数据库类和处理函数
from main import Database, AI_MODEL

# AI 处理适配函数 (统一使用 prompt_utils)
from prompt_utils import process_tweet_for_import

# ========== 配置 ==========
# 新 API 端点
OPENNANA_API_BASE = "https://api.opennana.com/api/prompts"
OPENNANA_LIST_API = OPENNANA_API_BASE  # GET ?page=1&limit=20&sort=created_at&order=DESC
# 详情 API: GET https://api.opennana.com/api/prompts/{slug}

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# 数据缓存目录
CACHE_DIR = Path(__file__).parent / "cache"
PROMPTS_CACHE_FILE = CACHE_DIR / "prompts.json"
PROGRESS_FILE = CACHE_DIR / "import_progress.json"

# 失败记录输出目录
FAILED_OUTPUT_DIR = Path(__file__).parent / "failed_imports"

# 分类由 process_tweet_for_import 统一处理，无需单独导入标签映射


def fetch_prompt_list(page: int = 1, limit: int = 100) -> Optional[Dict]:
    """
    获取 prompt 列表（单页）

    Args:
        page: 页码
        limit: 每页数量

    Returns:
        API 响应数据或 None
    """
    url = f"{OPENNANA_LIST_API}?page={page}&limit={limit}&sort=created_at&order=DESC"

    try:
        response = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://opennana.com/",
            "Origin": "https://opennana.com"
        })
        response.raise_for_status()
        data = response.json()

        if data.get("status") == 200:
            return data.get("data", {})
        else:
            print(f"❌ API 返回错误: {data.get('msg', 'Unknown error')}")
            return None
    except Exception as e:
        print(f"❌ 获取列表失败 (page={page}): {e}")
        return None


def fetch_prompt_detail(slug: str) -> Optional[Dict]:
    """
    获取单个 prompt 的详情

    Args:
        slug: prompt 的 slug，如 "prompt-1128"

    Returns:
        详情数据或 None
    """
    url = f"{OPENNANA_API_BASE}/{slug}"

    try:
        response = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://opennana.com/",
            "Origin": "https://opennana.com"
        })
        response.raise_for_status()
        data = response.json()

        if data.get("status") == 200:
            return data.get("data", {})
        else:
            return None
    except Exception as e:
        print(f"⚠️ 获取详情失败 ({slug}): {e}")
        return None


def fetch_opennana_data(force_refresh: bool = False, fetch_details: bool = True, max_items: int = None, max_pages: int = 2, page_size: int = 20) -> Optional[Dict]:
    """
    从 OpenNana 新 API 获取数据，支持本地缓存

    Args:
        force_refresh: 强制从远程获取，忽略本地缓存
        fetch_details: 是否获取详情（用于完整导入）
        max_items: 最大获取数量（用于测试），None 表示不限制
        max_pages: 最大获取页数（默认 2）
        page_size: 每页获取数量（默认 20）

    Returns:
        格式化的数据: {"total": int, "items": [...]}
    """
    # 创建缓存目录
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 检查本地缓存
    if not force_refresh and PROMPTS_CACHE_FILE.exists():
        try:
            with open(PROMPTS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            total = data.get("total", len(data.get("items", [])))
            cache_time = PROMPTS_CACHE_FILE.stat().st_mtime
            cache_date = datetime.fromtimestamp(cache_time).strftime("%Y-%m-%d %H:%M:%S")

            print(f"📦 使用本地缓存: {PROMPTS_CACHE_FILE}")
            print(f"   缓存时间: {cache_date}")
            print(f"   共 {total} 条记录")
            print(f"   (使用 --refresh 强制更新缓存)")

            return data
        except Exception as e:
            print(f"⚠️ 读取缓存失败: {e}，重新获取...")

    # 从新 API 获取数据
    print(f"📡 正在从新 API 获取数据...")
    print(f"   列表 API: {OPENNANA_LIST_API}")
    print(f"   配置: max_pages={max_pages}, page_size={page_size}")

    all_items = []
    page = 1
    limit = page_size  # 每页获取数量

    # 1. 先获取所有列表数据
    while True:
        print(f"   📄 获取列表第 {page} 页...")
        list_data = fetch_prompt_list(page=page, limit=limit)

        if not list_data:
            break

        items = list_data.get("items", [])
        pagination = list_data.get("pagination", {})

        if not items:
            break

        all_items.extend(items)

        total_pages = pagination.get("total_pages", 1)
        has_more = pagination.get("has_more", False)

        print(f"      获取到 {len(items)} 条，共 {pagination.get('total', '?')} 条")

        # 如果设置了最大数量限制，检查是否达到
        if max_items and len(all_items) >= max_items:
            all_items = all_items[:max_items]
            print(f"   ⚡ 达到最大数量限制 ({max_items})，停止获取列表")
            break

        # 如果设置了最大页数限制，检查是否达到
        if max_pages and page >= max_pages:
            print(f"   ⚡ 达到最大页数限制 ({max_pages} 页)，停止获取列表")
            break

        if not has_more or page >= total_pages:
            break

        page += 1

    if not all_items:
        print("❌ 未获取到任何数据")
        return None

    print(f"✅ 列表获取完成: 共 {len(all_items)} 条")

    # 2. 获取详情（如果需要）
    if fetch_details:
        print(f"📡 正在获取详情...")
        detailed_items = []

        for i, item in enumerate(all_items, 1):
            slug = item.get("slug")
            if not slug:
                continue

            if i % 50 == 0 or i == len(all_items):
                print(f"   进度: {i}/{len(all_items)}")

            detail = fetch_prompt_detail(slug)
            if detail:
                # 转换为兼容旧格式的数据结构
                converted = convert_to_legacy_format(detail)
                detailed_items.append(converted)
            else:
                # 详情获取失败，使用列表中的基础数据
                detailed_items.append({
                    "id": item.get("id"),
                    "slug": slug,
                    "title": item.get("title", "Untitled"),
                    "images": [item.get("cover_image")] if item.get("cover_image") else [],
                    "prompts": [],
                    "tags": [],
                    "source": None
                })

        all_items = detailed_items
        print(f"✅ 详情获取完成: {len(detailed_items)} 条")

    # 构建返回数据
    result = {
        "total": len(all_items),
        "items": all_items
    }

    # 保存到本地缓存
    try:
        with open(PROMPTS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"💾 已缓存到: {PROMPTS_CACHE_FILE}")
    except Exception as e:
        print(f"⚠️ 保存缓存失败: {e}")

    return result


def convert_to_legacy_format(detail: Dict) -> Dict:
    """
    将新 API 的详情数据转换为兼容旧格式的数据结构

    新 API 字段:
        - source_url: Twitter/X 链接
        - source_name: 作者名
        - prompts: [{text, type}] 数组
        - images: 图片 URL 数组
        - tags: 标签数组

    旧格式字段:
        - source: {url, name}
        - prompts: [string] 数组
        - images: [string] 数组 (相对路径)
        - tags: [string] 数组
    """
    # 提取提示词文本（优先英文，否则中文）
    prompts_data = detail.get("prompts", [])
    prompt_texts = []

    # 优先使用英文提示词
    for p in prompts_data:
        if p.get("type") == "en" and p.get("text"):
            prompt_texts.append(p["text"])
            break

    # 如果没有英文，使用中文
    if not prompt_texts:
        for p in prompts_data:
            if p.get("text"):
                prompt_texts.append(p["text"])
                break

    # 构建 source 对象
    source = None
    if detail.get("source_url"):
        source = {
            "url": detail.get("source_url"),
            "name": detail.get("source_name", "")
        }

    return {
        "id": detail.get("id"),
        "slug": detail.get("slug"),
        "title": detail.get("title", "Untitled"),
        "prompts": prompt_texts,
        "images": detail.get("images", []),
        "tags": detail.get("tags", []),
        "source": source,
        "model": detail.get("model"),
        # 保留原始数据供需要时使用
        "_raw_prompts": prompts_data
    }


def load_progress() -> Dict:
    """加载处理进度"""
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed_ids": [], "last_updated": None}


def save_progress(progress: Dict):
    """保存处理进度"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    progress["last_updated"] = datetime.now(timezone.utc).isoformat()
    
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存进度失败: {e}")


def clear_progress():
    """清除处理进度"""
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("🗑️ 已清除处理进度")


def extract_twitter_url(source: Dict) -> Optional[str]:
    """从 source 对象中提取 Twitter/X URL"""
    if not source:
        return None
    
    url = source.get("url", "")
    
    # 检查是否是 Twitter/X 链接
    if re.match(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+", url):
        # 标准化为 x.com
        return url.replace("twitter.com", "x.com")
    
    return None


def process_opennana_item(db: Database, item: Dict, skip_twitter: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    """
    处理单个 OpenNana 条目 - 使用统一处理函数

    策略:
    - 必须有 Twitter URL 且能获取图片才入库
    - 使用统一处理函数 process_tweet_for_import

    返回: {"success": bool, "method": str, "error": str or None, "twitter_failed": bool}
    """
    source = item.get("source") or {}

    # 提取 Twitter URL
    twitter_url = extract_twitter_url(source)

    # 获取原始提示词
    prompts = item.get("prompts", [])
    raw_prompt = prompts[0] if prompts else ""

    if not raw_prompt:
        return {"success": False, "method": "skipped", "error": "No prompt text", "twitter_failed": False}

    # 必须有 Twitter URL
    if not twitter_url:
        return {"success": False, "method": "skipped", "error": "No Twitter URL", "twitter_failed": False}

    # 使用统一处理函数
    result = process_tweet_for_import(
        db=db,
        tweet_url=twitter_url,
        raw_text=raw_prompt,
        import_source="opennana",
        ai_model=AI_MODEL,
        dry_run=dry_run
    )

    return result


def save_failed_twitter_items(failed_twitter_items: List[Dict], timestamp: str):
    """保存 Twitter 处理失败的条目到文件供人工处理"""
    if not failed_twitter_items:
        return None
    
    # 创建输出目录
    FAILED_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 生成文件名
    filename = f"twitter_failed_{timestamp}.json"
    filepath = FAILED_OUTPUT_DIR / filename
    
    # 保存为 JSON 文件
    output_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(failed_twitter_items),
        "description": "Twitter 图片获取失败的条目（未入库）",
        "instructions": [
            "这些条目的 Twitter 图片获取失败，未入库",
            "人工处理步骤:",
            "1. 访问 twitter_url 获取高清图片 URL",
            "2. 手动入库或使用脚本处理",
            "3. 或使用 --skip-twitter 跳过 Twitter 处理，直接用 OpenNana 图片入库"
        ],
        "items": failed_twitter_items
    }
    
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    return filepath


def run_import(limit: int = None, skip_twitter: bool = False, dry_run: bool = False,
               only_twitter: bool = False, start_id: int = None, force_refresh: bool = False,
               resume: bool = True, reset_progress: bool = False,
               max_pages: int = 2, page_size: int = 20):
    """
    运行导入流程

    Args:
        limit: 限制处理数量
        skip_twitter: 跳过 Twitter 处理
        dry_run: 预览模式
        only_twitter: 仅处理有 X 来源的
        start_id: 从指定 ID 开始
        force_refresh: 强制刷新缓存
        resume: 断点续传（默认开启）
        reset_progress: 重置进度
        max_pages: 最大获取页数
        page_size: 每页获取数量
    """
    print("=" * 70)
    print("📦 OpenNana Prompt Gallery 导入")
    print("=" * 70)
    print(f"数据源: {OPENNANA_API_BASE}")
    print(f"获取配置: max_pages={max_pages}, page_size={page_size}")
    print(f"跳过 Twitter 处理: {skip_twitter}")
    print(f"仅处理有 X 来源的: {only_twitter}")
    print(f"预览模式: {dry_run}")
    print(f"断点续传: {resume}")
    if limit:
        print(f"限制数量: {limit}")
    if start_id:
        print(f"起始 ID: {start_id}")
    print("=" * 70)
    
    # 重置进度
    if reset_progress:
        clear_progress()
    
    # 检查配置
    if not DATABASE_URL:
        print("❌ 缺少 DATABASE_URL 环境变量")
        sys.exit(1)
    
    # 获取数据（支持缓存）
    data = fetch_opennana_data(force_refresh=force_refresh, max_pages=max_pages, page_size=page_size)
    if not data:
        sys.exit(1)
    
    items = data.get("items", [])
    
    # 如果只处理有 Twitter 来源的
    if only_twitter:
        items = [item for item in items if extract_twitter_url(item.get("source"))]
        print(f"📊 有 X 来源的条目: {len(items)}")
    
    # 按 ID 从小到大排序
    items = sorted(items, key=lambda x: x.get("id", 0))
    print(f"📊 按 ID 升序排列")
    
    # 如果指定了起始 ID
    if start_id:
        items = [item for item in items if item.get("id", 0) >= start_id]
        print(f"📊 ID >= {start_id} 的条目: {len(items)}")
    
    # 加载进度，过滤已处理的条目
    progress = load_progress()
    processed_ids = set(progress.get("processed_ids", []))
    
    if resume and processed_ids:
        original_count = len(items)
        items = [item for item in items if item.get("id") not in processed_ids]
        skipped_count = original_count - len(items)
        
        if skipped_count > 0:
            print(f"📊 已处理（跳过）: {skipped_count}")
            print(f"   上次更新: {progress.get('last_updated', 'N/A')}")
    
    # 限制数量
    if limit:
        items = items[:limit]
    
    total_items = len(items)
    print(f"\n🔄 准备处理 {total_items} 条记录...\n")
    
    if total_items == 0:
        print("✅ 没有需要处理的记录")
        return
    
    # 连接数据库
    db = Database(DATABASE_URL)
    
    try:
        db.connect()
        print("✅ 数据库连接成功\n")
    except Exception as e:
        print(f"❌ 数据库连接失败: {e}")
        sys.exit(1)
    
    # 统计
    stats = {
        "total": len(items),
        "processed": 0,
        "success_twitter": 0,
        "success_json": 0,
        "skipped": 0,
        "failed": 0,
        "twitter_failed": 0,
    }
    
    failed_items = []
    failed_twitter_items = []  # Twitter 处理失败的条目
    
    # 生成时间戳
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 定期刷新失败文件的间隔（每 20 条）
    FLUSH_INTERVAL = 20
    failed_file = None
    
    try:
        for i, item in enumerate(items, 1):
            item_id = item.get("id", "?")
            title = item.get("title", "Untitled")[:40]
            source = item.get("source") or {}
            twitter_url = extract_twitter_url(source)
            
            # 显示进度条
            progress_pct = (i / total_items) * 100
            print(f"[{i}/{total_items}] ({progress_pct:.1f}%) ID={item_id}: {title}")
            
            if twitter_url:
                print(f"   🔗 X: {twitter_url}")
            
            result = process_opennana_item(db, item, skip_twitter=skip_twitter, dry_run=dry_run)
            stats["processed"] += 1
            
            # 记录 Twitter 处理失败的条目
            if result.get("twitter_failed"):
                stats["twitter_failed"] += 1

                # 新 API 返回的是完整图片 URL
                images = item.get("images", [])
                
                failed_twitter_items.append({
                    "id": item_id,
                    "title": item.get("title", "Untitled"),
                    "twitter_url": twitter_url,
                    "error": result.get("twitter_error", "Unknown error"),
                    "saved_to_db": result.get("success", False),  # 是否已入库（使用备用图片）
                    # 用于人工处理的关键数据
                    "prompt_preview": (item.get("prompts", [""])[0][:200] + "...") if item.get("prompts") else None,
                    "full_prompt": item.get("prompts", [""])[0] if item.get("prompts") else None,  # 完整提示词
                    "images": images[:5],  # 保留前5张图片URL
                    "tags": item.get("tags", []),
                    "model": item.get("model"),
                    "source_name": (item.get("source") or {}).get("name"),
                })
            
            if result["success"]:
                if result["method"] == "hybrid":
                    stats["success_twitter"] += 1
                    print(f"   ✅ 成功入库 (Twitter图片+AI分类)")
                elif result["method"] == "json_direct":
                    stats["success_json"] += 1
                    print(f"   ✅ 成功入库 (OpenNana图片+AI分类)")
                elif result["method"] == "dry_run":
                    print(f"   ✅ 预览通过")
            else:
                if result["method"] == "skipped":
                    stats["skipped"] += 1
                    print(f"   ⏭️ 跳过: {result['error']}")
                elif result["method"] == "twitter_failed":
                    # Twitter 失败，不入库，记录到文件
                    print(f"   📝 记录到失败文件 (Twitter图片获取失败)")
                elif result["method"] == "save_failed":
                    stats["failed"] += 1
                    failed_items.append({"id": item_id, "title": title, "error": result["error"]})
                    print(f"   ❌ 保存失败: {result['error']}")
                else:
                    stats["failed"] += 1
                    failed_items.append({"id": item_id, "title": title, "error": result["error"]})
                    print(f"   ❌ 失败: {result['error']}")
            
            # 保存进度（每处理一条就保存，支持中断续传）
            if not dry_run and item_id != "?":
                processed_ids.add(item_id)
                # 每处理 10 条保存一次进度，减少 IO
                if i % 10 == 0 or i == total_items:
                    save_progress({"processed_ids": list(processed_ids)})
            
            # 定期刷新失败文件（每 FLUSH_INTERVAL 条或最后一条）
            if not dry_run and failed_twitter_items and (i % FLUSH_INTERVAL == 0 or i == total_items):
                failed_file = save_failed_twitter_items(failed_twitter_items, timestamp)
                print(f"   💾 已刷新失败记录到文件 ({len(failed_twitter_items)} 条)")
            
            print()
        
        # 最终保存 Twitter 处理失败的条目到文件
        if failed_twitter_items and not dry_run:
            failed_file = save_failed_twitter_items(failed_twitter_items, timestamp)
        
        # 输出统计
        print("=" * 70)
        print("📊 导入完成 - 统计汇总")
        print("=" * 70)
        print(f"\n总计: {stats['total']}")
        print(f"已处理: {stats['processed']}")
        print(f"✅ 成功 (Twitter): {stats['success_twitter']}")
        print(f"✅ 成功 (JSON): {stats['success_json']}")
        print(f"⏭️ 跳过: {stats['skipped']}")
        print(f"❌ 失败: {stats['failed']}")
        print(f"⚠️ Twitter 处理失败: {stats['twitter_failed']}")
        
        if failed_items:
            print("\n" + "=" * 70)
            print("❌ 完全失败的条目:")
            print("=" * 70)
            for item in failed_items[:10]:
                print(f"   ID={item['id']}: {item['title']}")
                print(f"   错误: {item['error']}")
                print()
            if len(failed_items) > 10:
                print(f"   ... 还有 {len(failed_items) - 10} 条失败记录")
        
        # 显示失败文件信息
        if failed_file:
            print("\n" + "=" * 70)
            print("📁 Twitter 图片获取失败的条目已保存:")
            print("=" * 70)
            print(f"   文件: {failed_file}")
            print(f"   数量: {len(failed_twitter_items)}")
            print(f"   说明: 这些条目未入库，需要人工处理")
            print(f"         或使用 --skip-twitter 跳过 Twitter 直接入库")
        
        print("\n" + "=" * 70)
        
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description="从 OpenNana Prompt Gallery 导入数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 导入所有数据（自动断点续传）
  python import_opennana.py
  
  # 仅导入有 X 来源的，限制 10 条
  python import_opennana.py --only-twitter --limit 10
  
  # 跳过 Twitter 处理，直接用 JSON 数据
  python import_opennana.py --skip-twitter
  
  # 强制刷新缓存
  python import_opennana.py --refresh
  
  # 重置进度，从头开始
  python import_opennana.py --reset
  
  # 不使用断点续传
  python import_opennana.py --no-resume
  
  # 预览模式
  python import_opennana.py --dry-run --limit 5

缓存文件:
  worker/cache/prompts.json        - JSON 数据缓存
  worker/cache/import_progress.json - 处理进度
        """
    )
    
    parser.add_argument("--limit", "-l", type=int, help="限制导入数量")
    parser.add_argument("--skip-twitter", "-s", action="store_true", 
                        help="跳过 Twitter 处理，直接使用 JSON 数据")
    parser.add_argument("--only-twitter", "-t", action="store_true",
                        help="仅处理有 X 来源的条目")
    parser.add_argument("--dry-run", "-d", action="store_true",
                        help="预览模式，不写入数据库")
    parser.add_argument("--start-id", type=int,
                        help="从指定 ID 开始处理 (ID 从大到小)")
    parser.add_argument("--refresh", "-r", action="store_true",
                        help="强制刷新缓存，重新下载 prompts.json")
    parser.add_argument("--no-resume", action="store_true",
                        help="禁用断点续传，处理所有条目")
    parser.add_argument("--reset", action="store_true",
                        help="重置进度，从头开始处理")
    parser.add_argument("--max-pages", type=int, default=2,
                        help="最大获取页数 (默认: 2)")
    parser.add_argument("--page-size", type=int, default=20,
                        help="每页获取数量 (默认: 20)")

    args = parser.parse_args()

    run_import(
        limit=args.limit,
        skip_twitter=args.skip_twitter,
        dry_run=args.dry_run,
        only_twitter=args.only_twitter,
        start_id=args.start_id,
        force_refresh=args.refresh,
        resume=not args.no_resume,
        reset_progress=args.reset,
        max_pages=args.max_pages,
        page_size=args.page_size
    )


if __name__ == "__main__":
    main()

