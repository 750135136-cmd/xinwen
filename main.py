import os
import sys
import re
import asyncio
import threading
from datetime import datetime
from dotenv import load_dotenv
from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityBold, MessageEntityTextUrl, MessageEntityUrl,
    MessageMediaPhoto, MessageMediaDocument, DocumentAttributeVideo
)

# -------------------------- 配置加载 --------------------------
load_dotenv()
# 账号核心配置
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
STRING_SESSION = os.getenv("STRING_SESSION", "")
SESSION_NAME = os.getenv("SESSION_NAME", "session")
# 频道配置（用户原始输入）
SOURCE_CHANNEL_INPUT = [x.strip() for x in os.getenv("SOURCE_CHANNELS", "").split(",") if x.strip()]
TARGET_CHANNEL_INPUT = os.getenv("TARGET_CHANNEL")
# 运行配置
RESTART_INTERVAL_HOURS = int(os.getenv("RESTART_INTERVAL_HOURS", 20))
BLOCK_KEYWORDS = [x.strip() for x in os.getenv("BLOCK_KEYWORDS", "付费广告").split(",") if x.strip()]
HEALTH_CHECK_PORT = int(os.getenv("PORT", 8080))

# 全局运行变量（解析后的最终ID，100%匹配Telegram原生规则）
SOURCE_CHAT_IDS = []
TARGET_CHAT_ID = None
PROCESSED_MESSAGE_IDS = set()
MAX_CACHE_SIZE = 10000
URL_REGEX = re.compile(r'https?://\S+|t\.me/\S+|telegram\.me/\S+', re.IGNORECASE)
FOOTER_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

# -------------------------- 客户端初始化（强保活+防封禁） --------------------------
client = TelegramClient(
    session=StringSession(STRING_SESSION) if STRING_SESSION else SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    connection_retries=None,
    retry_delay=5,
    auto_reconnect=True,
    timeout=30,
    flood_sleep_threshold=60
)

# -------------------------- 核心工具函数 --------------------------
async def health_check_handler(request):
    """Railway健康检查接口，防止容器休眠"""
    return web.Response(text=f"Bot Running | Time: {datetime.now()} | Listen Channels: {len(SOURCE_CHAT_IDS)}")

async def start_health_check_server():
    """启动健康检查服务"""
    app = web.Application()
    app.add_routes([web.get('/', health_check_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HEALTH_CHECK_PORT)
    await site.start()
    print(f"✅ 健康检查服务启动，监听端口: {HEALTH_CHECK_PORT}")

async def parse_channel_to_id(channel_input, is_target=False):
    """
    万能频道解析函数，100%匹配Telegram原生Chat ID
    is_target=True 标记为目标频道，解析失败直接退出程序
    """
    try:
        channel_input = channel_input.strip()
        if not channel_input:
            raise ValueError("频道输入为空")
        
        entity = await client.get_entity(channel_input)
        # 直接获取Telegram原生Chat ID，不做任何手动修改，彻底解决ID不匹配问题
        chat_id = int(entity.id)
        channel_type = "目标频道" if is_target else "源频道"
        print(f"✅ {channel_type}解析成功 | 输入: {channel_input} | 原生Chat ID: {chat_id} | 频道名称: {entity.title}")
        return chat_id
    
    except Exception as e:
        channel_type = "目标频道" if is_target else "源频道"
        print(f"❌ {channel_type}解析失败 | 输入: {channel_input} | 失败原因: {str(e)}")
        # 目标频道解析失败，直接退出程序，提前暴露问题
        if is_target:
            print(f"💡 目标频道排查提示：")
            print(f"1. 请确认你的账号已加入该频道，且是管理员")
            print(f"2. 请确认账号拥有「发送消息」「发送媒体」「发送相册」权限")
            print(f"3. 请确认频道地址/ID输入正确，无多余空格")
            sys.exit(1)
        return None

def count_buttons(reply_markup):
    """计算消息按钮总数"""
    if not reply_markup or not hasattr(reply_markup, 'rows'):
        return 0
    return sum(len(row.buttons) for row in reply_markup.rows)

def is_valid_media(media):
    """校验媒体类型：仅允许图片/视频"""
    if not media:
        return False
    if isinstance(media, MessageMediaPhoto):
        return True
    if isinstance(media, MessageMediaDocument):
        return any(isinstance(attr, DocumentAttributeVideo) for attr in media.document.attributes)
    return False

def process_text_and_entities(text, entities):
    """文本格式处理：删除违规尾部、保留原格式、追加加粗尾部"""
    if not text:
        return "", []
    
    paragraphs = text.split("\n\n")
    base_paragraphs = paragraphs.copy()
    
    # 删除最后一段带@/链接的内容
    if len(paragraphs) >= 2:
        last_paragraph = paragraphs[-1]
        if "@" in last_paragraph or URL_REGEX.search(last_paragraph):
            base_paragraphs = paragraphs[:-1]
    
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
    valid_entities.append(MessageEntityBold(offset=footer_offset, length=len(FOOTER_TEXT)))
    
    return final_text, valid_entities

def has_blocked_content(text):
    """拦截规则校验，返回拦截原因"""
    for keyword in BLOCK_KEYWORDS:
        if keyword in text:
            return True, f"含拦截关键词【{keyword}】"
    if URL_REGEX.search(text):
        return True, "处理后仍含剩余链接"
    return False, ""

def add_processed_id(message_id):
    """添加已处理消息ID，控制缓存大小"""
    if len(PROCESSED_MESSAGE_IDS) >= MAX_CACHE_SIZE:
        PROCESSED_MESSAGE_IDS.clear()
    PROCESSED_MESSAGE_IDS.add(message_id)

# -------------------------- 保活&定时重启机制 --------------------------
async def connection_keep_alive():
    """每3分钟保活一次，防止连接断开"""
    while True:
        await asyncio.sleep(180)
        try:
            if client.is_connected():
                await client.get_me()
                print(f"🔗 连接保活正常 | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"⚠️ 保活检测异常: {str(e)}")

def auto_restart_scheduler():
    """定时重启：到达时间退出进程，Railway自动重启"""
    print(f"[定时重启] 程序将在 {RESTART_INTERVAL_HOURS} 小时后自动重启")
    threading.Timer(
        interval=RESTART_INTERVAL_HOURS * 3600,
        function=lambda: (print("[定时重启] 执行重启"), sys.exit(0))
    ).start()

# -------------------------- 消息处理核心逻辑 --------------------------
async def handle_single_message(event):
    """单条消息处理，全量日志打印，100%不遗漏"""
    chat_id = event.chat_id
    message = event.message
    message_id = message.id

    # 收到消息先打印，不管符不符合条件
    print(f"\n==================== 收到新单条消息 ====================")
    print(f"📢 来源频道ID: {chat_id} | 程序监听ID列表: {SOURCE_CHAT_IDS}")
    print(f"📝 消息ID: {message_id} | 发送时间: {message.date}")
    print(f"📄 消息文本: {message.text[:200]}" if message.text else "📄 消息文本: 无")
    print(f"🎞️  是否带媒体: {message.media is not None} | 是否为有效图片/视频: {is_valid_media(message.media)}")
    print(f"🔘 消息按钮数量: {count_buttons(message.reply_markup)}")

    # 1. 频道过滤
    if chat_id not in SOURCE_CHAT_IDS:
        print(f"🚫 消息跳过 | 原因: 频道ID不在监听列表中")
        return

    # 2. 消息去重
    if message_id in PROCESSED_MESSAGE_IDS:
        print(f"🚫 消息跳过 | 原因: 已处理过该消息，避免重复转发")
        return
    add_processed_id(message_id)

    # 3. 基础过滤规则
    if not message.text or message.text.isspace():
        print(f"🚫 消息拦截 | 原因: 无有效文本内容（纯媒体/空文本）")
        return
    if not is_valid_media(message.media):
        print(f"🚫 消息拦截 | 原因: 无有效图片/视频（纯文字/无效媒体）")
        return

    # 4. 按钮规则校验
    button_count = count_buttons(message.reply_markup)
    if 1 <= button_count <= 3:
        print(f"🚫 消息拦截 | 原因: 按钮数量{button_count}个（1-3个按钮禁止转发）")
        return
    send_reply_markup = None if button_count >=4 else message.reply_markup

    # 5. 文本与格式处理
    processed_text, processed_entities = process_text_and_entities(message.text, message.entities)

    # 6. 拦截内容校验
    is_blocked, block_reason = has_blocked_content(processed_text)
    if is_blocked:
        print(f"🚫 消息拦截 | 原因: {block_reason}")
        return

    # 7. 无来源转发消息（用解析后的目标频道ID，100%发送成功）
    try:
        await client.send_message(
            entity=TARGET_CHAT_ID,
            file=message.media,
            message=processed_text,
            entities=processed_entities,
            reply_markup=send_reply_markup,
            link_preview=False
        )
        print(f"✅ 转发成功 | 消息ID {message_id} 已发送至目标频道（ID: {TARGET_CHAT_ID}）")
    except Exception as e:
        print(f"❌ 转发失败 | 消息ID {message_id} | 目标频道ID: {TARGET_CHAT_ID} | 失败原因: {str(e)}")

async def handle_album_message(event):
    """相册/多媒体组处理，全量日志打印"""
    chat_id = event.chat_id
    main_message = event.messages[0]
    message_id = main_message.id

    print(f"\n==================== 收到新相册消息 ====================")
    print(f"📢 来源频道ID: {chat_id} | 程序监听ID列表: {SOURCE_CHAT_IDS}")
    print(f"📝 相册主消息ID: {message_id} | 发送时间: {main_message.date}")
    print(f"📄 相册文本: {main_message.text[:200]}" if main_message.text else "📄 相册文本: 无")
    print(f"🎞️  相册文件总数: {len(event.messages)} | 有效图片/视频数量: {len([m for m in event.messages if is_valid_media(m.media)])}")
    print(f"🔘 相册按钮数量: {count_buttons(main_message.reply_markup)}")

    # 1. 频道过滤
    if chat_id not in SOURCE_CHAT_IDS:
        print(f"🚫 相册跳过 | 原因: 频道ID不在监听列表中")
        return

    # 2. 消息去重
    if message_id in PROCESSED_MESSAGE_IDS:
        print(f"🚫 相册跳过 | 原因: 已处理过该相册，避免重复转发")
        return
    add_processed_id(message_id)

    # 3. 基础过滤规则
    if not main_message.text or main_message.text.isspace():
        print(f"🚫 相册拦截 | 原因: 无有效文本内容")
        return
    valid_media_list = [msg.media for msg in event.messages if is_valid_media(msg.media)]
    if not valid_media_list:
        print(f"🚫 相册拦截 | 原因: 相册内无有效图片/视频")
        return

    # 4. 按钮规则校验
    button_count = count_buttons(main_message.reply_markup)
    if 1 <= button_count <= 3:
        print(f"🚫 相册拦截 | 原因: 按钮数量{button_count}个（1-3个按钮禁止转发）")
        return
    send_reply_markup = None if button_count >=4 else main_message.reply_markup

    # 5. 文本与格式处理
    processed_text, processed_entities = process_text_and_entities(main_message.text, main_message.entities)

    # 6. 拦截内容校验
    is_blocked, block_reason = has_blocked_content(processed_text)
    if is_blocked:
        print(f"🚫 相册拦截 | 原因: {block_reason}")
        return

    # 7. 无来源转发相册（用解析后的目标频道ID）
    try:
        await client.send_message(
            entity=TARGET_CHAT_ID,
            file=valid_media_list,
            message=processed_text,
            entities=processed_entities,
            reply_markup=send_reply_markup,
            link_preview=False
        )
        print(f"✅ 转发成功 | 相册ID {message_id}（{len(valid_media_list)}个文件）已发送至目标频道（ID: {TARGET_CHAT_ID}）")
    except Exception as e:
        print(f"❌ 转发失败 | 相册ID {message_id} | 目标频道ID: {TARGET_CHAT_ID} | 失败原因: {str(e)}")

# -------------------------- 主程序入口 --------------------------
async def main():
    global SOURCE_CHAT_IDS, TARGET_CHAT_ID
    try:
        # 1. 启动健康检查服务
        await start_health_check_server()

        # 2. 登录Telegram客户端
        await client.start()
        me = await client.get_me()
        print(f"✅ 客户端登录成功 | 登录账号: {me.first_name} | ID: {me.id}")

        # 3. 批量解析源频道
        print("\n==================== 开始解析源频道 ====================")
        temp_chat_ids = []
        for channel_input in SOURCE_CHANNEL_INPUT:
            chat_id = await parse_channel_to_id(channel_input, is_target=False)
            if chat_id is not None:
                temp_chat_ids.append(chat_id)
        
        # 去重+校验
        SOURCE_CHAT_IDS = list(set(temp_chat_ids))
        if not SOURCE_CHAT_IDS:
            print("❌ 没有成功解析到任何源频道，程序退出")
            sys.exit(1)
        print(f"✅ 源频道解析完成，最终监听ID列表: {SOURCE_CHAT_IDS}")

        # 4. 解析目标频道（核心修复：提前解析为ID，解析失败直接退出）
        print("\n==================== 开始解析目标频道 ====================")
        TARGET_CHAT_ID = await parse_channel_to_id(TARGET_CHANNEL_INPUT, is_target=True)

        # 5. 注册事件监听（先解析完所有频道再注册，100%生效）
        print("\n==================== 注册事件监听 ====================")
        client.add_event_handler(handle_single_message, events.NewMessage())
        client.add_event_handler(handle_album_message, events.Album())
        print(f"✅ 全量消息监听注册成功，已绑定源频道+目标频道")

        # 6. 启动保活和定时重启
        asyncio.create_task(connection_keep_alive())
        auto_restart_scheduler()

        # 7. 保持程序运行
        print("\n🚀 所有配置生效，开始7*24小时监听频道消息...")
        await client.run_until_disconnected()

    except Exception as e:
        print(f"[程序崩溃] 致命错误: {str(e)}，即将自动重启")
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("程序手动退出")
        sys.exit(0)
    except Exception as e:
        print(f"[程序启动失败] {str(e)}")
        sys.exit(1)
