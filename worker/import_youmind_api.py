#!/usr/bin/env python3
"""
YouMind Nano Banana Pro Prompts 导入脚本

工作流程:
1. 从 YouMind API 获取提示词数据
2. 使用 AI 解析分类和标签
3. 写入数据库，标记 import_source = 'youmind'

环境变量:
  DATABASE_URL - PostgreSQL 连接字符串 (必需)
  AI_MODEL     - AI 模型 (默认: openai)

用法:
  python import_youmind_api.py                    # 导入所有提示词
  python import_youmind_api.py --limit 10         # 限制导入数量
  python import_youmind_api.py --dry-run          # 预览模式，不写入数据库
  python import_youmind_api.py --test             # 测试 API 连接
"""

import argparse
import json
import os
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
YOUMIND_API_URL = "https://youmind.com/youhome-api/prompts"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
IMPORT_SOURCE = "youmind"  # 导入来源标识

# 数据缓存目录
CACHE_DIR = Path(__file__).parent / "cache"
YOUMIND_CACHE_FILE = CACHE_DIR / "youmind_prompts.json"
PROGRESS_FILE = CACHE_DIR / "youmind_import_progress.json"

# 失败记录输出目录
FAILED_OUTPUT_DIR = Path(__file__).parent / "failed_imports"


def fetch_youmind_page(page: int = 1, limit: int = 30) -> Optional[Dict]:
    """
    从 YouMind API 获取单页数据

    Args:
        page: 页码（从 1 开始）
        limit: 每页数量

    Returns:
        API 响应数据
    """
    payload = {
        "model": "nano-banana-pro",
        "page": page,
        "limit": limit,
        "locale": "en-US",
        "campaign": "nano-banana-pro-prompts",
        "filterMode": "imageCategories"
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Origin": "https://youmind.com",
        "Referer": "https://youmind.com/nano-banana-pro-prompts",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"'
    }

    try:
        print(f"📡 请求 API: page={page}, limit={limit}")
        response = requests.post(YOUMIND_API_URL, json=payload, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()
        return data

    except requests.exceptions.Timeout:
        print(f"❌ 请求超时 (page={page})")
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ 请求失败 (page={page}): {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失败 (page={page}): {e}")
        return None


def fetch_all_youmind_data(force_refresh: bool = False, max_pages: int = None) -> Optional[List[Dict]]:
    """
    从 YouMind API 获取所有提示词数据，支持本地缓存

    Args:
        force_refresh: 强制从远程获取，忽略本地缓存
        max_pages: 最大页数限制

    Returns:
        提示词列表
    """
    # 创建缓存目录
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 检查本地缓存
    if not force_refresh and YOUMIND_CACHE_FILE.exists():
        try:
            with open(YOUMIND_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            cache_time = YOUMIND_CACHE_FILE.stat().st_mtime
            cache_date = datetime.fromtimestamp(cache_time).strftime("%Y-%m-%d %H:%M:%S")

            print(f"📦 使用本地缓存: {YOUMIND_CACHE_FILE}")
            print(f"   缓存时间: {cache_date}")
            print(f"   共 {len(data)} 条记录")
            print(f"   (使用 --refresh 强制更新缓存)")

            return data
        except Exception as e:
            print(f"⚠️ 读取缓存失败: {e}，重新获取...")

    # 从 API 获取所有数据
    print(f"🌐 正在从 API 获取数据...")

    all_prompts = []
    page = 1
    limit = 100

    while True:
        if max_pages and page > max_pages:
            print(f"⚠️ 达到最大页数限制: {max_pages}")
            break

        data = fetch_youmind_page(page=page, limit=limit)

        if not data:
            print(f"❌ 获取第 {page} 页失败")
            break

        # 解析响应数据结构
        # YouMind API 返回: {"prompts": [...], "total": 100, "hasMore": true}
        prompts = []

        if isinstance(data, list):
            prompts = data
        elif isinstance(data, dict):
            prompts = data.get("prompts") or data.get("data") or data.get("items") or []

        if not prompts:
            print(f"✅ 第 {page} 页无数据，已获取所有数据")
            break

        print(f"   ✓ 第 {page} 页: {len(prompts)} 条")
        all_prompts.extend(prompts)

        # 检查是否还有更多数据
        if isinstance(data, dict):
            # 优先使用 hasMore 标志
            has_more = data.get("hasMore")
            if has_more is False:
                total = data.get("total", len(all_prompts))
                print(f"✅ 已获取所有数据 ({len(all_prompts)}/{total})")
                break

            # 检查总数
            total = data.get("total") or data.get("totalCount")
            if total and len(all_prompts) >= total:
                print(f"✅ 已获取所有数据 ({len(all_prompts)}/{total})")
                break

        # 如果返回的数据少于 limit，说明没有更多数据了
        if len(prompts) < limit:
            print(f"✅ 已获取所有数据 (最后一页)")
            break

        page += 1

    if not all_prompts:
        print("❌ 未能获取到任何数据")
        return None

    print(f"✅ 共获取 {len(all_prompts)} 条提示词")

    # 保存到缓存
    try:
        with open(YOUMIND_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_prompts, f, ensure_ascii=False, indent=2)
        print(f"💾 已缓存到: {YOUMIND_CACHE_FILE}")
    except Exception as e:
        print(f"⚠️ 保存缓存失败: {e}")

    return all_prompts


def test_api():
    """测试 API 连接并显示响应结构"""
    print("=" * 70)
    print("🧪 测试 YouMind API")
    print("=" * 70)

    data = fetch_youmind_page(page=1, limit=2)

    if not data:
        print("❌ API 测试失败")
        return

    print("\n✅ API 响应成功")
    print("\n📋 响应结构:")
    print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])  # 只显示前 2000 字符

    # 分析数据结构
    print("\n" + "=" * 70)
    print("📊 数据结构分析:")
    print("=" * 70)

    if isinstance(data, list):
        print(f"✓ 响应类型: 数组")
        print(f"✓ 数组长度: {len(data)}")
        if data:
            print(f"✓ 第一个元素的键: {list(data[0].keys())}")
    elif isinstance(data, dict):
        print(f"✓ 响应类型: 对象")
        print(f"✓ 顶层键: {list(data.keys())}")

        # 尝试找到提示词数组
        for key in ["prompts", "data", "items", "results"]:
            if key in data and isinstance(data[key], list):
                print(f"✓ 找到提示词数组: data['{key}']")
                print(f"✓ 数组长度: {len(data[key])}")
                if data[key]:
                    print(f"✓ 第一个元素的键: {list(data[key][0].keys())}")
                break

    print("\n" + "=" * 70)


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


# 分类由 process_tweet_for_import 统一处理
# 如需从 tags 推断分类，可从 prompt_utils 导入:
# from prompt_utils import TAG_TO_CATEGORY, infer_category_from_tags


def save_failed_twitter_items(failed_twitter_items: List[Dict], timestamp: str):
    """保存 Twitter 处理失败的条目到文件供人工处理"""
    if not failed_twitter_items:
        return None

    # 创建输出目录
    FAILED_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 生成文件名
    filename = f"youmind_twitter_failed_{timestamp}.json"
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
            "3. 或使用 --skip-twitter 跳过 Twitter 处理，直接用 YouMind 图片入库"
        ],
        "items": failed_twitter_items
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    return filepath


def process_youmind_item(db: Database, item: Dict, skip_twitter: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    """
    处理单个 YouMind 提示词 - 使用统一处理函数

    策略:
    - 必须有 Twitter URL 且能获取图片才入库
    - 使用统一处理函数 process_tweet_for_import

    返回: {"success": bool, "method": str, "error": str or None, "twitter_failed": bool}
    """
    item_id = item.get("id", "unknown")

    # 获取原始提示词
    raw_prompt = item.get("content") or item.get("translatedContent") or item.get("description")

    if not raw_prompt:
        return {"success": False, "method": "skipped", "error": "No prompt text", "twitter_failed": False}

    # 提取 Twitter URL
    twitter_url = item.get("sourceLink")
    # 标准化为 x.com
    if twitter_url and "twitter.com" in twitter_url:
        twitter_url = twitter_url.replace("twitter.com", "x.com")

    # 必须有 Twitter URL
    if not twitter_url:
        return {"success": False, "method": "skipped", "error": "No Twitter URL", "twitter_failed": False}

    # 使用统一处理函数
    result = process_tweet_for_import(
        db=db,
        tweet_url=twitter_url,
        raw_text=raw_prompt,
        import_source=IMPORT_SOURCE,
        ai_model=AI_MODEL,
        dry_run=dry_run
    )

    return result


def run_import(limit: int = None, dry_run: bool = False, force_refresh: bool = False,
               resume: bool = True, reset_progress: bool = False, max_pages: int = None):
    """
    运行导入流程

    Args:
        limit: 限制处理数量
        dry_run: 预览模式
        force_refresh: 强制刷新缓存
        resume: 断点续传（默认开启）
        reset_progress: 重置进度
        max_pages: 最大页数
    """
    print("=" * 70)
    print("🌐 YouMind Nano Banana Pro Prompts 导入")
    print("=" * 70)
    print(f"数据源: {YOUMIND_API_URL}")
    print(f"导入来源标识: {IMPORT_SOURCE}")
    print(f"预览模式: {dry_run}")
    print(f"断点续传: {resume}")
    if limit:
        print(f"限制数量: {limit}")
    if max_pages:
        print(f"最大页数: {max_pages}")
    print("=" * 70)

    # 重置进度
    if reset_progress:
        clear_progress()

    # 检查配置
    if not DATABASE_URL:
        print("❌ 缺少 DATABASE_URL 环境变量")
        sys.exit(1)

    # 获取数据（支持缓存）
    prompts = fetch_all_youmind_data(force_refresh=force_refresh, max_pages=max_pages)
    if not prompts:
        sys.exit(1)

    # 加载进度，过滤已处理的条目
    progress = load_progress()
    processed_ids = set(progress.get("processed_ids", []))

    if resume and processed_ids:
        original_count = len(prompts)
        prompts = [item for item in prompts if item.get("id") not in processed_ids]
        skipped_count = original_count - len(prompts)

        if skipped_count > 0:
            print(f"📊 已处理（跳过）: {skipped_count}")
            print(f"   上次更新: {progress.get('last_updated', 'N/A')}")

    # 限制数量
    if limit:
        prompts = prompts[:limit]

    total_items = len(prompts)
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
        "total": len(prompts),
        "processed": 0,
        "success": 0,
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
        for i, item in enumerate(prompts, 1):
            item_id = item.get("id", "?")
            title = item.get("title", "Untitled")[:40]
            twitter_url = item.get("sourceLink")
            if twitter_url and "twitter.com" in twitter_url:
                twitter_url = twitter_url.replace("twitter.com", "x.com")

            # 显示进度条
            progress_pct = (i / total_items) * 100
            print(f"[{i}/{total_items}] ({progress_pct:.1f}%) ID={item_id}: {title}")

            if twitter_url:
                print(f"   🔗 X: {twitter_url}")

            result = process_youmind_item(db, item, dry_run=dry_run)
            stats["processed"] += 1

            # 记录 Twitter 处理失败的条目
            if result.get("twitter_failed"):
                stats["twitter_failed"] += 1

                failed_twitter_items.append({
                    "id": item_id,
                    "title": item.get("title", "Untitled"),
                    "twitter_url": twitter_url,
                    "error": result.get("twitter_error", "Unknown error"),
                    "saved_to_db": result.get("success", False),
                    # 用于人工处理的关键数据
                    "prompt_preview": (item.get("content") or item.get("translatedContent") or "")[:200] + "...",
                    "full_prompt": item.get("content") or item.get("translatedContent") or "",
                    "images": item.get("media", [])[:5],
                    "tags": [],
                })

            if result["success"]:
                stats["success"] += 1
                if result["method"] == "dry_run":
                    print(f"   ✅ 预览通过")
                else:
                    print(f"   ✅ 成功入库")
            else:
                if result["method"] == "skipped":
                    stats["skipped"] += 1
                    print(f"   ⏭️ 跳过: {result['error']}")
                elif result["method"] == "twitter_failed":
                    # Twitter 失败，不入库，记录到文件
                    print(f"   📝 记录到失败文件 (Twitter图片获取失败)")
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
        print(f"✅ 成功: {stats['success']}")
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
        description="从 YouMind API 导入 Nano Banana Pro Prompts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 测试 API 连接
  python import_youmind_api.py --test

  # 导入所有数据（自动断点续传）
  python import_youmind_api.py

  # 限制导入 10 条
  python import_youmind_api.py --limit 10

  # 限制抓取 2 页
  python import_youmind_api.py --max-pages 2

  # 强制刷新缓存
  python import_youmind_api.py --refresh

  # 重置进度，从头开始
  python import_youmind_api.py --reset

  # 预览模式
  python import_youmind_api.py --dry-run --limit 5

缓存文件:
  worker/cache/youmind_prompts.json        - 数据缓存
  worker/cache/youmind_import_progress.json - 处理进度
        """
    )

    parser.add_argument("--test", "-t", action="store_true",
                        help="测试 API 连接并显示响应结构")
    parser.add_argument("--limit", "-l", type=int, help="限制导入数量")
    parser.add_argument("--max-pages", "-p", type=int, help="最大抓取页数")
    parser.add_argument("--dry-run", "-d", action="store_true",
                        help="预览模式，不写入数据库")
    parser.add_argument("--refresh", "-r", action="store_true",
                        help="强制刷新缓存，重新抓取数据")
    parser.add_argument("--no-resume", action="store_true",
                        help="禁用断点续传，处理所有条目")
    parser.add_argument("--reset", action="store_true",
                        help="重置进度，从头开始处理")

    args = parser.parse_args()

    if args.test:
        test_api()
    else:
        run_import(
            limit=args.limit,
            dry_run=args.dry_run,
            force_refresh=args.refresh,
            resume=not args.no_resume,
            reset_progress=args.reset,
            max_pages=args.max_pages
        )


if __name__ == "__main__":
    main()
