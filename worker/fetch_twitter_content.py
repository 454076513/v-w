#!/usr/bin/env python3
"""
Twitter/X Content Fetcher
获取 Twitter/X 推文的正文内容和图片

使用方法:
    python fetch_twitter_content.py <tweet_url>
    
示例:
    python fetch_twitter_content.py https://x.com/oggii_0/status/2001232399368380637
"""

import re
import sys
import os
import requests
from urllib.parse import urlparse

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


# Pollinations API 配置
POLLINATIONS_API_URL = "https://text.pollinations.ai/"
DEFAULT_MODEL = "openai"  # 免费模型，也可以使用 "deepseek" (需要 seed tier)

# Gitee AI API 配置 (fallback)
GITEE_AI_API_URL = "https://ai.gitee.com/v1/chat/completions"
GITEE_AI_MODEL = "DeepSeek-V3"
GITEE_AI_API_KEY = os.environ.get("GITEE_AI_API_KEY", "")


def _call_gitee_ai(messages: list) -> str:
    """
    调用 Gitee AI API (fallback)，使用 stream 模式避免超时
    
    Args:
        messages: OpenAI 格式的消息列表
    
    Returns:
        AI 响应内容
    """
    import json
    
    if not GITEE_AI_API_KEY:
        raise Exception("GITEE_AI_API_KEY 环境变量未设置")
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {GITEE_AI_API_KEY}',
        'Accept': 'text/event-stream',
    }
    
    payload = {
        "model": GITEE_AI_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "stream": True,  # 启用流式输出
    }
    
    # 使用 stream=True 避免读取超时
    response = requests.post(
        GITEE_AI_API_URL, 
        json=payload, 
        headers=headers, 
        timeout=(10, 300),  # (连接超时, 读取超时)
        stream=True
    )
    
    if response.status_code != 200:
        raise Exception(f"Gitee AI 请求失败: {response.status_code} - {response.text}")
    
    # 收集流式响应
    full_content = []
    
    for line in response.iter_lines():
        if not line:
            continue
        
        line = line.decode('utf-8')
        
        # SSE 格式: "data: {...}"
        if line.startswith('data: '):
            data_str = line[6:]  # 去掉 "data: " 前缀
            
            # 结束标记
            if data_str == '[DONE]':
                break
            
            try:
                data = json.loads(data_str)
                if "choices" in data and len(data["choices"]) > 0:
                    delta = data["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full_content.append(content)
            except json.JSONDecodeError:
                continue
    
    if not full_content:
        raise Exception("Gitee AI 返回空响应")
    
    return "".join(full_content)


def _call_pollinations_ai(messages: list, model: str = DEFAULT_MODEL) -> str:
    """
    调用 Pollinations AI API

    Args:
        messages: OpenAI 格式的消息列表
        model: 使用的模型

    Returns:
        AI 响应内容
    """
    # 确保 model 不为空
    if not model or not model.strip():
        model = DEFAULT_MODEL

    headers = {
        'Content-Type': 'application/json',
    }

    payload = {
        "model": model,
        "messages": messages,
    }
    
    response = requests.post(POLLINATIONS_API_URL, json=payload, headers=headers, timeout=60)
    
    if response.status_code == 200:
        # 响应可能是纯文本或 JSON
        import json as json_module
        
        try:
            data = response.json()
            if isinstance(data, dict):
                # OpenAI 格式: {"choices": [{"message": {"content": "..."}}]}
                if "choices" in data:
                    return data["choices"][0]["message"]["content"]
                # 简化格式: {"content": "..."}
                elif "content" in data:
                    return data["content"]
                elif "reasoning_content" in data:
                    return data["reasoning_content"]
                else:
                    # 如果返回的是直接的 JSON 对象（比如分类结果），转回 JSON 字符串
                    return json_module.dumps(data, ensure_ascii=False)
            elif isinstance(data, str):
                return data
            else:
                return json_module.dumps(data, ensure_ascii=False)
        except:
            # 纯文本响应
            return response.text.strip()
    else:
        raise Exception(f"Pollinations API 请求失败: {response.status_code} - {response.text}")


def _call_ai_with_fallback(messages: list, model: str = DEFAULT_MODEL) -> str:
    """
    调用 AI API，如果 Pollinations 失败则 fallback 到 Gitee AI
    
    Args:
        messages: OpenAI 格式的消息列表
        model: Pollinations 使用的模型
    
    Returns:
        AI 响应内容
    """
    # 首先尝试 Pollinations AI
    try:
        result = _call_pollinations_ai(messages, model)
        return result
    except Exception as pollinations_error:
        print(f"⚠️ Pollinations AI 失败: {pollinations_error}")
        
        # Fallback 到 Gitee AI
        if GITEE_AI_API_KEY:
            print(f"🔄 尝试 Gitee AI (DeepSeek-V3) 作为 fallback...")
            try:
                result = _call_gitee_ai(messages)
                print("✓ Gitee AI 调用成功")
                return result
            except Exception as gitee_error:
                print(f"✗ Gitee AI 也失败: {gitee_error}")
                raise Exception(f"所有 AI 服务都失败: Pollinations ({pollinations_error}), Gitee ({gitee_error})")
        else:
            print("⚠️ GITEE_AI_API_KEY 未设置，无法使用 fallback")
            raise pollinations_error


def extract_prompt_with_ai(text: str, model: str = DEFAULT_MODEL) -> str:
    """
    使用 AI API 从文本中提取提示词
    优先使用 Pollinations AI，失败后 fallback 到 Gitee AI (DeepSeek-V3)
    
    Args:
        text: 推文正文内容
        model: 使用的模型，默认 openai，可选 deepseek
    
    Returns:
        提取出的提示词
    """
    messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant that extracts AI image generation prompts from text. Extract only the prompt itself, without any additional explanation or formatting. If no prompt is found, return 'No prompt found'."
        },
        {
            "role": "user",
            "content": f"Extract the AI image generation prompt from this text and return only the prompt itself:\n\n{text}"
        }
    ]
    
    try:
        return _call_ai_with_fallback(messages, model)
    except requests.exceptions.Timeout:
        raise Exception("API 请求超时")
    except Exception as e:
        raise Exception(f"提取提示词失败: {e}")


# 预定义的分类列表
PROMPT_CATEGORIES = [
    "人像/肖像 (Portrait)",
    "风景/自然 (Landscape/Nature)",
    "动物 (Animals)",
    "建筑/城市 (Architecture/Urban)",
    "抽象艺术 (Abstract Art)",
    "科幻/未来 (Sci-Fi/Futuristic)",
    "奇幻/魔法 (Fantasy/Magic)",
    "动漫/卡通 (Anime/Cartoon)",
    "写实摄影 (Realistic Photography)",
    "插画/绘画 (Illustration/Painting)",
    "时尚/服装 (Fashion/Clothing)",
    "食物/美食 (Food)",
    "产品/商业 (Product/Commercial)",
    "恐怖/黑暗 (Horror/Dark)",
    "可爱/萌系 (Cute/Kawaii)",
    "复古/怀旧 (Vintage/Retro)",
    "极简主义 (Minimalist)",
    "超现实 (Surreal)",
    "其他 (Other)",
]


def classify_prompt_with_ai(prompt: str, model: str = DEFAULT_MODEL) -> dict:
    """
    使用 AI API 对提示词进行分类
    优先使用 Pollinations AI，失败后 fallback 到 Gitee AI (DeepSeek-V3)
    
    Args:
        prompt: 提示词内容
        model: 使用的模型
    
    Returns:
        包含分类结果的字典 {"category": "分类", "confidence": "高/中/低", "reason": "原因"}
    """
    categories_str = "\n".join([f"- {cat}" for cat in PROMPT_CATEGORIES])
    
    messages = [
        {
            "role": "system",
            "content": f"""You are an AI image prompt classifier. Analyze the given prompt and classify it into one of the following categories:

{categories_str}

Respond in JSON format with exactly these fields:
- "title": a concise, descriptive title for this prompt in English (3-8 words, like a short headline)
- "category": the main category (choose from the list above, use the English part only, e.g., "Portrait", "Landscape/Nature")
- "sub_categories": array of 1-3 secondary categories in English (e.g., ["Fashion/Clothing", "Realistic Photography"])
- "style": detected art style (e.g., "photorealistic", "anime", "oil painting", "3D render", etc.)
- "confidence": "high", "medium", or "low"
- "reason": brief explanation in English (1 sentence)

Example response:
{{"title": "Fashion Actress Bird's Eye View", "category": "Portrait", "sub_categories": ["Fashion/Clothing"], "style": "photorealistic", "confidence": "high", "reason": "The prompt describes a Japanese actress in a black coat from above"}}"""
        },
        {
            "role": "user",
            "content": f"Classify this AI image generation prompt:\n\n{prompt}"
        }
    ]
    
    try:
        response_text = _call_ai_with_fallback(messages, model)
        
        # 尝试解析 JSON
        import json
        
        result = None
        
        # 清理响应文本
        cleaned_text = response_text.strip()
        
        # 移除可能的 markdown 代码块标记
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:]
        elif cleaned_text.startswith("```"):
            cleaned_text = cleaned_text[3:]
        if cleaned_text.endswith("```"):
            cleaned_text = cleaned_text[:-3]
        cleaned_text = cleaned_text.strip()
        
        # 尝试直接解析
        try:
            result = json.loads(cleaned_text)
        except json.JSONDecodeError:
            # 尝试从响应中提取 JSON (支持嵌套)
            json_match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except:
                    pass
        
        if not result:
            # 解析失败，返回原始响应
            print(f"      ⚠️ JSON 解析失败，原始响应: {response_text[:200]}")
            return {
                "title": "Untitled Prompt",
                "category": "Other",
                "sub_categories": [],
                "style": "unknown",
                "confidence": "low",
                "reason": "Failed to parse classification result"
            }
        
        # 标准化结果，确保所有必要字段存在
        normalized = {
            "title": result.get("title", "未命名提示词"),
            "category": result.get("category", "其他 (Other)"),
            "sub_categories": result.get("sub_categories", []),
            "style": result.get("style", "unknown"),
            "confidence": result.get("confidence", "中"),
            "reason": result.get("reason", ""),
        }
        
        # 确保 title 是字符串
        if not isinstance(normalized["title"], str) or not normalized["title"].strip():
            normalized["title"] = "Untitled Prompt"
        
        # 确保 sub_categories 是列表
        if not isinstance(normalized["sub_categories"], list):
            normalized["sub_categories"] = []
        
        # 清理 sub_categories
        cleaned_tags = []
        for tag in normalized["sub_categories"]:
            if isinstance(tag, str) and tag.strip():
                cleaned_tags.append(tag.strip())
        normalized["sub_categories"] = cleaned_tags
        
        # 添加 style 到 tags 中（如果不为空）
        if normalized["style"] and normalized["style"] != "unknown":
            if normalized["style"] not in normalized["sub_categories"]:
                normalized["sub_categories"].append(normalized["style"])
        
        print(f"      📋 分类结果: title={normalized['title']}, category={normalized['category']}, tags={normalized['sub_categories']}")
        
        return normalized
        
    except requests.exceptions.Timeout:
        raise Exception("API 请求超时")
    except Exception as e:
        raise Exception(f"分类失败: {e}")


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
    
    # 提取媒体
    if "media" in tweet and "photos" in tweet["media"]:
        for photo in tweet["media"]["photos"]:
            result["images"].append(photo.get("url", ""))
    
    return result


def parse_vxtwitter_result(data: dict) -> dict:
    """解析 VxTwitter API 的返回结果"""
    result = {
        "text": "",
        "images": [],
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
    
    # 提取媒体
    if "media_extended" in data:
        for media in data["media_extended"]:
            if media.get("type") == "image":
                result["images"].append(media.get("url", ""))
    
    return result


def fetch_tweet(url: str, download_images: bool = True, output_dir: str = ".", 
                extract_prompt: bool = False, ai_model: str = DEFAULT_MODEL) -> dict:
    """
    获取推文内容的主函数
    会依次尝试不同的方法直到成功
    
    Args:
        url: 推文 URL
        download_images: 是否下载图片
        output_dir: 输出目录
        extract_prompt: 是否使用 AI 提取提示词
        ai_model: AI 模型名称 (openai, deepseek 等)
    """
    from datetime import datetime
    
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
    
    # 使用 AI 提取提示词
    if extract_prompt and result.get("text"):
        print()
        print(f"   🤖 AI 处理 (模型: {ai_model})")
        
        # 提取提示词
        print(f"      [1/2] 提取提示词...")
        try:
            extracted_prompt = extract_prompt_with_ai(result["text"], model=ai_model)
            result["extracted_prompt"] = extracted_prompt
            
            if extracted_prompt and extracted_prompt != "No prompt found":
                prompt_preview = extracted_prompt[:80].replace("\n", " ")
                print(f"      ✓ 提取成功: {prompt_preview}...")
                
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
    print(f"✅ [SUCCESS] 推文处理完成: {url}")
    print(f"   用户: @{username} | 推文ID: {tweet_id}")
    print(f"   获取方式: {fetch_method}")
    print(f"   图片数量: {len(result.get('images', []))}")
    if result.get("extracted_prompt") and result["extracted_prompt"] != "No prompt found":
        print(f"   提示词: 已提取")
        if result.get("classification"):
            print(f"   分类: {result['classification'].get('category', '未知')}")
    else:
        print(f"   提示词: 未找到")
    print(f"   耗时: {elapsed:.1f}s")
    print("=" * 70)
    
    return result


def main():
    import argparse
    
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

