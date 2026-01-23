#!/usr/bin/env python3
"""
X/Twitter AI Art Account Monitor
监听 AI 艺术账号，自动提取提示词并入库

技术方案:
- twikit: 使用 X 账号 cookies 获取用户时间线
- FxTwitter/VxTwitter API: 获取推文详情和互动数据 (备用)

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
    X_COOKIE            - X 账号 cookies JSON (推荐): '{"auth_token": "xxx", "ct0": "xxx"}'
    X_USERNAME          - X 账号用户名 (备用登录方式)
    X_EMAIL             - X 账号邮箱
    X_PASSWORD          - X 账号密码
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

# HTTP 请求
import requests
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# 导入 AI 处理函数 (统一使用 prompt_utils)
from prompt_utils import DEFAULT_MODEL, process_tweet_for_import

# 导入 Twitter API 函数
from fetch_twitter_content import (
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

# X 账号凭证
X_USERNAME = os.environ.get("X_USERNAME", "")
X_EMAIL = os.environ.get("X_EMAIL", "")
X_PASSWORD = os.environ.get("X_PASSWORD", "")

# Cookies 配置 (优先使用环境变量)
X_COOKIE = os.environ.get("X_COOKIE", "")  # JSON 字符串: '{"auth_token": "xxx", "ct0": "xxx"}'
COOKIES_FILE = Path(__file__).parent / "x_cookies.json"

# 代理配置 (如果需要)
PROXY_URL = os.environ.get("X_PROXY", "")

# 状态文件 (记录已处理的推文 ID)
STATE_FILE = Path(__file__).parent / "x_monitor_state.json"

# ========== 防刷配置 (Rate Limit) ==========
# 参考: https://github.com/d60/twikit/blob/main/ratelimits.md

# 请求间隔配置 (秒)
DELAY_BETWEEN_TWEETS = (2, 5)       # 处理每条推文后的随机延迟
DELAY_BETWEEN_ACCOUNTS = (5, 10)    # 切换账号时的随机延迟
DELAY_ON_RATE_LIMIT = 60            # 遇到限流时的基础等待时间
MAX_RETRIES_ON_RATE_LIMIT = 3       # 限流重试次数
DELAY_BETWEEN_API_CALLS = (1, 3)    # API 调用间隔

# 默认监听的 AI 艺术账号 (基于数据库高频统计 + twitterhot.vercel.app)
DEFAULT_ACCOUNTS = [
    # Top 20 高频账号 (从数据库统计)
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
    # === 以下账号来自 twitterhot.vercel.app (2026-01-10 导入) ===
    "0xInk_",
    "0xbisc",
    "369labsx",
    "3DVR3",
    "4on_yon_x",
    "94vanAI",
    "AIFrontliner",
    "AIMevzulari",
    "AIwithSynthia",
    "AIwithkhan",
    "Adam38363368936",
    "AdemVessell",
    "AleRVG",
    "AllaAisling",
    "AllarHaltsonen",
    "AltugAkgul",
    "AmirMushich",
    "Angaisb_",
    "Arminn_Ai",
    "BeanieBlossom",
    "BeautyVerse_Lab",
    "Bitturing",
    "BrettFromDJ",
    "CaptainHaHaa",
    "CharaspowerAI",
    "ChatgptAIskill",
    "ChillaiKalan__",
    "Citrini7",
    "ClaireSilver12",
    "Creatify_AI",
    "Cydiar404",
    "D_studioproject",
    "Dari_Designs",
    "David_eficaz",
    "DilumSanjaya",
    "DmitryLepisov",
    "DrFonts",
    "EHuanglu",
    "FitzGPT",
    "FlowbyGoogle",
    "FuSheng_0306",
    "GammaApp",
    "GeminiApp",
    "GlitterPixely",
    "GoogleLabs",
    "Gorden_Sun",
    "HBCoop_",
    "Harboris_27",
    "IamEmily2050",
    "Ibrahim56072637",
    "IqraSaifiii",
    "JZhen72937",
    "JasonBud",
    "JefferyTatsuya",
    "Jimmy_JingLv",
    "KanaWorks_AI",
    "KeorUnreal",
    "Kerroudjm",
    "KusoPhoto",
    "LZhou15365",
    "LiEvanna85716",
    "Limorio_",
    "LinusEkenstam",
    "LudovicCreator",
    "LufzzLiz",
    "MANISH1027512",
    "Me_Rock369",
    "Mho_23",
    "MissMi1973",
    "MrDavids1",
    "Mr_AllenT",
    "NahFlo2n",
    "Naiknelofar788",
    "NanoBanana",
    "NanoBanana_labs",
    "Noguma_Morino",
    "OTFHD",
    "OdinLovis",
    "Ok_shuai",
    "OkunevUA",
    "PolymarketMoney",
    "RAVIKUMARSAHU78",
    "Raylan89",
    "ReflctWillie",
    "RobotCleopatra",
    "Ror_Fly",
    "SDT_side",
    "SSSS_CRYPTOMAN",
    "Saboo_Shubham_",
    "Saccc_c",
    "Samann_ai",
    "Sheldon056",
    "Shimayus",
    "ShreyaYadav___",
    "SiboEsenkova",
    "Taaruk_",
    "TechByMarkandey",
    "TechieBySA",
    "TheMattBerman",
    "The_Sycomore",
    "Tz_2022",
    "VibeMarketer_",
    "Whizz_ai",
    "WuxiaRocks",
    "YZOkulu",
    "ZHO_ZHO_ZHO",
    "Zar_xplorer",
    "_3912657840",
    "_MehdiSharifi_",
    "_smcf",
    "aaliya_va",
    "ai_for_success",
    "aiwarts",
    "anandh_ks_",
    "anvishapai",
    "anzedetn",
    "archi_reum",
    "artisin_ai",
    "asdfghdevv",
    "avstudiosng",
    "ayami_marketing",
    "aziz4ai",
    "bananababydoll",
    "beechinour",
    "beginnersblog1",
    "berryxia",
    "berryxia_ai",
    "bindureddy",
    "bobbykun_banana",
    "bozhou_ai",
    "brad_zhang2024",
    "canghecode",
    "cartunmafia",
    "cfryant",
    "cheerselflin",
    "cheese_ai07",
    "chengzi_95330",
    "chillhousedev",
    "cnyzgkc",
    "condzxyz",
    "craftian_keskin",
    "design_with_ayo",
    "develogon0",
    "dhumann",
    "dr_cintas",
    "ducktheaff",
    "ecommartinez",
    "egeberkina",
    "elCarlosVega",
    "emollick",
    "eveningbtc",
    "eviljer",
    "excel_niisan",
    "fAIkout",
    "felo_ai",
    "firatbilal",
    "firemadeher",
    "fofrAI",
    "freddier",
    "futamen_0308",
    "gaucheai",
    "genel_ai",
    "genspark_ai",
    "genspark_japan",
    "ghumare64",
    "gisellaesthetic",
    "gizakdag",
    "glennwrites1",
    "gokayfem",
    "goo_vision",
    "guicastellanos1",
    "hAru_mAki_ch",
    "harboriis",
    "hckmstrrahul",
    "helinvision",
    "henrydaubrez",
    "higgsfield_ai",
    "hx831126",
    "iX00AI",
    "iam_vampire_0",
    "iamsofiaijaz",
    "iamtonyzhu",
    "icreatelife",
    "imxiaohu",
    "itis_Jarvo33",
    "jamesyeung18",
    "john_my07",
    "kaanakz",
    "kabu_st0ck",
    "karim_yourself",
    "kashmir_ki_lark",
    "kei31",
    "kingofdairyque",
    "kohaku_00",
    "ksmhope",
    "langzihan",
    "learn2vibe",
    "linxiaobei888",
    "loveko28516",
    "madebygoogle",
    "manerun_",
    "mattiapomelli",
    "maxescu",
    "med3bbas",
    "meng_dagg695",
    "michaelrabone",
    "miilesus",
    "milbon_",
    "mimi_aiart",
    "minchoi",
    "miyabi_foxx",
    "mmmiyama_D",
    "monicamoonx",
    "moshimoshi_ai",
    "msjiaozhu",
    "munou_ac",
    "nabe1975",
    "nagano_yoh",
    "nimentrix",
    "ninohut",
    "notoro_ai",
    "old_pgmrs_will",
    "op7418",
    "oreno_musume",
    "osanseviero",
    "ozan_sihay",
    "paularambles",
    "qisi_ai",
    "r4jjesh",
    "ratman_aiillust",
    "rionaifantasy",
    "rovvmut_",
    "ryosan1904",
    "s_tiva",
    "sasakitoshinao",
    "schnapoon",
    "serena_ailab",
    "sergeantsref",
    "shota7180",
    "showheyohtaki",
    "sidharthgehlot",
    "so_ainsight",
    "sodaguyx",
    "sonucnc2",
    "ss_uulq09",
    "stitchbygoogle",
    "studio_veco",
    "sudharps",
    "sundarpichai",
    "sundyme",
    "taiyo_ai_gakuse",
    "techhalla",
    "tegnike",
    "testingcatalog",
    "threeaus",
    "tisch_eins",
    "tkm_hmng8",
    "treydtw",
    "trxuanxw",
    "tsyn18",
    "ttmouse",
    "tuzi_ai",
    "underwoodxie96",
    "venturetwins",
    "wad0427",
    "wanerfu",
    "xiaojietongxue",
    "yachimat_manga",
    "yammmy_hedgehog",
    "yanhua1010",
    "youraipulse",
    "yuanzhe68949664",
    "yyyole",
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

# 导入统一分类映射 (定义在 prompt_utils.py)
from prompt_utils import CATEGORY_MAP, map_category


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

def random_delay(delay_range: tuple, description: str = ""):
    """添加随机延迟，模拟人类行为"""
    delay = random.uniform(delay_range[0], delay_range[1])
    if description:
        print(f"   [Delay] {description}: {delay:.1f}s")
    time.sleep(delay)


class XMonitor:
    """X/Twitter 监控器 - 使用 twikit 获取用户时间线"""

    def __init__(self, use_guest: bool = False):
        # use_guest 参数保留以兼容 CLI，但不再使用
        self.client = None
        self.logged_in = False
        self.request_count = 0
        self.last_request_time = 0

    def _clear_cf_cookies(self):
        """清理 Cloudflare cookies 避免冲突"""
        if self.client and hasattr(self.client, '_session') and self.client._session:
            try:
                # 获取 session 的 cookies jar
                session = self.client._session
                if hasattr(session, 'cookies') and hasattr(session.cookies, 'jar'):
                    # 删除所有 __cf_bm cookies
                    cookies_to_remove = []
                    for cookie in session.cookies.jar:
                        if cookie.name == '__cf_bm':
                            cookies_to_remove.append((cookie.name, cookie.domain))
                    for name, domain in cookies_to_remove:
                        try:
                            session.cookies.delete(name, domain=domain)
                        except:
                            pass
                    if cookies_to_remove:
                        print(f"   [twikit] Cleared {len(cookies_to_remove)} __cf_bm cookies")
            except Exception as e:
                print(f"   [twikit] Failed to clear cookies: {e}")

    async def init_client(self):
        """初始化客户端 - 使用 twikit + cookies"""
        if not HAS_TWIKIT:
            raise RuntimeError("twikit not installed. Run: pip install twikit")

        # 运行时获取环境变量 (确保能读到 GitHub Actions 设置的 secrets)
        x_cookie = os.environ.get("X_COOKIE", "")
        x_username = os.environ.get("X_USERNAME", "")
        x_email = os.environ.get("X_EMAIL", "")
        x_password = os.environ.get("X_PASSWORD", "")
        proxy_url = os.environ.get("X_PROXY", "")

        # 调试信息
        print(f"[twikit] X_COOKIE env: {'set (' + str(len(x_cookie)) + ' chars)' if x_cookie else 'not set'}")
        print(f"[twikit] X_USERNAME env: {'set' if x_username else 'not set'}")
        print(f"[twikit] COOKIES_FILE: {COOKIES_FILE} (exists: {COOKIES_FILE.exists()})")

        # 初始化客户端 (支持代理)
        if proxy_url:
            print(f"[twikit] Using proxy: {proxy_url[:20]}...")
            self.client = Client('en-US', proxy=proxy_url)
        else:
            self.client = Client('en-US')

        # 尝试使用 cookies 登录 (优先使用环境变量)
        if x_cookie:
            try:
                # 支持多种格式
                cookie_str = x_cookie.strip()
                # 如果是单引号包裹，转换为双引号
                if cookie_str.startswith("'") and cookie_str.endswith("'"):
                    cookie_str = cookie_str[1:-1]
                cookie_data = json.loads(cookie_str)
                print(f"[twikit] Parsed cookie keys: {list(cookie_data.keys())}")

                # 写入临时文件供 twikit 加载
                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                    json.dump(cookie_data, f)
                    temp_cookie_file = f.name
                self.client.load_cookies(temp_cookie_file)
                os.unlink(temp_cookie_file)  # 删除临时文件
                print("[twikit] Loaded cookies from X_COOKIE env")
                self.logged_in = True
                return
            except json.JSONDecodeError as e:
                print(f"[twikit] Failed to parse X_COOKIE JSON: {e}")
                print(f"[twikit] X_COOKIE value (first 50 chars): {x_cookie[:50]}...")
            except Exception as e:
                print(f"[twikit] Failed to load cookies from env: {e}")

        if COOKIES_FILE.exists():
            try:
                self.client.load_cookies(str(COOKIES_FILE))
                print("[twikit] Loaded cookies from file")
                self.logged_in = True
                return
            except Exception as e:
                print(f"[twikit] Failed to load cookies from file: {e}")

        # 使用账号密码登录
        if x_username and x_password:
            try:
                print("[twikit] Logging in with credentials...")
                await self.client.login(
                    auth_info_1=x_username,
                    auth_info_2=x_email,
                    password=x_password
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
        print("需要 X 账号 cookies 才能获取用户时间线")
        print("=" * 60)
        print("\n设置方法:")
        print("1. 在 GitHub Secrets 中添加 X_COOKIE:")
        print('   值格式: {"auth_token": "xxx", "ct0": "xxx"}')
        print("\n2. 或者从浏览器导出 cookies:")
        print("   - 在 Chrome 登录 x.com")
        print("   - F12 > Application > Cookies > https://x.com")
        print("   - 复制 auth_token 和 ct0 的值")
        print("\n" + "=" * 60)
        raise RuntimeError("No X_COOKIE env or cookies file found.")

    async def _handle_rate_limit(self, e: Exception, retry_count: int) -> bool:
        """处理限流，返回是否应该重试"""
        # 检查是否是 TooManyRequests 异常
        if 'TooManyRequests' in str(type(e).__name__) or '429' in str(e):
            if retry_count < MAX_RETRIES_ON_RATE_LIMIT:
                # 尝试获取重置时间
                wait_time = DELAY_ON_RATE_LIMIT * (retry_count + 1)  # 指数退避
                if hasattr(e, 'rate_limit_reset') and e.rate_limit_reset:
                    wait_time = max(wait_time, e.rate_limit_reset - time.time())

                # 添加随机抖动 (jitter)
                jitter = random.uniform(0, 10)
                wait_time += jitter

                print(f"   [Rate Limit] Waiting {wait_time:.0f}s before retry ({retry_count + 1}/{MAX_RETRIES_ON_RATE_LIMIT})...")
                await asyncio.sleep(wait_time)
                return True
        return False

    async def get_user_tweets(self, username: str, count: int = 20) -> List[Dict]:
        """获取用户最新推文 (带重试和限流处理)"""
        if not self.logged_in:
            await self.init_client()

        tweets = []
        retry_count = 0

        while retry_count <= MAX_RETRIES_ON_RATE_LIMIT:
            try:
                # 清理可能冲突的 Cloudflare cookies
                self._clear_cf_cookies()

                # API 调用间隔
                time_since_last = time.time() - self.last_request_time
                if time_since_last < DELAY_BETWEEN_API_CALLS[0]:
                    await asyncio.sleep(DELAY_BETWEEN_API_CALLS[0] - time_since_last + random.uniform(0, 1))

                # 先获取用户信息
                user = await self.client.get_user_by_screen_name(username)
                self.last_request_time = time.time()
                self.request_count += 1

                if not user:
                    print(f"   [twikit] User not found: @{username}")
                    return tweets

                # 短暂延迟后获取推文
                await asyncio.sleep(random.uniform(*DELAY_BETWEEN_API_CALLS))

                # 获取用户推文
                user_tweets = await self.client.get_user_tweets(user.id, 'Tweets', count=count)
                self.last_request_time = time.time()
                self.request_count += 1

                for tweet in user_tweets:
                    try:
                        # 优先使用 full_text 获取长推文 (note_tweet) 的完整内容
                        tweet_text = ""
                        try:
                            tweet_text = tweet.full_text or tweet.text or ""
                        except Exception:
                            tweet_text = tweet.text or ""

                        tweet_data = {
                            "id": tweet.id,
                            "text": tweet_text,
                            "username": username,
                            "url": f"https://x.com/{username}/status/{tweet.id}",
                            "likes": tweet.favorite_count or 0,
                            "retweets": tweet.retweet_count or 0,
                            "views": tweet.view_count or 0,
                            "created_at": str(tweet.created_at) if tweet.created_at else None,
                            "images": [],
                        }

                        # 提取图片
                        if tweet.media:
                            for media in tweet.media:
                                if hasattr(media, 'media_url') and media.media_url:
                                    img_url = media.media_url
                                    if img_url.startswith('http://'):
                                        img_url = img_url.replace('http://', 'https://')
                                    tweet_data["images"].append(img_url)
                                elif hasattr(media, 'media_url_https') and media.media_url_https:
                                    tweet_data["images"].append(media.media_url_https)

                        tweets.append(tweet_data)

                    except Exception as e:
                        print(f"   [Warning] Failed to parse tweet: {e}")
                        continue

                print(f"   [twikit] Got {len(tweets)} tweets (requests: {self.request_count})")
                return tweets

            except Exception as e:
                error_str = str(e)

                # 处理 cookie 冲突
                if 'Multiple cookies exist' in error_str or '__cf_bm' in error_str:
                    print(f"   [twikit] Cookie conflict detected, clearing and retrying...")
                    self._clear_cf_cookies()
                    retry_count += 1
                    await asyncio.sleep(random.uniform(2, 5))
                    continue

                # 处理限流
                if await self._handle_rate_limit(e, retry_count):
                    retry_count += 1
                    continue

                print(f"   [twikit] Error getting tweets: {e}")
                break

        # 如果 twikit 失败，尝试使用 FxTwitter 备用方案
        print(f"   [Fallback] Trying Syndication API...")
        syn_tweets = fetch_user_timeline_syndication(username, count)
        if syn_tweets:
            for st in syn_tweets:
                # 添加延迟避免 Syndication API 也被限流
                await asyncio.sleep(random.uniform(0.5, 1.5))
                details = fetch_tweet_details(st["id"], username)
                if details:
                    tweets.append(details)
                if len(tweets) >= count:
                    break

        return tweets


# ========== 主处理逻辑 ==========

async def process_tweet(db: Database, tweet: Dict, state: Dict,
                        viral_only: bool = False, dry_run: bool = False) -> bool:
    """处理单条推文 - 使用统一处理函数

    Args:
        db: 数据库连接
        tweet: 推文数据
        state: 处理状态
        viral_only: 是否只处理爆款推文 (保留用于兼容性，但不再使用)
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

    # 检查是否有图片
    if not images:
        mark_tweet_processed(state, tweet_id)
        return False

    # 尝试用 FxTwitter 获取更完整的文本（展开短链接、获取长推文）
    try:
        fx_data = fetch_with_fxtwitter(tweet_id, username)
        fx_result = parse_fxtwitter_result(fx_data)
        if fx_result:
            fx_text = fx_result.get("text", "")
            if fx_text and len(fx_text) > len(text):
                print(f"   [FxTwitter] Got longer text: {len(text)} -> {len(fx_text)} chars")
                text = fx_text
            # 如果 FxTwitter 有更多图片，补充
            if fx_result.get("images") and len(fx_result["images"]) > len(images):
                images = fx_result["images"]
    except Exception as e:
        print(f"   [FxTwitter] Failed to get full text: {e}")

    # 显示推文信息
    print(f"\n   [Tweet] @{username} - {tweet_id}")
    print(f"   Text: {text[:100]}...")
    print(f"   Stats: ❤️ {likes:,} | 🔁 {retweets:,} | 👁️ {views:,}")
    print(f"   Images: {len(images)}")

    # 使用统一处理函数
    result = process_tweet_for_import(
        db=db,
        tweet_url=tweet_url,
        raw_text=text,
        raw_images=images,
        author=username,
        import_source="x-monitor",
        ai_model=AI_MODEL,
        dry_run=dry_run,
        skip_twitter_fetch=True  # 已有 Twitter 图片
    )

    mark_tweet_processed(state, tweet_id)

    if result["success"]:
        return True
    else:
        error = result.get("error", "")
        if error and error != "Already exists":
            print(f"   [Skip] {error}")
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
    print(f"Rate Limit: {DELAY_BETWEEN_ACCOUNTS[0]}-{DELAY_BETWEEN_ACCOUNTS[1]}s between accounts")
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
        "advertisement": 0,    # 广告内容
        "prompt_in_reply": 0,  # Prompt 在评论中
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
                    elif result == "advertisement":
                        stats["advertisement"] += 1
                    elif result == "prompt_in_reply":
                        stats["prompt_in_reply"] += 1
                    elif result is True:
                        stats["prompts_saved"] += 1

                    # 处理推文间延迟 (避免 AI API 限流)
                    await asyncio.sleep(random.uniform(*DELAY_BETWEEN_TWEETS))

                # 账号间延迟 (避免 Twitter 限流)
                delay = random.uniform(*DELAY_BETWEEN_ACCOUNTS)
                print(f"   [Delay] Next account in {delay:.1f}s...")
                await asyncio.sleep(delay)

            except Exception as e:
                error_str = str(e)
                print(f"   [Error] {e}")
                stats["errors"] += 1

                # 如果是限流错误，等待更长时间
                if 'TooManyRequests' in str(type(e).__name__) or '429' in error_str:
                    wait_time = DELAY_ON_RATE_LIMIT + random.uniform(0, 30)
                    print(f"   [Rate Limit] Waiting {wait_time:.0f}s before next account...")
                    await asyncio.sleep(wait_time)

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
    print(f"  ├─ Advertisement (skipped): {stats['advertisement']}")
    print(f"  ├─ Prompt in reply (skipped): {stats['prompt_in_reply']}")
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
    parser = argparse.ArgumentParser(
        description="X/Twitter AI Art Account Monitor (使用 twikit + cookies 认证)",
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
        # 指定账号模式：只使用指定的账号
        accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    else:
        # 合并模式：数据库高频作者 + 默认账号列表
        db_authors = []
        if DATABASE_URL:
            try:
                db = Database(DATABASE_URL)
                db.connect()
                top_count = args.top if args.top > 0 else 50  # 默认取 top 50
                authors = db.get_top_authors(top_count)
                db.close()
                db_authors = [row['author'] for row in authors]
                print(f"[DB] Got {len(db_authors)} authors from database")
            except Exception as e:
                print(f"[DB] Failed to get authors: {e}")

        # 合并并去重（保持顺序：数据库优先，然后是默认列表中的新账号）
        seen = set()
        accounts = []
        for author in db_authors + DEFAULT_ACCOUNTS:
            author_lower = author.lower()  # 忽略大小写去重
            if author_lower not in seen:
                seen.add(author_lower)
                accounts.append(author)

        print(f"[Accounts] Total {len(accounts)} unique accounts (DB: {len(db_authors)}, Default: {len(DEFAULT_ACCOUNTS)})")

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
