#!/usr/bin/env python3
"""
AIART.PICS 提示词导入脚本

直接从 aiart.pics 网站爬取数据并导入到数据库。

工作流程:
1. 用 Playwright 爬取 aiart.pics 列表页获取所有 slug
2. 访问详情页获取提示词和 x_url
3. 从 Twitter 获取高清图片
4. 使用 AI 分析分类后入库

环境变量:
  DATABASE_URL - PostgreSQL 连接字符串 (必需)
  AI_MODEL     - AI 模型 (默认: openai)

用法:
  python import_aiart_pics.py                    # 爬取并导入
  python import_aiart_pics.py --limit 10         # 限制导入数量
  python import_aiart_pics.py --dry-run          # 预览模式
  python import_aiart_pics.py --pages 5          # 只爬取前 5 页
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
from fetch_twitter_content import fetch_tweet, classify_prompt_with_ai, extract_username

# ========== 配置 ==========
BASE_URL = "https://aiart.pics"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# 数据文件
CACHE_DIR = Path(__file__).parent / "cache"
PROGRESS_FILE = CACHE_DIR / "aiart_pics_import_progress.json"

# 失败记录
FAILED_OUTPUT_DIR = Path(__file__).parent / "failed_imports"


async def fetch_list_page(page, page_num: int) -> List[Dict]:
    """爬取列表页获取 slug 列表"""
    url = f"{BASE_URL}/?page={page_num}"

    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(3000)

    # 从图片 URL 提取 slug (网站已改为 onclick 而非 <a> 链接)
    items = await page.evaluate("""
        () => {
            const items = [];
            // 查找所有 prompt 图片
            document.querySelectorAll('img[src*="/prompts/"]').forEach(img => {
                const src = img.src;
                // URL 格式: https://img1.aiart.pics/images/prompts/20260104/slug-name-1.jpg
                const match = src.match(/\\/prompts\\/\\d+\\/(.+?)-\\d+\\.(?:jpg|png|webp)/);
                if (match) {
                    const slug = match[1];
                    // 向上查找卡片容器获取标题
                    let el = img;
                    let title = '';
                    for (let i = 0; i < 6 && el; i++) {
                        const h = el.querySelector('h2, h3, p.font-medium, [class*="title"]');
                        if (h) {
                            title = h.innerText?.trim() || '';
                            break;
                        }
                        el = el.parentElement;
                    }
                    items.push({ slug, title });
                }
            });
            // 去重
            const seen = new Set();
            return items.filter(item => {
                if (seen.has(item.slug)) return false;
                seen.add(item.slug);
                return true;
            });
        }
    """)

    return items


async def fetch_detail_page(page, slug: str) -> Optional[Dict]:
    """爬取详情页获取提示词和 x_url"""
    url = f"{BASE_URL}/?prompt={slug}"

    await page.goto(url, wait_until="networkidle", timeout=30000)

    # 等待内容加载
    try:
        await page.wait_for_selector('a[href*="/status/"]', timeout=5000)
    except Exception:
        await page.wait_for_timeout(3000)

    # 提取数据
    data = await page.evaluate("""
        () => {
            const result = { prompt: null, x_url: null, title: null };

            // 提取 x_url
            const xLink = document.querySelector('a[href*="x.com/"][href*="/status/"], a[href*="twitter.com/"][href*="/status/"]');
            if (xLink) {
                result.x_url = xLink.getAttribute('href').replace('twitter.com', 'x.com');
            }

            // 提取提示词 (优先 textarea，其次 .prose / pre)
            const textarea = document.querySelector('textarea');
            if (textarea && textarea.value) {
                result.prompt = textarea.value.trim();
            } else {
                const prose = document.querySelector('.prose');
                if (prose) {
                    result.prompt = prose.innerText.trim();
                } else {
                    const pre = document.querySelector('pre');
                    if (pre) result.prompt = pre.innerText.trim();
                }
            }

            // 提取标题
            const h1 = document.querySelector('h1');
            if (h1) result.title = h1.innerText.trim();

            return result;
        }
    """)

    return data if data.get('x_url') or data.get('prompt') else None


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


async def process_item(db: Database, page, slug: str, title: str, dry_run: bool = False) -> Dict[str, Any]:
    """
    处理单个条目

    流程:
    1. 从详情页获取提示词和 x_url
    2. 从 Twitter 获取图片
    3. 使用 AI 分类
    4. 写入数据库
    """
    # 1. 获取详情页数据
    print(f"   🌐 获取详情页...")
    try:
        detail = await fetch_detail_page(page, slug)
    except Exception as e:
        return {"success": False, "method": "page_failed", "error": f"Page error: {e}"}

    if not detail:
        return {"success": False, "method": "skipped", "error": "No data on page"}

    x_url = detail.get("x_url", "")

    if not x_url:
        return {"success": False, "method": "skipped", "error": "No x_url"}

    # 检查是否已存在
    if db.prompt_exists(x_url):
        return {"success": False, "method": "skipped", "error": "Already exists"}

    # 2. 从 Twitter 获取图片和文本（不使用原网页的 prompt/title）
    print(f"   🐦 从 Twitter 获取数据...")
    try:
        result = fetch_tweet(
            x_url,
            download_images=False,
            extract_prompt=False,
            ai_model=AI_MODEL
        )
    except Exception as e:
        return {"success": False, "method": "twitter_failed", "error": str(e), "twitter_failed": True}

    if not result:
        return {"success": False, "method": "twitter_failed", "error": "fetch_tweet returned None", "twitter_failed": True}

    images = result.get("images", [])
    if not images:
        return {"success": False, "method": "twitter_failed", "error": "No images", "twitter_failed": True}

    # 从 Twitter 获取 prompt（full_text）
    prompt = result.get("full_text", "").strip()
    if not prompt:
        return {"success": False, "method": "twitter_failed", "error": "No prompt from Twitter", "twitter_failed": True}

    print(f"   ✅ 获取到 {len(images)} 张图片")

    # 3. AI 分类 - 优先使用 AI 结果
    print(f"   🤖 AI 分类...")
    final_title = None
    category = None
    tags = []

    try:
        classification = classify_prompt_with_ai(prompt, AI_MODEL)
        if classification:
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

    # Fallback: AI 失败时使用默认值（不使用原网页数据）
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

    # 4. 提取作者并写入数据库
    try:
        author = extract_username(x_url)
    except:
        author = None

    try:
        record = db.save_prompt(
            title=final_title,
            prompt=prompt,
            category=category,
            tags=tags,
            images=images[:5],
            source_link=x_url,
            author=author,
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
    """异步导入流程 - 直接从网页爬取"""
    from playwright.async_api import async_playwright

    print("=" * 70)
    print("📦 AIART.PICS 导入 (网页爬取)")
    print("=" * 70)
    print(f"数据源: {BASE_URL}")
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
    processed_slugs = set(progress.get("processed_slugs", []))
    if resume and processed_slugs:
        print(f"📊 已处理: {len(processed_slugs)} 条")
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
        "twitter_failed": 0,
    }

    failed_items = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    processed_count = 0

    async with async_playwright() as p:
        # 启动浏览器
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            try:
                browser = await p.firefox.launch(headless=True)
            except Exception:
                browser = await p.chromium.launch(
                    headless=True,
                    executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                )

        try:
            page = await browser.new_page()
            page_num = 1

            while True:
                # 检查是否达到页数限制
                if max_pages and page_num > max_pages:
                    print(f"\n📄 已达到最大页数 {max_pages}")
                    break

                # 检查是否达到数量限制
                if limit and processed_count >= limit:
                    print(f"\n📊 已达到数量限制 {limit}")
                    break

                # 爬取列表页
                print(f"\n📄 爬取第 {page_num} 页...")
                try:
                    items = await fetch_list_page(page, page_num)
                except Exception as e:
                    print(f"   ❌ 列表页爬取失败: {e}")
                    break

                if not items:
                    print(f"   📭 没有更多数据")
                    break

                stats["pages"] += 1
                stats["items_found"] += len(items)
                print(f"   找到 {len(items)} 条记录")

                # 处理每个条目
                for item in items:
                    slug = item.get("slug", "")
                    title = item.get("title", "")

                    # 检查是否已处理
                    if resume and slug in processed_slugs:
                        continue

                    # 检查数量限制
                    if limit and processed_count >= limit:
                        break

                    processed_count += 1
                    print(f"\n[{processed_count}] {slug[:50]}")

                    result = await process_item(db, page, slug, title, dry_run=dry_run)

                    if result.get("twitter_failed"):
                        stats["twitter_failed"] += 1
                        failed_items.append({
                            "slug": slug,
                            "error": result.get("error", "Unknown")
                        })

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

                    # 保存进度
                    if not dry_run:
                        processed_slugs.add(slug)
                        if processed_count % 10 == 0:
                            save_progress({"processed_slugs": list(processed_slugs)})

                page_num += 1

            # 最终保存进度
            if not dry_run:
                save_progress({"processed_slugs": list(processed_slugs)})

        finally:
            await browser.close()

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
    print(f"⚠️ Twitter 失败: {stats['twitter_failed']}")

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
        description="从 AIART.PICS 导入数据到数据库 (网页爬取)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 爬取并导入
  python import_aiart_pics.py

  # 限制导入数量
  python import_aiart_pics.py --limit 10

  # 只爬取前 5 页
  python import_aiart_pics.py --pages 5

  # 预览模式
  python import_aiart_pics.py --dry-run --limit 5

  # 重置进度
  python import_aiart_pics.py --reset

流程:
  1. 用 Playwright 爬取 aiart.pics 列表页获取所有 slug
  2. 访问详情页获取提示词和 x_url
  3. 从 Twitter 获取高清图片
  4. 使用 AI 分类后入库
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
