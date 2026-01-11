#!/usr/bin/env python3
"""
从 aiart_pics_x_urls.json 导入数据到数据库

工作流程:
1. 读取 cache/aiart_pics_x_urls.json (已爬取的 x_url 数据)
2. 筛选有 x_url 的记录
3. 从 Twitter 获取图片和内容
4. 使用 AI 分类
5. 写入数据库

用法:
  python import_aiart_x_urls.py                    # 导入所有有 x_url 的记录
  python import_aiart_x_urls.py --limit 10         # 限制数量
  python import_aiart_x_urls.py --dry-run          # 预览模式
  python import_aiart_x_urls.py --reset            # 重置进度
"""

import os
import sys
import json
import argparse
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from pathlib import Path

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

# 导入主模块
from main import Database, AI_MODEL
from prompt_utils import process_tweet_for_import

# ========== 配置 ==========
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# 数据文件
CACHE_DIR = Path(__file__).parent / "cache"
X_URLS_FILE = CACHE_DIR / "aiart_pics_x_urls.json"
PROGRESS_FILE = CACHE_DIR / "aiart_x_urls_import_progress.json"

# 失败记录
FAILED_OUTPUT_DIR = Path(__file__).parent / "failed_imports"


def load_x_urls_data() -> Optional[Dict]:
    """加载 x_urls 数据"""
    if not X_URLS_FILE.exists():
        print(f"❌ 文件不存在: {X_URLS_FILE}")
        return None

    with open(X_URLS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"📦 已加载: {X_URLS_FILE}")
    print(f"   总记录: {data.get('total', 0)}")
    print(f"   有 x_url: {data.get('with_x_url', 0)}")

    return data


def load_progress() -> Dict:
    """加载处理进度"""
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed_slugs": [], "last_updated": None}


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


def save_failed_items(failed_items: list, timestamp: str) -> Optional[Path]:
    """保存失败记录"""
    if not failed_items:
        return None

    FAILED_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = FAILED_OUTPUT_DIR / f"aiart_x_urls_failed_{timestamp}.json"

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(failed_items),
        "items": failed_items
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return filepath


def process_item(db: Database, item: Dict, dry_run: bool = False) -> Dict[str, Any]:
    """
    处理单个条目

    流程: 使用统一处理函数 process_tweet_for_import
    """
    x_url = item.get("x_url", "")

    if not x_url:
        return {"success": False, "method": "skipped", "error": "No x_url", "twitter_failed": False}

    # 使用统一处理函数
    result = process_tweet_for_import(
        db=db,
        tweet_url=x_url,
        import_source="aiart_pics",
        ai_model=AI_MODEL,
        dry_run=dry_run
    )

    return result


def run_import(limit: int = None, dry_run: bool = False,
               resume: bool = True, reset_progress: bool = False):
    """运行导入流程"""
    print("=" * 70)
    print("📦 AIART.PICS X URLs 导入")
    print("=" * 70)
    print(f"数据源: {X_URLS_FILE}")
    print(f"预览模式: {dry_run}")
    print(f"断点续传: {resume}")
    if limit:
        print(f"限制数量: {limit}")
    print("=" * 70)

    # 重置进度
    if reset_progress:
        clear_progress()

    # 检查配置
    if not DATABASE_URL:
        print("❌ 缺少 DATABASE_URL 环境变量")
        sys.exit(1)

    # 加载数据
    data = load_x_urls_data()
    if not data:
        sys.exit(1)

    items = data.get("items", [])

    # 只处理有 x_url 的
    items = [item for item in items if item.get("x_url")]
    print(f"📊 有 x_url 的记录: {len(items)}")

    # 加载进度
    progress = load_progress()
    processed_slugs = set(progress.get("processed_slugs", []))

    if resume and processed_slugs:
        original_count = len(items)
        items = [item for item in items if item.get("slug") not in processed_slugs]
        skipped = original_count - len(items)
        if skipped > 0:
            print(f"📊 已处理（跳过）: {skipped}")
            print(f"   上次更新: {progress.get('last_updated', 'N/A')}")

    # 限制数量
    if limit:
        items = items[:limit]

    total = len(items)
    print(f"\n🔄 准备处理 {total} 条记录...\n")

    if total == 0:
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
        "total": total,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "twitter_failed": 0,
    }

    failed_items = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        for i, item in enumerate(items, 1):
            slug = item.get("slug", "?")
            x_url = item.get("x_url", "")

            progress_pct = (i / total) * 100
            print(f"[{i}/{total}] ({progress_pct:.1f}%) {slug[:50]}")
            if x_url:
                print(f"   🔗 {x_url}")

            result = process_item(db, item, dry_run=dry_run)

            if result.get("twitter_failed"):
                stats["twitter_failed"] += 1
                failed_items.append({
                    "slug": slug,
                    "x_url": x_url,
                    "error": result.get("error", "Unknown")
                })

            if result["success"]:
                stats["success"] += 1
                print(f"   ✅ 成功入库")
            else:
                if result["method"] == "skipped":
                    stats["skipped"] += 1
                    print(f"   ⏭️ 跳过: {result['error']}")
                elif result["method"] == "twitter_failed":
                    print(f"   ❌ Twitter 失败: {result['error']}")
                else:
                    stats["failed"] += 1
                    print(f"   ❌ 失败: {result['error']}")

            # 保存进度
            if not dry_run:
                processed_slugs.add(slug)
                if i % 10 == 0 or i == total:
                    save_progress({"processed_slugs": list(processed_slugs)})

            print()

        # 保存失败记录
        failed_file = None
        if failed_items and not dry_run:
            failed_file = save_failed_items(failed_items, timestamp)

        # 输出统计
        print("=" * 70)
        print("📊 导入完成 - 统计汇总")
        print("=" * 70)
        print(f"总计: {stats['total']}")
        print(f"✅ 成功: {stats['success']}")
        print(f"⏭️ 跳过: {stats['skipped']}")
        print(f"❌ 失败: {stats['failed']}")
        print(f"⚠️ Twitter 失败: {stats['twitter_failed']}")

        if failed_file:
            print(f"\n📁 失败记录已保存: {failed_file}")

        print("=" * 70)

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description="从 aiart_pics_x_urls.json 导入数据到数据库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 导入所有有 x_url 的记录
  python import_aiart_x_urls.py

  # 限制导入数量
  python import_aiart_x_urls.py --limit 10

  # 预览模式
  python import_aiart_x_urls.py --dry-run --limit 5

  # 重置进度
  python import_aiart_x_urls.py --reset
        """
    )

    parser.add_argument("--limit", "-l", type=int, help="限制导入数量")
    parser.add_argument("--dry-run", "-d", action="store_true", help="预览模式")
    parser.add_argument("--no-resume", action="store_true", help="禁用断点续传")
    parser.add_argument("--reset", action="store_true", help="重置进度")

    args = parser.parse_args()

    run_import(
        limit=args.limit,
        dry_run=args.dry_run,
        resume=not args.no_resume,
        reset_progress=args.reset
    )


if __name__ == "__main__":
    main()
