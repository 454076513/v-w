#!/usr/bin/env python3
"""
AIART.PICS 提示词导入脚本

通过 aiart.pics API 获取数据并导入到数据库。

工作流程:
1. 通过 API (/api/prompts) 获取提示词列表
2. 使用 API 返回的数据（包含提示词、图片、作者、标签）
3. 如需要则进行 AI 分类
4. 写入数据库

环境变量:
  DATABASE_URL - PostgreSQL 连接字符串 (必需)
  AI_MODEL     - AI 模型 (默认: openai)

用法:
  python import_aiart_pics.py                    # 导入数据
  python import_aiart_pics.py --limit 10         # 限制导入数量
  python import_aiart_pics.py --dry-run          # 预览模式
  python import_aiart_pics.py --pages 5          # 只获取前 5 页
  python import_aiart_pics.py --reset            # 重置进度
"""

import os
import sys
import json
import asyncio
import argparse
from typing import Optional, List, Dict, Any
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
from fetch_twitter_content import classify_prompt_with_ai

# ========== 配置 ==========
BASE_URL = "https://aiart.pics"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# 数据文件
CACHE_DIR = Path(__file__).parent / "cache"
PROGRESS_FILE = CACHE_DIR / "aiart_pics_import_progress.json"

# 失败记录
FAILED_OUTPUT_DIR = Path(__file__).parent / "failed_imports"


async def fetch_prompts_from_api(limit: int = 50, offset: int = 0) -> List[Dict]:
    """通过 API 获取提示词列表（无需 Playwright）"""
    import aiohttp

    url = f"{BASE_URL}/api/prompts?limit={limit}&offset={offset}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                return []
            data = await response.json()
            return data.get("prompts", [])


def extract_data_from_api_item(item: Dict) -> Optional[Dict]:
    """从 API 返回的 item 中提取需要的数据"""
    origin_url = item.get("originUrl", "")
    if not origin_url:
        return None

    # 合并 prompts 数组为单个字符串
    prompts = item.get("prompts", [])
    prompt_text = "\n".join(prompts) if prompts else ""

    # 提取标题 (优先英文)
    title_obj = item.get("title", {})
    title = title_obj.get("en") or title_obj.get("zh") or ""

    # 提取图片 URL
    images = []
    img_base = "https://img1.aiart.pics/"
    for img in item.get("images", []):
        path = img.get("path", "")
        if path:
            images.append(f"{img_base}{path}")

    # 提取作者
    author_obj = item.get("author", {})
    author = author_obj.get("username") or author_obj.get("name") or ""

    # 提取标签
    tags = item.get("tags", [])

    return {
        "x_url": origin_url.replace("twitter.com", "x.com"),
        "prompt": prompt_text,
        "title": title,
        "images": images,
        "author": author,
        "tags": tags,
        "id": item.get("id", ""),
    }


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


def save_failed_items(failed_items: List[Dict], timestamp: str) -> Optional[Path]:
    """保存失败记录"""
    if not failed_items:
        return None

    FAILED_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = FAILED_OUTPUT_DIR / f"aiart_pics_failed_{timestamp}.json"

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(failed_items),
        "items": failed_items
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return filepath


def process_api_item(db: Database, api_data: Dict, dry_run: bool = False) -> Dict[str, Any]:
    """
    处理 API 返回的单个条目

    流程:
    1. 从 API 数据提取信息
    2. 使用 AI 分类（可选）
    3. 写入数据库
    """
    x_url = api_data.get("x_url", "")
    prompt = api_data.get("prompt", "")
    images = api_data.get("images", [])
    api_title = api_data.get("title", "")
    api_author = api_data.get("author", "")
    api_tags = api_data.get("tags", [])

    if not x_url:
        return {"success": False, "method": "skipped", "error": "No x_url"}

    if not prompt:
        return {"success": False, "method": "skipped", "error": "No prompt"}

    if not images:
        return {"success": False, "method": "skipped", "error": "No images"}

    # 检查是否已存在
    if db.prompt_exists(x_url):
        return {"success": False, "method": "skipped", "error": "Already exists"}

    print(f"   ✅ API 数据: {len(images)} 张图片")

    # AI 分类 - 使用 API 提供的标签，或进行 AI 分类
    final_title = api_title
    category = None
    tags = api_tags[:5] if api_tags else []

    # 如果没有标签，尝试 AI 分类
    if not tags:
        print(f"   🤖 AI 分类...")
        try:
            classification = classify_prompt_with_ai(prompt, AI_MODEL)
            if classification:
                if not final_title:
                    ai_title = classification.get("title", "").strip()
                    if ai_title and ai_title != "Untitled Prompt":
                        final_title = ai_title

                ai_category = classification.get("category", "").strip()
                if ai_category:
                    category = ai_category

                if classification.get("sub_categories"):
                    tags = classification["sub_categories"][:5]

                print(f"   ✅ AI 分类: {category}")
        except Exception as e:
            print(f"   ⚠️ AI 分类失败: {e}")

    # Fallback
    if not final_title:
        final_title = "Untitled"
    if not category:
        category = "Illustration"

    if dry_run:
        print(f"   🔍 [Dry Run] 将入库:")
        print(f"      标题: {final_title}")
        print(f"      分类: {category}")
        print(f"      图片: {len(images)}")
        print(f"      提示词: {prompt[:80]}...")
        return {"success": True, "method": "dry_run", "error": None}

    # 写入数据库
    try:
        record = db.save_prompt(
            title=final_title,
            prompt=prompt,
            category=category,
            tags=tags,
            images=images[:5],
            source_link=x_url,
            author=api_author,
            import_source="aiart_pics"
        )

        if record:
            return {"success": True, "method": "imported", "error": None}
        else:
            return {"success": False, "method": "save_failed", "error": "Database save returned None"}
    except Exception as e:
        return {"success": False, "method": "save_failed", "error": str(e)}


async def run_import_async(limit: int = None, max_pages: int = None, dry_run: bool = False,
                           resume: bool = True, reset_progress: bool = False):
    """异步导入流程 - 通过 API 获取数据"""
    print("=" * 70)
    print("📦 AIART.PICS 导入 (API)")
    print("=" * 70)
    print(f"数据源: {BASE_URL}/api/prompts")
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

    # 加载进度
    progress = load_progress()
    processed_ids = set(progress.get("processed_slugs", []))  # 现在存储 ID 而非 slug
    if resume and processed_ids:
        print(f"📊 已处理: {len(processed_ids)} 条")
        print(f"   上次更新: {progress.get('last_updated', 'N/A')}")

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
        "pages": 0,
        "items_found": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
    }

    failed_items = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    processed_count = 0

    page_num = 0
    page_size = 50

    while True:
        # 检查是否达到页数限制
        if max_pages and page_num >= max_pages:
            print(f"\n📄 已达到最大页数 {max_pages}")
            break

        # 检查是否达到数量限制
        if limit and processed_count >= limit:
            print(f"\n📊 已达到数量限制 {limit}")
            break

        # 通过 API 获取数据
        offset = page_num * page_size
        print(f"\n📄 获取第 {page_num + 1} 页 (offset={offset})...")
        try:
            items = await fetch_prompts_from_api(limit=page_size, offset=offset)
        except Exception as e:
            print(f"   ❌ API 请求失败: {e}")
            break

        if not items:
            print(f"   📭 没有更多数据")
            break

        stats["pages"] += 1
        stats["items_found"] += len(items)
        print(f"   找到 {len(items)} 条记录")

        # 处理每个条目
        for item in items:
            item_id = item.get("id", "")

            # 检查是否已处理
            if resume and item_id in processed_ids:
                continue

            # 检查数量限制
            if limit and processed_count >= limit:
                break

            # 提取数据
            api_data = extract_data_from_api_item(item)
            if not api_data:
                continue

            processed_count += 1
            title_display = api_data.get("title", "")[:40] or item_id[:20]
            print(f"\n[{processed_count}] {title_display}")

            result = process_api_item(db, api_data, dry_run=dry_run)

            if result["success"]:
                stats["success"] += 1
                print(f"   ✅ 成功入库")
            else:
                if result["method"] == "skipped":
                    stats["skipped"] += 1
                    print(f"   ⏭️ 跳过: {result['error']}")
                else:
                    stats["failed"] += 1
                    print(f"   ❌ 失败: {result['error']}")
                    failed_items.append({
                        "id": item_id,
                        "error": result.get("error", "Unknown")
                    })

            # 保存进度
            if not dry_run:
                processed_ids.add(item_id)
                if processed_count % 10 == 0:
                    save_progress({"processed_slugs": list(processed_ids)})

        page_num += 1

    # 最终保存进度
    if not dry_run:
        save_progress({"processed_slugs": list(processed_ids)})

    # 保存失败记录
    failed_file = None
    if failed_items and not dry_run:
        failed_file = save_failed_items(failed_items, timestamp)

    # 输出统计
    print("\n" + "=" * 70)
    print("📊 导入完成 - 统计汇总")
    print("=" * 70)
    print(f"爬取页数: {stats['pages']}")
    print(f"发现记录: {stats['items_found']}")
    print(f"✅ 成功: {stats['success']}")
    print(f"⏭️ 跳过: {stats['skipped']}")
    print(f"❌ 失败: {stats['failed']}")

    if failed_file:
        print(f"\n📁 失败记录已保存: {failed_file}")

    print("=" * 70)

    db.close()


def run_import(limit: int = None, max_pages: int = None, dry_run: bool = False,
               resume: bool = True, reset_progress: bool = False):
    """同步入口"""
    asyncio.run(run_import_async(
        limit=limit,
        max_pages=max_pages,
        dry_run=dry_run,
        resume=resume,
        reset_progress=reset_progress
    ))


def main():
    parser = argparse.ArgumentParser(
        description="从 AIART.PICS 导入数据到数据库 (API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 导入数据
  python import_aiart_pics.py

  # 限制导入数量
  python import_aiart_pics.py --limit 10

  # 只获取前 5 页
  python import_aiart_pics.py --pages 5

  # 预览模式
  python import_aiart_pics.py --dry-run --limit 5

  # 重置进度
  python import_aiart_pics.py --reset

流程:
  1. 通过 API (/api/prompts) 获取提示词列表
  2. 使用 API 返回的数据（包含提示词、图片、作者、标签）
  3. 如需要则进行 AI 分类
  4. 写入数据库
        """
    )

    parser.add_argument("--limit", "-l", type=int, help="限制导入数量")
    parser.add_argument("--pages", "-p", type=int, default=2, help="最大爬取页数 (默认: 2)")
    parser.add_argument("--dry-run", "-d", action="store_true", help="预览模式")
    parser.add_argument("--no-resume", action="store_true", help="禁用断点续传")
    parser.add_argument("--reset", action="store_true", help="重置进度")

    args = parser.parse_args()

    run_import(
        limit=args.limit,
        max_pages=args.pages,
        dry_run=args.dry_run,
        resume=not args.no_resume,
        reset_progress=args.reset
    )


if __name__ == "__main__":
    main()
