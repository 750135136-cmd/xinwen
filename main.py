import os
import sys
import re
import asyncio
import threading
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityBold, MessageEntityTextUrl, MessageEntityUrl,
    MessageMediaPhoto, MessageMediaDocument, DocumentAttributeVideo
)

# -------------------------- 加载环境变量 --------------------------
load_dotenv()

# 正确的环境变量读取逻辑：os.getenv("变量名", 默认值)
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
STRING_SESSION = os.getenv("STRING_SESSION", "")
SESSION_NAME = os.getenv("SESSION_NAME", "session")
# 源频道：环境变量里填写，多个用英文逗号分隔，支持@用户名/链接/数字ID
SOURCE_CHANNEL_INPUT = os.getenv("SOURCE_CHANNELS", "").split(",")
# 目标频道：环境变量里填写
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")
# 重启间隔：默认20小时
RESTART_INTERVAL_HOURS = int(os.getenv("RESTART_INTERVAL_HOURS", 20))
# 拦截关键词：默认"付费广告"，多个用英文逗号分隔
BLOCK_KEYWORDS = os.getenv("BLOCK_KEYWORDS", "付费广告").split(",")

# 全局变量：解析后的源频道ID列表
SOURCE_CHAT_IDS = []
# 链接检测正则
URL_REGEX = re.compile(r'https?://\S+|t\.me/\S+|telegram\.me/\S+', re.IGNORECASE)
# 尾部固定文案
FOOTER_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

# -------------------------- 初始化客户端 --------------------------
if STRING_SESSION:
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# -------------------------- 核心工具函数 --------------------------
async def parse_channel_to_id(channel_input):
    """万能频道解析：支持所有格式的频道地址，自动转为ID"""
    try:
        channel_input = channel_input.strip()
        if not channel_input:
            return None
        
        entity = await client.get_entity(channel_input)
        chat_id = entity.id
        print(f"✅ 频道解析成功 | 输入: {channel_input} | ID: {chat_id} | 名称: {entity.title}")
        return chat_id
    
    except Exception as e:
        print(f"❌ 频道解析失败 | 输入: {channel_input} | 原因: {str(e)}")
        print(f"💡 排查：请确认账号已加入该频道，输入地址/ID正确")
        return None

def count_buttons(reply_markup):
    """计算消息按钮总数"""
    if not reply_markup or not hasattr(reply_markup, 'rows'):
        return 0
    total = 0
    for row in reply_markup.rows:
        total += len(row.buttons)
    return total

def is_valid_media(media):
    """校验媒体类型：仅允许图片/视频"""
    if not media:
        return False
    if isinstance(media, MessageMediaPhoto):
        return True
    if isinstance(media, MessageMediaDocument):
        for attr in media.document.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return True
    return False

def process_text_and_entities(text, entities):
    """处理文本和格式：删除违规尾部、保留原格式、追加加粗尾部"""
    if not text:
        return "", []
    
    # 拆分段落，处理尾部
    paragraphs = text.split("\n\n")
    base_paragraphs = paragraphs.copy()
    
    # 删除最后一段带@/链接的内容
    if len(paragraphs) >= 2:
        last_paragraph = paragraphs[-1]
        if "@" in last_paragraph or URL_REGEX.search(last_paragraph):
            base_paragraphs = paragraphs[:-1]
    
    # 拼接基础文本
    base_text = "\n\n".join(base_paragraphs)
    base_length = len(base_text)
    
    # 保留原格式实体
    valid_entities = []
    if entities:
        for ent in entities:
            if ent.offset + ent.length <= base_length:
                valid_entities.append(ent)
    
    # 追加加粗尾部
    final_text = f"{base_text}\n\n{FOOTER_TEXT}"
    footer_offset = base_length + 2
    footer_entity = MessageEntityBold(offset=footer_offset, length=len(FOOTER_TEXT))
    valid_entities.append(footer_entity)
    
    return final_text, valid_entities

def has_blocked_content(text):
    """检查拦截内容：关键词、剩余链接"""
    for keyword in BLOCK_KEYWORDS:
        if keyword.strip() and keyword.strip() in text:
            return True
    if URL_REGEX.search(text):
        return True
    return False

# -------------------------- 定时重启函数 --------------------------
def auto_restart_scheduler():
    """定时重启，Railway会自动重启退出的容器"""
    print(f"[定时重启] 程序将在 {RESTART_INTERVAL_HOURS} 小时后自动重启")
    restart_timer = threading.Timer(
        interval=RESTART_INTERVAL_HOURS * 3600,
        function=lambda: (print("[定时重启] 执行重启"), sys.exit(0))
    )
    restart_timer.daemon = True
    restart_timer.start()

# -------------------------- 消息事件处理 --------------------------
# 单条带媒体+文字的消息
@client.on(events.NewMessage())
async def handle_single_message(event):
    if event.chat_id not in SOURCE_CHAT_IDS:
        return
    
    message = event.message
    try:
        # 基础过滤：仅带有效媒体+非空文字的消息
        if not message.text or message.text.isspace():
            return
        if not is_valid_media(message.media):
            return

        # 按钮规则校验
        button_count = count_buttons(message.reply_markup)
        if 1 <= button_count <= 3:
            print(f"[拦截] 消息含{button_count}个按钮，符合拦截规则")
            return
        send_reply_markup = None if button_count >=4 else message.reply_markup

        # 文本格式处理
        processed_text, processed_entities = process_text_and_entities(message.text, message.entities)

        # 拦截内容校验
        if has_blocked_content(processed_text):
            print(f"[拦截] 消息含拦截关键词或剩余链接")
            return

        # 无来源转发
        await client.send_message(
            entity=TARGET_CHANNEL,
            file=message.media,
            message=processed_text,
            entities=processed_entities,
            reply_markup=send_reply_markup,
            link_preview=False
        )
        print(f"[转发成功] 单条媒体消息已转发")

    except Exception as e:
        print(f"[单条消息处理错误] {str(e)}")

# 多媒体组/相册
@client.on(events.Album())
async def handle_album_message(event):
    if event.chat_id not in SOURCE_CHAT_IDS:
        return
    
    album = event
    try:
        main_message = album.messages[0]
        # 基础过滤：仅带非空文字的相册
        if not main_message.text or main_message.text.isspace():
            return
        # 校验媒体有效性
        valid_media_list = []
        for msg in album.messages:
            if is_valid_media(msg.media):
                valid_media_list.append(msg.media)
        if not valid_media_list:
            return

        # 按钮规则校验
        button_count = count_buttons(main_message.reply_markup)
        if 1 <= button_count <= 3:
            print(f"[拦截] 相册含{button_count}个按钮，符合拦截规则")
            return
        send_reply_markup = None if button_count >=4 else main_message.reply_markup

        # 文本格式处理
        processed_text, processed_entities = process_text_and_entities(main_message.text, main_message.entities)

        # 拦截内容校验
        if has_blocked_content(processed_text):
            print(f"[拦截] 相册含拦截关键词或剩余链接")
            return

        # 无来源转发相册
        await client.send_message(
            entity=TARGET_CHANNEL,
            file=valid_media_list,
            message=processed_text,
            entities=processed_entities,
            reply_markup=send_reply_markup,
            link_preview=False
        )
        print(f"[转发成功] 多媒体组（{len(valid_media_list)}个文件）已转发")

    except Exception as e:
        print(f"[相册处理错误] {str(e)}")

# -------------------------- 主程序入口 --------------------------
async def main():
    global SOURCE_CHAT_IDS
    # 登录客户端
    await client.start()
    print("✅ 客户端登录成功，开始解析源频道...")

    # 批量解析源频道
    temp_chat_ids = []
    for channel_input in SOURCE_CHANNEL_INPUT:
        chat_id = await parse_channel_to_id(channel_input)
        if chat_id is not None:
            temp_chat_ids.append(chat_id)
    
    # 去重+校验
    SOURCE_CHAT_IDS = list(set(temp_chat_ids))
    if not SOURCE_CHAT_IDS:
        print("❌ 没有成功解析到任何源频道，程序退出")
        sys.exit(1)
    print(f"✅ 源频道解析完成，共监听 {len(SOURCE_CHAT_IDS)} 个频道")

    # 解析目标频道
    try:
        target_entity = await client.get_entity(TARGET_CHANNEL)
        print(f"✅ 目标频道解析成功 | 名称: {target_entity.title} | ID: {target_entity.id}")
    except Exception as e:
        print(f"❌ 目标频道解析失败 | 原因: {str(e)}")
        print(f"💡 排查：请确认账号是目标频道管理员，拥有发送消息/媒体权限")
        sys.exit(1)

    # 启动定时重启
    auto_restart_scheduler()

    # 持续监听
    print("🚀 所有配置生效，开始监听频道消息...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("程序手动退出")
        sys.exit(0)
    except Exception as e:
        print(f"[程序崩溃] {str(e)}，即将自动重启")
        sys.exit(1)
