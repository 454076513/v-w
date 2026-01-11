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
PROMPT_IN_REPLY_PATTERNS = [
    r'prompt\s*[👇⬇️↓🔽]',
    r'[👇⬇️↓🔽]\s*prompt',
    r'prompt\s+below',
    r'prompt\s+in\s+(the\s+)?(comment|reply|replies|thread)',
    r'check\s+(the\s+)?(comment|reply|replies)',
    r'see\s+(the\s+)?(comment|reply|replies)',
    r'(comment|reply|replies)\s+for\s+prompt',
    r'full\s+prompt\s+[👇⬇️↓🔽]',
    r'提示词\s*[👇⬇️↓🔽]',
    r'[👇⬇️↓🔽]\s*提示词',
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


# ========== 提示词提取 ==========

def extract_prompt_regex(text: str) -> str:
    """
    使用正则表达式从文本中提取 prompt
    用于快速提取格式规范的 prompt，避免 AI 调用

    Args:
        text: 文本内容

    Returns:
        提取的 prompt 或 None
    """
    if not text:
        return None

    # 常见的 prompt 引导模式
    patterns = [
        # 👉Prompt: ... 或 Prompt: ...
        r'(?:👉\s*)?[Pp]rompt[:\s]+(.+)',
        # "prompt" 后面跟着换行和内容
        r'[Pp]rompt\s*\n+(.+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            prompt = match.group(1).strip()
            # 清理开头的引号、括号等
            prompt = re.sub(r'^[\"\'\[\(]+', '', prompt)
            # 如果 prompt 足够长，认为是有效的
            if len(prompt) > 50:
                return prompt

    return None


def extract_prompt(text: str, model: str = DEFAULT_MODEL, use_ai: bool = True) -> dict:
    """
    从文本中提取提示词（主函数）

    先尝试正则表达式，失败后使用 AI

    Args:
        text: 文本内容
        model: AI 模型名称
        use_ai: 是否使用 AI（正则失败时）

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

    # 首先检测是否是 "prompt 在评论中" 的情况
    if detect_prompt_in_reply(text):
        result["prompt"] = "Prompt in reply"
        result["location"] = "reply"
        result["method"] = "pattern"
        return result

    # 尝试正则表达式提取
    regex_result = extract_prompt_regex(text)
    if regex_result:
        result["prompt"] = regex_result
        result["location"] = "post"
        result["method"] = "regex"
        return result

    # 使用 AI 提取
    if use_ai:
        try:
            ai_result = _extract_prompt_with_ai(text, model)
            if ai_result and ai_result not in ["No prompt found", "Prompt in reply", "Advertisement"]:
                result["prompt"] = ai_result
                result["location"] = "post"
                result["method"] = "ai"
            elif ai_result == "Prompt in reply":
                result["prompt"] = "Prompt in reply"
                result["location"] = "reply"
                result["method"] = "ai"
            elif ai_result == "Advertisement":
                result["prompt"] = "Advertisement"
                result["location"] = None
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
   If it's an advertisement, return 'Advertisement'.

2. Extract only the actual prompt itself, without any additional explanation or formatting.
3. If the text contains indicators like "Prompt👇", "prompt below", "check comment", "prompt in reply" etc., it means the actual prompt is in a reply/comment, not in the main post. In this case, return 'Prompt in reply'.
4. If the text only contains a title or description of what the image shows (like "Nano Banana prompt" or "Any person to Trash Pop Collage") but NOT the actual detailed prompt, return 'No prompt found'.
5. A real prompt usually contains detailed descriptions, style parameters (like --ar, --v), or specific technical terms.
6. If no actual prompt is found, return 'No prompt found'."""
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
