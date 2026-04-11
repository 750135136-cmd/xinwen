import os
import sys
import re
import asyncio
import threading
from datetime import datetime
from collections import OrderedDict
from dotenv import load_dotenv
from aiohttp import web
# Telethon核心导入
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    # 全量富文本格式实体
    MessageEntityBold, MessageEntityItalic, MessageEntityUnderline,
    MessageEntityStrike, MessageEntityCode, MessageEntityPre,
    MessageEntityBlockquote, MessageEntityTextUrl, MessageEntityUrl,
    # 媒体类型
    MessageMediaPhoto, MessageMediaDocument, DocumentAttributeVideo,
    # 频道与权限类型
    Channel, Chat, ChatAdminRights
)

# -------------------------- 内置UTF-16代理对处理函数 --------------------------
def add_surrogate(text: str) -> str:
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")

def remove_surrogate(text: str) -> str:
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")

# -------------------------- 全局常量配置 --------------------------
TG_MAX_TEXT_LENGTH = 4096
TG_MAX_MEDIA_SIZE_MB = 2000
MAX_PROCESSED_CACHE = 10000
URL_REGEX = re.compile(
    r'(https?://[^\s]+)|(t\.me/[^\s]+)|(telegram\.me/[^\s]+)',
    re.IGNORECASE
)
FOOTER_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

# -------------------------- 环境变量加载与前置校验 --------------------------
load_dotenv()
ENV_CONFIG = {
    "API_ID": os.getenv("API_ID"),
    "API_HASH": os.getenv("API_HASH"),
    "STRING_SESSION": os.getenv("STRING_SESSION", ""),
    "SESSION_NAME": os.getenv("SESSION_NAME", "session"),
    "SOURCE_CHANNELS": os.getenv("SOURCE_CHANNELS", ""),
    "TARGET_CHANNEL": os.getenv("TARGET_CHANNEL"),
    "RESTART_INTERVAL_HOURS": int(os.getenv("RESTART_INTERVAL_HOURS", 20)),
    "BLOCK_KEYWORDS": os.getenv("BLOCK_KEYWORDS", "付费广告"),
    "HEALTH_CHECK_PORT": int(os.getenv("PORT", 8080))
}

def pre_check_env():
    required_fields = ["API_ID", "API_HASH", "SOURCE_CHANNELS", "TARGET_CHANNEL"]
    missing_fields = [field for field in required_fields if not ENV_CONFIG[field]]
    if missing_fields:
        print(f"❌ 启动失败！缺失必填环境变量: {', '.join(missing_fields)}")
        sys.exit(1)
    if not ENV_CONFIG["API_ID"].isdigit():
        print(f"❌ 启动失败！API_ID必须为纯数字")
        sys.exit(1)
    if not ENV_CONFIG["SOURCE_CHANNELS"].strip():
        print(f"❌ 启动失败！SOURCE_CHANNELS不能为空")
        sys.exit(1)
    print("✅ 环境变量前置校验通过")

pre_check_env()

# 环境变量格式化
API_ID = int(ENV_CONFIG["API_ID"])
API_HASH = ENV_CONFIG["API_HASH"]
STRING_SESSION = ENV_CONFIG["STRING_SESSION"]
SESSION_NAME = ENV_CONFIG["SESSION_NAME"]
SOURCE_CHANNEL_INPUT = [x.strip() for x in ENV_CONFIG["SOURCE_CHANNELS"].split(",") if x.strip()]
TARGET_CHANNEL_INPUT = ENV_CONFIG["TARGET_CHANNEL"]
RESTART_INTERVAL_HOURS = ENV_CONFIG["RESTART_INTERVAL_HOURS"]
BLOCK_KEYWORDS = [x.strip() for x in ENV_CONFIG["BLOCK_KEYWORDS"].split(",") if x.strip()]
HEALTH_CHECK_PORT = ENV_CONFIG["HEALTH_CHECK_PORT"]

# -------------------------- 全局运行变量 --------------------------
SOURCE_CHAT_IDS = []
TARGET_CHAT_ID = None
PROCESSED_MESSAGE_IDS = OrderedDict()
RESTART_TIMER = None

# -------------------------- Telegram客户端初始化 --------------------------
client = TelegramClient(
    session=StringSession(STRING_SESSION) if STRING_SESSION else SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    connection_retries=None,
    retry_delay=5,
    auto_reconnect=True,
    timeout=30,
    flood_sleep_threshold=120,
    device_model="Windows 11",
    system_version="10.0.22631",
    app_version="5.10.0",
    lang_code="zh-CN",
    system_lang_code="zh-CN"
)

# -------------------------- 核心工具函数 --------------------------
async def health_check_handler(request):
    return web.Response(
        text=f"Bot Running | Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Listen Channels: {len(SOURCE_CHAT_IDS)}",
        content_type="text/plain; charset=utf-8"
    )

async def start_health_check_server():
    app = web.Application()
    app.add_routes([web.get('/', health_check_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HEALTH_CHECK_PORT)
    await site.start()
    print(f"✅ 健康检查服务启动，监听端口: {HEALTH_CHECK_PORT}")

async def parse_channel_to_id(channel_input, is_target=False):
    try:
        channel_input = channel_input.strip()
        if not channel_input:
            raise ValueError("频道输入为空")
        
        if not (
            channel_input.startswith("-100")
            or channel_input.isdigit()
            or channel_input.startswith(("http://", "https://", "t.me/"))
        ):
            if not channel_input.startswith("@"):
                channel_input = f"@{channel_input}"
        
        entity = await client.get_entity(channel_input)
        channel_type = "目标频道" if is_target else "源频道"

        if isinstance(entity, Channel):
            raw_id = str(entity.id).replace("-100", "")
            standard_chat_id = int(f"-100{raw_id}")
        else:
            standard_chat_id = int(entity.id)
        
        print(f"✅ {channel_type}解析成功 | 输入: {channel_input} | 标准Chat ID: {standard_chat_id} | 频道名称: {entity.title}")
        return standard_chat_id, entity
    
    except Exception as e:
        channel_type = "目标频道" if is_target else "源频道"
        print(f"❌ {channel_type}解析失败 | 输入: {channel_input} | 失败原因: {str(e)}")
        if is_target:
            print(f"💡 目标频道排查提示：")
            print(f"1. 请确认你的账号已加入该频道，且是管理员")
            print(f"2. 请确认频道地址/ID输入正确，无多余空格")
            sys.exit(1)
        return None, None

def count_buttons(reply_markup):
    if not reply_markup or not hasattr(reply_markup, 'rows'):
        return 0
    if not hasattr(reply_markup, 'inline') or not reply_markup.inline:
        return 0
    return sum(len(row.buttons) for row in reply_markup.rows)

def is_valid_media(media):
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
    if not text:
        return "", []
    
    surrogate_text = add_surrogate(text)
    paragraphs = surrogate_text.split("\n\n")
    base_paragraphs = paragraphs.copy()
    
    if len(paragraphs) >= 2:
        last_paragraph = paragraphs[-1]
        if "@" in last_paragraph or URL_REGEX.search(last_paragraph):
            base_paragraphs = paragraphs[:-1]
    
    base_surrogate = "\n\n".join(base_paragraphs)
    base_text = remove_surrogate(base_surrogate)
    base_length = len(base_surrogate)
    
    valid_entities = []
    if entities:
        for ent in entities:
            if ent.offset + ent.length <= base_length:
                valid_entities.append(ent)
    
    final_surrogate = f"{base_surrogate}\n\n{add_surrogate(FOOTER_TEXT)}"
    final_text = remove_surrogate(final_surrogate)
    footer_offset = len(base_surrogate) + 2
    if footer_offset + len(add_surrogate(FOOTER_TEXT)) <= len(final_surrogate):
        footer_entity = MessageEntityBold(offset=footer_offset, length=len(add_surrogate(FOOTER_TEXT)))
        valid_entities.append(footer_entity)
    
    return final_text, valid_entities

def has_blocked_content(text):
    for keyword in BLOCK_KEYWORDS:
        if keyword.strip() and keyword.strip() in text:
            return True, f"含拦截关键词【{keyword}】"
    if URL_REGEX.search(text):
        return True, "处理后仍含剩余链接"
    return False, ""

def add_processed_id(message_id):
    global PROCESSED_MESSAGE_IDS
    if message_id in PROCESSED_MESSAGE_IDS:
        PROCESSED_MESSAGE_IDS.move_to_end(message_id)
        return
    if len(PROCESSED_MESSAGE_IDS) >= MAX_PROCESSED_CACHE:
        PROCESSED_MESSAGE_IDS.popitem(last=False)
    PROCESSED_MESSAGE_IDS[message_id] = True

# -------------------------- 保活&重启机制 --------------------------
async def connection_keep_alive():
    while True:
        await asyncio.sleep(180)
        try:
            if client.is_connected():
                await client.get_me()
                print(f"🔗 连接保活正常 | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"⚠️ 保活检测异常，等待自动重连 | 原因: {str(e)}")
            await asyncio.sleep(5)

def auto_restart_scheduler():
    global RESTART_TIMER
    print(f"[定时重启] 程序将在 {RESTART_INTERVAL_HOURS} 小时后自动重启")
    RESTART_TIMER = threading.Timer(
        interval=RESTART_INTERVAL_HOURS * 3600,
        function=lambda: (print("[定时重启] 执行重启"), sys.exit(0))
    )
    RESTART_TIMER.daemon = True
    RESTART_TIMER.start()

def graceful_shutdown():
    global RESTART_TIMER
    if RESTART_TIMER and RESTART_TIMER.is_alive():
        RESTART_TIMER.cancel()
        print("✅ 定时重启线程已关闭")
    print("👋 程序正常退出")

# -------------------------- 消息处理核心逻辑 --------------------------
async def handle_single_message(event):
    now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    chat_id = event.chat_id
    message = event.message
    message_id = message.id

    if message.grouped_id is not None:
        return

    print(f"\n==================== [{now_time}] 收到新单条消息 ====================")
    print(f"📢 消息来源Chat ID: {chat_id} | 程序监听ID列表: {SOURCE_CHAT_IDS}")
    print(f"📝 消息ID: {message_id} | 发送时间: {message.date}")
    print(f"📄 消息文本: {message.text[:200]}" if message.text else "📄 消息文本: 无")
    print(f"🎞️  是否带媒体: {message.media is not None} | 是否为有效图片/视频: {is_valid_media(message.media)}")
    print(f"🔘 消息按钮数量: {count_buttons(message.reply_markup)}")

    if chat_id not in SOURCE_CHAT_IDS:
        print(f"🚫 消息跳过 | 原因: 频道ID不在监听列表中")
        return

    if message_id in PROCESSED_MESSAGE_IDS:
        print(f"🚫 消息跳过 | 原因: 已处理过该消息，避免重复转发")
        return
    add_processed_id(message_id)

    if not message.text or message.text.isspace():
        print(f"🚫 消息拦截 | 原因: 无有效文本内容（纯媒体/空文本）")
        return
    if not is_valid_media(message.media):
        print(f"🚫 消息拦截 | 原因: 无有效图片/视频（纯文字/无效媒体）")
        return

    button_count = count_buttons(message.reply_markup)
    if 1 <= button_count <= 3:
        print(f"🚫 消息拦截 | 原因: 按钮数量{button_count}个（1-3个按钮禁止转发）")
        return
    send_reply_markup = None if button_count >=4 else message.reply_markup

    processed_text, processed_entities = process_text_and_entities(message.text, message.entities)

    is_blocked, block_reason = has_blocked_content(processed_text)
    if is_blocked:
        print(f"🚫 消息拦截 | 原因: {block_reason}")
        return

    if len(processed_text) > TG_MAX_TEXT_LENGTH:
        print(f"❌ 发送失败 | 原因: 处理后文本长度{len(processed_text)}，超过Telegram最大限制{TG_MAX_TEXT_LENGTH}")
        return
    if hasattr(message.media, 'document') and message.media.document:
        media_size_mb = message.media.document.size / 1024 / 1024
        if media_size_mb > TG_MAX_MEDIA_SIZE_MB:
            print(f"❌ 发送失败 | 原因: 媒体大小{media_size_mb:.2f}MB，超过Telegram最大限制{TG_MAX_MEDIA_SIZE_MB}MB")
            return

    try:
        await client.send_message(
            entity=TARGET_CHAT_ID,
            file=message.media,
            message=processed_text,
            entities=processed_entities,
            reply_markup=send_reply_markup,
            link_preview=False,
            silent=False
        )
        print(f"✅ 转发成功 | 消息ID {message_id} 已发送至目标频道（ID: {TARGET_CHAT_ID}）")
    except FloodWaitError as e:
        print(f"⚠️ 触发Telegram限流，等待{e.seconds}秒后重试")
        await asyncio.sleep(e.seconds)
        try:
            await client.send_message(
                entity=TARGET_CHAT_ID,
                file=message.media,
                message=processed_text,
                entities=processed_entities,
                reply_markup=send_reply_markup,
                link_preview=False,
                silent=False
            )
            print(f"✅ 重试转发成功 | 消息ID {message_id}")
        except Exception as retry_e:
            print(f"❌ 重试转发失败 | 消息ID {message_id} | 原因: {str(retry_e)}")
    except Exception as e:
        print(f"❌ 转发失败 | 消息ID {message_id} | 失败原因: {str(e)}")

async def handle_album_message(event):
    now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    chat_id = event.chat_id
    main_message = event.messages[0]
    message_id = main_message.id

    print(f"\n==================== [{now_time}] 收到新相册消息 ====================")
    print(f"📢 相册来源频道ID: {chat_id} | 程序监听ID列表: {SOURCE_CHAT_IDS}")
    print(f"📝 相册主消息ID: {message_id} | 发送时间: {main_message.date}")
    print(f"📄 相册文本: {main_message.text[:200]}" if main_message.text else "📄 相册文本: 无")
    print(f"🎞️  相册文件总数: {len(event.messages)} | 有效图片/视频数量: {len([m for m in event.messages if is_valid_media(m.media)])}")
    print(f"🔘 相册按钮数量: {count_buttons(main_message.reply_markup)}")

    if chat_id not in SOURCE_CHAT_IDS:
        print(f"🚫 相册跳过 | 原因: 频道ID不在监听列表中")
        return

    if message_id in PROCESSED_MESSAGE_IDS:
        print(f"🚫 相册跳过 | 原因: 已处理过该相册，避免重复转发")
        return
    add_processed_id(message_id)

    if not main_message.text or main_message.text.isspace():
        print(f"🚫 相册拦截 | 原因: 无有效文本内容")
        return
    valid_media_list = [msg.media for msg in event.messages if is_valid_media(msg.media)]
    if not valid_media_list:
        print(f"🚫 相册拦截 | 原因: 相册内无有效图片/视频")
        return

    full_text = main_message.text
    for msg in event.messages[1:]:
        if msg.text and msg.text.strip():
            full_text += f"\n\n{msg.text}"

    button_count = count_buttons(main_message.reply_markup)
    if 1 <= button_count <= 3:
        print(f"🚫 相册拦截 | 原因: 按钮数量{button_count}个（1-3个按钮禁止转发）")
        return
    send_reply_markup = None if button_count >=4 else main_message.reply_markup

    processed_text, processed_entities = process_text_and_entities(full_text, main_message.entities)

    is_blocked, block_reason = has_blocked_content(processed_text)
    if is_blocked:
        print(f"🚫 相册拦截 | 原因: {block_reason}")
        return

    if len(processed_text) > TG_MAX_TEXT_LENGTH:
        print(f"❌ 发送失败 | 原因: 处理后文本长度{len(processed_text)}，超过Telegram最大限制{TG_MAX_TEXT_LENGTH}")
        return

    try:
        await client.send_message(
            entity=TARGET_CHAT_ID,
            file=valid_media_list,
            message=processed_text,
            entities=processed_entities,
            reply_markup=send_reply_markup,
            link_preview=False,
            silent=False
        )
        print(f"✅ 转发成功 | 相册ID {message_id}（{len(valid_media_list)}个文件）已发送至目标频道")
    except FloodWaitError as e:
        print(f"⚠️ 触发Telegram限流，等待{e.seconds}秒后重试")
        await asyncio.sleep(e.seconds)
        try:
            await client.send_message(
                entity=TARGET_CHAT_ID,
                file=valid_media_list,
                message=processed_text,
                entities=processed_entities,
                reply_markup=send_reply_markup,
                link_preview=False,
                silent=False
            )
            print(f"✅ 重试转发成功 | 相册ID {message_id}")
        except Exception as retry_e:
            print(f"❌ 重试转发失败 | 相册ID {message_id} | 原因: {str(retry_e)}")
    except Exception as e:
        print(f"❌ 转发失败 | 相册ID {message_id} | 失败原因: {str(e)}")

# -------------------------- 主程序入口 --------------------------
async def main():
    global SOURCE_CHAT_IDS, TARGET_CHAT_ID
    try:
        await start_health_check_server()

        try:
            await client.start()
            me = await client.get_me()
            print(f"✅ 客户端登录成功 | 登录账号: {me.first_name} | ID: {me.id}")
        except Exception as e:
            print(f"❌ 客户端登录失败 | 原因: {str(e)}")
            print(f"💡 排查提示：请确认API_ID/API_HASH正确，StringSession有效，账号未被封禁")
            sys.exit(1)

        print("\n==================== 开始解析源频道 ====================")
        temp_chat_ids = []
        for channel_input in SOURCE_CHANNEL_INPUT:
            chat_id, _ = await parse_channel_to_id(channel_input, is_target=False)
            if chat_id is not None:
                temp_chat_ids.append(chat_id)
        
        SOURCE_CHAT_IDS = list(set(temp_chat_ids))
        if not SOURCE_CHAT_IDS:
            print("❌ 没有成功解析到任何源频道，程序退出")
            sys.exit(1)
        print(f"✅ 源频道解析完成，最终监听ID列表: {SOURCE_CHAT_IDS}")

        print("\n==================== 开始解析目标频道 ====================")
        TARGET_CHAT_ID, target_entity = await parse_channel_to_id(TARGET_CHANNEL_INPUT, is_target=True)

        # 【修复】兼容频道/群组的权限校验，无字段不崩溃
        print("\n==================== 目标频道权限校验 ====================")
        try:
            participant = await client.get_permissions(target_entity, me)
            if hasattr(participant.participant, 'admin_rights'):
                admin_rights: ChatAdminRights = participant.participant.admin_rights
                # 频道核心权限校验：仅校验发布消息权限
                if isinstance(target_entity, Channel):
                    if not admin_rights.post_messages:
                        print(f"❌ 权限校验失败！账号无「发布消息」权限")
                        sys.exit(1)
                    print(f"✅ 权限校验通过！频道「发布消息」权限已开启")
                # 普通群组权限校验
                elif isinstance(target_entity, Chat):
                    if not admin_rights.send_messages:
                        print(f"❌ 权限校验失败！账号无「发送消息」权限")
                        sys.exit(1)
                    if not admin_rights.send_media:
                        print(f"❌ 权限校验失败！账号无「发送媒体」权限")
                        sys.exit(1)
                    print(f"✅ 权限校验通过！群组「发送消息/媒体」权限已开启")
            else:
                print(f"⚠️  警告：无法读取管理员权限，将尝试直接发送消息")
        except Exception as e:
            print(f"⚠️  权限校验异常: {str(e)}，程序将继续运行，发送失败会打印详细日志")

        print("\n==================== 注册事件监听 ====================")
        client.add_event_handler(handle_single_message, events.NewMessage())
        client.add_event_handler(handle_album_message, events.Album())
        print(f"✅ 全量消息监听注册成功，已绑定 {len(SOURCE_CHAT_IDS)} 个源频道")

        asyncio.create_task(connection_keep_alive())
        auto_restart_scheduler()

        print("\n🚀 所有配置生效，开始7*24小时稳定监听...")
        await client.run_until_disconnected()

    except Exception as e:
        print(f"[程序崩溃] 致命错误: {str(e)}，即将自动重启")
        graceful_shutdown()
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        graceful_shutdown()
        sys.exit(0)
    except Exception as e:
        print(f"[程序启动失败] {str(e)}")
        graceful_shutdown()
        sys.exit(1)
