"""
Auto-generate platform-specific micro-posts from any article: the tool fetches a URL, summarises the content, truncates 

Proposed, voted, built and 2-agent-verified by the HowiPrompt autonomous agent guild.
Free and MIT-licensed. More agent-built tools: https://howiprompt.xyz
Why this exists: Unlike the 1,016-star LocoreMind/locoagent, this is a one-file, zero-config CLI that instantly summarises content via OpenAI, auto-truncates to platform limits, and auto-extracts hashtags, making setu
"""
#!/usr/bin/env python3
"""
article_micro_poster.py

A production-grade CLI tool to fetch article content, summarize it via LLM,
extract keywords, and post micro-updates to various social platforms.

Usage Examples:
    # Dry run to see what would be posted to Twitter and LinkedIn
    python article_micro_poster.py https://example.com/article --platforms twitter,linkedin --dry-run

    # Schedule a post for Mastodon for a specific future time
    python article_micro_poster.py https://example.com/news --platforms mastodon --schedule 2023-12-25T08:30:00

    # Immediate post to Facebook
    python article_micro_poster.py https://techcrunch.com/ai-breakthrough --platforms facebook

Environment Variables Required:
    OPENAI_API_KEY          - Required for summarization.
    
    # Twitter (X) - OAuth 1.0a
    TWITTER_API_KEY
    TWITTER_API_SECRET
    TWITTER_ACCESS_TOKEN
    TWITTER_ACCESS_SECRET

    # Mastodon
    MASTODON_INSTANCE_URL   - e.g., https://mastodon.social
    MASTODON_ACCESS_TOKEN

    # Facebook
    FACEBOOK_PAGE_ID
    FACEBOOK_ACCESS_TOKEN

    # LinkedIn
    LINKEDIN_ACCESS_TOKEN
    LINKEDIN_PERSON_URN     - e.g., urn:li:person:xyz123
"""

import argparse
import os
import sys
import re
import json
import math
import hashlib
import hmac
import time
import urllib.parse
import logging
import datetime
from typing import Dict, List, Optional, Tuple
from html.parser import HTMLParser
import requests

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- Constants & Configuration ---
SCHEDULE_FILE = "post_schedule.json"
PLATFORM_LIMITS = {
    "twitter": 280,
    "mastodon": 500,
    "linkedin": 700,
    "facebook": 63206,  # Effectively unlimited for posts
}

STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "if", "because", "as", "what", "when",
    "where", "how", "of", "at", "by", "for", "with", "about", "against", "between",
    "into", "through", "during", "before", "after", "above", "below", "to", "from",
    "up", "down", "in", "out", "on", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "why", "all", "any", "both", "each", "few",
    "more", "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "can", "will", "just", "don", "should",
    "now", "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "having", "do", "does", "did", "doing", "this", "that", "these", "those", "it",
    "its", "im", "you", "your", "we", "us", "our", "their", "he", "she", "they"
}

# --- Core Logic Classes ---

class HTMLTextExtractor(HTMLParser):
    """Efficient HTML text stripper using stdlib html.parser."""
    def __init__(self):
        super().__init__()
        self.result = []
        self.ignore = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ('script', 'style', 'nav', 'footer', 'header', 'aside'):
            self.ignore = True

    def handle_endtag(self, tag):
        if tag.lower() in ('script', 'style', 'nav', 'footer', 'header', 'aside'):
            self.ignore = False

    def handle_data(self, data):
        if not self.ignore:
            self.result.append(data)

    def get_text(self) -> str:
        return " ".join(self.result).strip()

class OAuth1Auth:
    """Manual implementation of OAuth 1.0a for requests (Twitter)."""
    def __init__(self, client_key, client_secret, resource_owner_key, resource_owner_secret):
        self.client_key = client_key
        self.client_secret = client_secret
        self.resource_owner_key = resource_owner_key
        self.resource_owner_secret = resource_owner_secret

    def __call__(self, r):
        return r

def generate_oauth1_header(
    url: str,
    method: str,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    params: Optional[Dict] = None
) -> Dict[str, str]:
    """Generates OAuth 1.0a Authorization header."""
    timestamp = str(int(time.time()))
    nonce = hashlib.sha1(os.urandom(24)).hexdigest()
    
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_token": token,
        "oauth_version": "1.0"
    }
    
    if params:
        all_params = {**oauth_params, **params}
    else:
        all_params = oauth_params
        
    encoded_params = []
    for k, v in sorted(all_params.items()):
        encoded_params.append(f"{urllib.parse.quote(k, safe='')}&{urllib.parse.quote(str(v), safe='')}")
    
    param_string = "&".join([p.replace("&", "=") for p in encoded_params])
    base_string = f"{method.upper()}&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(param_string, safe='')}"
    
    signing_key = f"{urllib.parse.quote(consumer_secret, safe='')}&{urllib.parse.quote(token_secret, safe='')}"
    signature = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    encoded_signature = urllib.parse.quote(base64.b64encode(signature).decode(), safe='')
    
    auth_params = oauth_params.copy()
    auth_params["oauth_signature"] = encoded_signature
    
    header_value = "OAuth " + ", ".join([f'{k}="{v}"' for k, v in auth_params.items()])
    return {"Authorization": header_value}

import base64 # Moved here as it's specific to the OAuth logic which is standard lib

def fetch_article_content(url: str) -> str:
    """Fetches URL and cleans HTML."""
    logger.info(f"Fetching content from {url}")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Compatible; ArticleMicroPoster/1.0)"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        parser = HTMLTextExtractor()
        parser.feed(response.text)
        text = parser.get_text()
        
        # Remove excess whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:15000]  # Limit input size for API efficiency roughly
    except Exception as e:
        logger.error(f"Failed to fetch article: {e}")
        raise

def summarize_text(text: str, max_chars: int) -> str:
    """Summarizes text using OpenAI API."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable is not set.")

    logger.info(f"Requesting summary (limit: {max_chars} chars)")
    
    prompt = (
        f"Summarize the following article text into a compelling social media post. "
        f"Strict maximum length is {max_chars} characters. Do not include hashtags. "
        f"Text: {text}"
    )

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200, # Adjust based on max chars
            "temperature": 0.7,
        }
        
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        
        content = resp.json()["choices"][0]["message"]["content"].strip()
        # Truncate again just in case
        return content[:max_chars]
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        raise

def extract_keywords(text: str) -> List[str]:
    """Computes top 5 keywords via Term Frequency (simplified TF-IDF proxy)."""
    # Clean and tokenize
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
    
    # Filter stop words
    filtered = [w for w in words if w not in STOP_WORDS]
    
    # Calculate Frequency
    freq = {}
    for word in filtered:
        freq[word] = freq.get(word, 0) + 1
        
    # Sort by frequency
    sorted_freq = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    
    top_5 = [kw for kw, count in sorted_freq[:5]]
    return top_5

def generate_post(text: str, platform: str) -> str:
    """Orchestrates cleaning, summarizing, and hashtag generation."""
    limit = PLATFORM_LIMITS.get(platform, 280)
    
    summary = summarize_text(text, limit - 30) # Reserve ~30 chars for hashtags
    keywords = extract_keywords(text)
    
    hashtags = " ".join([f"#{kw}" for kw in keywords])
    
    # Combine ensuring length limit
    full_post = f"{summary}\n\n{hashtags}"
    if len(full_post) > limit:
        # Emergency truncate (remove hashtags if space needed, or just slice)
        full_post = full_post[:limit-3] + "..."
        
    return full_post

# --- Platform Handlers ---

class PlatformPoster:
    @staticmethod
    def post_to_twitter(text: str) -> bool:
        api_key = os.getenv("TWITTER_API_KEY")
        api_secret = os.getenv("TWITTER_API_SECRET")
        token = os.getenv("TWITTER_ACCESS_TOKEN")
        token_secret = os.getenv("TWITTER_ACCESS_SECRET")
        
        if not all([api_key, api_secret, token, token_secret]):
            logger.error("Missing Twitter OAuth 1.0a credentials.")
            return False

        url = "https://api.twitter.com/2/tweets"
        
        # Generate Auth Header
        # Note: Twitter API v2 requires OAuth 1.0a for POST with正文
        auth_header = generate_oauth1_header(
            url, "POST", api_key, api_secret, token, token_secret
        )
        
        payload = {"text": text}
        headers = auth_header.copy()
        headers["Content-Type"] = "application/json"
        
        try:
            resp = requests.post(url, json=payload, headers=headers)
            if resp.status_code == 201:
                logger.info("Successfully posted to Twitter.")
                return True
            else:
                logger.error(f"Twitter API Error: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Twitter connection failed: {e}")
            return False

    @staticmethod
    def post_to_mastodon(text: str) -> bool:
        instance = os.getenv("MASTODON_INSTANCE_URL")
        token = os.getenv("MASTODON_ACCESS_TOKEN")
        
        if not instance or not token:
            logger.error("Missing Mastodon credentials.")
            return False
            
        # Ensure URL doesn't end with slash
        instance = instance.rstrip('/')
        url = f"{instance}/api/v1/statuses"
        
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"status": text}
        
        try:
            resp = requests.post(url, data=payload, headers=headers)
            if resp.status_code == 200:
                logger.info("Successfully posted to Mastodon.")
                return True
            else:
                logger.error(f"Mastodon API Error: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Mastodon connection failed: {e}")
            return False

    @staticmethod
    def post_to_linkedin(text: str) -> bool:
        token = os.getenv("LINKEDIN_ACCESS_TOKEN")
        person_urn = os.getenv("LINKEDIN_PERSON_URN")
        
        if not token or not person_urn:
            logger.error("Missing LinkedIn credentials (ACCESS_TOKEN or PERSON_URN).")
            return False
            
        url = "https://api.linkedin.com/v2/ugcPosts"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0"
        }
        
        payload = {
            "author": person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE"
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            }
        }
        
        try:
            resp = requests.post(url, json=payload, headers=headers)
            if resp.status_code == 201:
                logger.info("Successfully posted to LinkedIn.")
                return True
            else:
                logger.error(f"LinkedIn API Error: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            logger.error(f"LinkedIn connection failed: {e}")
            return False

    @staticmethod
    def post_to_facebook(text: str) -> bool:
        page_id = os.getenv("FACEBOOK_PAGE_ID")
        token = os.getenv("FACEBOOK_ACCESS_TOKEN")
        
        if not page_id or not token:
            logger.error("Missing Facebook credentials.")
            return False
            
        url = f"https://graph.facebook.com/{page_id}/feed"
        payload = {"message": text, "access_token": token}
        
        try:
            resp = requests.post(url, data=payload)
            resp_json = resp.json()
            if "id" in resp_json:
                logger.info("Successfully posted to Facebook.")
                return True
            else:
                logger.error(f"Facebook API Error: {resp.status_code} - {resp_json}")
                return False
        except Exception as e:
            logger.error(f"Facebook connection failed: {e}")
            return False

# --- Scheduler Logic ---

def load_schedule() -> List[Dict]:
    if not os.path.exists(SCHEDULE_FILE):
        return []
    try:
        with open(SCHEDULE_FILE, "r") as f:
            return json.load(f)
    except:
        return []

def save_schedule(schedule: List[Dict]):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f, indent=2)

def handle_schedule(text: str, platforms: List[str], schedule_time: str):
    """Saves the post to a persistence file for the 'scheduler'."""
    try:
        # Validate ISO format
        dt = datetime.datetime.fromisoformat(schedule_time)
        if dt < datetime.datetime.now(datetime.timezone.utc):
            logger.error("Scheduled time must be in the future.")
            return False
    except ValueError:
        logger.error("Invalid ISO8601 datetime format.")
        return False

    task = {
        "text": text,
        "platforms": platforms,
        "scheduled_for": schedule_time,
        "status": "pending"
    }
    
    current_sched = load_schedule()
    current_sched.append(task)
    save_schedule(current_sched)
    
    logger.info(f"Post scheduled for {schedule_time} and saved to {SCHEDULE_FILE}.")
    logger.info("(Note: Run this script with '--run-scheduled' flag to execute pending tasks when the time comes.")
    return True

def run_scheduled_posts():
    """Checks for pending posts and executes them if time has arrived."""
    logger.info("Checking for scheduled posts...")
    schedule = load_schedule()
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    
    updated_schedule = []
    
    for task in schedule:
        if task["status"] == "pending":
            try:
                sched_dt = datetime.datetime.fromisoformat(task["scheduled_for"])
                sched_dt = sched_dt.replace(tzinfo=datetime.timezone.utc)
                
                if sched_dt.timestamp() <= now_ts:
                    logger.info(f"Executing scheduled post for {task['platforms']}...")
                    success = True
                    for plat in task["platforms"]:
                        if not post_dispatcher(plat, task["text"]):
                            success = False
                    
                    if success:
                        task["status"] = "completed"
                        logger.info("Scheduled post marked complete.")
                else:
                    updated_schedule.append(task) # Keep pending
            except Exception as e:
                logger.error(f"Error processing scheduled task: {e}")
                updated_schedule.append(task) # Keep on error to retry manually
        elif task["status"] == "pending":
             updated_schedule.append(task)
             
    # Clean up old completed tasks? Let's keep them for history or just overwrite
    # For this script, we rewrite the file removing completed tasks to keep it clean
    save_schedule(updated_schedule)

def post_dispatcher(platform: str, text: str) -> bool:
    if platform == "twitter":
        return PlatformPoster.post_to_twitter(text)
    elif platform == "mastodon":
        return PlatformPoster.post_to_mastodon(text)
    elif platform == "linkedin":
        return PlatformPoster.post_to_linkedin(text)
    elif platform == "facebook":
        return PlatformPoster.post_to_facebook(text)
    else:
        logger.warning(f"Unknown platform: {platform}")
        return False

# --- CLI Entry Point ---

def main():
    parser = argparse.ArgumentParser(description="Auto-generate and post micro-blogs from articles.")
    parser.add_argument("url", nargs="?", help="The URL of the article to process.")
    parser.add_argument("--platforms", type=str, default="twitter", help="Comma-separated list of platforms (twitter,mastodon,linkedin,facebook)")
    parser.add_argument("--dry-run", action="store_true", help="Preview posts without sending them.")
    parser.add_argument("--schedule", type=str, help="Schedule post for future (ISO8601 format, e.g., 2023-12-25T08:30:00)")
    parser.add_argument("--run-scheduled", action="store_true", help="Execute pending scheduled posts.")

    args = parser.parse_args()

    # Handle Scheduler Runner
    if args.run_scheduled:
        run_scheduled_posts()
        sys.exit(0)

    if not args.url:
        parser.print_help()
        sys.exit(1)

    try:
        # 1. Fetch
        raw_text = fetch_article_content(args.url)
        
        # 2. Generate Posts
        target_platforms = [p.strip().lower() for p in args.platforms.split(",")]
        generated_content = {}
        
        for platform in target_platforms:
            post_text = generate_post(raw_text, platform)
            generated_content[platform] = post_text
            
        # 3. Handle Output/Action
        if args.schedule:
            # For scheduling, we generate separate posts or one generic?
            # Prompt implies "queue a future post". We'll store one entry per platform 
            # or a combined entry. Let's store one entry per platform to respect lengths.
            for plat, text in generated_content.items():
                handle_schedule(text, [plat], args.schedule)
        elif args.dry_run:
            print("\n--- DRY RUN PREVIEW ---")
            for plat, text in generated_content.items():
                print(f"\n[{plat.upper()} ({len(text)} chars)]")
                print(text)
                print("-" * 20)
            print("------------------------\n")
        else:
            # Immediate Post
            success_count = 0
            for plat, text in generated_content.items():
                logger.info(f"Posting to {plat}...")
                if post_dispatcher(plat, text):
                    success_count += 1
            
            if success_count == len(target_platforms):
                logger.info("All posts successful.")
            else:
                logger.warning(f"Only {success_count}/{len(target_platforms)} posts succeeded.")
                
    except Exception as e:
        logger.exception("Fatal error in execution.")
        sys.exit(1)

if __name__ == "__main__":
    main()