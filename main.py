import os
import sys
import re
import asyncio
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityBold, MessageEntityTextUrl, MessageEntityUrl,
    MessageMediaPhoto, MessageMediaDocument, DocumentAttributeVideo
)

# -------------------------- 基础配置加载 --------------------------
load_dotenv()
# 核心账号配置
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
STRING_SESSION = os.getenv("STRING_SESSION", "")
SESSION_NAME = os.getenv("SESSION_NAME", "session")
# 频道配置
SOURCE_CHANNEL_INPUT = os.getenv("SOURCE_CHANNELS", "").split(",")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")
# 运行配置
RESTART_INTERVAL_HOURS = int(os.getenv("RESTART_INTERVAL_HOURS", 20))
BLOCK_KEYWORDS = os.getenv("BLOCK_KEYWORDS", "付费广告").split(",")
HEALTH_CHECK_PORT = int(os.getenv("PORT", 8080))  # Railway自动分配端口

# 全局运行变量
SOURCE_CHAT_IDS = []  # 解析后的源频道ID列表
PROCESSED_MESSAGE_IDS = set()  # 消息去重集合，避免重复转发
MAX_CACHE_SIZE = 10000  # 去重缓存最大容量
# 正则规则
URL_REGEX = re.compile(r'https?://\S+|t\.me/\S+|telegram\.me/\S+', re.IGNORECASE)
# 固定尾部文案
FOOTER_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

# -------------------------- 客户端初始化 --------------------------
if STRING_SESSION:
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH,
                            connection_retries=None,  # 无限重连
                            retry_delay=5,  # 重连间隔5秒
                            auto_reconnect=True)
else:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH,
                            connection_retries=None,
                            retry_delay=5,
                            auto_reconnect=True)

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

async def parse_channel_to_id(channel_input):
    """万能频道解析，兼容私有/典藏频道"""
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
        print(f"💡 排查：账号必须已加入该频道，输入地址/ID正确")
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
    footer_entity = MessageEntityBold(offset=footer_offset, length=len(FOOTER_TEXT))
    valid_entities.append(footer_entity)
    
    return final_text, valid_entities

def has_blocked_content(text):
    """拦截规则校验，返回拦截原因"""
    for keyword in BLOCK_KEYWORDS:
        if keyword.strip() and keyword.strip() in text:
            return True, f"含拦截关键词【{keyword}】"
    if URL_REGEX.search(text):
        return True, "处理后仍含链接"
    return False, ""

def add_processed_id(message_id):
    """添加已处理消息ID，控制缓存大小"""
    if len(PROCESSED_MESSAGE_IDS) >= MAX_CACHE_SIZE:
        PROCESSED_MESSAGE_IDS.clear()
    PROCESSED_MESSAGE_IDS.add(message_id)

# -------------------------- 保活&定时重启机制 --------------------------
async def connection_keep_alive():
    """连接保活：每5分钟向Telegram发送ping，防止连接断开"""
    while True:
        await asyncio.sleep(300)
        try:
            if client.is_connected():
                await client.get_me()
                print(f"🔗 连接保活正常 | 时间: {datetime.now()}")
        except Exception as e:
            print(f"⚠️ 保活检测异常: {str(e)}")

def auto_restart_scheduler():
    """定时重启：到达时间退出进程，Railway自动重启"""
    print(f"[定时重启] 程序将在 {RESTART_INTERVAL_HOURS} 小时后自动重启")
    restart_timer = threading.Timer(
        interval=RESTART_INTERVAL_HOURS * 3600,
        function=lambda: (print("[定时重启] 执行重启"), sys.exit(0))
    )
    restart_timer.daemon = True
    restart_timer.start()

# -------------------------- 消息处理核心逻辑 --------------------------
async def handle_message(event, is_album=False):
    """统一消息处理逻辑，兼容单条消息和相册"""
    # 基础信息提取
    chat_id = event.chat_id
    message = event.message if not is_album else event.messages[0]
    message_id = message.id
    message_time = message.date

    # 1. 消息去重
    if message_id in PROCESSED_MESSAGE_IDS:
        return
    add_processed_id(message_id)

    # 2. 打印收到消息的全量日志（核心！帮你定位问题）
    print(f"\n==================== 收到新消息 ====================")
    print(f"📢 来源频道ID: {chat_id} | 消息ID: {message_id} | 时间: {message_time}")
    print(f"📝 是否带文本: {'是' if message.text else '否'} | 文本长度: {len(message.text) if message.text else 0}")
    print(f"🎞️  是否带有效媒体: {'是' if is_album else is_valid_media(message.media)}")
    print(f"🔘 按钮数量: {count_buttons(message.reply_markup)}")

    # 3. 基础过滤规则
    # 3.1 无文本直接拦截
    if not message.text or message.text.isspace():
        print(f"🚫 拦截消息ID {message_id} | 原因: 纯媒体无文本/空文本")
        return
    # 3.2 无有效媒体直接拦截
    if not is_album and not is_valid_media(message.media):
        print(f"🚫 拦截消息ID {message_id} | 原因: 纯文字无媒体/无效媒体")
        return
    # 3.3 相册媒体校验
    valid_media_list = []
    if is_album:
        for msg in event.messages:
            if is_valid_media(msg.media):
                valid_media_list.append(msg.media)
        if not valid_media_list:
            print(f"🚫 拦截消息ID {message_id} | 原因: 相册内无有效图片/视频")
            return

    # 4. 按钮规则校验
    button_count = count_buttons(message.reply_markup)
    if 1 <= button_count <= 3:
        print(f"🚫 拦截消息ID {message_id} | 原因: 按钮数量{button_count}个（1-3个按钮拦截）")
        return
    send_reply_markup = None if button_count >=4 else message.reply_markup

    # 5. 文本与格式处理
    processed_text, processed_entities = process_text_and_entities(message.text, message.entities)

    # 6. 拦截内容校验
    is_blocked, block_reason = has_blocked_content(processed_text)
    if is_blocked:
        print(f"🚫 拦截消息ID {message_id} | 原因: {block_reason}")
        return

    # 7. 无来源转发消息
    try:
        send_file = valid_media_list if is_album else message.media
        await client.send_message(
            entity=TARGET_CHANNEL,
            file=send_file,
            message=processed_text,
            entities=processed_entities,
            reply_markup=send_reply_markup,
            link_preview=False
        )
        print(f"✅ 转发成功 | 消息ID {message_id} | 已发送至目标频道")
    except Exception as e:
        print(f"❌ 转发失败 | 消息ID {message_id} | 原因: {str(e)}")

# -------------------------- 主程序入口 --------------------------
async def main():
    global SOURCE_CHAT_IDS
    try:
        # 1. 启动健康检查服务（防止Railway休眠）
        await start_health_check_server()

        # 2. 登录Telegram客户端
        await client.start()
        me = await client.get_me()
        print(f"✅ 客户端登录成功 | 登录账号: {me.first_name} | ID: {me.id}")

        # 3. 批量解析源频道
        print("\n==================== 开始解析源频道 ====================")
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

        # 4. 校验目标频道
        print("\n==================== 校验目标频道 ====================")
        try:
            target_entity = await client.get_entity(TARGET_CHANNEL)
            print(f"✅ 目标频道解析成功 | 名称: {target_entity.title} | ID: {target_entity.id}")
        except Exception as e:
            print(f"❌ 目标频道解析失败 | 原因: {str(e)}")
            print(f"💡 排查：账号必须是目标频道管理员，拥有发送消息/媒体权限")
            sys.exit(1)

        # 5. 【核心修复】动态注册事件监听（解析完频道后再注册！）
        print("\n==================== 注册事件监听 ====================")
        # 注册单条消息事件
        client.add_event_handler(
            callback=lambda e: handle_message(e, is_album=False),
            event=events.NewMessage(chats=SOURCE_CHAT_IDS)
        )
        # 注册相册/多媒体组事件
        client.add_event_handler(
            callback=lambda e: handle_message(e, is_album=True),
            event=events.Album(chats=SOURCE_CHAT_IDS)
        )
        print(f"✅ 事件监听注册成功，已订阅 {len(SOURCE_CHAT_IDS)} 个频道的消息推送")

        # 6. 启动保活任务
        asyncio.create_task(connection_keep_alive())

        # 7. 启动定时重启
        auto_restart_scheduler()

        # 8. 保持程序运行
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
        print(f"[程序崩溃] 启动失败: {str(e)}，即将自动重启")
        sys.exit(1)
