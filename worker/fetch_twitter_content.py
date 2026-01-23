#!/usr/bin/env python3
"""
Twitter/X Content Fetcher
获取 Twitter/X 推文的正文内容和图片

使用方法:
    python fetch_twitter_content.py <tweet_url>

示例:
    python fetch_twitter_content.py https://x.com/oggii_0/status/2001232399368380637
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

# 加载环境变量
try:
    from dotenv import load_dotenv
    root_dir = Path(__file__).parent.parent
    env_local = root_dir / ".env.local"
    env_file = root_dir / ".env"

    if env_local.exists():
        load_dotenv(env_local)
    elif env_file.exists():
        load_dotenv(env_file)
except ImportError:
    pass

# 可选依赖
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# 导入公用模块 (统一的 AI 提取和分类)
from prompt_utils import (
    extract_prompt,
    extract_prompt_regex,
    extract_prompt_simple,
    classify_prompt,
    detect_prompt_in_reply,
    detect_prompt_in_alt,
    call_ai,
    DEFAULT_MODEL,
)

# 向后兼容别名 (供其他模块导入使用)
extract_prompt_from_text = extract_prompt_regex  # 正则提取
classify_prompt_with_ai = classify_prompt        # AI分类


def extract_prompt_with_ai(text: str, model: str = DEFAULT_MODEL) -> str:
    """
    向后兼容的 AI 提取函数

    使用 prompt_utils.extract_prompt 的统一流程

    Returns:
        提取的 prompt 字符串，或:
        - "Prompt in ALT": prompt 在图片 ALT 文本中
        - "Prompt in reply": prompt 在评论中
        - "No prompt found": 未找到 prompt
        - "Advertisement": 内容是广告/推广
    """
    result = extract_prompt(text, model=model, use_ai=True)

    if result["prompt"] == "Advertisement":
        return "Advertisement"

    if result["location"] == "alt":
        return "Prompt in ALT"

    if result["location"] == "reply":
        return "Prompt in reply"

    if result["prompt"]:
        return result["prompt"]

    return "No prompt found"

# Twitter Cookies 配置 (用于获取评论)
COOKIES_FILE = Path(__file__).parent / "x_cookies.json"
X_COOKIE = os.environ.get("X_COOKIE", "")  # JSON 字符串: '{"auth_token": "xxx", "ct0": "xxx"}'


def _load_twitter_cookies() -> dict:
    """加载 Twitter cookies"""
    # 优先使用环境变量
    if X_COOKIE:
        try:
            return json.loads(X_COOKIE)
        except:
            pass

    # 从文件加载
    if COOKIES_FILE.exists():
        try:
            with open(COOKIES_FILE) as f:
                return json.load(f)
        except:
            pass

    return {}


def fetch_author_replies(tweet_id: str, author_username: str) -> list:
    """
    使用独立子进程获取作者对自己帖子的回复
    通过子进程调用避免连接池问题

    Args:
        tweet_id: 推文 ID
        author_username: 原始作者用户名

    Returns:
        作者回复列表，每个元素包含 {"text": "...", "is_author": True}
    """
    # 检查 cookies 是否存在
    cookies = _load_twitter_cookies()
    if not cookies:
        print("      ⚠️ 未配置 Twitter cookies，无法获取评论")
        return []

    auth_token = cookies.get("auth_token", "")
    ct0 = cookies.get("ct0", "")

    if not auth_token or not ct0:
        print("      ⚠️ Twitter cookies 缺少 auth_token 或 ct0")
        return []

    # 使用子进程调用独立脚本，避免连接池问题
    script_path = Path(__file__).parent / "fetch_replies.py"

    try:
        result = subprocess.run(
            [sys.executable, str(script_path), tweet_id, author_username],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0 and result.stdout.strip():
            replies = json.loads(result.stdout.strip())
            return replies
        else:
            if result.stderr:
                print(f"      ⚠️ 子进程错误: {result.stderr[:200]}")
            return []

    except subprocess.TimeoutExpired:
        print("      ⚠️ 获取评论超时")
        return []
    except json.JSONDecodeError as e:
        print(f"      ⚠️ 解析回复失败: {e}")
        return []
    except Exception as e:
        print(f"      ⚠️ 获取评论失败: {e}")
        return []




def extract_tweet_id(url: str) -> str:
    """从 URL 中提取推文 ID"""
    # 支持 twitter.com 和 x.com
    pattern = r'(?:twitter\.com|x\.com)/\w+/status/(\d+)'
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    raise ValueError(f"无法从 URL 中提取推文 ID: {url}")


def extract_username(url: str) -> str:
    """从 URL 中提取用户名"""
    pattern = r'(?:twitter\.com|x\.com)/(\w+)/status/\d+'
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    raise ValueError(f"无法从 URL 中提取用户名: {url}")


def fetch_with_syndication_api(tweet_id: str) -> dict:
    """
    使用 Twitter Syndication API 获取推文内容
    这是公开 API，不需要认证
    """
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=0"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    
    response = requests.get(url, headers=headers, timeout=30)
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Syndication API 请求失败: {response.status_code}")


def fetch_with_fxtwitter(tweet_id: str, username: str) -> dict:
    """
    使用 FxTwitter/FixupX API 获取推文内容
    这是第三方服务，提供更好的嵌入体验
    """
    url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; TwitterBot/1.0)',
        'Accept': 'application/json',
    }
    
    response = requests.get(url, headers=headers, timeout=30)
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"FxTwitter API 请求失败: {response.status_code}")


def fetch_with_vxtwitter(tweet_id: str, username: str) -> dict:
    """
    使用 VxTwitter API 获取推文内容
    """
    url = f"https://api.vxtwitter.com/{username}/status/{tweet_id}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; TwitterBot/1.0)',
        'Accept': 'application/json',
    }
    
    response = requests.get(url, headers=headers, timeout=30)
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"VxTwitter API 请求失败: {response.status_code}")


def fetch_with_playwright(url: str) -> dict:
    """
    使用 Playwright 浏览器自动化获取推文内容
    需要安装: pip install playwright && playwright install chromium
    """
    if not HAS_PLAYWRIGHT:
        raise ImportError("Playwright 未安装。请运行: pip install playwright && playwright install chromium")
    
    result = {"text": None, "images": []}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        )
        page = context.new_page()
        
        try:
            page.goto(url, wait_until='networkidle', timeout=30000)
            page.wait_for_timeout(3000)  # 等待页面完全加载
            
            # 获取推文文本
            tweet_text_selectors = [
                'article[data-testid="tweet"] div[data-testid="tweetText"]',
                'article div[lang]',
                '[data-testid="tweetText"]',
            ]
            
            for selector in tweet_text_selectors:
                try:
                    text_element = page.query_selector(selector)
                    if text_element:
                        result["text"] = text_element.inner_text()
                        break
                except:
                    continue
            
            # 获取图片
            image_selectors = [
                'article[data-testid="tweet"] img[src*="pbs.twimg.com/media"]',
                'article img[src*="twimg.com/media"]',
                '[data-testid="tweetPhoto"] img',
            ]
            
            for selector in image_selectors:
                try:
                    images = page.query_selector_all(selector)
                    for img in images:
                        src = img.get_attribute('src')
                        if src and 'pbs.twimg.com' in src:
                            # 获取高清版本
                            high_res = re.sub(r'\?.*$', '?format=jpg&name=large', src)
                            if high_res not in result["images"]:
                                result["images"].append(high_res)
                except:
                    continue
            
        finally:
            browser.close()
    
    return result


def download_image(url: str, save_path: str) -> str:
    """下载图片到本地"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    }
    
    response = requests.get(url, headers=headers, timeout=30, stream=True)
    
    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return save_path
    else:
        raise Exception(f"图片下载失败: {response.status_code}")


def parse_syndication_result(data: dict) -> dict:
    """解析 Syndication API 的返回结果"""
    result = {
        "text": "",
        "images": [],
        "user": {},
        "created_at": "",
        "stats": {},
    }
    
    if "text" in data:
        result["text"] = data["text"]
    
    if "user" in data:
        result["user"] = {
            "name": data["user"].get("name", ""),
            "screen_name": data["user"].get("screen_name", ""),
        }
    
    if "created_at" in data:
        result["created_at"] = data["created_at"]
    
    # 提取互动统计
    result["stats"] = {
        "replies": data.get("reply_count", 0),
        "retweets": data.get("retweet_count", 0),
        "likes": data.get("favorite_count", 0),
        "bookmarks": data.get("bookmark_count", 0),
        "views": data.get("views_count", 0),
    }
    
    # 提取媒体
    if "mediaDetails" in data:
        for media in data["mediaDetails"]:
            if media.get("type") == "photo":
                result["images"].append(media.get("media_url_https", ""))
    
    # 也检查 photos 字段
    if "photos" in data:
        for photo in data["photos"]:
            url = photo.get("url", "")
            if url and url not in result["images"]:
                result["images"].append(url)
    
    return result


def parse_fxtwitter_result(data: dict) -> dict:
    """解析 FxTwitter API 的返回结果"""
    result = {
        "text": "",
        "images": [],
        "image_alt_texts": [],  # 图片 ALT 文本列表
        "user": {},
        "created_at": "",
        "stats": {},
    }

    tweet = data.get("tweet", {})

    if "text" in tweet:
        result["text"] = tweet["text"]

    if "author" in tweet:
        result["user"] = {
            "name": tweet["author"].get("name", ""),
            "screen_name": tweet["author"].get("screen_name", ""),
        }

    if "created_at" in tweet:
        result["created_at"] = tweet["created_at"]

    # 提取互动统计
    result["stats"] = {
        "replies": tweet.get("replies", 0),
        "retweets": tweet.get("retweets", 0),
        "likes": tweet.get("likes", 0),
        "bookmarks": tweet.get("bookmarks", 0),
        "views": tweet.get("views", 0),
    }

    # 提取媒体 (包括 ALT 文本)
    if "media" in tweet and "photos" in tweet["media"]:
        for photo in tweet["media"]["photos"]:
            result["images"].append(photo.get("url", ""))
            alt_text = photo.get("altText", "")
            if alt_text:
                result["image_alt_texts"].append(alt_text)

    return result


def parse_vxtwitter_result(data: dict) -> dict:
    """解析 VxTwitter API 的返回结果"""
    result = {
        "text": "",
        "images": [],
        "image_alt_texts": [],  # 图片 ALT 文本列表
        "user": {},
        "created_at": "",
        "stats": {},
    }

    if "text" in data:
        result["text"] = data["text"]

    result["user"] = {
        "name": data.get("user_name", ""),
        "screen_name": data.get("user_screen_name", ""),
    }

    if "date" in data:
        result["created_at"] = data["date"]

    # 提取互动统计
    result["stats"] = {
        "replies": data.get("replies", 0),
        "retweets": data.get("retweets", 0),
        "likes": data.get("likes", 0),
        "bookmarks": data.get("bookmarks", 0),
        "views": data.get("views", 0),
    }

    # 提取媒体 (包括 ALT 文本)
    if "media_extended" in data:
        for media in data["media_extended"]:
            if media.get("type") == "image":
                result["images"].append(media.get("url", ""))
                alt_text = media.get("altText", "")
                if alt_text:
                    result["image_alt_texts"].append(alt_text)

    return result


def fetch_tweet(url: str, download_images: bool = True, output_dir: str = ".",
                extract_prompt: bool = False, ai_model: str = DEFAULT_MODEL,
                detect_ads: bool = True) -> dict:
    """
    获取推文内容的主函数
    会依次尝试不同的方法直到成功

    Args:
        url: 推文 URL
        download_images: 是否下载图片
        output_dir: 输出目录
        extract_prompt: 是否使用 AI 提取提示词
        ai_model: AI 模型名称 (openai, deepseek 等)
        detect_ads: 是否检测广告内容 (默认 True)

    Returns:
        dict: 包含以下字段:
            - text: 推文文本
            - images: 图片 URL 列表
            - is_advertisement: 是否为广告 (仅当 detect_ads=True)
            - prompt_location: "post" | "reply" | "advertisement"
            - extracted_prompt: 提取的 prompt (仅当 extract_prompt=True)
            - ...
    """
    start_time = datetime.now()
    
    # 解析 URL
    try:
        tweet_id = extract_tweet_id(url)
        username = extract_username(url)
    except Exception as e:
        print(f"❌ [FAILED] 无效的 Twitter URL: {url}")
        print(f"   错误: {e}")
        raise Exception(f"无效的 Twitter URL: {url}")
    
    print()
    print("=" * 70)
    print(f"🐦 开始处理推文")
    print(f"   URL: {url}")
    print(f"   推文 ID: {tweet_id}")
    print(f"   用户名: @{username}")
    print(f"   时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    result = None
    fetch_method = None
    fetch_errors = []
    
    # 方法 1: 尝试 FxTwitter API
    print("   [1/4] 尝试 FxTwitter API...")
    try:
        data = fetch_with_fxtwitter(tweet_id, username)
        result = parse_fxtwitter_result(data)
        if result and result.get("text"):
            fetch_method = "FxTwitter"
            print("   ✓ FxTwitter API 成功")
        else:
            raise Exception("返回数据为空")
    except Exception as e:
        fetch_errors.append(f"FxTwitter: {e}")
        print(f"   ✗ FxTwitter API 失败: {e}")
    
    # 方法 2: 尝试 VxTwitter API
    if not result or not result.get("text"):
        print("   [2/4] 尝试 VxTwitter API...")
        try:
            data = fetch_with_vxtwitter(tweet_id, username)
            result = parse_vxtwitter_result(data)
            if result and result.get("text"):
                fetch_method = "VxTwitter"
                print("   ✓ VxTwitter API 成功")
            else:
                raise Exception("返回数据为空")
        except Exception as e:
            fetch_errors.append(f"VxTwitter: {e}")
            print(f"   ✗ VxTwitter API 失败: {e}")
    
    # 方法 3: 尝试 Twitter Syndication API
    if not result or not result.get("text"):
        print("   [3/4] 尝试 Syndication API...")
        try:
            data = fetch_with_syndication_api(tweet_id)
            result = parse_syndication_result(data)
            if result and result.get("text"):
                fetch_method = "Syndication"
                print("   ✓ Syndication API 成功")
            else:
                raise Exception("返回数据为空")
        except Exception as e:
            fetch_errors.append(f"Syndication: {e}")
            print(f"   ✗ Syndication API 失败: {e}")
    
    # 方法 4: 尝试 Playwright (需要安装)
    if not result or not result.get("text"):
        if HAS_PLAYWRIGHT:
            print("   [4/4] 尝试 Playwright 浏览器...")
            try:
                result = fetch_with_playwright(url)
                if result and result.get("text"):
                    fetch_method = "Playwright"
                    print("   ✓ Playwright 成功")
                else:
                    raise Exception("返回数据为空")
            except Exception as e:
                fetch_errors.append(f"Playwright: {e}")
                print(f"   ✗ Playwright 失败: {e}")
        else:
            print("   [4/4] 跳过 Playwright (未安装)")
    
    # 检查是否成功获取内容
    if not result or not result.get("text"):
        elapsed = (datetime.now() - start_time).total_seconds()
        print()
        print(f"❌ [FAILED] 推文获取失败: {url}")
        print(f"   用户: @{username} | 推文ID: {tweet_id}")
        print(f"   耗时: {elapsed:.1f}s")
        print(f"   尝试的方法及错误:")
        for err in fetch_errors:
            print(f"      - {err}")
        print("=" * 70)
        raise Exception(f"所有获取方法都失败了: {url}")
    
    print()
    print(f"   ✓ 内容获取成功 (via {fetch_method})")
    text_preview = result.get("text", "")[:100].replace("\n", " ")
    print(f"   📝 内容预览: {text_preview}...")
    
    # 下载图片
    if download_images and result.get("images"):
        print()
        print(f"   🖼️  发现 {len(result['images'])} 张图片")
        
        os.makedirs(output_dir, exist_ok=True)
        downloaded_images = []
        
        for i, img_url in enumerate(result["images"]):
            # 获取高清版本
            if "?" in img_url:
                high_res_url = re.sub(r'name=\w+', 'name=large', img_url)
            else:
                high_res_url = img_url + "?format=jpg&name=large"
            
            filename = f"tweet_{tweet_id}_image_{i+1}.jpg"
            filepath = os.path.join(output_dir, filename)
            
            try:
                download_image(high_res_url, filepath)
                downloaded_images.append(filepath)
                print(f"      ✓ 图片 {i+1}: {filename}")
            except Exception as e:
                print(f"      ✗ 图片 {i+1} 下载失败: {e}")
        
        result["downloaded_images"] = downloaded_images

    # 初始化广告标记
    result["is_advertisement"] = False

    # 独立的广告检测（当 extract_prompt=False 但 detect_ads=True 时）
    if detect_ads and not extract_prompt and result.get("text"):
        print()
        print(f"   🔍 广告检测...")
        try:
            ad_check = extract_prompt_with_ai(result["text"], model=ai_model)
            if ad_check == "Advertisement":
                print(f"      🚫 检测到广告/推广内容")
                result["is_advertisement"] = True
                result["prompt_location"] = "advertisement"
            else:
                print(f"      ✓ 非广告内容")
        except Exception as e:
            print(f"      ⚠️ 广告检测失败: {e}")

    # 使用 AI 提取提示词
    if extract_prompt and result.get("text"):
        print()
        print(f"   🤖 AI 处理 (模型: {ai_model})")

        # 先检测 prompt 位置
        prompt_in_alt = detect_prompt_in_alt(result["text"])
        prompt_in_reply = detect_prompt_in_reply(result["text"])

        # 优先检查 ALT 文本
        if prompt_in_alt and result.get("image_alt_texts"):
            print(f"      ✓ 检测到 prompt 在 ALT 文本中")
            result["prompt_location"] = "alt"

            # 直接从 ALT 文本获取提示词 (通常第一个 ALT 就是)
            alt_prompt = result["image_alt_texts"][0]
            result["extracted_prompt"] = alt_prompt
            prompt_preview = alt_prompt[:80].replace("\n", " ")
            print(f"      ✓ 从 ALT 文本提取成功: {prompt_preview}...")

            # 对提取的提示词进行分类
            print(f"      [2/2] 分类提示词...")
            try:
                classification = classify_prompt_with_ai(alt_prompt, model=ai_model)
                result["classification"] = classification

                title = classification.get("title", "未知")
                category = classification.get("category", "未知")
                confidence = classification.get("confidence", "未知")
                print(f"      ✓ 分类成功: {title} | {category} | 置信度: {confidence}")
            except Exception as e:
                print(f"      ✗ 分类失败: {e}")
                result["classification"] = None

            # 跳过后续处理
            extracted_prompt = alt_prompt

        elif prompt_in_reply:
            print(f"      ⚠️ 检测到 prompt 可能在评论/回复中")
            result["prompt_location"] = "reply"
            extracted_prompt = None  # 将在下面处理
        else:
            result["prompt_location"] = "post"
            extracted_prompt = None  # 将在下面处理

        # 如果不是从 ALT 提取的，使用原有逻辑
        if result.get("prompt_location") != "alt":
            # 提取提示词
            print(f"      [1/2] 提取提示词...")
            try:
                extracted_prompt = extract_prompt_with_ai(result["text"], model=ai_model)
                result["extracted_prompt"] = extracted_prompt

                # 处理不同的提取结果
                if extracted_prompt == "Advertisement":
                    print(f"      🚫 检测到广告/推广内容，跳过")
                    result["is_advertisement"] = True
                    result["prompt_location"] = "advertisement"
                    result["classification"] = None
                elif extracted_prompt == "Prompt in ALT":
                    # AI 识别到提示词在 ALT 中，从 ALT 文本获取
                    print(f"      ✓ AI 检测到 prompt 在 ALT 文本中")
                    if result.get("image_alt_texts"):
                        alt_prompt = result["image_alt_texts"][0]
                        result["extracted_prompt"] = alt_prompt
                        result["prompt_location"] = "alt"
                        prompt_preview = alt_prompt[:80].replace("\n", " ")
                        print(f"      ✓ 从 ALT 文本提取成功: {prompt_preview}...")

                        # 对提取的提示词进行分类
                        print(f"      [2/2] 分类提示词...")
                        try:
                            classification = classify_prompt_with_ai(alt_prompt, model=ai_model)
                            result["classification"] = classification

                            title = classification.get("title", "未知")
                            category = classification.get("category", "未知")
                            confidence = classification.get("confidence", "未知")
                            print(f"      ✓ 分类成功: {title} | {category} | 置信度: {confidence}")
                        except Exception as e:
                            print(f"      ✗ 分类失败: {e}")
                            result["classification"] = None
                    else:
                        print(f"      ⚠️ 未找到图片 ALT 文本")
                        result["classification"] = None
                elif extracted_prompt == "Prompt in reply":
                    print(f"      ⚠️ Prompt 在评论/回复中，尝试获取作者回复...")
                    result["prompt_location"] = "reply"

                    # 尝试获取作者的回复
                    # 优先使用 API 返回的实际作者用户名（URL 中的用户名可能不准确）
                    actual_author = result.get("user", {}).get("screen_name", username)
                    if actual_author != username:
                        print(f"      ℹ️ 实际作者: @{actual_author} (URL 中: @{username})")
                    author_replies = fetch_author_replies(tweet_id, actual_author)
                    if author_replies:
                        print(f"      ✓ 获取到 {len(author_replies)} 条作者回复")

                        # 合并所有作者回复，从中提取 prompt
                        combined_reply_text = "\n\n".join([r["text"] for r in author_replies])
                        result["author_replies"] = author_replies

                        # 尝试从回复中提取 prompt
                        print(f"      [1.5/2] 从作者回复中提取提示词...")

                        # 首先尝试正则表达式提取 (更快更可靠)
                        reply_prompt = None
                        for reply in author_replies:
                            reply_prompt = extract_prompt_from_text(reply["text"])
                            if reply_prompt:
                                print(f"      ✓ 使用正则表达式提取成功")
                                break

                        # 如果正则没有提取到，尝试 AI 提取
                        if not reply_prompt:
                            print(f"      ℹ️ 正则未匹配，尝试 AI 提取...")
                            try:
                                # 直接调用 AI 提取，不再检测 "Prompt in reply"
                                reply_prompt = call_ai([
                                    {
                                        "role": "system",
                                        "content": "You are a helpful assistant that extracts AI image generation prompts from text. Extract only the prompt itself, without any additional explanation or formatting. If no prompt is found, return 'No prompt found'."
                                    },
                                    {
                                        "role": "user",
                                        "content": f"Extract the AI image generation prompt from this text and return only the prompt itself:\n\n{combined_reply_text}"
                                    }
                                ], ai_model)

                                if reply_prompt == "No prompt found":
                                    reply_prompt = None
                            except Exception as e:
                                print(f"      ⚠️ AI 提取失败: {e}")
                                reply_prompt = None

                        if reply_prompt:
                            extracted_prompt = reply_prompt
                            result["extracted_prompt"] = extracted_prompt
                            result["prompt_location"] = "reply"  # 标记是从回复中提取的
                            prompt_preview = extracted_prompt[:80].replace("\n", " ")
                            print(f"      ✓ 从回复中提取成功: {prompt_preview}...")

                            # 对提取的提示词进行分类
                            print(f"      [2/2] 分类提示词...")
                            try:
                                classification = classify_prompt_with_ai(extracted_prompt, model=ai_model)
                                result["classification"] = classification

                                title = classification.get("title", "未知")
                                category = classification.get("category", "未知")
                                confidence = classification.get("confidence", "未知")
                                print(f"      ✓ 分类成功: {title} | {category} | 置信度: {confidence}")
                            except Exception as e:
                                print(f"      ✗ 分类失败: {e}")
                                result["classification"] = None
                        else:
                            print(f"      ⚠️ 作者回复中也未找到提示词")
                            result["classification"] = None
                    else:
                        print(f"      ⚠️ 未获取到作者回复 (可能需要配置 cookies)")
                        result["classification"] = None
                elif extracted_prompt and extracted_prompt != "No prompt found":
                    prompt_preview = extracted_prompt[:80].replace("\n", " ")
                    print(f"      ✓ 提取成功: {prompt_preview}...")
                    result["prompt_location"] = "post"

                    # 对提取的提示词进行分类
                    print(f"      [2/2] 分类提示词...")
                    try:
                        classification = classify_prompt_with_ai(extracted_prompt, model=ai_model)
                        result["classification"] = classification

                        title = classification.get("title", "未知")
                        category = classification.get("category", "未知")
                        confidence = classification.get("confidence", "未知")
                        print(f"      ✓ 分类成功: {title} | {category} | 置信度: {confidence}")
                    except Exception as e:
                        print(f"      ✗ 分类失败: {e}")
                        result["classification"] = None
                else:
                    print(f"      ⚠️ 未找到提示词")
                    result["classification"] = None
            except Exception as e:
                print(f"      ✗ 提取失败: {e}")
                result["extracted_prompt"] = None
                result["classification"] = None

    # 完成
    elapsed = (datetime.now() - start_time).total_seconds()
    print()
    prompt_location = result.get("prompt_location", "unknown")
    extracted_prompt = result.get("extracted_prompt", "")

    # 判断是否成功提取了 prompt (即使是从评论中提取的)
    has_valid_prompt = extracted_prompt and extracted_prompt not in ["No prompt found", "Prompt in reply", "Advertisement"]

    if prompt_location == "advertisement":
        print(f"🚫 [ADVERTISEMENT] 推文为广告内容，已跳过: {url}")
        print(f"   用户: @{username} | 推文ID: {tweet_id}")
        print(f"   获取方式: {fetch_method}")
        print(f"   耗时: {elapsed:.1f}s")
        print("=" * 70)
        return result

    if has_valid_prompt:
        if prompt_location == "alt":
            print(f"✅ [SUCCESS_FROM_ALT] 推文处理完成: {url}")
            print(f"   用户: @{username} | 推文ID: {tweet_id}")
            print(f"   获取方式: {fetch_method}")
            print(f"   图片数量: {len(result.get('images', []))}")
            print(f"   提示词: 已从图片 ALT 文本提取")
        elif prompt_location == "reply":
            print(f"✅ [SUCCESS_FROM_REPLY] 推文处理完成: {url}")
            print(f"   用户: @{username} | 推文ID: {tweet_id}")
            print(f"   获取方式: {fetch_method}")
            print(f"   图片数量: {len(result.get('images', []))}")
            print(f"   提示词: 已从评论中提取")
        else:
            print(f"✅ [SUCCESS] 推文处理完成: {url}")
            print(f"   用户: @{username} | 推文ID: {tweet_id}")
            print(f"   获取方式: {fetch_method}")
            print(f"   图片数量: {len(result.get('images', []))}")
            print(f"   提示词: 已提取")
        if result.get("classification"):
            print(f"   分类: {result['classification'].get('category', '未知')}")
    elif prompt_location == "reply":
        print(f"⚠️ [PROMPT_IN_REPLY] 推文处理完成: {url}")
        print(f"   用户: @{username} | 推文ID: {tweet_id}")
        print(f"   获取方式: {fetch_method}")
        print(f"   图片数量: {len(result.get('images', []))}")
        print(f"   提示词位置: 评论/回复中 (未能提取)")
    else:
        print(f"⚠️ [NO_PROMPT] 推文处理完成: {url}")
        print(f"   用户: @{username} | 推文ID: {tweet_id}")
        print(f"   获取方式: {fetch_method}")
        print(f"   图片数量: {len(result.get('images', []))}")
        print(f"   提示词: 未找到")
    print(f"   耗时: {elapsed:.1f}s")
    print("=" * 70)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Twitter/X 内容获取工具 - 获取推文正文、图片和互动统计",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基本用法 (默认启用提示词提取和分类)
  python fetch_twitter_content.py https://x.com/oggii_0/status/2001232399368380637
  
  # 使用 deepseek 模型
  python fetch_twitter_content.py https://x.com/oggii_0/status/2001232399368380637 --model deepseek
  
  # 禁用提示词提取
  python fetch_twitter_content.py https://x.com/oggii_0/status/2001232399368380637 --no-extract
  
  # 指定输出目录
  python fetch_twitter_content.py https://x.com/oggii_0/status/2001232399368380637 -o ./output
        """
    )
    
    parser.add_argument("url", help="推文 URL (支持 x.com 和 twitter.com)")
    parser.add_argument("-o", "--output", default=".", help="输出目录 (默认: 当前目录)")
    parser.add_argument("--no-extract", action="store_true", 
                        help="禁用 AI 提取提示词和分类 (默认启用)")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL,
                        help=f"AI 模型 (默认: {DEFAULT_MODEL}, 可选: deepseek, openai 等)")
    parser.add_argument("--no-download", action="store_true", help="不下载图片")
    
    args = parser.parse_args()
    
    url = args.url
    output_dir = args.output
    
    extract_prompt = not args.no_extract  # 默认启用提取提示词
    
    print("=" * 50)
    print("Twitter/X 内容获取工具")
    print("=" * 50)
    print(f"URL: {url}")
    print(f"输出目录: {output_dir}")
    if extract_prompt:
        print(f"AI 模型: {args.model}")
    print("=" * 50)
    
    try:
        result = fetch_tweet(
            url, 
            download_images=not args.no_download, 
            output_dir=output_dir,
            extract_prompt=extract_prompt,
            ai_model=args.model
        )
        
        print("\n" + "=" * 50)
        print("获取结果")
        print("=" * 50)
        
        if result.get("user"):
            user = result["user"]
            print(f"用户: {user.get('name', '')} (@{user.get('screen_name', '')})")
        
        if result.get("created_at"):
            print(f"时间: {result['created_at']}")
        
        # 显示互动统计
        if result.get("stats"):
            stats = result["stats"]
            print(f"\n互动统计:")
            print(f"  💬 评论数 (Replies): {stats.get('replies', 0)}")
            print(f"  🔁 转发数 (Retweets): {stats.get('retweets', 0)}")
            print(f"  ❤️  点赞数 (Likes): {stats.get('likes', 0)}")
            print(f"  🔖 书签数 (Bookmarks): {stats.get('bookmarks', 0)}")
            print(f"  👁️  浏览量 (Views): {stats.get('views', 0)}")
        
        print(f"\n正文内容:")
        print("-" * 50)
        print(result.get("text", "无法获取"))
        print("-" * 50)
        
        # 显示提取的提示词
        if result.get("extracted_prompt"):
            print(f"\n🎨 提取的提示词:")
            print("-" * 50)
            print(result["extracted_prompt"])
            print("-" * 50)
        
        # 显示分类结果
        if result.get("classification"):
            cls = result["classification"]
            print(f"\n📂 提示词分类:")
            print("-" * 50)
            if cls.get("title"):
                print(f"  📌 标题: {cls.get('title')}")
            print(f"  主分类: {cls.get('category', '未知')}")
            if cls.get("sub_categories"):
                print(f"  次分类: {', '.join(cls['sub_categories'])}")
            if cls.get("style"):
                print(f"  风格: {cls.get('style', '未知')}")
            print(f"  置信度: {cls.get('confidence', '未知')}")
            if cls.get("reason"):
                print(f"  原因: {cls.get('reason', '')}")
            print("-" * 50)
        
        if result.get("images"):
            print(f"\n图片 URL ({len(result['images'])} 张):")
            for i, img in enumerate(result["images"], 1):
                print(f"  {i}. {img}")
        
        if result.get("downloaded_images"):
            print(f"\n已下载图片:")
            for img_path in result["downloaded_images"]:
                print(f"  - {img_path}")
        
        # 保存结果到文件
        output_file = os.path.join(output_dir, f"tweet_{extract_tweet_id(url)}_content.txt")
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n")
            if result.get("user"):
                f.write(f"User: {result['user'].get('name', '')} (@{result['user'].get('screen_name', '')})\n")
            if result.get("created_at"):
                f.write(f"Time: {result['created_at']}\n")
            
            # 保存互动统计
            if result.get("stats"):
                stats = result["stats"]
                f.write(f"\nStats:\n")
                f.write(f"  Replies: {stats.get('replies', 0)}\n")
                f.write(f"  Retweets: {stats.get('retweets', 0)}\n")
                f.write(f"  Likes: {stats.get('likes', 0)}\n")
                f.write(f"  Bookmarks: {stats.get('bookmarks', 0)}\n")
                f.write(f"  Views: {stats.get('views', 0)}\n")
            
            f.write(f"\nText:\n{result.get('text', '')}\n")
            
            # 保存提取的提示词
            if result.get("extracted_prompt"):
                f.write(f"\nExtracted Prompt:\n{result['extracted_prompt']}\n")
            
            # 保存分类结果
            if result.get("classification"):
                cls = result["classification"]
                f.write(f"\nClassification:\n")
                if cls.get("title"):
                    f.write(f"  Title: {cls.get('title')}\n")
                f.write(f"  Category: {cls.get('category', 'Unknown')}\n")
                if cls.get("sub_categories"):
                    f.write(f"  Sub-categories: {', '.join(cls['sub_categories'])}\n")
                if cls.get("style"):
                    f.write(f"  Style: {cls.get('style', 'Unknown')}\n")
                f.write(f"  Confidence: {cls.get('confidence', 'Unknown')}\n")
                if cls.get("reason"):
                    f.write(f"  Reason: {cls.get('reason', '')}\n")
            
            if result.get("images"):
                f.write(f"\nImages:\n")
                for img in result["images"]:
                    f.write(f"  {img}\n")
        print(f"\n✓ 内容已保存到: {output_file}")
        
    except Exception as e:
        print(f"\n错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

