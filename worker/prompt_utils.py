#!/usr/bin/env python3
"""
Prompt Utils - 提示词提取和分类工具

公用模块，提供：
- AI 提示词提取
- 提示词分类
- 正则表达式快速提取

使用方法:
    from prompt_utils import extract_prompt, classify_prompt

    # 提取提示词
    result = extract_prompt(text, model="openai")

    # 分类提示词
    classification = classify_prompt(prompt, model="openai")
"""

import re
import os
import requests
from pathlib import Path

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


# ========== 配置 ==========

# Pollinations API 配置
POLLINATIONS_API_URL = "https://text.pollinations.ai/"
DEFAULT_MODEL = "openai"

# Gitee AI API 配置 (fallback 1)
GITEE_AI_API_URL = "https://ai.gitee.com/v1/chat/completions"
GITEE_AI_MODEL = "DeepSeek-V3"
GITEE_AI_API_KEY = os.environ.get("GITEE_AI_API_KEY", "")

# NVIDIA API 配置 (fallback 2)
NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODEL = "deepseek-ai/deepseek-v3.2"
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "mHMcKtSCRsFEXQ2gyipZS6bn1aU01szMrkCRORruRFvtbCCwmjqeO")


# ========== AI 调用 ==========

def call_ai(messages: list, model: str = DEFAULT_MODEL) -> str:
    """
    调用 AI API，依次尝试 Pollinations -> Gitee AI -> NVIDIA API

    Args:
        messages: OpenAI 格式的消息列表
        model: Pollinations 使用的模型

    Returns:
        AI 响应内容
    """
    errors = []

    # 首先尝试 Pollinations AI
    try:
        result = _call_pollinations_ai(messages, model)
        return result
    except Exception as pollinations_error:
        print(f"⚠️ Pollinations AI 失败: {pollinations_error}")
        errors.append(f"Pollinations ({pollinations_error})")

    # Fallback 1: Gitee AI
    if GITEE_AI_API_KEY:
        print(f"🔄 尝试 Gitee AI (DeepSeek-V3) 作为 fallback...")
        try:
            result = _call_gitee_ai(messages)
            print("✓ Gitee AI 调用成功")
            return result
        except Exception as gitee_error:
            print(f"✗ Gitee AI 也失败: {gitee_error}")
            errors.append(f"Gitee ({gitee_error})")
    else:
        print("⚠️ GITEE_AI_API_KEY 未设置，跳过 Gitee AI")

    # Fallback 2: NVIDIA API
    if NVIDIA_API_KEY:
        print(f"🔄 尝试 NVIDIA API (DeepSeek-V3.2) 作为 fallback...")
        try:
            result = _call_nvidia_ai(messages)
            print("✓ NVIDIA API 调用成功")
            return result
        except Exception as nvidia_error:
            print(f"✗ NVIDIA API 也失败: {nvidia_error}")
            errors.append(f"NVIDIA ({nvidia_error})")
    else:
        print("⚠️ NVIDIA_API_KEY 未设置，跳过 NVIDIA API")

    # 所有服务都失败
    raise Exception(f"所有 AI 服务都失败: {', '.join(errors)}")


def _call_pollinations_ai(messages: list, model: str = DEFAULT_MODEL) -> str:
    """
    调用 Pollinations AI API

    Args:
        messages: OpenAI 格式的消息列表
        model: 使用的模型

    Returns:
        AI 响应内容
    """
    import json as json_module

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
        "stream": True,
    }

    response = requests.post(
        GITEE_AI_API_URL,
        json=payload,
        headers=headers,
        timeout=(10, 300),
        stream=True
    )

    if response.status_code != 200:
        raise Exception(f"Gitee AI 请求失败: {response.status_code} - {response.text}")

    full_content = []

    for line in response.iter_lines():
        if not line:
            continue

        line = line.decode('utf-8')

        if line.startswith('data: '):
            data_str = line[6:]

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


def _call_nvidia_ai(messages: list) -> str:
    """
    调用 NVIDIA API (fallback 2)，使用 stream 模式

    Args:
        messages: OpenAI 格式的消息列表

    Returns:
        AI 响应内容
    """
    import json

    if not NVIDIA_API_KEY:
        raise Exception("NVIDIA_API_KEY 环境变量未设置")

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {NVIDIA_API_KEY}',
        'Accept': 'text/event-stream',
    }

    payload = {
        "model": NVIDIA_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "stream": True,
    }

    response = requests.post(
        NVIDIA_API_URL,
        json=payload,
        headers=headers,
        timeout=(10, 300),
        stream=True
    )

    if response.status_code != 200:
        raise Exception(f"NVIDIA API 请求失败: {response.status_code} - {response.text}")

    full_content = []

    for line in response.iter_lines():
        if not line:
            continue

        line = line.decode('utf-8')

        if line.startswith('data: '):
            data_str = line[6:]

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
        raise Exception("NVIDIA API 返回空响应")

    return "".join(full_content)


# ========== 提示词检测 ==========

# 检测 "prompt 在评论中" 的指示符模式
# 向下箭头符号: 👇⬇️↓🔽⤵️
PROMPT_IN_REPLY_PATTERNS = [
    r'prompt\s*[👇⬇️↓🔽⤵️]',
    r'[👇⬇️↓🔽⤵️]\s*prompt',
    r'prompt\s+below',
    r'prompt\s+in\s+(the\s+)?(comment|reply|replies|thread)',
    r'check\s+(the\s+)?(comment|reply|replies)',
    r'see\s+(the\s+)?(comment|reply|replies)',
    r'(comment|reply|replies)\s+for\s+prompt',
    r'full\s+prompt\s+[👇⬇️↓🔽⤵️]',
    r'提示词\s*[👇⬇️↓🔽⤵️]',
    r'[👇⬇️↓🔽⤵️]\s*提示词',
    # 文末带向下箭头表示内容在下方（即使不紧跟 prompt 关键词）
    r'[👇⬇️↓🔽⤵️]\s*$',
]


def detect_prompt_in_reply(text: str) -> bool:
    """
    检测文本是否表明 prompt 在评论/回复中

    Args:
        text: 文本内容

    Returns:
        True 如果检测到 prompt 可能在评论中
    """
    if not text:
        return False

    text_lower = text.lower()

    for pattern in PROMPT_IN_REPLY_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True

    return False


# 检测 "prompt 在 ALT 文本中" 的指示符模式
PROMPT_IN_ALT_PATTERNS = [
    r'prompt\s+in\s+(the\s+)?alt',
    r'alt\s+for\s+prompt',
    r'see\s+alt',
    r'check\s+alt',
    r'\(prompt\s+in\s+alt\s*!?\s*\)',
    r'提示词在\s*alt',
    r'alt\s*里',
]


def detect_prompt_in_alt(text: str) -> bool:
    """
    检测文本是否表明 prompt 在图片 ALT 文本中

    Args:
        text: 文本内容

    Returns:
        True 如果检测到 prompt 可能在 ALT 文本中
    """
    if not text:
        return False

    text_lower = text.lower()

    for pattern in PROMPT_IN_ALT_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True

    return False


# ========== 提示词提取 ==========

def extract_prompt_regex(text: str) -> str:
    """
    使用正则表达式从文本中提取 prompt
    仅处理最简单、最确定的格式，其他情况交给 AI

    Args:
        text: 文本内容

    Returns:
        提取的 prompt 或 None
    """
    if not text:
        return None

    # 只匹配最明确的格式: "Prompt:" 后面紧跟冒号
    # 其他复杂情况交给 AI 判断
    match = re.search(r'(?:👉\s*)?[Pp]rompt\s*:\s*(.+)', text, re.DOTALL)
    if match:
        prompt = match.group(1).strip()
        # 清理开头的引号、括号等
        prompt = re.sub(r'^[\"\'\[\(]+', '', prompt)
        # prompt 足够长才认为有效
        if len(prompt) > 50:
            return prompt

    return None


def extract_prompt(text: str, model: str = DEFAULT_MODEL, use_ai: bool = True) -> dict:
    """
    从文本中提取提示词（主函数）

    优先使用 AI 判断，正则仅作为辅助

    Args:
        text: 文本内容
        model: AI 模型名称
        use_ai: 是否使用 AI

    Returns:
        dict: {
            "prompt": 提取的 prompt 或 None,
            "location": "post" | "reply" | None,
            "method": "regex" | "ai" | None
        }
    """
    result = {
        "prompt": None,
        "location": None,
        "method": None
    }

    if not text:
        return result

    # 优先使用 AI 提取（更智能、更准确）
    if use_ai:
        try:
            ai_result = _extract_prompt_with_ai(text, model)

            # 检测是否为广告/无效内容（AI 有时返回解释性文字而非精确关键词）
            if ai_result:
                ai_lower = ai_result.lower()
                is_ad = (
                    ai_result == "Advertisement" or
                    "promotional content" in ai_lower or
                    "advertisement" in ai_lower or
                    "does not contain" in ai_lower or
                    "no actual prompt" in ai_lower or
                    "not an actual prompt" in ai_lower or
                    "is not a prompt" in ai_lower or
                    "doesn't contain" in ai_lower or
                    "self-promotion" in ai_lower or
                    "engagement bait" in ai_lower
                )
                is_no_prompt = (
                    ai_result == "No prompt found" or
                    "no prompt" in ai_lower or
                    "not found" in ai_lower
                )
                is_in_alt = (
                    ai_result == "Prompt in ALT" or
                    "prompt in alt" in ai_lower
                )
                is_in_reply = (
                    ai_result == "Prompt in reply" or
                    "prompt in reply" in ai_lower or
                    "in the reply" in ai_lower or
                    "in the comment" in ai_lower
                )

                if is_ad:
                    result["prompt"] = "Advertisement"
                    result["location"] = None
                    result["method"] = "ai"
                elif is_in_alt:
                    result["prompt"] = "Prompt in ALT"
                    result["location"] = "alt"
                    result["method"] = "ai"
                elif is_in_reply:
                    result["prompt"] = "Prompt in reply"
                    result["location"] = "reply"
                    result["method"] = "ai"
                elif is_no_prompt:
                    # 不设置 prompt，保持为 None
                    result["method"] = "ai"
                elif ai_result not in ["No prompt found", "Prompt in reply", "Prompt in ALT", "Advertisement"]:
                    result["prompt"] = ai_result
                    result["location"] = "post"
                    result["method"] = "ai"
        except Exception as e:
            print(f"⚠️ AI 提取失败: {e}")

    return result


def _extract_prompt_with_ai(text: str, model: str = DEFAULT_MODEL) -> str:
    """
    使用 AI API 从文本中提取提示词

    Args:
        text: 文本内容
        model: 使用的模型

    Returns:
        提取出的提示词，或特殊值:
        - "Prompt in reply": prompt 在评论/回复中
        - "No prompt found": 未找到 prompt
        - "Advertisement": 内容是广告/推广
    """
    messages = [
        {
            "role": "system",
            "content": """You are a helpful assistant that extracts AI image generation prompts from text.

IMPORTANT RULES:
1. FIRST, check if this is an advertisement or promotional content. Signs of ads include:
   - Product promotions, sales, discounts, deals
   - Service promotions (courses, tools, subscriptions)
   - Affiliate links, referral codes, promo codes
   - "Buy now", "Limited time", "Sign up", "Join", "Subscribe"
   - App/software promotions without actual prompts
   - Giveaways that require following/retweeting
   - Self-promotion of services or products
   - Lists of AI tools or software recommendations (e.g., "Top 10 AI tools", "100+ AI Tools")
   - Engagement bait: "Like", "Comment", "RT", "Retweet", "Follow me", "Must follow", "Bookmark this"
   - Numbered lists of tool names or categories (e.g., "1. Research - ChatGPT, YouChat...")
   If it's an advertisement or tool list, return 'Advertisement'.

2. Extract only the actual prompt itself, without any additional explanation or formatting.
3. CRITICAL: Check if the prompt is NOT in the main post but in a reply/comment. Return 'Prompt in reply' if:
   - Text ends with down arrow emoji: 👇 ⬇️ ↓ 🔽 ⤵️ (these mean "see below/in comments")
   - Text says "prompt below", "check comment", "prompt in reply", "see thread"
   - Text discusses a prompt (e.g., "This prompt works great!", "Try this prompt") but doesn't include the actual detailed prompt text
   - Text is short (under 200 chars) and talks ABOUT a prompt without containing one
   When in doubt and the text ends with ⤵️ or similar arrows, return 'Prompt in reply'.
4. If the text contains indicators like "Prompt in ALT", "see ALT", "check ALT", "ALT for prompt", or mentions that the prompt is in the image's alt text, return 'Prompt in ALT'.
5. If the text only contains a title or description of what the image shows (like "Nano Banana prompt" or "Any person to Trash Pop Collage") but NOT the actual detailed prompt, return 'No prompt found'.
6. A real AI image generation prompt usually contains:
   - Detailed scene descriptions (subjects, actions, environments)
   - Visual style specifications (lighting, colors, mood)
   - Technical parameters (--ar, --v, --style, resolution)
   - Art style references (photorealistic, anime, oil painting, etc.)
7. The following are NOT prompts - return 'No prompt found':
   - Lists of AI tools or software names
   - News or commentary about AI
   - Tutorials without actual prompts
   - General discussions about image generation
8. If no actual prompt is found, return 'No prompt found'."""
        },
        {
            "role": "user",
            "content": f"Extract the AI image generation prompt from this text and return only the prompt itself:\n\n{text}"
        }
    ]

    return call_ai(messages, model)


def extract_prompt_simple(text: str, model: str = DEFAULT_MODEL) -> str:
    """
    简单的 AI 提取（不检测 "Prompt in reply"）
    用于从已知包含 prompt 的文本中提取

    Args:
        text: 文本内容
        model: 使用的模型

    Returns:
        提取出的提示词或 None
    """
    # 先尝试正则
    regex_result = extract_prompt_regex(text)
    if regex_result:
        return regex_result

    # AI 提取
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
        result = call_ai(messages, model)
        if result and result != "No prompt found":
            return result
    except Exception as e:
        print(f"⚠️ AI 提取失败: {e}")

    return None


# ========== 提示词分类 ==========

# 预定义的分类列表 (统一分类系统)
# 注意: AI 返回的 category 应该使用英文部分 (括号内)
PROMPT_CATEGORIES = [
    "人像/肖像 (Portrait)",
    "风景 (Landscape)",
    "自然/动物 (Nature)",
    "建筑/城市 (Architecture)",
    "抽象艺术 (Abstract)",
    "科幻/未来 (Sci-Fi)",
    "奇幻/魔法 (Fantasy)",
    "动漫/卡通 (Anime)",
    "写实摄影 (Photography)",
    "插画/绘画 (Illustration)",
    "时尚/服装 (Fashion)",
    "食物/美食 (Food)",
    "产品/商业 (Product)",
    "电影感/影视 (Cinematic)",
    "恐怖/黑暗 (Horror)",
    "可爱/萌系 (Cute)",
    "复古/怀旧 (Retro)",
    "极简主义 (Minimalist)",
    "超现实 (Surreal)",
    "3D渲染 (3D Render)",
    "赛博朋克 (Cyberpunk)",
    "像素艺术 (Pixel Art)",
    "其他 (Other)",
]

# 分类英文名列表 (用于验证和映射)
VALID_CATEGORIES = [
    "Portrait", "Landscape", "Nature", "Architecture", "Abstract",
    "Sci-Fi", "Fantasy", "Anime", "Photography", "Illustration",
    "Fashion", "Food", "Product", "Cinematic", "Horror", "Cute",
    "Retro", "Minimalist", "Surreal", "3D Render", "Cyberpunk",
    "Pixel Art", "Other"
]

# 统一分类映射表: AI 可能返回的各种格式 -> 标准分类名
# 所有脚本应该导入此映射表以保持一致性
CATEGORY_MAP = {
    # 标准分类 (直接映射)
    "Portrait": "Portrait",
    "Landscape": "Landscape",
    "Nature": "Nature",
    "Architecture": "Architecture",
    "Abstract": "Abstract",
    "Sci-Fi": "Sci-Fi",
    "Fantasy": "Fantasy",
    "Anime": "Anime",
    "Photography": "Photography",
    "Illustration": "Illustration",
    "Fashion": "Fashion",
    "Food": "Food",
    "Product": "Product",
    "Cinematic": "Cinematic",
    "Horror": "Horror",
    "Cute": "Cute",
    "Retro": "Retro",
    "Minimalist": "Minimalist",
    "Surreal": "Surreal",
    "3D Render": "3D Render",
    "Cyberpunk": "Cyberpunk",
    "Pixel Art": "Pixel Art",
    "Other": "Other",
    # 旧格式兼容 (AI 可能返回的变体)
    "Landscape/Nature": "Landscape",
    "Animals": "Nature",
    "Architecture/Urban": "Architecture",
    "Urban": "Architecture",
    "Abstract Art": "Abstract",
    "Sci-Fi/Futuristic": "Sci-Fi",
    "Futuristic": "Sci-Fi",
    "Fantasy/Magic": "Fantasy",
    "Magic": "Fantasy",
    "Anime/Cartoon": "Anime",
    "Cartoon": "Anime",
    "Realistic Photography": "Photography",
    "Illustration/Painting": "Illustration",
    "Painting": "Illustration",
    "Fashion/Clothing": "Fashion",
    "Clothing": "Fashion",
    "Product/Commercial": "Product",
    "Commercial": "Product",
    "Horror/Dark": "Horror",
    "Dark": "Horror",
    "Cute/Kawaii": "Cute",
    "Kawaii": "Cute",
    "Vintage/Retro": "Retro",
    "Vintage": "Retro",
    # 旧系统分类兼容
    "Clay / Felt": "Cute",
    "Retro / Vintage": "Retro",
    "3D": "3D Render",
}


def map_category(classification: dict) -> str:
    """
    将 AI 返回的分类映射到标准分类名

    Args:
        classification: AI 分类结果字典，包含 "category" 字段

    Returns:
        标准分类名
    """
    raw_category = classification.get("category", "Other")

    # 直接匹配
    if raw_category in CATEGORY_MAP:
        return CATEGORY_MAP[raw_category]

    # 模糊匹配 (大小写不敏感)
    raw_lower = raw_category.lower()
    for key, value in CATEGORY_MAP.items():
        if key.lower() == raw_lower:
            return value
        if key.lower() in raw_lower or raw_lower in key.lower():
            return value

    # 验证是否是有效分类
    if raw_category in VALID_CATEGORIES:
        return raw_category

    # 默认返回 Illustration
    return "Illustration"


# 统一标签到分类映射表
# 用于从 tags 推断分类，所有导入脚本共用
TAG_TO_CATEGORY = {
    # 人像
    "portrait": "Portrait",
    "character": "Portrait",
    "face": "Portrait",
    "headshot": "Portrait",
    "selfie": "Portrait",
    "person": "Portrait",
    # 风景
    "landscape": "Landscape",
    "scenery": "Landscape",
    "outdoor": "Landscape",
    "mountain": "Landscape",
    "beach": "Landscape",
    "sunset": "Landscape",
    # 自然/动物
    "nature": "Nature",
    "animal": "Nature",
    "animals": "Nature",
    "wildlife": "Nature",
    "pet": "Nature",
    "cat": "Nature",
    "dog": "Nature",
    "bird": "Nature",
    "flower": "Nature",
    "plant": "Nature",
    "forest": "Nature",
    # 建筑
    "architecture": "Architecture",
    "building": "Architecture",
    "city": "Architecture",
    "urban": "Architecture",
    "interior": "Architecture",
    "house": "Architecture",
    "room": "Architecture",
    # 抽象
    "abstract": "Abstract",
    "pattern": "Abstract",
    "geometric": "Abstract",
    # 科幻
    "sci-fi": "Sci-Fi",
    "scifi": "Sci-Fi",
    "futuristic": "Sci-Fi",
    "space": "Sci-Fi",
    "robot": "Sci-Fi",
    "spaceship": "Sci-Fi",
    "alien": "Sci-Fi",
    "gaming": "Sci-Fi",
    # 奇幻
    "fantasy": "Fantasy",
    "magic": "Fantasy",
    "dragon": "Fantasy",
    "fairy": "Fantasy",
    "wizard": "Fantasy",
    "medieval": "Fantasy",
    "mythical": "Fantasy",
    # 动漫
    "anime": "Anime",
    "cartoon": "Anime",
    "manga": "Anime",
    "comic": "Anime",
    "chibi": "Anime",
    # 摄影
    "photography": "Photography",
    "photo": "Photography",
    "realistic": "Photography",
    "photorealistic": "Photography",
    "real": "Photography",
    # 插画
    "illustration": "Illustration",
    "painting": "Illustration",
    "artwork": "Illustration",
    "drawing": "Illustration",
    "art": "Illustration",
    "infographic": "Illustration",
    "typography": "Illustration",
    "watercolor": "Illustration",
    "oil-painting": "Illustration",
    # 时尚
    "fashion": "Fashion",
    "clothing": "Fashion",
    "outfit": "Fashion",
    "model": "Fashion",
    "dress": "Fashion",
    # 食物
    "food": "Food",
    "cuisine": "Food",
    "dish": "Food",
    "cooking": "Food",
    # 产品
    "product": "Product",
    "commercial": "Product",
    "advertisement": "Product",
    "vehicle": "Product",
    "car": "Product",
    "logo": "Product",
    # 电影感
    "cinematic": "Cinematic",
    "movie": "Cinematic",
    "film": "Cinematic",
    "dramatic": "Cinematic",
    # 恐怖
    "horror": "Horror",
    "dark": "Horror",
    "creepy": "Horror",
    "scary": "Horror",
    "gothic": "Horror",
    "zombie": "Horror",
    # 可爱
    "cute": "Cute",
    "kawaii": "Cute",
    "adorable": "Cute",
    "paper-craft": "Cute",
    "clay": "Cute",
    "felt": "Cute",
    "plush": "Cute",
    # 复古
    "retro": "Retro",
    "vintage": "Retro",
    "nostalgic": "Retro",
    "80s": "Retro",
    "90s": "Retro",
    "classic": "Retro",
    # 极简
    "minimalist": "Minimalist",
    "minimal": "Minimalist",
    "simple": "Minimalist",
    "clean": "Minimalist",
    # 超现实
    "surreal": "Surreal",
    "surrealism": "Surreal",
    "dreamlike": "Surreal",
    "dream": "Surreal",
    # 3D渲染
    "3d": "3D Render",
    "3d render": "3D Render",
    "3d-render": "3D Render",
    "render": "3D Render",
    "blender": "3D Render",
    "cgi": "3D Render",
    # 赛博朋克
    "cyberpunk": "Cyberpunk",
    "neon": "Cyberpunk",
    "cyber": "Cyberpunk",
    # 像素艺术
    "pixel": "Pixel Art",
    "pixel art": "Pixel Art",
    "pixel-art": "Pixel Art",
    "pixelart": "Pixel Art",
    "8-bit": "Pixel Art",
    "8bit": "Pixel Art",
    "16-bit": "Pixel Art",
    "16bit": "Pixel Art",
    # 其他
    "creative": "Other",
    "other": "Other",
}


def infer_category_from_tags(tags: list) -> str:
    """
    从标签列表推断分类

    Args:
        tags: 标签列表，如 ["portrait", "fashion", "realistic"]

    Returns:
        推断的分类名，如 "Portrait"
    """
    if not tags:
        return "Other"

    for tag in tags:
        if not isinstance(tag, str):
            continue
        tag_lower = tag.lower().strip()
        if tag_lower in TAG_TO_CATEGORY:
            return TAG_TO_CATEGORY[tag_lower]

    return "Illustration"


def classify_prompt(prompt: str, model: str = DEFAULT_MODEL) -> dict:
    """
    使用 AI 对提示词进行分类

    Args:
        prompt: 提示词内容
        model: 使用的模型

    Returns:
        分类结果字典: {
            "title": "标题",
            "category": "分类",
            "sub_categories": ["次分类"],
            "style": "风格",
            "confidence": "high/medium/low",
            "reason": "原因"
        }
    """
    import json

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
        response_text = call_ai(messages, model)

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
            # 尝试从响应中提取 JSON
            json_match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except:
                    pass

        if not result:
            print(f"⚠️ JSON 解析失败，原始响应: {response_text[:200]}")
            return {
                "title": "Untitled Prompt",
                "category": "Other",
                "sub_categories": [],
                "style": "unknown",
                "confidence": "low",
                "reason": "Failed to parse classification result"
            }

        # 标准化结果
        normalized = {
            "title": result.get("title", "Untitled Prompt"),
            "category": result.get("category", "Other"),
            "sub_categories": result.get("sub_categories", []),
            "style": result.get("style", "unknown"),
            "confidence": result.get("confidence", "medium"),
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

        # 添加 style 到 tags 中
        if normalized["style"] and normalized["style"] != "unknown":
            if normalized["style"] not in normalized["sub_categories"]:
                normalized["sub_categories"].append(normalized["style"])

        return normalized

    except requests.exceptions.Timeout:
        raise Exception("API 请求超时")
    except Exception as e:
        raise Exception(f"分类失败: {e}")


# ========== 便捷函数 ==========

def extract_and_validate_prompt(raw_text: str, model: str = DEFAULT_MODEL) -> dict:
    """
    统一的 prompt 提取和验证函数（供导入脚本使用）

    对原始文本进行 AI 提取，并验证结果是否有效。

    Args:
        raw_text: 原始文本内容
        model: AI 模型名称

    Returns:
        dict: {
            "success": bool,           # 是否成功提取到有效 prompt
            "prompt": str or None,     # 提取的 prompt（成功时）
            "method": str,             # 提取方法: "regex" | "ai"
            "error": str or None       # 错误信息（失败时）
        }
    """
    if not raw_text or not raw_text.strip():
        return {
            "success": False,
            "prompt": None,
            "method": None,
            "error": "Empty input text"
        }

    try:
        result = extract_prompt(raw_text, model=model, use_ai=True)
        prompt = result.get("prompt")
        method = result.get("method", "unknown")

        # 检查特殊返回值
        if not prompt:
            return {
                "success": False,
                "prompt": None,
                "method": method,
                "error": "AI extraction returned empty"
            }

        if prompt == "Advertisement":
            return {
                "success": False,
                "prompt": None,
                "method": method,
                "error": "Advertisement content"
            }

        if prompt == "Prompt in reply":
            return {
                "success": False,
                "prompt": None,
                "method": method,
                "error": "Prompt in reply"
            }

        if prompt == "No prompt found":
            return {
                "success": False,
                "prompt": None,
                "method": method,
                "error": "No prompt found by AI"
            }

        # 成功
        return {
            "success": True,
            "prompt": prompt,
            "method": method,
            "error": None
        }

    except Exception as e:
        return {
            "success": False,
            "prompt": None,
            "method": None,
            "error": f"AI extraction failed: {e}"
        }


def process_text(text: str, model: str = DEFAULT_MODEL, classify: bool = True) -> dict:
    """
    一站式处理：提取提示词并分类

    Args:
        text: 文本内容
        model: AI 模型
        classify: 是否分类

    Returns:
        dict: {
            "prompt": 提取的 prompt,
            "location": "post" | "reply",
            "method": "regex" | "ai",
            "classification": 分类结果 (如果 classify=True)
        }
    """
    result = extract_prompt(text, model)

    if classify and result["prompt"] and result["prompt"] not in ["Prompt in reply", "No prompt found"]:
        try:
            result["classification"] = classify_prompt(result["prompt"], model)
        except Exception as e:
            print(f"⚠️ 分类失败: {e}")
            result["classification"] = None
    else:
        result["classification"] = None

    return result


# ========== 从评论获取提示词 ==========

def fetch_author_replies(tweet_id: str, author_username: str) -> list:
    """
    获取作者对自己帖子的回复（通过子进程调用避免连接池问题）

    Args:
        tweet_id: 推文 ID
        author_username: 原始作者用户名

    Returns:
        作者回复列表，每个元素包含 {"text": "...", "is_author": True}
    """
    import subprocess
    import json
    import sys

    # 检查 cookies 是否存在
    cookies_file = Path(__file__).parent / "x_cookies.json"
    x_cookie_env = os.environ.get("X_COOKIE", "")

    cookies = {}
    if x_cookie_env:
        try:
            cookies = json.loads(x_cookie_env)
        except:
            pass
    elif cookies_file.exists():
        try:
            with open(cookies_file) as f:
                cookies = json.load(f)
        except:
            pass

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
                # 只显示非 DEBUG 的错误
                error_lines = [l for l in result.stderr.split('\n') if not l.startswith('DEBUG:')]
                if error_lines:
                    print(f"      ⚠️ 子进程错误: {' '.join(error_lines)[:200]}")
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


def extract_prompt_from_replies(replies: list, model: str = DEFAULT_MODEL) -> str:
    """
    从作者回复列表中提取 prompt

    Args:
        replies: 回复列表 [{"text": "...", ...}, ...]
        model: AI 模型

    Returns:
        提取的 prompt 或 None
    """
    if not replies:
        return None

    # 首先尝试正则表达式提取（更快更可靠）
    for reply in replies:
        reply_text = reply.get("text", "")
        prompt = extract_prompt_regex(reply_text)
        if prompt:
            return prompt

    # 如果正则没有提取到，尝试 AI 提取
    # 合并所有回复文本
    combined_text = "\n\n".join([r.get("text", "") for r in replies])

    try:
        messages = [
            {
                "role": "system",
                "content": "You are a helpful assistant that extracts AI image generation prompts from text. Extract only the prompt itself, without any additional explanation or formatting. If no prompt is found, return 'No prompt found'."
            },
            {
                "role": "user",
                "content": f"Extract the AI image generation prompt from this text and return only the prompt itself:\n\n{combined_text}"
            }
        ]

        result = call_ai(messages, model)
        if result and result != "No prompt found":
            return result

    except Exception as e:
        print(f"      ⚠️ AI 从回复提取失败: {e}")

    return None


def extract_prompt_with_replies(
    text: str,
    tweet_id: str,
    author_username: str,
    model: str = DEFAULT_MODEL
) -> dict:
    """
    从文本中提取提示词，如果提示词在评论中则尝试从评论获取

    这是一个通用函数，供所有爬取脚本使用。

    Args:
        text: 推文正文
        tweet_id: 推文 ID
        author_username: 作者用户名
        model: AI 模型

    Returns:
        dict: {
            "success": bool,           # 是否成功提取
            "prompt": str or None,     # 提取的 prompt
            "location": "post" | "reply" | None,  # prompt 位置
            "method": "regex" | "ai" | None,      # 提取方法
            "from_reply": bool,        # 是否从评论获取
            "error": str or None       # 错误信息（失败时）
        }
    """
    result = {
        "success": False,
        "prompt": None,
        "location": None,
        "method": None,
        "from_reply": False,
        "error": None
    }

    if not text:
        result["error"] = "Empty text"
        return result

    # 第一步：尝试从正文提取
    extract_result = extract_prompt(text, model=model, use_ai=True)
    prompt = extract_result.get("prompt")
    location = extract_result.get("location")
    method = extract_result.get("method")

    # 处理广告
    if prompt == "Advertisement":
        result["error"] = "Advertisement"
        result["method"] = method
        return result

    # 处理 "Prompt in reply" - 尝试从评论获取
    if prompt == "Prompt in reply" or location == "reply":
        print(f"      ⚠️ Prompt 在评论中，尝试获取作者回复...")

        # 获取作者回复
        replies = fetch_author_replies(tweet_id, author_username)

        if replies:
            print(f"      ✓ 获取到 {len(replies)} 条作者回复")

            # 从回复中提取 prompt
            reply_prompt = extract_prompt_from_replies(replies, model)

            if reply_prompt:
                result["success"] = True
                result["prompt"] = reply_prompt
                result["location"] = "reply"
                result["method"] = "ai"
                result["from_reply"] = True
                print(f"      ✓ 从评论中提取成功: {reply_prompt[:80]}...")
                return result
            else:
                result["error"] = "Failed to extract prompt from replies"
                print(f"      ⚠️ 从评论中未能提取到 prompt")
        else:
            result["error"] = "No author replies found"
            print(f"      ⚠️ 未获取到作者回复")

        return result

    # 处理未找到 prompt
    if not prompt or prompt == "No prompt found":
        result["error"] = "No prompt found"
        result["method"] = method
        return result

    # 成功从正文提取
    result["success"] = True
    result["prompt"] = prompt
    result["location"] = location or "post"
    result["method"] = method
    result["from_reply"] = False
    return result


# ========== 统一处理函数 ==========

def extract_tweet_id(url: str) -> str:
    """从 Twitter URL 提取 tweet ID"""
    import re
    match = re.search(r'/status/(\d+)', url)
    return match.group(1) if match else ""


def extract_username(url: str) -> str:
    """从 Twitter URL 提取用户名"""
    import re
    match = re.search(r'(?:twitter\.com|x\.com)/([^/]+)/status', url)
    return match.group(1) if match else ""


def process_tweet_for_import(
    db,
    tweet_url: str,
    raw_text: str = None,
    raw_images: list = None,
    author: str = None,
    import_source: str = "unknown",
    ai_model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    skip_twitter_fetch: bool = False,
) -> dict:
    """
    统一的推文处理入库函数

    规则:
    - 必须有 Twitter 图片才入库
    - 失败时返回详细错误信息供记录

    Args:
        db: Database 实例 (需要有 prompt_exists 和 save_prompt 方法)
        tweet_url: 推文 URL
        raw_text: 已有的原始文本（可选）
        raw_images: 已有的图片列表（仅当是 Twitter 图片时使用）
        author: 作者用户名
        import_source: 导入来源标识
        ai_model: AI 模型名称
        dry_run: 预览模式，不写入数据库
        skip_twitter_fetch: 跳过 Twitter 抓取（已有图片时）

    Returns:
        {
            "success": bool,
            "method": str,  # "imported", "skipped", "twitter_failed", "save_failed", "dry_run"
            "error": str or None,
            "twitter_failed": bool,
            "twitter_error": str or None,
            "data": dict or None  # 失败时返回已处理的数据供记录
        }
    """
    result = {
        "success": False,
        "method": "skipped",
        "error": None,
        "twitter_failed": False,
        "twitter_error": None,
        "data": None
    }

    if not tweet_url:
        result["error"] = "No tweet URL"
        return result

    # 1. 检查重复
    if db.prompt_exists(tweet_url):
        result["error"] = "Already exists"
        return result

    # 2. 获取图片和文本
    text = raw_text
    images = raw_images or []
    is_advertisement = False

    if not skip_twitter_fetch or not images:
        # 需要从 Twitter 获取数据
        try:
            from fetch_twitter_content import fetch_tweet
            print(f"   🐦 从 Twitter 获取数据...")

            twitter_result = fetch_tweet(
                tweet_url,
                download_images=False,
                extract_prompt=False,  # 我们自己用 extract_prompt_with_replies
                ai_model=ai_model,
                detect_ads=True
            )

            if not twitter_result:
                result["method"] = "twitter_failed"
                result["twitter_failed"] = True
                result["twitter_error"] = "fetch_tweet returned None"
                result["error"] = result["twitter_error"]
                return result

            # 获取图片
            twitter_images = twitter_result.get("images", [])
            if not twitter_images:
                result["method"] = "twitter_failed"
                result["twitter_failed"] = True
                result["twitter_error"] = "No images from Twitter"
                result["error"] = result["twitter_error"]
                return result

            images = twitter_images[:5]
            print(f"   ✅ 获取到 {len(images)} 张图片")

            # 获取文本（如果没有提供）
            if not text:
                text = twitter_result.get("text", "")

            # 检测广告
            is_advertisement = twitter_result.get("is_advertisement", False)

        except Exception as e:
            result["method"] = "twitter_failed"
            result["twitter_failed"] = True
            result["twitter_error"] = str(e)
            result["error"] = str(e)
            return result

    # 3. 广告检测
    if is_advertisement:
        result["error"] = "Advertisement content detected"
        print(f"   🚫 检测到广告内容，跳过")
        return result

    # 4. 检查图片
    if not images:
        result["method"] = "twitter_failed"
        result["twitter_failed"] = True
        result["twitter_error"] = "No images available"
        result["error"] = "No images available"
        return result

    # 5. 提取 Prompt（支持从评论获取）
    if not text:
        result["error"] = "No text content"
        return result

    tweet_id = extract_tweet_id(tweet_url)
    username = author or extract_username(tweet_url)

    print(f"   🤖 AI 提取 prompt...")
    extract_result = extract_prompt_with_replies(
        text=text,
        tweet_id=tweet_id,
        author_username=username,
        model=ai_model
    )

    if not extract_result["success"]:
        error = extract_result.get("error", "Unknown error")
        result["error"] = error
        if error == "Advertisement":
            print(f"   🚫 检测到广告内容，跳过")
        elif "reply" in error.lower():
            print(f"   ⚠️ {error}")
        else:
            print(f"   ⚠️ AI 提取失败: {error}")
        return result

    extracted_prompt = extract_result["prompt"]
    from_reply = extract_result.get("from_reply", False)

    if from_reply:
        print(f"   ✅ 从评论中提取到 prompt")
    else:
        print(f"   ✅ 提取成功: {extracted_prompt[:60]}...")

    # 检查 prompt 长度
    if len(extracted_prompt.strip()) < 20:
        result["error"] = f"Prompt too short ({len(extracted_prompt)} chars)"
        print(f"   ⚠️ Prompt 太短，跳过")
        return result

    # 6. AI 分类
    print(f"   🤖 AI 分类...")
    try:
        classification = classify_prompt(extracted_prompt, model=ai_model)
    except Exception as e:
        print(f"   ⚠️ AI 分类失败: {e}")
        classification = {}

    # 准备数据
    title = classification.get("title", "").strip()
    invalid_titles = ["Untitled Prompt", "No Prompt Provided", "Unknown Prompt",
                      "No Title", "Untitled", "N/A", ""]
    if not title or title.lower() in [t.lower() for t in invalid_titles]:
        title = f"@{username} #{tweet_id[-6:]}" if tweet_id else f"@{username}"

    category = classification.get("category", "Illustration").strip()
    if not category:
        category = "Illustration"

    tags = classification.get("sub_categories", [])
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip() for t in tags if t][:5]

    print(f"   ✅ 分类: {category}, 标签: {tags[:3]}")

    # 7. Dry Run
    if dry_run:
        print(f"   🔍 [Dry Run] 将入库:")
        print(f"      标题: {title}")
        print(f"      分类: {category}")
        print(f"      标签: {tags}")
        print(f"      图片: {len(images)}")
        print(f"      提示词: {extracted_prompt[:80]}...")
        result["success"] = True
        result["method"] = "dry_run"
        return result

    # 8. 入库
    print(f"   💾 保存到数据库...")
    try:
        record = db.save_prompt(
            title=title,
            prompt=extracted_prompt,
            category=category,
            tags=tags,
            images=images[:5],
            source_link=tweet_url,
            author=username,
            import_source=import_source
        )

        if record:
            print(f"   ✅ 已保存: {title}")
            result["success"] = True
            result["method"] = "imported"
            return result
        else:
            result["method"] = "save_failed"
            result["error"] = "Database save returned None"
            return result

    except Exception as e:
        result["method"] = "save_failed"
        result["error"] = str(e)
        print(f"   ❌ 保存失败: {e}")
        return result
