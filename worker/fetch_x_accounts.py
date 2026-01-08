#!/usr/bin/env python3
"""
X/Twitter AI Art Account Monitor
监听 AI 艺术账号，自动提取提示词并入库

技术方案:
- Twitter Syndication API: 获取用户时间线 (无需认证)
- FxTwitter/VxTwitter API: 获取推文详情和互动数据

使用方法:
    # 运行监听 (单次)
    python fetch_x_accounts.py

    # 使用数据库中的高频作者
    python fetch_x_accounts.py --top 20

    # 持续监听 (每 N 分钟检查一次)
    python fetch_x_accounts.py --interval 30

    # 只处理爆款推文
    python fetch_x_accounts.py --viral-only

    # 预览模式 (不写入数据库)
    python fetch_x_accounts.py --dry-run

    # 监听特定账号
    python fetch_x_accounts.py --accounts midjourney,openai

环境变量:
    DATABASE_URL        - PostgreSQL 连接字符串 (必需)
    AI_MODEL            - AI 模型 (默认: openai)
"""

import os
import sys
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

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

# twikit 已弃用，使用 Syndication API + FxTwitter 替代
HAS_TWIKIT = False

# HTTP 请求
import requests
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# 导入现有的 AI 处理函数
from fetch_twitter_content import (
    extract_prompt_with_ai,
    classify_prompt_with_ai,
    DEFAULT_MODEL,
    fetch_with_fxtwitter,
    fetch_with_vxtwitter,
    parse_fxtwitter_result,
    parse_vxtwitter_result,
)

# 数据库
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("Please install psycopg2: pip install psycopg2-binary")
    sys.exit(1)

# ========== 配置 ==========

DATABASE_URL = os.environ.get("DATABASE_URL", "")
AI_MODEL = os.environ.get("AI_MODEL", DEFAULT_MODEL)

# 状态文件 (记录已处理的推文 ID)
STATE_FILE = Path(__file__).parent / "x_monitor_state.json"

# 默认监听的 AI 艺术账号 (基于数据库高频统计)
DEFAULT_ACCOUNTS = [
    # Top 20 高频账号
    "songguoxiansen",    # #1 - 144 prompts
    "Gdgtify",           # #2 - 123 prompts
    "Ankit_patel211",    # #3 - 99 prompts
    "dotey",             # #4 - 96 prompts
    "azed_ai",           # #5 - 85 prompts
    "lexx_aura",         # #6 - 79 prompts
    "YaseenK7212",       # #7 - 79 prompts
    "saniaspeaks_",      # #8 - 78 prompts
    "ZaraIrahh",         # #9 - 75 prompts
    "Just_sharon7",      # #10 - 74 prompts
    "xmliisu",           # #11 - 71 prompts
    "Vivekhy",           # #12 - 64 prompts
    "astronomerozge1",   # #13 - 64 prompts
    "siennalovesai",     # #14 - 61 prompts
    "Strength04_X",      # #15 - 60 prompts
    "aleenaamiir",       # #16 - 57 prompts
    "SimplyAnnisa",      # #17 - 54 prompts
    "umesh_ai",          # #18 - 53 prompts
    "oggii_0",           # #19 - 44 prompts
    "xmiiru_",           # #20 - 43 prompts
]


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

    def get_top_authors(self, limit: int = 30) -> List[Dict]:
        """获取高频作者列表"""
        conn = self.connect()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT author, COUNT(*) as count
                FROM prompts
                WHERE author IS NOT NULL
                  AND author != ''
                  AND deleted_at IS NULL
                GROUP BY author
                ORDER BY count DESC
                LIMIT %s
            """, (limit,))
            return [dict(row) for row in cur.fetchall()]


# ========== 状态管理 ==========

def load_state() -> Dict:
    """加载状态文件"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"processed_tweets": [], "last_check": None}


def save_state(state: Dict):
    """保存状态文件"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def is_tweet_processed(state: Dict, tweet_id: str) -> bool:
    """检查推文是否已处理"""
    return tweet_id in state.get("processed_tweets", [])


def mark_tweet_processed(state: Dict, tweet_id: str):
    """标记推文为已处理"""
    if "processed_tweets" not in state:
        state["processed_tweets"] = []

    state["processed_tweets"].append(tweet_id)

    # 只保留最近 10000 条记录
    if len(state["processed_tweets"]) > 10000:
        state["processed_tweets"] = state["processed_tweets"][-5000:]

    save_state(state)


# ========== 提示词特征匹配 ==========

# Nano Banana 相关关键词
PROMPT_KEYWORDS = [
    # 产品名称
    "nano banana", "nanobanana", "小香蕉", "香蕉",
    "nano banana pro", "gemini", "gemini 2.5", "gemini 3",
    "gemini image", "gemini pro",

    # 其他 AI 图像工具
    "midjourney", "mj", "stable diffusion", "sd", "dall-e", "dalle",
    "flux", "comfyui", "leonardo", "ideogram", "runway",
    "可灵", "kling", "即梦", "通义万相", "文心一格",

    # 提示词标识
    "提示词", "咒语", "prompt", "prompts",

    # 常见动作开头 (自然语言描述风格)
    "创建一个", "生成一个", "设计一个", "制作一个", "画一个",
    "create a", "generate a", "design a", "make a", "draw a",

    # Midjourney 特有参数
    "--ar", "--v ", "--style", "--s ", "--c ", "--q ",

    # SD/ComfyUI 特有
    "(masterpiece", "best quality", "8k uhd", "highly detailed",
]

# 需要包含图片的推文才考虑
MIN_IMAGES = 1

# 文本最小长度 (过滤太短的推文)
MIN_TEXT_LENGTH = 30


def is_likely_prompt_tweet(tweet: Dict) -> tuple[bool, str]:
    """
    第一阶段过滤: 基于关键词和特征判断是否可能包含提示词

    Args:
        tweet: 推文数据

    Returns:
        (is_likely, reason): 是否可能是提示词推文及原因
    """
    text = tweet.get("text", "").lower()
    images = tweet.get("images", [])

    # 必须有图片
    if len(images) < MIN_IMAGES:
        return False, "no_images"

    # 文本太短
    if len(text) < MIN_TEXT_LENGTH:
        return False, "text_too_short"

    # 检查关键词
    matched_keywords = []
    for keyword in PROMPT_KEYWORDS:
        if keyword.lower() in text:
            matched_keywords.append(keyword)

    if matched_keywords:
        return True, f"keywords: {', '.join(matched_keywords[:3])}"

    # 检查是否有长段落描述 (Nano Banana 风格的自然语言提示词)
    # 通常提示词会有较长的连续描述
    if len(text) > 200:
        # 检查是否有中文描述性内容
        chinese_descriptors = ["风格", "场景", "背景", "人物", "颜色", "光线", "氛围", "构图"]
        for desc in chinese_descriptors:
            if desc in text:
                return True, f"descriptive: {desc}"

    return False, "no_match"


# ========== 爆款定义 ==========

# 爆款阈值配置
VIRAL_THRESHOLDS = {
    "likes_min": 1000,        # 点赞 >= 1000
    "retweets_min": 500,      # 转发 >= 500
    "views_min": 100000,      # 浏览量 >= 100k
    "likes_small_account": 500,  # 小账号（<10k粉）点赞阈值
    "engagement_rate_min": 0.01,  # 互动率 >= 1%
}


def is_viral_tweet(tweet: Dict, follower_count: int = 0) -> tuple[bool, str]:
    """
    判断推文是否为爆款

    Args:
        tweet: 推文数据字典
        follower_count: 发布者粉丝数（用于计算互动率）

    Returns:
        (is_viral, reason): 是否爆款及原因
    """
    likes = tweet.get("likes", 0) or 0
    retweets = tweet.get("retweets", 0) or 0
    views = tweet.get("views", 0) or 0

    reasons = []

    # 绝对数值判断
    if likes >= VIRAL_THRESHOLDS["likes_min"]:
        reasons.append(f"likes={likes}")

    if retweets >= VIRAL_THRESHOLDS["retweets_min"]:
        reasons.append(f"retweets={retweets}")

    if views >= VIRAL_THRESHOLDS["views_min"]:
        reasons.append(f"views={views}")

    # 小账号判断（粉丝数 < 10k）
    if follower_count > 0 and follower_count < 10000:
        if likes >= VIRAL_THRESHOLDS["likes_small_account"]:
            reasons.append(f"small_account_viral(likes={likes})")

    # 互动率判断（如果有粉丝数）
    if follower_count > 0:
        engagement = (likes + retweets) / follower_count
        if engagement >= VIRAL_THRESHOLDS["engagement_rate_min"]:
            reasons.append(f"engagement_rate={engagement:.1%}")

    is_viral = len(reasons) > 0
    reason = ", ".join(reasons) if reasons else "not_viral"

    return is_viral, reason


def get_viral_score(tweet: Dict) -> int:
    """
    计算推文的爆款评分（用于排序）

    基于 X 算法权重：
    - 点赞: +30 分/个
    - 转发: +20 分/个
    - 浏览量: +0.001 分/个

    Returns:
        爆款评分
    """
    likes = tweet.get("likes", 0) or 0
    retweets = tweet.get("retweets", 0) or 0
    views = tweet.get("views", 0) or 0

    score = (likes * 30) + (retweets * 20) + (views * 0.001)
    return int(score)


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
    """将 AI 分类映射到系统分类"""
    raw_category = classification.get("category", "Other")

    if raw_category in CATEGORY_MAP:
        return CATEGORY_MAP[raw_category]

    raw_lower = raw_category.lower()
    for key, value in CATEGORY_MAP.items():
        if key.lower() in raw_lower or raw_lower in key.lower():
            return value

    return "Photography"


# ========== Nitter/RSS 方式获取时间线 ==========

# Nitter 实例列表 (公开可用)
NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.1d4.us",
    "https://nitter.net",
    "https://nitter.cz",
]

# RSSHub 实例 (另一种获取 Twitter Timeline 的方式)
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
]


def fetch_user_timeline_nitter(username: str, count: int = 20) -> List[Dict]:
    """
    使用 Nitter RSS 获取用户时间线

    Args:
        username: Twitter 用户名 (不含 @)
        count: 获取数量

    Returns:
        推文列表
    """
    tweets = []

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/rss+xml, application/xml, text/xml',
    }

    for instance in NITTER_INSTANCES:
        try:
            rss_url = f"{instance}/{username}/rss"
            response = requests.get(rss_url, headers=headers, timeout=15)

            if response.status_code == 200:
                # 解析 RSS
                tweets = parse_nitter_rss(response.text, username, count)
                if tweets:
                    print(f"   [Nitter] Got {len(tweets)} tweets from {instance}")
                    return tweets
        except Exception as e:
            print(f"   [Nitter] {instance} failed: {e}")
            continue

    return tweets


def parse_nitter_rss(xml_content: str, username: str, count: int = 20) -> List[Dict]:
    """解析 Nitter RSS 内容"""
    import re

    tweets = []

    if not HAS_BS4:
        print("   [Nitter] BeautifulSoup not available")
        return tweets

    soup = BeautifulSoup(xml_content, 'xml')
    items = soup.find_all('item')[:count]

    for item in items:
        try:
            # 提取链接和推文 ID
            link = item.find('link')
            if not link:
                continue
            link_text = link.get_text()

            # 从链接提取 tweet_id
            # 格式: https://nitter.xxx/username/status/123456789#m
            match = re.search(r'/status/(\d+)', link_text)
            if not match:
                continue
            tweet_id = match.group(1)

            # 提取正文
            description = item.find('description')
            text = ""
            images = []

            if description:
                desc_html = description.get_text()
                desc_soup = BeautifulSoup(desc_html, 'html.parser')

                # 提取文本 (去除图片描述)
                for img in desc_soup.find_all('img'):
                    img_src = img.get('src', '')
                    if 'pbs.twimg.com' in img_src or 'twimg.com' in img_src:
                        images.append(img_src)
                    img.decompose()

                text = desc_soup.get_text(separator=' ').strip()

            # 提取发布时间
            pub_date = item.find('pubDate')
            created_at = pub_date.get_text() if pub_date else None

            tweets.append({
                "id": tweet_id,
                "text": text,
                "created_at": created_at,
                "username": username,
                "url": f"https://x.com/{username}/status/{tweet_id}",
                "images": images,
                "likes": 0,  # RSS 不提供这些数据
                "retweets": 0,
                "views": 0,
            })
        except Exception as e:
            continue

    return tweets


def fetch_user_timeline_syndication(username: str, count: int = 20) -> List[Dict]:
    """
    使用 Twitter Syndication API 获取用户时间线
    这是 Twitter 官方的嵌入 API，不需要认证
    """
    import re

    tweets = []

    # Twitter Syndication Timeline API
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml',
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)

        if response.status_code == 200:
            # 从 JSON 数据中提取 tweet IDs
            tweet_ids = re.findall(r'"id_str":"(\d+)"', response.text)

            seen_ids = set()
            for tweet_id in tweet_ids:
                if tweet_id not in seen_ids:
                    seen_ids.add(tweet_id)
                    tweets.append({
                        "id": tweet_id,
                        "username": username,
                        "url": f"https://x.com/{username}/status/{tweet_id}",
                    })
                    if len(tweets) >= count:
                        break

            if tweets:
                print(f"   [Syndication] Got {len(tweets)} tweet IDs")
            else:
                # 检查是否账号不存在或被限制
                if "UserUnavailable" in response.text or "This account doesn" in response.text:
                    print(f"   [Syndication] Account unavailable or suspended")
                elif len(response.text) < 1000:
                    print(f"   [Syndication] Empty or minimal response")
        else:
            print(f"   [Syndication] HTTP {response.status_code}")

    except Exception as e:
        print(f"   [Syndication] Error: {e}")

    return tweets


def fetch_user_timeline_rsshub(username: str, count: int = 20) -> List[Dict]:
    """
    使用 RSSHub 获取用户时间线

    Args:
        username: Twitter 用户名 (不含 @)
        count: 获取数量

    Returns:
        推文列表 (只包含 tweet_id，需要后续获取详情)
    """
    import re

    tweets = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/rss+xml, application/xml, text/xml',
    }

    for instance in RSSHUB_INSTANCES:
        try:
            # RSSHub Twitter 路由: /twitter/user/:id
            rss_url = f"{instance}/twitter/user/{username}"
            response = requests.get(rss_url, headers=headers, timeout=15)

            if response.status_code == 200:
                # 解析 RSS 提取 tweet IDs
                if HAS_BS4:
                    soup = BeautifulSoup(response.text, 'xml')
                    items = soup.find_all('item')[:count]

                    for item in items:
                        link = item.find('link')
                        if link:
                            link_text = link.get_text()
                            # 提取 tweet_id
                            match = re.search(r'/status/(\d+)', link_text)
                            if match:
                                tweet_id = match.group(1)
                                tweets.append({
                                    "id": tweet_id,
                                    "username": username,
                                    "url": f"https://x.com/{username}/status/{tweet_id}",
                                })

                if tweets:
                    print(f"   [RSSHub] Got {len(tweets)} tweet IDs from {instance}")
                    return tweets
        except Exception as e:
            print(f"   [RSSHub] {instance} failed: {e}")
            continue

    return tweets


def fetch_tweet_details(tweet_id: str, username: str) -> Optional[Dict]:
    """
    获取单条推文的详细信息 (包括图片、互动数据)
    使用 FxTwitter/VxTwitter API
    """
    try:
        data = fetch_with_fxtwitter(tweet_id, username)
        result = parse_fxtwitter_result(data)
        if result and result.get("text"):
            stats = result.get("stats", {})
            return {
                "id": tweet_id,
                "text": result.get("text", ""),
                "images": result.get("images", []),
                "username": username,
                "url": f"https://x.com/{username}/status/{tweet_id}",
                "likes": stats.get("likes", 0),
                "retweets": stats.get("retweets", 0),
                "views": stats.get("views", 0),
                "created_at": result.get("created_at"),
            }
    except Exception:
        pass

    try:
        data = fetch_with_vxtwitter(tweet_id, username)
        result = parse_vxtwitter_result(data)
        if result and result.get("text"):
            stats = result.get("stats", {})
            return {
                "id": tweet_id,
                "text": result.get("text", ""),
                "images": result.get("images", []),
                "username": username,
                "url": f"https://x.com/{username}/status/{tweet_id}",
                "likes": stats.get("likes", 0),
                "retweets": stats.get("retweets", 0),
                "views": stats.get("views", 0),
                "created_at": result.get("created_at"),
            }
    except Exception:
        pass

    return None


# ========== X/Twitter 客户端 ==========

class XMonitor:
    """X/Twitter 监控器 - 使用 Syndication API + FxTwitter"""

    def __init__(self, use_guest: bool = False):
        # use_guest 参数保留以兼容 CLI，但不再使用
        pass

    async def init_client(self):
        """初始化客户端 - 使用无需认证的 API"""
        print("[X] Using Syndication API + FxTwitter (no auth required)")

    async def get_user_tweets(self, username: str, count: int = 20) -> List[Dict]:
        """获取用户最新推文"""
        tweets = []

        # 方法 1: Twitter Syndication API (官方嵌入 API，最可靠)
        print(f"   [1] Trying Syndication API...")
        syn_tweets = fetch_user_timeline_syndication(username, count)
        if syn_tweets:
            for st in syn_tweets:
                details = fetch_tweet_details(st["id"], username)
                if details:
                    tweets.append(details)
                if len(tweets) >= count:
                    break
            if tweets:
                return tweets

        # 方法 2: Nitter RSS (备用)
        print(f"   [2] Trying Nitter RSS...")
        nitter_tweets = fetch_user_timeline_nitter(username, count)
        if nitter_tweets:
            for nt in nitter_tweets:
                details = fetch_tweet_details(nt["id"], username)
                if details:
                    tweets.append(details)
                else:
                    tweets.append(nt)
                if len(tweets) >= count:
                    break

        return tweets


# ========== 主处理逻辑 ==========

async def process_tweet(db: Database, tweet: Dict, state: Dict,
                        viral_only: bool = False, dry_run: bool = False) -> bool:
    """处理单条推文

    Args:
        db: 数据库连接
        tweet: 推文数据
        state: 处理状态
        viral_only: 是否只处理爆款推文
        dry_run: 预览模式，不写入数据库
    """
    tweet_id = tweet["id"]
    tweet_url = tweet["url"]
    text = tweet["text"]
    images = tweet["images"]
    username = tweet["username"]
    likes = tweet.get("likes", 0)
    retweets = tweet.get("retweets", 0)
    views = tweet.get("views", 0)

    # 检查是否已处理
    if is_tweet_processed(state, tweet_id):
        return False

    # 检查数据库是否已存在
    if db.prompt_exists(tweet_url):
        mark_tweet_processed(state, tweet_id)
        return False

    # ========== 第一阶段: 特征匹配过滤 ==========
    is_likely, likely_reason = is_likely_prompt_tweet(tweet)
    if not is_likely:
        # 静默跳过不太可能是提示词的推文
        mark_tweet_processed(state, tweet_id)
        # 返回特殊值表示被第一阶段过滤
        return "filtered_stage1"

    # 爆款判断
    is_viral, viral_reason = is_viral_tweet(tweet)
    viral_score = get_viral_score(tweet)

    # 如果只要爆款，跳过非爆款
    if viral_only and not is_viral:
        mark_tweet_processed(state, tweet_id)
        return False

    # 显示推文信息 (通过第一阶段筛选的)
    viral_badge = "🔥 VIRAL" if is_viral else ""
    print(f"\n   [Tweet] @{username} - {tweet_id} {viral_badge}")
    print(f"   Match: {likely_reason}")
    print(f"   Text: {text[:100]}...")
    print(f"   Stats: ❤️ {likes:,} | 🔁 {retweets:,} | 👁️ {views:,} | Score: {viral_score:,}")
    print(f"   Images: {len(images)}")
    if is_viral:
        print(f"   Viral: {viral_reason}")

    # ========== 第二阶段: AI 提取和验证 ==========
    try:
        print(f"   [AI] Extracting prompt...")
        extracted_prompt = extract_prompt_with_ai(text, model=AI_MODEL)

        if not extracted_prompt or extracted_prompt == "No prompt found":
            print(f"   [Skip] AI found no prompt")
            mark_tweet_processed(state, tweet_id)
            # 返回特殊值表示被第二阶段 AI 过滤
            return "filtered_stage2"

        # AI 分类
        print(f"   [AI] Classifying...")
        classification = classify_prompt_with_ai(extracted_prompt, model=AI_MODEL)

        # 准备数据
        title = classification.get("title", "").strip()
        if not title or title == "Untitled Prompt":
            title = f"@{username} #{tweet_id[-6:]}"

        category = map_category(classification)
        tags = classification.get("sub_categories", [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if t][:5]

        # Dry Run 模式
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
            import_source=f"x-monitor"
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


async def monitor_accounts(
    accounts: List[str],
    tweets_per_account: int = 10,
    viral_only: bool = False,
    dry_run: bool = False
) -> Dict:
    """监听账号列表

    Args:
        accounts: 要监听的账号列表
        tweets_per_account: 每个账号获取的推文数量
        viral_only: 是否只处理爆款推文
        dry_run: 预览模式，不写入数据库
    """

    print("=" * 60)
    print("X/Twitter AI Art Monitor")
    print("=" * 60)
    print(f"Accounts: {len(accounts)}")
    print(f"Viral Only: {viral_only}")
    print(f"Dry Run: {dry_run}")
    print(f"AI Model: {AI_MODEL}")
    print("=" * 60)

    # 检查数据库
    if not DATABASE_URL:
        print("[Error] DATABASE_URL not set")
        return {"error": "DATABASE_URL not set"}

    # 初始化
    db = Database(DATABASE_URL)
    monitor = XMonitor()
    state = load_state()

    stats = {
        "accounts_checked": 0,
        "tweets_found": 0,
        "filtered_stage1": 0,  # 第一阶段过滤 (特征匹配)
        "filtered_stage2": 0,  # 第二阶段过滤 (AI 提取失败)
        "prompts_saved": 0,
        "errors": 0,
    }

    try:
        db.connect()
        print("[DB] Connected")

        await monitor.init_client()
        print("[X] Client ready")

        # 遍历账号
        for i, username in enumerate(accounts, 1):
            print(f"\n[{i}/{len(accounts)}] Checking @{username}...")

            try:
                tweets = await monitor.get_user_tweets(username, tweets_per_account)
                stats["accounts_checked"] += 1
                stats["tweets_found"] += len(tweets)

                print(f"   Found {len(tweets)} tweets")

                for tweet in tweets:
                    result = await process_tweet(db, tweet, state, viral_only=viral_only, dry_run=dry_run)
                    if result == "filtered_stage1":
                        stats["filtered_stage1"] += 1
                    elif result == "filtered_stage2":
                        stats["filtered_stage2"] += 1
                    elif result is True:
                        stats["prompts_saved"] += 1

                # 避免请求过快
                await asyncio.sleep(2)

            except Exception as e:
                print(f"   [Error] {e}")
                stats["errors"] += 1

        # 更新状态
        state["last_check"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    finally:
        db.close()

    # 输出统计
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Accounts checked: {stats['accounts_checked']}")
    print(f"Tweets found: {stats['tweets_found']}")
    print(f"  ├─ Stage 1 filtered (no keywords): {stats['filtered_stage1']}")
    print(f"  ├─ Stage 2 filtered (AI no prompt): {stats['filtered_stage2']}")
    print(f"  └─ Prompts saved: {stats['prompts_saved']}")
    print(f"Errors: {stats['errors']}")
    print("=" * 60)

    return stats


async def run_continuous(
    accounts: List[str],
    interval_minutes: int = 30,
    viral_only: bool = False,
    dry_run: bool = False
):
    """持续监听模式"""
    print(f"Starting continuous monitor (interval: {interval_minutes} min)")
    print(f"Viral Only: {viral_only}")
    print(f"Dry Run: {dry_run}")
    print("Press Ctrl+C to stop\n")

    while True:
        try:
            await monitor_accounts(accounts, viral_only=viral_only, dry_run=dry_run)

            print(f"\nNext check in {interval_minutes} minutes...")
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
        description="X/Twitter AI Art Account Monitor (使用 Syndication API + FxTwitter，无需认证)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List top authors from database
  python fetch_x_accounts.py --list-authors

  # Run once with default accounts
  python fetch_x_accounts.py

  # Monitor top 20 authors from database
  python fetch_x_accounts.py --top 20

  # Dry run (don't save to database)
  python fetch_x_accounts.py --top 10 --dry-run

  # Continuous monitoring top 30 authors (every 30 min)
  python fetch_x_accounts.py --top 30 --interval 30

  # Only process viral tweets (high engagement)
  python fetch_x_accounts.py --top 30 --viral-only

  # Continuous viral-only monitoring
  python fetch_x_accounts.py --top 30 --interval 30 --viral-only

  # Monitor specific accounts
  python fetch_x_accounts.py --accounts midjourney,openai
        """
    )

    parser.add_argument("--accounts", "-a", type=str,
                        help="Comma-separated account list")
    parser.add_argument("--top", "-t", type=int, default=0,
                        help="Use top N authors from database (e.g., --top 20)")
    parser.add_argument("--list-authors", action="store_true",
                        help="List top authors from database and exit")
    parser.add_argument("--interval", "-i", type=int, default=0,
                        help="Continuous mode interval in minutes (0=run once)")
    parser.add_argument("--count", "-c", type=int, default=10,
                        help="Tweets per account (default: 10)")
    parser.add_argument("--viral-only", "-v", action="store_true",
                        help="Only process viral tweets (likes>=1000, retweets>=500, views>=100k)")
    parser.add_argument("--dry-run", "-d", action="store_true",
                        help="Dry run mode - fetch and process but don't save to database")

    args = parser.parse_args()

    # 列出高频作者
    if args.list_authors:
        if not DATABASE_URL:
            print("Error: DATABASE_URL not set")
            return
        db = Database(DATABASE_URL)
        db.connect()
        authors = db.get_top_authors(50)
        db.close()
        print("Top 50 Authors (from database):")
        print("=" * 50)
        for i, row in enumerate(authors, 1):
            print(f"{i:3}. @{row['author']:<25} {row['count']:>5} prompts")
        return

    # 解析账号列表
    if args.accounts:
        accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    elif args.top > 0:
        # 从数据库获取高频作者
        if not DATABASE_URL:
            print("Error: DATABASE_URL not set")
            return
        db = Database(DATABASE_URL)
        db.connect()
        authors = db.get_top_authors(args.top)
        db.close()
        accounts = [row['author'] for row in authors]
        print(f"Using top {len(accounts)} authors from database")
    else:
        accounts = DEFAULT_ACCOUNTS

    # 运行
    if args.interval > 0:
        asyncio.run(run_continuous(
            accounts,
            interval_minutes=args.interval,
            viral_only=args.viral_only,
            dry_run=args.dry_run
        ))
    else:
        asyncio.run(monitor_accounts(
            accounts,
            tweets_per_account=args.count,
            viral_only=args.viral_only,
            dry_run=args.dry_run
        ))


if __name__ == "__main__":
    main()
