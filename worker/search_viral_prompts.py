#!/usr/bin/env python3
"""
X/Twitter 爆款提示词搜索器
使用 twikit 直接搜索关键词，获取高互动的 AI 提示词

技术方案:
- twikit: 搜索 Twitter 关键词 (支持 min_faves 过滤)
- FxTwitter: 获取推文详情和图片
- AI: 提取和分类提示词

使用方法:
    # 搜索爆款提示词 (默认 min_faves:500)
    python search_viral_prompts.py

    # 自定义最低点赞数
    python search_viral_prompts.py --min-likes 1000

    # 搜索特定关键词
    python search_viral_prompts.py --keyword "nano banana"

    # 预览模式
    python search_viral_prompts.py --dry-run

    # 持续搜索 (每 30 分钟)
    python search_viral_prompts.py --interval 30

环境变量:
    DATABASE_URL        - PostgreSQL 连接字符串 (必需)
    GITEE_AI_API_KEY    - AI 模型 API Key
    X_COOKIE            - X 账号 cookies JSON (推荐): '{"auth_token": "xxx", "ct0": "xxx"}'
    X_USERNAME          - X 账号用户名 (备用登录方式)
    X_EMAIL             - X 账号邮箱
    X_PASSWORD          - X 账号密码
"""

import os
import sys
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict

# 加载环境变量
try:
    from dotenv import load_dotenv
    root_dir = Path(__file__).parent.parent
    env_local = root_dir / ".env.local"
    env_file = root_dir / ".env"

    if env_local.exists():
        load_dotenv(env_local)
        print(f"[env] Loaded: {env_local}")
    elif env_file.exists():
        load_dotenv(env_file)
        print(f"[env] Loaded: {env_file}")
except ImportError:
    pass

# twikit
try:
    from twikit import Client
    HAS_TWIKIT = True
except ImportError:
    HAS_TWIKIT = False
    print("[Warning] twikit not installed. Run: pip install twikit")

# 数据库
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("Please install psycopg2: pip install psycopg2-binary")
    sys.exit(1)

# 导入 AI 处理函数 (统一使用 prompt_utils)
from prompt_utils import DEFAULT_MODEL

# 导入 Twitter API 函数和 AI 处理适配函数
from fetch_twitter_content import (
    extract_prompt_with_ai,
    classify_prompt_with_ai,
    fetch_with_fxtwitter,
    parse_fxtwitter_result,
)

# ========== 配置 ==========

DATABASE_URL = os.environ.get("DATABASE_URL", "")
AI_MODEL = os.environ.get("AI_MODEL", DEFAULT_MODEL)

# X 账号凭证
X_USERNAME = os.environ.get("X_USERNAME", "")
X_EMAIL = os.environ.get("X_EMAIL", "")
X_PASSWORD = os.environ.get("X_PASSWORD", "")

# Cookies 配置 (优先使用环境变量)
X_COOKIE = os.environ.get("X_COOKIE", "")  # JSON 字符串: '{"auth_token": "xxx", "ct0": "xxx"}'
COOKIES_FILE = Path(__file__).parent / "x_cookies.json"

# 代理配置 (如果需要)
PROXY_URL = os.environ.get("X_PROXY", "")

# 状态文件
STATE_FILE = Path(__file__).parent / "x_search_state.json"

# ========== 搜索关键词 ==========

# Nano Banana 相关搜索词
SEARCH_KEYWORDS = [
    # Nano Banana 直接搜索
    '"nano banana"',
    'nanobanana',
    '"Nano Banana Pro"',

    # Gemini 图像生成
    '"gemini image"',
    '"gemini 2.5" image',
    'gemini生图',

    # 小香蕉 (中文)
    '小香蕉 提示词',
    '小香蕉 prompt',
]

# 默认搜索配置
DEFAULT_MIN_LIKES = 100  # Nano Banana 内容较新，降低阈值
DEFAULT_MIN_RETWEETS = 20
DEFAULT_RESULTS_PER_KEYWORD = 30
DEFAULT_DAYS_BACK = 1  # 只搜索最近 N 天的推文
DEFAULT_HOURS_BACK = 1  # 只处理最近 N 小时的推文 (0=不限制)


# ========== 数据库操作 ==========

class Database:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.conn = None

    def connect(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(self.connection_string)
        return self.conn

    def close(self):
        if self.conn and not self.conn.closed:
            self.conn.close()

    def execute_write(self, query: str, params: tuple = None) -> Optional[Dict]:
        conn = self.connect()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            conn.commit()
            if cur.description:
                result = cur.fetchone()
                return dict(result) if result else None
            return None

    def execute_one(self, query: str, params: tuple = None) -> Optional[Dict]:
        conn = self.connect()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            result = cur.fetchone()
            return dict(result) if result else None

    def prompt_exists(self, source_link: str) -> bool:
        result = self.execute_one(
            "SELECT id FROM prompts WHERE source_link = %s",
            (source_link,)
        )
        return result is not None

    def save_prompt(self, title: str, prompt: str, category: str,
                    tags: List[str], images: List[str], source_link: str,
                    author: str = None, import_source: str = None) -> Optional[Dict]:
        return self.execute_write(
            """
            INSERT INTO prompts (title, prompt, category, tags, images, source_link, author, import_source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (title, prompt, category, tags or [], images or [], source_link, author, import_source)
        )


# ========== 状态管理 ==========

def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"processed_tweets": [], "last_search": None}


def save_state(state: Dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def is_tweet_processed(state: Dict, tweet_id: str) -> bool:
    return tweet_id in state.get("processed_tweets", [])


def mark_tweet_processed(state: Dict, tweet_id: str):
    if "processed_tweets" not in state:
        state["processed_tweets"] = []
    state["processed_tweets"].append(tweet_id)
    # 保留最近 10000 条
    if len(state["processed_tweets"]) > 10000:
        state["processed_tweets"] = state["processed_tweets"][-5000:]
    save_state(state)


# ========== 分类映射 ==========

CATEGORY_MAP = {
    "Portrait": "Portrait",
    "Landscape/Nature": "Landscape",
    "Landscape": "Landscape",
    "Nature": "Nature",
    "Animals": "Nature",
    "Architecture/Urban": "Architecture",
    "Architecture": "Architecture",
    "Abstract Art": "Abstract",
    "Abstract": "Abstract",
    "Sci-Fi/Futuristic": "Sci-Fi",
    "Sci-Fi": "Sci-Fi",
    "Fantasy/Magic": "Fantasy",
    "Fantasy": "Fantasy",
    "Anime/Cartoon": "Anime",
    "Anime": "Anime",
    "Realistic Photography": "Photography",
    "Photography": "Photography",
    "Illustration/Painting": "Illustration",
    "Illustration": "Illustration",
    "Fashion/Clothing": "Fashion",
    "Fashion": "Fashion",
    "Food": "Food",
    "Product/Commercial": "Product",
    "Product": "Product",
    "Cinematic": "Cinematic",
    "Horror/Dark": "Cinematic",
    "Cute/Kawaii": "Clay / Felt",
    "Vintage/Retro": "Retro / Vintage",
    "Minimalist": "Minimalist",
    "Surreal": "Abstract",
    "Other": "Other",
}


def map_category(classification: Dict) -> str:
    raw_category = classification.get("category", "Other")
    if raw_category in CATEGORY_MAP:
        return CATEGORY_MAP[raw_category]
    raw_lower = raw_category.lower()
    for key, value in CATEGORY_MAP.items():
        if key.lower() in raw_lower or raw_lower in key.lower():
            return value
    return "Photography"


# ========== twikit 搜索 ==========

class TwikitSearcher:
    """使用 twikit 搜索推文"""

    def __init__(self):
        self.client = None
        self.logged_in = False

    async def login(self):
        """登录 X 账号"""
        if not HAS_TWIKIT:
            raise RuntimeError("twikit not installed")

        # 初始化客户端 (支持代理)
        if PROXY_URL:
            print(f"[twikit] Using proxy: {PROXY_URL[:20]}...")
            self.client = Client('en-US', proxy=PROXY_URL)
        else:
            self.client = Client('en-US')

        # 尝试使用 cookies 登录 (优先使用环境变量)
        if X_COOKIE:
            try:
                import tempfile
                cookie_data = json.loads(X_COOKIE)
                # 写入临时文件供 twikit 加载
                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                    json.dump(cookie_data, f)
                    temp_cookie_file = f.name
                self.client.load_cookies(temp_cookie_file)
                os.unlink(temp_cookie_file)  # 删除临时文件
                print("[twikit] Loaded cookies from X_COOKIE env")
                self.logged_in = True
                return
            except Exception as e:
                print(f"[twikit] Failed to load cookies from env: {e}")

        if COOKIES_FILE.exists():
            try:
                self.client.load_cookies(str(COOKIES_FILE))
                # 验证 cookies 是否有效
                print("[twikit] Loaded cookies from file")
                self.logged_in = True
                return
            except Exception as e:
                print(f"[twikit] Failed to load cookies: {e}")

        # 使用账号密码登录
        if X_USERNAME and X_PASSWORD:
            try:
                print("[twikit] Logging in with credentials...")
                await self.client.login(
                    auth_info_1=X_USERNAME,
                    auth_info_2=X_EMAIL,
                    password=X_PASSWORD
                )
                # 保存 cookies
                self.client.save_cookies(str(COOKIES_FILE))
                print("[twikit] Login successful, cookies saved")
                self.logged_in = True
                return
            except Exception as e:
                print(f"[twikit] Login failed: {e}")
                raise

        # 提示如何获取 cookies
        print("\n" + "=" * 60)
        print("需要 X 账号 cookies 才能搜索")
        print("=" * 60)
        print("\n方法: 从浏览器导出 cookies")
        print("1. 在 Chrome 登录 x.com")
        print("2. 按 F12 打开开发者工具")
        print("3. 切换到 Application > Cookies > https://x.com")
        print("4. 找到以下 cookies 并复制值:")
        print("   - auth_token")
        print("   - ct0")
        print("5. 创建 worker/x_cookies.json 文件:\n")
        print('''{
    "auth_token": "你的auth_token值",
    "ct0": "你的ct0值"
}''')
        print("\n" + "=" * 60)
        raise RuntimeError("No cookies file found. See instructions above.")

    async def search(self, keyword: str, min_likes: int = 500,
                     min_retweets: int = 0, count: int = 20,
                     days_back: int = 7) -> List[Dict]:
        """
        搜索推文

        Args:
            keyword: 搜索关键词
            min_likes: 最低点赞数
            min_retweets: 最低转发数
            count: 结果数量
            days_back: 只搜索最近 N 天的推文

        Returns:
            推文列表
        """
        if not self.logged_in:
            await self.login()

        # 构建搜索查询
        # 使用 Twitter 高级搜索语法
        query_parts = [keyword]

        # 添加时间过滤 - 只搜索最近 N 天
        if days_back > 0:
            from datetime import timedelta
            since_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
            query_parts.append(f"since:{since_date}")

        # 添加图片过滤
        query_parts.append("filter:images")

        # 添加互动过滤
        if min_likes > 0:
            query_parts.append(f"min_faves:{min_likes}")
        if min_retweets > 0:
            query_parts.append(f"min_retweets:{min_retweets}")

        # 排除转推
        query_parts.append("-filter:retweets")

        query = " ".join(query_parts)
        print(f"   Query: {query}")

        tweets = []
        try:
            # 搜索最新结果 (Latest 模式更稳定)
            results = await self.client.search_tweet(query, 'Latest', count=count)

            for tweet in results:
                try:
                    tweet_data = {
                        "id": tweet.id,
                        "text": tweet.text or "",
                        "username": tweet.user.screen_name if tweet.user else "unknown",
                        "user_name": tweet.user.name if tweet.user else "Unknown",
                        "url": f"https://x.com/{tweet.user.screen_name}/status/{tweet.id}" if tweet.user else "",
                        "likes": tweet.favorite_count or 0,
                        "retweets": tweet.retweet_count or 0,
                        "views": tweet.view_count or 0,
                        "created_at": str(tweet.created_at) if tweet.created_at else None,
                        "images": [],
                    }

                    # 提取图片 (twikit 2.3+ 使用 Photo/Video 类)
                    if tweet.media:
                        for media in tweet.media:
                            # 优先使用 media_url (实际图片地址)
                            if hasattr(media, 'media_url') and media.media_url:
                                img_url = media.media_url
                                # 确保使用 https
                                if img_url.startswith('http://'):
                                    img_url = img_url.replace('http://', 'https://')
                                tweet_data["images"].append(img_url)
                            elif hasattr(media, 'media_url_https') and media.media_url_https:
                                tweet_data["images"].append(media.media_url_https)

                    tweets.append(tweet_data)
                except Exception as e:
                    print(f"   [Warning] Failed to parse tweet: {e}")
                    continue

        except Exception as e:
            print(f"   [Error] Search failed: {e}")

        return tweets

    async def search_multiple(self, keywords: List[str], min_likes: int = 500,
                              count_per_keyword: int = 20, days_back: int = 7) -> List[Dict]:
        """搜索多个关键词"""
        all_tweets = []
        seen_ids = set()

        for keyword in keywords:
            print(f"\n[Search] Keyword: {keyword}")
            tweets = await self.search(keyword, min_likes=min_likes, count=count_per_keyword, days_back=days_back)

            for tweet in tweets:
                if tweet["id"] not in seen_ids:
                    seen_ids.add(tweet["id"])
                    all_tweets.append(tweet)

            print(f"   Found {len(tweets)} tweets, {len(all_tweets)} total unique")

            # 避免请求过快
            await asyncio.sleep(2)

        return all_tweets


# ========== 处理逻辑 ==========

async def process_tweet(db: Database, tweet: Dict, state: Dict,
                        dry_run: bool = False, hours_back: int = 0) -> bool:
    """处理单条推文"""
    tweet_id = tweet["id"]
    tweet_url = tweet["url"]
    text = tweet["text"]
    images = tweet.get("images", [])
    username = tweet["username"]
    likes = tweet.get("likes", 0)
    retweets = tweet.get("retweets", 0)
    views = tweet.get("views", 0)
    created_at = tweet.get("created_at", "")

    # 检查推文时间是否在指定小时内
    if hours_back > 0 and created_at:
        try:
            from datetime import timedelta
            from dateutil import parser as date_parser
            tweet_time = date_parser.parse(created_at)
            # 确保时区一致
            if tweet_time.tzinfo is None:
                tweet_time = tweet_time.replace(tzinfo=timezone.utc)
            # 放宽时间限制到 1.5 倍
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_back * 1.5)
            if tweet_time < cutoff_time:
                print(f"   [Skip] Tweet too old: {created_at} (cutoff: {hours_back * 1.5:.0f}h)")
                return False
        except Exception as e:
            print(f"   [Warning] Failed to parse tweet time: {e}")

    # 检查是否已处理
    if is_tweet_processed(state, tweet_id):
        return False

    # 检查数据库是否已存在
    if db.prompt_exists(tweet_url):
        mark_tweet_processed(state, tweet_id)
        return False

    # 总是尝试从 FxTwitter 获取完整内容
    # twikit 搜索 API 对于长推文 (Twitter Notes) 可能返回截断的文本
    try:
        fx_data = fetch_with_fxtwitter(tweet_id, username)
        fx_result = parse_fxtwitter_result(fx_data)
        if fx_result:
            # 使用 FxTwitter 返回的完整文本 (如果更长)
            fx_text = fx_result.get("text", "")
            if fx_text and len(fx_text) > len(text):
                print(f"   [FxTwitter] Got longer text: {len(text)} -> {len(fx_text)} chars")
                text = fx_text
                tweet["text"] = fx_text
            # 补充图片
            if fx_result.get("images") and not images:
                images = fx_result["images"]
                tweet["images"] = images
    except Exception as e:
        print(f"   [FxTwitter] Failed to get full text: {e}")

    if not images:
        print(f"   [Skip] No images: @{username}/{tweet_id}")
        mark_tweet_processed(state, tweet_id)
        return False

    # 显示推文信息
    print(f"\n   [Tweet] @{username} - {tweet_id}")
    print(f"   Text: {text[:100]}...")
    print(f"   Stats: ❤️ {int(likes or 0):,} | 🔁 {int(retweets or 0):,} | 👁️ {int(views or 0):,}")
    print(f"   Images: {len(images)}")

    # AI 提取提示词
    try:
        print(f"   [AI] Extracting prompt...")
        extracted_prompt = extract_prompt_with_ai(text, model=AI_MODEL)

        # 检查是否为广告
        if extracted_prompt == "Advertisement":
            print(f"   [Skip] Advertisement content detected")
            mark_tweet_processed(state, tweet_id)
            return False

        # 检查是否提取失败
        invalid_prompts = [
            "No prompt found",
            "No prompt",
            "None",
            "N/A",
            "",
        ]
        if not extracted_prompt or extracted_prompt.strip().lower() in [p.lower() for p in invalid_prompts]:
            print(f"   [Skip] AI found no prompt")
            mark_tweet_processed(state, tweet_id)
            return False

        # 检查提示词是否太短 (可能是无效提取)
        if len(extracted_prompt.strip()) < 20:
            print(f"   [Skip] Prompt too short ({len(extracted_prompt)} chars)")
            mark_tweet_processed(state, tweet_id)
            return False

        # AI 分类
        print(f"   [AI] Classifying...")
        classification = classify_prompt_with_ai(extracted_prompt, model=AI_MODEL)

        # 准备数据
        title = classification.get("title", "").strip()

        # 检查是否是无效标题 (表示解析失败)
        invalid_titles = [
            "Untitled Prompt",
            "No Prompt Provided",
            "Unknown Prompt",
            "No Title",
            "Untitled",
            "N/A",
            "",
        ]
        if title.lower() in [t.lower() for t in invalid_titles]:
            # 使用默认标题
            title = f"@{username} #{tweet_id[-6:]}"

        # 再次检查分类是否有效 - 如果 category 也是 Unknown/Other 且没有 tags，可能是无效提取
        raw_category = classification.get("category", "")
        tags = classification.get("sub_categories", [])
        if (raw_category.lower() in ["unknown", "other", ""] and
            (not tags or len(tags) == 0) and
            title.startswith("@")):  # 使用了默认标题
            print(f"   [Skip] Classification failed (unknown category, no tags)")
            mark_tweet_processed(state, tweet_id)
            return False

        category = map_category(classification)
        tags = classification.get("sub_categories", [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if t][:5]

        # Dry Run
        if dry_run:
            print(f"   [DRY RUN] Would save: {title}")
            print(f"      Category: {category}")
            print(f"      Tags: {tags}")
            print(f"      Images: {len(images)}")
            print(f"      Prompt: {extracted_prompt[:100]}...")
            mark_tweet_processed(state, tweet_id)
            return True

        # 保存到数据库
        print(f"   [DB] Saving: {title}")
        prompt_record = db.save_prompt(
            title=title,
            prompt=extracted_prompt,
            category=category,
            tags=tags,
            images=images[:5],
            source_link=tweet_url,
            author=username,
            import_source="x-search-viral"
        )

        if prompt_record:
            print(f"   [OK] Saved: {title}")
            mark_tweet_processed(state, tweet_id)
            return True
        else:
            print(f"   [Error] Failed to save")
            return False

    except Exception as e:
        print(f"   [Error] {e}")
        mark_tweet_processed(state, tweet_id)
        return False


async def search_viral_prompts(
    keywords: List[str] = None,
    min_likes: int = DEFAULT_MIN_LIKES,
    count_per_keyword: int = DEFAULT_RESULTS_PER_KEYWORD,
    days_back: int = DEFAULT_DAYS_BACK,
    hours_back: int = DEFAULT_HOURS_BACK,
    dry_run: bool = False
) -> Dict:
    """
    搜索爆款提示词

    Args:
        keywords: 搜索关键词列表
        min_likes: 最低点赞数
        count_per_keyword: 每个关键词搜索数量
        days_back: 只搜索最近 N 天的推文 (Twitter API 级别)
        hours_back: 只处理最近 N 小时的推文 (0=不限制)
        dry_run: 预览模式
    """
    keywords = keywords or SEARCH_KEYWORDS

    print("=" * 60)
    print("X/Twitter Viral Prompt Search")
    print("=" * 60)
    print(f"Keywords: {len(keywords)}")
    print(f"Min Likes: {min_likes:,}")
    print(f"Days Back: {days_back}")
    print(f"Hours Back: {hours_back}" + (" (filter enabled)" if hours_back > 0 else " (no filter)"))
    print(f"Count per keyword: {count_per_keyword}")
    print(f"Dry Run: {dry_run}")
    print(f"AI Model: {AI_MODEL}")
    print("=" * 60)

    if not DATABASE_URL:
        print("[Error] DATABASE_URL not set")
        return {"error": "DATABASE_URL not set"}

    if not HAS_TWIKIT:
        print("[Error] twikit not installed")
        return {"error": "twikit not installed"}

    # 初始化
    db = Database(DATABASE_URL)
    searcher = TwikitSearcher()
    state = load_state()

    stats = {
        "keywords_searched": 0,
        "tweets_found": 0,
        "prompts_saved": 0,
        "skipped": 0,
        "errors": 0,
    }

    try:
        db.connect()
        print("[DB] Connected")

        await searcher.login()
        print("[twikit] Ready")

        # 搜索所有关键词
        all_tweets = await searcher.search_multiple(
            keywords,
            min_likes=min_likes,
            count_per_keyword=count_per_keyword,
            days_back=days_back
        )

        stats["keywords_searched"] = len(keywords)
        stats["tweets_found"] = len(all_tweets)

        print(f"\n[Processing] {len(all_tweets)} unique tweets...")

        # 按点赞数排序
        all_tweets.sort(key=lambda x: x.get("likes", 0), reverse=True)

        for i, tweet in enumerate(all_tweets, 1):
            print(f"\n[{i}/{len(all_tweets)}]", end="")
            result = await process_tweet(db, tweet, state, dry_run=dry_run, hours_back=hours_back)
            if result:
                stats["prompts_saved"] += 1
            else:
                stats["skipped"] += 1

        # 更新状态
        state["last_search"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    except Exception as e:
        print(f"[Error] {e}")
        stats["errors"] += 1
    finally:
        db.close()

    # 输出统计
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Keywords searched: {stats['keywords_searched']}")
    print(f"Tweets found: {stats['tweets_found']}")
    print(f"Prompts saved: {stats['prompts_saved']}")
    print(f"Skipped: {stats['skipped']}")
    print(f"Errors: {stats['errors']}")
    print("=" * 60)

    return stats


async def run_continuous(
    keywords: List[str] = None,
    min_likes: int = DEFAULT_MIN_LIKES,
    days_back: int = DEFAULT_DAYS_BACK,
    hours_back: int = DEFAULT_HOURS_BACK,
    interval_minutes: int = 30,
    dry_run: bool = False
):
    """持续搜索模式"""
    print(f"Starting continuous search (interval: {interval_minutes} min)")
    print("Press Ctrl+C to stop\n")

    while True:
        try:
            await search_viral_prompts(
                keywords=keywords,
                min_likes=min_likes,
                days_back=days_back,
                hours_back=hours_back,
                dry_run=dry_run
            )

            print(f"\nNext search in {interval_minutes} minutes...")
            await asyncio.sleep(interval_minutes * 60)

        except KeyboardInterrupt:
            print("\nStopped by user")
            break
        except Exception as e:
            print(f"\n[Error] {e}")
            print(f"Retrying in {interval_minutes} minutes...")
            await asyncio.sleep(interval_minutes * 60)


# ========== CLI ==========

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Search viral AI prompts on X/Twitter using twikit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Search with default settings (min_likes=500)
  python search_viral_prompts.py

  # Higher threshold for more viral content
  python search_viral_prompts.py --min-likes 1000

  # Search specific keyword
  python search_viral_prompts.py --keyword "nano banana prompt"

  # Dry run (don't save to database)
  python search_viral_prompts.py --dry-run

  # Continuous search (every 30 min)
  python search_viral_prompts.py --interval 30

  # Custom keywords (comma-separated)
  python search_viral_prompts.py --keywords "midjourney prompt,AI art prompt"

Environment Variables:
  DATABASE_URL     - PostgreSQL connection string
  X_USERNAME       - X account username
  X_EMAIL          - X account email
  X_PASSWORD       - X account password
        """
    )

    parser.add_argument("--keyword", "-k", type=str,
                        help="Single search keyword")
    parser.add_argument("--keywords", type=str,
                        help="Comma-separated keywords")
    parser.add_argument("--min-likes", "-l", type=int, default=DEFAULT_MIN_LIKES,
                        help=f"Minimum likes (default: {DEFAULT_MIN_LIKES})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS_BACK,
                        help=f"Only search tweets from last N days (default: {DEFAULT_DAYS_BACK})")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS_BACK,
                        help=f"Only process tweets from last N hours, 0=no filter (default: {DEFAULT_HOURS_BACK})")
    parser.add_argument("--count", "-c", type=int, default=DEFAULT_RESULTS_PER_KEYWORD,
                        help=f"Results per keyword (default: {DEFAULT_RESULTS_PER_KEYWORD})")
    parser.add_argument("--interval", "-i", type=int, default=0,
                        help="Continuous mode interval in minutes (0=run once)")
    parser.add_argument("--dry-run", "-d", action="store_true",
                        help="Dry run mode - don't save to database")

    args = parser.parse_args()

    # 解析关键词
    keywords = None
    if args.keyword:
        keywords = [args.keyword]
    elif args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    # 运行
    if args.interval > 0:
        asyncio.run(run_continuous(
            keywords=keywords,
            min_likes=args.min_likes,
            days_back=args.days,
            hours_back=args.hours,
            interval_minutes=args.interval,
            dry_run=args.dry_run
        ))
    else:
        asyncio.run(search_viral_prompts(
            keywords=keywords,
            min_likes=args.min_likes,
            days_back=args.days,
            hours_back=args.hours,
            count_per_keyword=args.count,
            dry_run=args.dry_run
        ))


if __name__ == "__main__":
    main()
