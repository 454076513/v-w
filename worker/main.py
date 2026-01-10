#!/usr/bin/env python3
"""
Muse Worker - 一体化邮件监听 + Twitter 抓取 + 数据库写入

工作流程:
1. 连接 Gmail 获取邮件
2. 解析邮件提取 Twitter/X 链接
3. 直接抓取 Twitter 内容
4. 使用 AI 提取提示词并分类
5. 写入 PostgreSQL 数据库

环境变量:
  DATABASE_URL        - PostgreSQL 连接字符串 (必需)
  GMAIL_EMAIL         - Gmail 账号 (必需)
  GMAIL_PASSWORD      - Gmail 应用专用密码 (必需)
  GMAIL_SENDER_FILTER - 发件人过滤关键词 (默认: grok)
  AI_MODEL            - AI 模型 (默认: openai)
"""

import os
import sys
import re
import imaplib
import email
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

# 尝试加载 .env.local
try:
    from dotenv import load_dotenv
    from pathlib import Path
    
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

# 导入 Twitter 抓取模块
from fetch_twitter_content import fetch_tweet, extract_tweet_id, extract_username

# 数据库连接
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
except ImportError:
    print("❌ 请安装 psycopg2: pip install psycopg2-binary")
    sys.exit(1)

# ========== 配置 ==========
DATABASE_URL = os.environ.get("DATABASE_URL", "")
GMAIL_EMAIL = os.environ.get("GMAIL_EMAIL", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")
GMAIL_SENDER_FILTER = os.environ.get("GMAIL_SENDER_FILTER", "grok")
GMAIL_MAILBOX = os.environ.get("GMAIL_MAILBOX", "INBOX")
AI_MODEL = os.environ.get("AI_MODEL", "openai")


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
    
    def email_processed(self, message_id: str) -> bool:
        result = self.execute_one(
            "SELECT id FROM email_records WHERE message_id = %s",
            (message_id,)
        )
        return result is not None
    
    def save_email(self, message_id: str, subject: str, sender: str, 
                   received_at: str, body: str, twitter_links: List[str]) -> Optional[Dict]:
        # 解析 RFC 2822 格式的日期为 PostgreSQL 兼容的 datetime
        parsed_date = None
        if received_at:
            try:
                parsed_date = parsedate_to_datetime(received_at)
            except Exception:
                # 如果解析失败，使用当前时间
                parsed_date = datetime.now(timezone.utc)
        else:
            parsed_date = datetime.now(timezone.utc)
        
        return self.execute_write(
            """
            INSERT INTO email_records (message_id, subject, sender, received_at, body, twitter_links, processed)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING *
            """,
            (message_id, subject, sender, parsed_date, body, twitter_links)
        )
    
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


# ========== Gmail 操作 ==========

def connect_gmail() -> Optional[imaplib.IMAP4_SSL]:
    """连接到 Gmail IMAP 服务器"""
    if not GMAIL_EMAIL or not GMAIL_PASSWORD:
        print("❌ 缺少 GMAIL_EMAIL 或 GMAIL_PASSWORD 环境变量")
        return None
    
    try:
        print(f"🔗 正在连接 Gmail ({GMAIL_EMAIL})...")
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_EMAIL, GMAIL_PASSWORD)
        print("✅ Gmail 登录成功")
        return mail
    except imaplib.IMAP4.error as e:
        print(f"❌ Gmail 登录失败: {e}")
        return None
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return None


def decode_mime_header(header_value: str) -> str:
    """解码 MIME 编码的邮件头"""
    if not header_value:
        return ""
    
    decoded_parts = []
    for part, charset in decode_header(header_value):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(charset or "utf-8", errors="ignore"))
            except:
                decoded_parts.append(part.decode("utf-8", errors="ignore"))
        else:
            decoded_parts.append(part)
    
    return "".join(decoded_parts)


def get_email_body(msg: Message) -> str:
    """提取邮件正文"""
    body = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            
            if "attachment" in content_disposition:
                continue
            
            if content_type == "text/plain":
                try:
                    charset = part.get_content_charset() or "utf-8"
                    body = part.get_payload(decode=True).decode(charset, errors="ignore")
                    break
                except:
                    pass
            elif content_type == "text/html" and not body:
                try:
                    charset = part.get_content_charset() or "utf-8"
                    html = part.get_payload(decode=True).decode(charset, errors="ignore")
                    body = re.sub(r"<[^>]+>", "", html)
                except:
                    pass
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            body = msg.get_payload(decode=True).decode(charset, errors="ignore")
        except:
            body = str(msg.get_payload())
    
    return body.strip()


def extract_twitter_links(text: str) -> List[str]:
    """从文本中提取 Twitter 链接"""
    twitter_urls = []
    
    # 格式 1: §NB§user1/status/123|user2/status/456§ (Grok 格式)
    pattern = r"§NB§(.+?)§"
    match = re.search(pattern, text, re.DOTALL)
    
    if match:
        content = match.group(1).strip()
        items = [item.strip() for item in content.split("|") if item.strip()]
        
        for item in items:
            if "/status/" in item:
                url = f"https://x.com/{item}"
                twitter_urls.append(url)
    
    # 格式 2: 直接的 URL
    url_pattern = r"https?://(?:twitter\.com|x\.com)/\w+/status/\d+"
    direct_urls = re.findall(url_pattern, text)
    
    for url in direct_urls:
        normalized = url.replace("twitter.com", "x.com")
        if normalized not in twitter_urls:
            twitter_urls.append(normalized)
    
    return twitter_urls


def fetch_emails(mail: imaplib.IMAP4_SSL) -> List[Dict[str, Any]]:
    """获取邮件列表"""
    emails = []
    
    try:
        mail.select(GMAIL_MAILBOX)
        
        search_criteria = f'(FROM "{GMAIL_SENDER_FILTER}")'
        print(f"🔍 搜索条件: {search_criteria}")
        
        status, messages = mail.search(None, search_criteria)
        
        if status != "OK":
            print("❌ 搜索失败")
            return emails
        
        email_ids = messages[0].split()
        
        if not email_ids:
            print("📭 没有找到匹配的邮件")
            return emails
        
        print(f"📬 找到 {len(email_ids)} 封邮件")
        
        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            
            if status != "OK":
                continue
            
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    email_data = {
                        "id": email_id.decode(),
                        "message_id": msg.get("Message-ID", ""),
                        "subject": decode_mime_header(msg.get("Subject", "")),
                        "from": decode_mime_header(msg.get("From", "")),
                        "date": msg.get("Date", ""),
                        "body": get_email_body(msg),
                    }
                    
                    emails.append(email_data)
        
        return emails
        
    except Exception as e:
        print(f"❌ 获取邮件失败: {e}")
        return emails


# ========== 分类映射 ==========

# 系统支持的分类列表
SYSTEM_CATEGORIES = [
    "Portrait", "Landscape", "Nature", "Architecture", "Abstract",
    "Sci-Fi", "Fantasy", "Anime", "Photography", "Illustration",
    "Fashion", "Food", "Product", "Cinematic", "Clay / Felt",
    "Retro / Vintage", "Minimalist", "Other"
]

def map_category(classification: Dict) -> str:
    """将 AI 分类映射到系统分类"""
    # 映射表：AI 返回的分类 -> 系统分类
    category_map = {
        # 英文分类（新格式）
        "Portrait": "Portrait",
        "Landscape/Nature": "Landscape",
        "Landscape": "Landscape",
        "Nature": "Nature",
        "Animals": "Nature",
        "Architecture/Urban": "Architecture",
        "Architecture": "Architecture",
        "Urban": "Architecture",
        "Abstract Art": "Abstract",
        "Abstract": "Abstract",
        "Sci-Fi/Futuristic": "Sci-Fi",
        "Sci-Fi": "Sci-Fi",
        "Futuristic": "Sci-Fi",
        "Fantasy/Magic": "Fantasy",
        "Fantasy": "Fantasy",
        "Magic": "Fantasy",
        "Anime/Cartoon": "Anime",
        "Anime": "Anime",
        "Cartoon": "Anime",
        "Realistic Photography": "Photography",
        "Photography": "Photography",
        "Illustration/Painting": "Illustration",
        "Illustration": "Illustration",
        "Painting": "Illustration",
        "Fashion/Clothing": "Fashion",
        "Fashion": "Fashion",
        "Clothing": "Fashion",
        "Food": "Food",
        "Product/Commercial": "Product",
        "Product": "Product",
        "Commercial": "Product",
        "Horror/Dark": "Cinematic",
        "Horror": "Cinematic",
        "Dark": "Cinematic",
        "Cinematic": "Cinematic",
        "Cute/Kawaii": "Clay / Felt",
        "Cute": "Clay / Felt",
        "Kawaii": "Clay / Felt",
        "Vintage/Retro": "Retro / Vintage",
        "Vintage": "Retro / Vintage",
        "Retro": "Retro / Vintage",
        "Minimalist": "Minimalist",
        "Surreal": "Abstract",
        "Other": "Other",
    }
    
    raw_category = classification.get("category", "Other")
    
    # 尝试直接匹配
    if raw_category in category_map:
        return category_map[raw_category]
    
    # 尝试部分匹配（大小写不敏感）
    raw_lower = raw_category.lower()
    for key, value in category_map.items():
        if key.lower() in raw_lower or raw_lower in key.lower():
            return value
    
    # 默认返回 Photography
    return "Photography"


# ========== 主流程 ==========

def process_twitter_url(db: Database, tweet_url: str) -> str:
    """
    处理单个 Twitter URL: 抓取 → 提取提示词 → 入库

    Returns:
        str: 处理结果状态
        - "saved": 成功保存
        - "exists": 已存在
        - "advertisement": 广告内容
        - "no_prompt": 未找到 prompt
        - "prompt_in_reply": prompt 在评论中
        - "failed": 处理失败
    """

    # 检查是否已存在
    if db.prompt_exists(tweet_url):
        print(f"   ⏭️ 已存在，跳过")
        return "exists"
    
    try:
        # 抓取推文内容
        result = fetch_tweet(
            tweet_url,
            download_images=False,
            extract_prompt=True,
            ai_model=AI_MODEL
        )
        
        if not result:
            print(f"   ❌ 抓取失败")
            return "failed"

        extracted_prompt = result.get("extracted_prompt", "")
        classification = result.get("classification") or {}
        images = result.get("images", [])
        prompt_location = result.get("prompt_location", "unknown")

        # 检查是否为广告 (由 fetch_tweet 统一处理)
        if result.get("is_advertisement"):
            print(f"   🚫 检测到广告/推广内容，跳过")
            return "advertisement"

        # 检查是否成功提取提示词
        if extracted_prompt == "Prompt in reply":
            print(f"   ⚠️ Prompt 在评论/回复中，主帖子不包含实际 prompt")
            print(f"   [Info] 需要手动获取评论内容: {tweet_url}")
            return "prompt_in_reply"

        if not extracted_prompt or extracted_prompt == "No prompt found":
            print(f"   ⚠️ 未找到提示词")
            return "no_prompt"
        
        # 准备数据
        # 从分类结果获取 title
        title = classification.get("title", "").strip()
        if not title or title == "Untitled Prompt":
            # 如果没有 title，使用推文 ID
            title = f"Tweet {extract_tweet_id(tweet_url)}"
        
        # 映射分类
        category = map_category(classification)
        
        # 获取 tags (sub_categories 已经在 classify_prompt_with_ai 中处理过了)
        tags = classification.get("sub_categories", [])
        if not isinstance(tags, list):
            tags = []
        
        # 确保 tags 是字符串列表
        tags = [str(t).strip() for t in tags if t]
        
        # 去重
        tags = list(dict.fromkeys(tags))
        
        # 调试日志
        print(f"   📝 准备入库:")
        print(f"      标题: {title}")
        print(f"      分类: {category}")
        print(f"      标签: {tags[:5]}")
        print(f"      图片数: {len(images)}")
        print(f"      提示词预览: {extracted_prompt[:100]}...")
        
        # 提取作者
        try:
            author = extract_username(tweet_url)
        except:
            author = None

        # 写入数据库
        prompt_record = db.save_prompt(
            title=title,
            prompt=extracted_prompt,
            category=category,
            tags=tags[:5],
            images=images[:5],
            source_link=tweet_url,
            author=author,
            import_source="gmail-grok"
        )
        
        if prompt_record:
            print(f"   ✅ 已保存: {title}")
            return "saved"
        else:
            print(f"   ❌ 保存失败")
            return "failed"

    except Exception as e:
        print(f"   ❌ 处理失败: {e}")
        return "failed"


def process_single_url(tweet_url: str):
    """直接处理单个 Twitter URL (用于手动触发)"""
    print("=" * 60)
    print("🐦 直接处理 Twitter URL")
    print("=" * 60)
    print(f"URL: {tweet_url}")
    print("=" * 60)
    
    # 连接数据库
    if not DATABASE_URL:
        print("❌ 缺少 DATABASE_URL 环境变量")
        sys.exit(1)
    
    db = Database(DATABASE_URL)
    
    try:
        db.connect()
        print("✅ 数据库连接成功\n")

        result = process_twitter_url(db, tweet_url)

        print("\n" + "=" * 60)
        if result == "saved":
            print("✅ 处理完成")
        elif result == "exists":
            print("⏭️ 已存在，跳过")
        elif result == "advertisement":
            print("🚫 广告内容，已跳过")
        elif result == "no_prompt":
            print("⚠️ 未找到提示词")
        elif result == "prompt_in_reply":
            print("⚠️ Prompt 在评论中")
        else:
            print("❌ 处理失败")
        print("=" * 60)
        
    finally:
        db.close()


def run_full_pipeline():
    """运行完整流水线: 邮件 → Twitter → 数据库"""
    print("=" * 60)
    print("🚀 Muse Worker - 完整流水线")
    print("=" * 60)
    print(f"Gmail: {GMAIL_EMAIL}")
    print(f"发件人过滤: {GMAIL_SENDER_FILTER}")
    print(f"AI 模型: {AI_MODEL}")
    print("=" * 60)
    
    # 检查配置
    if not DATABASE_URL:
        print("❌ 缺少 DATABASE_URL 环境变量")
        sys.exit(1)
    
    if not GMAIL_EMAIL or not GMAIL_PASSWORD:
        print("❌ 缺少 GMAIL_EMAIL 或 GMAIL_PASSWORD 环境变量")
        sys.exit(1)
    
    # 连接数据库
    print("\n📡 连接数据库...")
    db = Database(DATABASE_URL)
    
    try:
        db.connect()
        print("✅ 数据库连接成功")
    except Exception as e:
        print(f"❌ 数据库连接失败: {e}")
        sys.exit(1)
    
    # 连接 Gmail
    print("\n📧 连接 Gmail...")
    mail = connect_gmail()
    if not mail:
        sys.exit(1)
    
    # 统计
    stats = {
        "emails_processed": 0,
        "emails_skipped": 0,
        "twitter_links": 0,
        "twitter_success": 0,
        "twitter_failed": 0,
        "twitter_exists": 0,
        "twitter_ads": 0,
        "prompts_saved": 0,
    }
    
    # 失败的 URL 记录
    failed_urls = []
    success_urls = []
    
    try:
        # 获取邮件
        print("\n📬 获取邮件...")
        emails = fetch_emails(mail)
        
        if not emails:
            print("没有找到邮件")
            return
        
        # 处理每封邮件
        print(f"\n🔄 处理 {len(emails)} 封邮件...")
        print("=" * 70)
        
        for i, email_data in enumerate(emails, 1):
            message_id = email_data.get("message_id", "")
            subject = email_data.get("subject", "")[:50]
            sender = email_data.get("from", "")
            
            print()
            print(f"📧 邮件 [{i}/{len(emails)}]: {subject}")
            print(f"   发件人: {sender}")
            
            # 检查是否已处理
            if db.email_processed(message_id):
                print(f"   ⏭️ 状态: 已处理，跳过")
                stats["emails_skipped"] += 1
                continue
            
            # 提取 Twitter 链接
            twitter_links = extract_twitter_links(email_data["body"])
            
            if not twitter_links:
                print(f"   ⚠️ 状态: 没有找到 Twitter 链接，跳过")
                stats["emails_skipped"] += 1
                continue
            
            print(f"   🔗 找到 {len(twitter_links)} 个 Twitter 链接:")
            for j, url in enumerate(twitter_links, 1):
                print(f"      [{j}] {url}")
            
            stats["twitter_links"] += len(twitter_links)
            
            # 处理每个链接
            print()
            for j, url in enumerate(twitter_links, 1):
                print(f"   🐦 处理链接 [{j}/{len(twitter_links)}]: {url}")

                try:
                    result = process_twitter_url(db, url)
                    if result == "saved":
                        stats["prompts_saved"] += 1
                        stats["twitter_success"] += 1
                        success_urls.append(url)
                        print(f"      ✅ 结果: 成功保存")
                    elif result == "advertisement":
                        stats["twitter_ads"] += 1
                        print(f"      🚫 结果: 广告内容，跳过")
                    elif result == "exists":
                        stats["twitter_exists"] += 1
                        print(f"      ⏭️ 结果: 已存在，跳过")
                    elif result in ["no_prompt", "prompt_in_reply"]:
                        stats["twitter_exists"] += 1
                        print(f"      ⏭️ 结果: 跳过 (无提示词)")
                    else:
                        stats["twitter_failed"] += 1
                        failed_urls.append({"url": url, "error": "处理失败"})
                        print(f"      ❌ 结果: 处理失败")
                except Exception as e:
                    stats["twitter_failed"] += 1
                    failed_urls.append({"url": url, "error": str(e)})
                    print(f"      ❌ 结果: 失败 - {e}")
            
            # 保存邮件记录
            db.save_email(
                message_id=message_id,
                subject=email_data["subject"],
                sender=email_data["from"],
                received_at=email_data["date"],
                body=email_data["body"],
                twitter_links=twitter_links
            )
            
            stats["emails_processed"] += 1
        
        # 输出统计
        print()
        print("=" * 70)
        print("📊 处理完成 - 统计汇总")
        print("=" * 70)
        print()
        print("📧 邮件处理:")
        print(f"   已处理: {stats['emails_processed']}")
        print(f"   已跳过: {stats['emails_skipped']}")
        print()
        print("🐦 Twitter 链接:")
        print(f"   总计: {stats['twitter_links']}")
        print(f"   ✅ 成功: {stats['twitter_success']}")
        print(f"   ⏭️ 跳过: {stats['twitter_exists']}")
        print(f"   🚫 广告: {stats['twitter_ads']}")
        print(f"   ❌ 失败: {stats['twitter_failed']}")
        print()
        print("💾 数据库:")
        print(f"   新增提示词: {stats['prompts_saved']}")
        
        # 如果有失败的 URL，打印详情
        if failed_urls:
            print()
            print("=" * 70)
            print("❌ 失败的 Twitter 链接详情:")
            print("=" * 70)
            for item in failed_urls:
                print(f"   URL: {item['url']}")
                print(f"   错误: {item['error']}")
                print()
        
        # 如果有成功的 URL，打印列表
        if success_urls:
            print()
            print("=" * 70)
            print("✅ 成功处理的 Twitter 链接:")
            print("=" * 70)
            for url in success_urls:
                print(f"   {url}")
        
        print()
        print("=" * 70)
        
    finally:
        try:
            mail.logout()
        except:
            pass
        db.close()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Muse Worker - 邮件监听 + Twitter 抓取 + 数据库写入",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 运行完整流水线 (Gmail → Twitter → 数据库)
  python main.py
  
  # 直接处理单个 Twitter URL
  python main.py --url "https://x.com/user/status/123456"
        """
    )
    
    parser.add_argument("--url", "-u", type=str, help="直接处理单个 Twitter URL")
    
    args = parser.parse_args()
    
    if args.url:
        process_single_url(args.url)
    else:
        run_full_pipeline()


if __name__ == "__main__":
    main()

