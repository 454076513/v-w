#!/usr/bin/env python3
"""
独立脚本：获取 Twitter 作者回复
用于避免连接池问题
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlencode

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


COOKIES_FILE = Path(__file__).parent / "x_cookies.json"
X_PROXY = os.environ.get("X_PROXY", "")


def load_cookies() -> dict:
    """加载 Twitter cookies"""
    if COOKIES_FILE.exists():
        try:
            with open(COOKIES_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}


def fetch_author_replies(tweet_id: str, author_username: str) -> list:
    """
    使用 Twitter GraphQL API 获取作者对自己帖子的回复
    """
    cookies = load_cookies()
    if not cookies:
        print("DEBUG: No cookies found", file=sys.stderr)
        return []

    auth_token = cookies.get("auth_token", "")
    ct0 = cookies.get("ct0", "")

    if not auth_token or not ct0:
        return []

    headers = {
        'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
        'x-csrf-token': ct0,
        'cookie': f'auth_token={auth_token}; ct0={ct0}',
        'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'content-type': 'application/json',
        'x-twitter-active-user': 'yes',
        'x-twitter-auth-type': 'OAuth2Session',
    }

    variables = {
        "focalTweetId": tweet_id,
        "with_rux_injections": False,
        "rankingMode": "Relevance",
        "includePromotedContent": True,
        "withCommunity": True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withBirdwatchNotes": True,
        "withVoice": True
    }

    features = {
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False
    }

    params = {
        'variables': json.dumps(variables),
        'features': json.dumps(features),
    }

    url = 'https://x.com/i/api/graphql/nBS-WpgA6ZG0CyNHD517JQ/TweetDetail?' + urlencode(params)

    try:
        # 使用全新的 Session
        session = requests.Session()

        # 配置代理
        proxies = None
        if X_PROXY:
            proxies = {"http": X_PROXY, "https": X_PROXY}
            print(f"DEBUG: Using proxy {X_PROXY}", file=sys.stderr)

        print(f"DEBUG: Making request...", file=sys.stderr)
        response = session.get(url, headers=headers, timeout=30, proxies=proxies)
        session.close()

        print(f"DEBUG: Response status={response.status_code}", file=sys.stderr)

        if response.status_code != 200:
            print(f"DEBUG: Non-200 response: {response.text[:200]}", file=sys.stderr)
            return []

        data = response.json()
        instructions = data.get('data', {}).get('threaded_conversation_with_injections_v2', {}).get('instructions', [])
        print(f"DEBUG: Found {len(instructions)} instructions", file=sys.stderr)

        replies = []
        for instruction in instructions:
            if instruction.get('type') == 'TimelineAddEntries':
                entries = instruction.get('entries', [])
                for entry in entries:
                    entry_id = entry.get('entryId', '')

                    # 回复线程
                    if 'conversationthread' in entry_id.lower():
                        items = entry.get('content', {}).get('items', [])
                        for item in items:
                            tweet_result = item.get('item', {}).get('itemContent', {}).get('tweet_results', {}).get('result', {})
                            if tweet_result:
                                legacy = tweet_result.get('legacy', {})
                                core = tweet_result.get('core', {})
                                user_results = core.get('user_results', {}).get('result', {})
                                user_legacy = user_results.get('legacy', {})

                                username = user_legacy.get('screen_name', '')

                                # 优先使用 note_tweet (长推文) 的文本
                                note_tweet = tweet_result.get('note_tweet', {})
                                if note_tweet:
                                    text = note_tweet.get('note_tweet_results', {}).get('result', {}).get('text', '')
                                    print(f"DEBUG: Found note_tweet with {len(text)} chars", file=sys.stderr)
                                else:
                                    text = legacy.get('full_text', '')

                                # 只保留作者的回复
                                if username.lower() == author_username.lower():
                                    # 过滤掉简短的感谢回复
                                    if len(text) > 50 or 'prompt' in text.lower():
                                        replies.append({
                                            "text": text,
                                            "username": username,
                                            "is_author": True
                                        })

        return replies

    except Exception as e:
        print(f"DEBUG: Exception in fetch_author_replies: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return []


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps([]), file=sys.stdout)
        sys.exit(0)

    tweet_id = sys.argv[1]
    author_username = sys.argv[2]

    # 短暂延迟
    time.sleep(0.5)

    # Debug to stderr
    print(f"DEBUG: tweet_id={tweet_id}, author={author_username}", file=sys.stderr)

    replies = fetch_author_replies(tweet_id, author_username)

    print(f"DEBUG: found {len(replies)} replies", file=sys.stderr)

    # Output to stdout
    print(json.dumps(replies, ensure_ascii=False), file=sys.stdout)
