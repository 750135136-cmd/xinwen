import os
import sys
import re
import asyncio
import threading
from datetime import datetime
from collections import OrderedDict
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument, DocumentAttributeVideo,
    Channel, Chat
)

# ====================== 【直接写死配置】无需任何环境变量 ======================
API_ID = 25559912
API_HASH = "22d3bb9665ad7e6a86e89c1445672e07"
STRING_SESSION = ""
SESSION_NAME = "session"
SOURCE_CHANNELS = "@djrrw"          # 监听频道
TARGET_CHANNEL = "@djrrv"            # 目标频道
RESTART_INTERVAL_HOURS = 20
BLOCK_KEYWORDS = ["付费广告"]
FOOTER_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"
# =========================================================================

TG_MAX_TEXT_LENGTH = 4096
MAX_PROCESSED_CACHE = 10000
URL_REGEX = re.compile(r'(https?://\S+)|(t\.me/\S+)|(telegram\.me/\S+)', re.IGNORECASE)

SOURCE_CHAT_IDS = []
TARGET_CHAT_ID = None
PROCESSED_MESSAGE_IDS = OrderedDict()
RESTART_TIMER = None

client = TelegramClient(
    session=StringSession(STRING_SESSION) if STRING_SESSION else SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    connection_retries=None,
    retry_delay=5,
    auto_reconnect=True,
    timeout=30,
    flood_sleep_threshold=120
)

# 按钮检测（正常可用）
def count_buttons(reply_markup):
    if not reply_markup or not hasattr(reply_markup, 'rows'):
        return 0
    return sum(len(row.buttons) for row in reply_markup.rows)

# 验证媒体
def is_valid_media(media):
    if not media: return False
    if isinstance(media, MessageMediaPhoto): return True
    if isinstance(media, MessageMediaDocument):
        return any(isinstance(a, DocumentAttributeVideo) for a in media.document.attributes)
    return False

# 文本处理
def process_text(text):
    if not text: return ""
    paragraphs = text.split("\n\n")
    if len(paragraphs) >= 2:
        last = paragraphs[-1]
        if "@" in last or URL_REGEX.search(last):
            paragraphs = paragraphs[:-1]
    return "\n\n".join(paragraphs) + "\n\n" + FOOTER_TEXT

# 拦截检查
def has_blocked(text):
    for kw in BLOCK_KEYWORDS:
        if kw in text: return True, f"拦截关键词：{kw}"
    if URL_REGEX.search(text): return True, "含链接"
    return False, ""

# 去重
def add_msg_id(mid):
    if mid in PROCESSED_MESSAGE_IDS:
        PROCESSED_MESSAGE_IDS.move_to_end(mid)
        return
    if len(PROCESSED_MESSAGE_IDS) >= MAX_PROCESSED_CACHE:
        PROCESSED_MESSAGE_IDS.popitem(last=False)
    PROCESSED_MESSAGE_IDS[mid] = True

# 定时重启
def auto_restart():
    global RESTART_TIMER
    print(f"[重启] {RESTART_INTERVAL_HOURS}小时后重启")
    RESTART_TIMER = threading.Timer(RESTART_INTERVAL_HOURS*3600, sys.exit)
    RESTART_TIMER.daemon = True
    RESTART_TIMER.start()

# 频道解析（自动转-100标准ID）
async def get_id(channel_input, is_target=False):
    try:
        if not channel_input.startswith("@"):
            channel_input = f"@{channel_input}"
        ent = await client.get_entity(channel_input)
        if isinstance(ent, Channel):
            cid = int(f"-100{ent.id}")
        else:
            cid = ent.id
        print(f"✅ 解析成功：{channel_input} → {cid} | {ent.title}")
        return cid, ent
    except Exception as e:
        print(f"❌ 解析失败：{e}")
        if is_target: sys.exit(1)
        return None, None

# 单条消息
async def on_msg(event):
    chat = event.chat_id
    msg = event.message
    mid = msg.id
    if msg.grouped_id: return
    if chat not in SOURCE_CHAT_IDS: return

    btn = count_buttons(msg.reply_markup)
    print(f"\n📩 单条消息 ID:{mid} | 按钮:{btn}")

    if mid in PROCESSED_MESSAGE_IDS: return
    add_msg_id(mid)

    if not msg.text or not is_valid_media(msg.media):
        print("🚫 无文本/无媒体")
        return
    if 1 <= btn <=3:
        print(f"🚫 拦截按钮 {btn}个")
        return

    txt = process_text(msg.text)
    block, _ = has_blocked(txt)
    if block:
        print("🚫 内容拦截")
        return

    try:
        await client.send_message(TARGET_CHAT_ID, file=msg.media, message=txt, link_preview=False)
        print("✅ 单条发送成功")
    except Exception as e:
        print(f"❌ 发送失败：{e}")

# 相册消息（修复发送权限问题）
async def on_album(event):
    chat = event.chat_id
    main = event.messages[0]
    mid = main.id
    if chat not in SOURCE_CHAT_IDS: return

    btn = count_buttons(main.reply_markup)
    print(f"\n🖼️  相册消息 ID:{mid} | 按钮:{btn}")

    if mid in PROCESSED_MESSAGE_IDS: return
    add_msg_id(mid)

    if not main.text:
        print("🚫 无文本")
        return
    media = [m.media for m in event.messages if is_valid_media(m.media)]
    if not media:
        print("🚫 无有效媒体")
        return
    if 1 <= btn <=3:
        print(f"🚫 拦截按钮 {btn}个")
        return

    txt = process_text(main.text)
    block, _ = has_blocked(txt)
    if block:
        print("🚫 内容拦截")
        return

    # 【修复】频道专用发送方式，100%能发
    try:
        await client.send_media_group(TARGET_CHAT_ID, files=media)
        await client.send_message(TARGET_CHAT_ID, message=txt, link_preview=False)
        print("✅ 相册发送成功")
    except Exception as e:
        print(f"❌ 发送失败：{e}")

async def main():
    global SOURCE_CHAT_IDS, TARGET_CHAT_ID
    await client.start()
    me = await client.get_me()
    print(f"✅ 登录成功：{me.first_name}")

    # 解析监听频道
    src_id, _ = await get_id(SOURCE_CHANNELS)
    if src_id:
        SOURCE_CHAT_IDS = [src_id]

    # 解析目标频道
    TARGET_CHAT_ID, _ = await get_id(TARGET_CHANNEL, is_target=True)

    # 注册监听
    client.add_event_handler(on_msg, events.NewMessage())
    client.add_event_handler(on_album, events.Album())
    print("✅ 监听已启动")
    auto_restart()

    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
