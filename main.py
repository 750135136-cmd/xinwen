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
from telethon.helpers import add_surrogate, remove_surrogate
from telethon.tl.types import (
    # 全量富文本格式实体，1:1还原所有格式
    MessageEntityBold, MessageEntityItalic, MessageEntityUnderline,
    MessageEntityStrike, MessageEntityCode, MessageEntityPre,
    MessageEntityBlockquote, MessageEntityTextUrl, MessageEntityUrl,
    # 媒体类型
    MessageMediaPhoto, MessageMediaDocument, DocumentAttributeVideo,
    # 频道与权限类型
    Channel, ChatAdminRights
)

# -------------------------- 全局常量配置 --------------------------
# Telegram平台硬限制
TG_MAX_TEXT_LENGTH = 4096
TG_MAX_MEDIA_SIZE_MB = 2000
# 业务配置
MAX_PROCESSED_CACHE = 10000  # 消息去重LRU缓存最大容量
URL_REGEX = re.compile(
    r'(https?://[^\s]+)|(t\.me/[^\s]+)|(telegram\.me/[^\s]+)',
    re.IGNORECASE
)
FOOTER_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

# -------------------------- 环境变量加载与前置校验 --------------------------
load_dotenv()
# 核心环境变量提取
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

# 【修复】核心环境变量前置校验，启动前就暴露问题
def pre_check_env():
    required_fields = ["API_ID", "API_HASH", "SOURCE_CHANNELS", "TARGET_CHANNEL"]
    missing_fields = [field for field in required_fields if not ENV_CONFIG[field]]
    if missing_fields:
        print(f"❌ 启动失败！缺失必填环境变量: {', '.join(missing_fields)}")
        sys.exit(1)
    # 校验API_ID是否为数字
    if not ENV_CONFIG["API_ID"].isdigit():
        print(f"❌ 启动失败！API_ID必须为纯数字")
        sys.exit(1)
    # 校验源频道是否为空
    if not ENV_CONFIG["SOURCE_CHANNELS"].strip():
        print(f"❌ 启动失败！SOURCE_CHANNELS不能为空")
        sys.exit(1)
    print("✅ 环境变量前置校验通过")

# 执行前置校验
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
# 【修复】LRU消息去重缓存，替代简单set，避免全量清空导致重复转发
PROCESSED_MESSAGE_IDS = OrderedDict()
# 定时重启线程全局句柄，用于优雅关闭
RESTART_TIMER = None

# -------------------------- Telegram客户端初始化（风控优化版） --------------------------
client = TelegramClient(
    session=StringSession(STRING_SESSION) if STRING_SESSION else SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    connection_retries=None,
    retry_delay=5,
    auto_reconnect=True,
    timeout=30,
    flood_sleep_threshold=120,
    # 模拟官方桌面客户端，大幅降低风控概率
    device_model="Windows 11",
    system_version="10.0.22631",
    app_version="5.10.0",
    lang_code="zh-CN",
    system_lang_code="zh-CN"
)

# -------------------------- 核心工具函数 --------------------------
async def health_check_handler(request):
    """Railway健康检查接口，防止容器休眠"""
    return web.Response(
        text=f"Bot Running | Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Listen Channels: {len(SOURCE_CHAT_IDS)}",
        content_type="text/plain; charset=utf-8"
    )

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
    【修复】万能频道解析，100%兼容所有输入格式，输出Telegram标准Chat ID
    彻底解决ID匹配错误、解析失败的问题
    """
    try:
        channel_input = channel_input.strip()
        if not channel_input:
            raise ValueError("频道输入为空")
        
        # 智能补全@前缀，仅对非ID、非链接的纯文本用户名补@
        if not (
            channel_input.startswith("-100")
            or channel_input.isdigit()
            or channel_input.startswith(("http://", "https://", "t.me/"))
        ):
            if not channel_input.startswith("@"):
                channel_input = f"@{channel_input}"
        
        # 解析频道实体
        entity = await client.get_entity(channel_input)
        channel_type = "目标频道" if is_target else "源频道"

        # 强制生成标准Chat ID，绝不重复拼接-100前缀
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
            print(f"2. 请确认账号拥有「发送消息」「发送媒体」「发送相册」权限")
            print(f"3. 请确认频道地址/ID输入正确，无多余空格")
            sys.exit(1)
        return None, None

def count_buttons(reply_markup):
    """计算Inline按钮总数，仅统计频道可用的内联按钮"""
    if not reply_markup or not hasattr(reply_markup, 'rows'):
        return 0
    # 仅统计InlineKeyboard按钮，忽略普通回复键盘（频道不可用）
    if not hasattr(reply_markup, 'inline') or not reply_markup.inline:
        return 0
    return sum(len(row.buttons) for row in reply_markup.rows)

def is_valid_media(media):
    """校验媒体类型：仅允许图片/视频，1:1还原原媒体"""
    if not media:
        return False
    # 允许图片
    if isinstance(media, MessageMediaPhoto):
        return True
    # 允许视频
    if isinstance(media, MessageMediaDocument):
        for attr in media.document.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return True
    return False

def process_text_and_entities(text, entities):
    """
    【修复】1:1还原所有富文本格式，彻底解决多字节字符offset错位问题
    支持：加粗、斜体、下划线、删除线、引用框、代码块、链接等所有Telegram格式
    """
    if not text:
        return "", []
    
    # 处理多字节字符，统一用UTF-16码元计算offset，和Telethon完全对齐
    surrogate_text = add_surrogate(text)
    paragraphs = surrogate_text.split("\n\n")
    base_paragraphs = paragraphs.copy()
    
    # 严格执行：最后一个空行后的段落，带@/链接则整段删除
    if len(paragraphs) >= 2:
        last_paragraph = paragraphs[-1]
        if "@" in last_paragraph or URL_REGEX.search(last_paragraph):
            base_paragraphs = paragraphs[:-1]
    
    # 拼接基础文本，还原为正常字符串
    base_surrogate = "\n\n".join(base_paragraphs)
    base_text = remove_surrogate(base_surrogate)
    base_length = len(base_surrogate)
    
    # 保留所有格式实体，仅过滤超出文本范围的，1:1还原原格式
    valid_entities = []
    if entities:
        for ent in entities:
            # 仅保留完全在基础文本范围内的实体，避免offset错位
            if ent.offset + ent.length <= base_length:
                valid_entities.append(ent)
    
    # 尾部加粗offset精准计算，极端情况也不会错位
    final_surrogate = f"{base_surrogate}\n\n{add_surrogate(FOOTER_TEXT)}"
    final_text = remove_surrogate(final_surrogate)
    footer_offset = len(base_surrogate) + 2  # 两个换行符的UTF-16长度
    # 确保offset不会越界
    if footer_offset + len(add_surrogate(FOOTER_TEXT)) <= len(final_surrogate):
        footer_entity = MessageEntityBold(offset=footer_offset, length=len(add_surrogate(FOOTER_TEXT)))
        valid_entities.append(footer_entity)
    
    return final_text, valid_entities

def has_blocked_content(text):
    """拦截规则校验，返回拦截原因"""
    # 关键词拦截
    for keyword in BLOCK_KEYWORDS:
        if keyword.strip() and keyword.strip() in text:
            return True, f"含拦截关键词【{keyword}】"
    # 剩余链接拦截
    if URL_REGEX.search(text):
        return True, "处理后仍含剩余链接"
    return False, ""

def add_processed_id(message_id):
    """【修复】LRU消息去重，超过容量自动删除最旧的记录，避免重复转发"""
    global PROCESSED_MESSAGE_IDS
    # 如果已存在，移动到末尾（最新）
    if message_id in PROCESSED_MESSAGE_IDS:
        PROCESSED_MESSAGE_IDS.move_to_end(message_id)
        return
    # 超过容量，删除最旧的记录
    if len(PROCESSED_MESSAGE_IDS) >= MAX_PROCESSED_CACHE:
        PROCESSED_MESSAGE_IDS.popitem(last=False)
    # 添加新记录
    PROCESSED_MESSAGE_IDS[message_id] = True

# -------------------------- 保活&重启机制（修复版） --------------------------
async def connection_keep_alive():
    """【修复】连接保活，全异常捕获，极端情况也不会导致事件循环崩溃"""
    while True:
        await asyncio.sleep(180)
        try:
            if client.is_connected():
                # 轻量ping，不触发风控
                await client.get_me()
                print(f"🔗 连接保活正常 | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"⚠️ 保活检测异常，等待自动重连 | 原因: {str(e)}")
            await asyncio.sleep(5)

def auto_restart_scheduler():
    """【修复】定时重启，保存线程句柄，支持优雅关闭"""
    global RESTART_TIMER
    print(f"[定时重启] 程序将在 {RESTART_INTERVAL_HOURS} 小时后自动重启")
    RESTART_TIMER = threading.Timer(
        interval=RESTART_INTERVAL_HOURS * 3600,
        function=lambda: (print("[定时重启] 执行重启"), sys.exit(0))
    )
    RESTART_TIMER.daemon = True
    RESTART_TIMER.start()

def graceful_shutdown():
    """优雅关闭程序，清理资源"""
    global RESTART_TIMER
    if RESTART_TIMER and RESTART_TIMER.is_alive():
        RESTART_TIMER.cancel()
        print("✅ 定时重启线程已关闭")
    print("👋 程序正常退出")

# -------------------------- 消息处理核心逻辑 --------------------------
async def handle_single_message(event):
    """【修复】单条消息处理，跳过相册消息，避免重复转发"""
    now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    chat_id = event.chat_id
    message = event.message
    message_id = message.id

    # 【核心修复】相册消息有grouped_id，直接跳过，仅由Album事件处理，彻底解决重复转发
    if message.grouped_id is not None:
        return

    # 全量日志打印，100%不遗漏
    print(f"\n==================== [{now_time}] 收到新单条消息 ====================")
    print(f"📢 消息来源Chat ID: {chat_id} | 程序监听ID列表: {SOURCE_CHAT_IDS}")
    print(f"📝 消息ID: {message_id} | 发送时间: {message.date}")
    print(f"📄 消息文本: {message.text[:200]}" if message.text else "📄 消息文本: 无")
    print(f"🎞️  是否带媒体: {message.media is not None} | 是否为有效图片/视频: {is_valid_media(message.media)}")
    print(f"🔘 消息按钮数量: {count_buttons(message.reply_markup)}")

    # 1. 频道ID匹配校验
    if chat_id not in SOURCE_CHAT_IDS:
        print(f"🚫 消息跳过 | 原因: 频道ID不在监听列表中")
        return

    # 2. 消息去重
    if message_id in PROCESSED_MESSAGE_IDS:
        print(f"🚫 消息跳过 | 原因: 已处理过该消息，避免重复转发")
        return
    add_processed_id(message_id)

    # 3. 基础规则过滤
    if not message.text or message.text.isspace():
        print(f"🚫 消息拦截 | 原因: 无有效文本内容（纯媒体/空文本）")
        return
    if not is_valid_media(message.media):
        print(f"🚫 消息拦截 | 原因: 无有效图片/视频（纯文字/无效媒体）")
        return

    # 4. 按钮规则处理
    button_count = count_buttons(message.reply_markup)
    if 1 <= button_count <= 3:
        print(f"🚫 消息拦截 | 原因: 按钮数量{button_count}个（1-3个按钮禁止转发）")
        return
    # 4个及以上按钮：清空所有按钮
    send_reply_markup = None if button_count >=4 else message.reply_markup

    # 5. 文本与格式处理
    processed_text, processed_entities = process_text_and_entities(message.text, message.entities)

    # 6. 拦截规则校验
    is_blocked, block_reason = has_blocked_content(processed_text)
    if is_blocked:
        print(f"🚫 消息拦截 | 原因: {block_reason}")
        return

    # 发送前校验
    if len(processed_text) > TG_MAX_TEXT_LENGTH:
        print(f"❌ 发送失败 | 原因: 处理后文本长度{len(processed_text)}，超过Telegram最大限制{TG_MAX_TEXT_LENGTH}")
        return
    if hasattr(message.media, 'document') and message.media.document:
        media_size_mb = message.media.document.size / 1024 / 1024
        if media_size_mb > TG_MAX_MEDIA_SIZE_MB:
            print(f"❌ 发送失败 | 原因: 媒体大小{media_size_mb:.2f}MB，超过Telegram最大限制{TG_MAX_MEDIA_SIZE_MB}MB")
            return

    # 7. 无来源转发消息，【修复】自动处理限流重试
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
        # 重试一次
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
    """【修复】相册/多媒体组处理，完整保留所有媒体和caption，1:1还原"""
    now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    chat_id = event.chat_id
    main_message = event.messages[0]
    message_id = main_message.id

    # 全量日志打印
    print(f"\n==================== [{now_time}] 收到新相册消息 ====================")
    print(f"📢 相册来源频道ID: {chat_id} | 程序监听ID列表: {SOURCE_CHAT_IDS}")
    print(f"📝 相册主消息ID: {message_id} | 发送时间: {main_message.date}")
    print(f"📄 相册文本: {main_message.text[:200]}" if main_message.text else "📄 相册文本: 无")
    print(f"🎞️  相册文件总数: {len(event.messages)} | 有效图片/视频数量: {len([m for m in event.messages if is_valid_media(m.media)])}")
    print(f"🔘 相册按钮数量: {count_buttons(main_message.reply_markup)}")

    # 1. 频道ID匹配校验
    if chat_id not in SOURCE_CHAT_IDS:
        print(f"🚫 相册跳过 | 原因: 频道ID不在监听列表中")
        return

    # 2. 消息去重
    if message_id in PROCESSED_MESSAGE_IDS:
        print(f"🚫 相册跳过 | 原因: 已处理过该相册，避免重复转发")
        return
    add_processed_id(message_id)

    # 3. 基础规则过滤
    if not main_message.text or main_message.text.isspace():
        print(f"🚫 相册拦截 | 原因: 无有效文本内容")
        return
    # 按原顺序保留所有有效媒体，1:1还原原相册顺序
    valid_media_list = [msg.media for msg in event.messages if is_valid_media(msg.media)]
    if not valid_media_list:
        print(f"🚫 相册拦截 | 原因: 相册内无有效图片/视频")
        return

    # 【修复】合并相册内所有子媒体的caption，完整还原原内容
    full_text = main_message.text
    for msg in event.messages[1:]:
        if msg.text and msg.text.strip():
            full_text += f"\n\n{msg.text}"

    # 4. 按钮规则处理
    button_count = count_buttons(main_message.reply_markup)
    if 1 <= button_count <= 3:
        print(f"🚫 相册拦截 | 原因: 按钮数量{button_count}个（1-3个按钮禁止转发）")
        return
    send_reply_markup = None if button_count >=4 else main_message.reply_markup

    # 5. 文本与格式处理
    processed_text, processed_entities = process_text_and_entities(full_text, main_message.entities)

    # 6. 拦截规则校验
    is_blocked, block_reason = has_blocked_content(processed_text)
    if is_blocked:
        print(f"🚫 相册拦截 | 原因: {block_reason}")
        return

    # 发送前校验
    if len(processed_text) > TG_MAX_TEXT_LENGTH:
        print(f"❌ 发送失败 | 原因: 处理后文本长度{len(processed_text)}，超过Telegram最大限制{TG_MAX_TEXT_LENGTH}")
        return

    # 7. 无来源转发相册，【修复】自动处理限流重试
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
        # 重试一次
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
        # 1. 启动健康检查服务
        await start_health_check_server()

        # 2. 登录Telegram客户端，【修复】无效Session兜底处理
        try:
            await client.start()
            me = await client.get_me()
            print(f"✅ 客户端登录成功 | 登录账号: {me.first_name} | ID: {me.id}")
        except Exception as e:
            print(f"❌ 客户端登录失败 | 原因: {str(e)}")
            print(f"💡 排查提示：请确认API_ID/API_HASH正确，StringSession有效，账号未被封禁")
            sys.exit(1)

        # 3. 批量解析源频道
        print("\n==================== 开始解析源频道 ====================")
        temp_chat_ids = []
        for channel_input in SOURCE_CHANNEL_INPUT:
            chat_id, _ = await parse_channel_to_id(channel_input, is_target=False)
            if chat_id is not None:
                temp_chat_ids.append(chat_id)
        
        # 去重+有效性校验
        SOURCE_CHAT_IDS = list(set(temp_chat_ids))
        if not SOURCE_CHAT_IDS:
            print("❌ 没有成功解析到任何源频道，程序退出")
            sys.exit(1)
        print(f"✅ 源频道解析完成，最终监听ID列表: {SOURCE_CHAT_IDS}")

        # 4. 解析目标频道 + 【修复】管理员权限前置校验
        print("\n==================== 开始解析目标频道 ====================")
        TARGET_CHAT_ID, target_entity = await parse_channel_to_id(TARGET_CHANNEL_INPUT, is_target=True)
        # 校验账号在目标频道的管理员权限
        try:
            participant = await client.get_permissions(target_entity, me)
            admin_rights: ChatAdminRights = participant.participant.admin_rights
            if not admin_rights.post_messages:
                print(f"❌ 目标频道权限校验失败！账号无「发送消息」权限")
                sys.exit(1)
            if not admin_rights.send_media:
                print(f"❌ 目标频道权限校验失败！账号无「发送媒体」权限")
                sys.exit(1)
            print(f"✅ 目标频道权限校验通过，拥有发送消息/媒体权限")
        except Exception as e:
            print(f"❌ 目标频道权限校验失败 | 原因: {str(e)}")
            print(f"💡 请确认你的账号是目标频道的管理员，且开启了发送消息/媒体权限")
            sys.exit(1)

        # 5. 注册事件监听，【修复】先解析完所有频道再注册，100%生效
        print("\n==================== 注册事件监听 ====================")
        client.add_event_handler(handle_single_message, events.NewMessage())
        client.add_event_handler(handle_album_message, events.Album())
        print(f"✅ 全量消息监听注册成功，已绑定 {len(SOURCE_CHAT_IDS)} 个源频道")

        # 6. 启动保活和定时重启
        asyncio.create_task(connection_keep_alive())
        auto_restart_scheduler()

        # 7. 保持程序运行
        print("\n🚀 所有配置生效，开始7*24小时稳定监听...")
        await client.run_until_disconnected()

    except Exception as e:
        print(f"[程序崩溃] 致命错误: {str(e)}，即将自动重启")
        graceful_shutdown()
        sys.exit(1)

# -------------------------- 程序启动入口 --------------------------
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
