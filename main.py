import re
import copy
import asyncio
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityBold
from telethon.extensions import html as tl_html

# ========= 配置 =========
API_ID = 25559912
API_HASH = "22d3bb9665ad7e6a86e89c1445672e07"
SESSION = "session"   # 根目录下的 session.session
RESTART_TIME = 72000  # 20小时
TAIL_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

# ========= 新增：回复联动&配置文件核心参数 =========
# 找不到原消息映射时，是否仍转发为普通消息（False=不转发，True=转发）
ALLOW_REPLY_WITHOUT_MAPPING = True
# 配置文件路径（和main.py同目录，Railway直接放根目录即可）
CHANNEL_CONFIG_FILE = "channel_config.txt"
# 消息ID持久化文件（自动生成，重启不丢失历史映射）
MESSAGE_MAPPING_FILE = "message_mapping.txt"

client = TelegramClient(SESSION, API_ID, API_HASH)
# ========= 新增：全局缓存变量（兼容原有单频道+新增多频道） =========
CHANNEL_MAP = {}
SOURCE_ENTITY_CACHE = {}
MESSAGE_ID_MAP = {}
FILE_LOCK = asyncio.Lock()  # 异步文件锁，防止多线程写文件冲突

# ========= 日志 =========
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ========= 新增：配置文件&消息映射持久化核心函数 =========
def load_channel_config() -> dict:
    """加载频道配置文件，兼容你原有单频道配置"""
    config_map = {}
    try:
        with open(CHANNEL_CONFIG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 2:
                log(f"配置文件格式错误，跳过该行: {line}")
                continue
            source, target = parts
            config_map[source] = target
        log(f"频道配置加载完成，共加载 {len(config_map)} 组频道映射")
        return config_map
    except FileNotFoundError:
        # 找不到配置文件，默认使用你原来的单频道配置，兜底兼容
        log(f"未找到 {CHANNEL_CONFIG_FILE}，默认使用原有单频道配置")
        return {"@gegong0000": "@hrxxw"}
    except Exception as e:
        log(f"配置文件加载失败: {e}")
        raise SystemExit(1)

async def load_message_mapping():
    """启动时加载历史消息ID映射，重启不丢失"""
    global MESSAGE_ID_MAP
    try:
        async with FILE_LOCK:
            with open(MESSAGE_MAPPING_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) != 4:
                    continue
                source_channel_id, source_msg_id, target_channel_id, target_msg_id = parts
                map_key = f"{source_channel_id}|{source_msg_id}"
                MESSAGE_ID_MAP[map_key] = f"{target_channel_id}|{target_msg_id}"
        log(f"消息映射加载完成，共加载 {len(MESSAGE_ID_MAP)} 条历史消息映射")
    except FileNotFoundError:
        # 文件不存在自动创建，无需手动操作
        with open(MESSAGE_MAPPING_FILE, "w", encoding="utf-8") as f:
            f.write("# 消息ID映射持久化文件，请勿手动修改 | 格式：监听频道ID|监听消息ID|目标频道ID|目标消息ID\n")
        log(f"未找到消息映射文件，已自动创建 {MESSAGE_MAPPING_FILE}")
    except Exception as e:
        log(f"消息映射加载失败: {e}")

async def save_message_mapping(source_channel_id: int, source_msg_id: int, target_channel_id: int, target_msg_id: int):
    """保存消息ID映射，同时更新内存和持久化文件"""
    map_key = f"{source_channel_id}|{source_msg_id}"
    map_value = f"{target_channel_id}|{target_msg_id}"
    MESSAGE_ID_MAP[map_key] = map_value
    try:
        async with FILE_LOCK:
            with open(MESSAGE_MAPPING_FILE, "a", encoding="utf-8") as f:
                f.write(f"{source_channel_id}|{source_msg_id}|{target_channel_id}|{target_msg_id}\n")
    except Exception as e:
        log(f"消息映射持久化失败: {e}")

# ========= 新增：回复目标ID获取核心函数 =========
def get_reply_target_id(source_channel_id: int, msg) -> int | None:
    """根据原消息的回复信息，获取目标频道对应的回复消息ID，兼容1.42.0"""
    if not hasattr(msg, "reply_to") or not msg.reply_to:
        return None
    reply_to_msg_id = getattr(msg.reply_to, "reply_to_msg_id", None)
    if not reply_to_msg_id:
        return None
    map_key = f"{source_channel_id}|{reply_to_msg_id}"
    target_map_value = MESSAGE_ID_MAP.get(map_key)
    if not target_map_value:
        return None
    _, target_msg_id = target_map_value.split("|")
    return int(target_msg_id)

# ========= 基础判断（你原有代码，完全未改动） =========
def has_link(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"(http|t\.me)", text, re.IGNORECASE))

def has_paid_ad(text: str) -> bool:
    return bool(text and "付费广告" in text)

# ========= 新增：判断是否是其他地方转发来的消息（你原有代码，完全未改动） =========
def is_forwarded_msg(msg) -> bool:
    return bool(getattr(msg, "fwd_from", None))

def count_buttons(msg) -> int:
    if not msg:
        return 0
    reply_markup = getattr(msg, "reply_markup", None)
    if reply_markup and hasattr(reply_markup, "rows"):
        return sum(len(row.buttons) for row in reply_markup.rows)
    if getattr(msg, "buttons", None):
        return sum(len(row) for row in msg.buttons)
    return 0

def pick_text_from_message(msg):
    txt = getattr(msg, "message", None) or getattr(msg, "raw_text", None) or ""
    return txt, getattr(msg, "entities", None)

# ========= 【修复】相册文本&实体获取（还原你原有核心逻辑，优化兜底，增加排查日志） =========
def pick_caption_from_album(event):
    if not event.messages:
        log("相册事件异常：无任何媒体消息")
        return "", []
    
    # 核心逻辑和你原来完全一致：强制按消息ID升序排序，Telegram仅在ID最小的首条保留caption
    sorted_msgs = sorted(event.messages, key=lambda m: m.id)
    main_msg = sorted_msgs[0]
    
    # 优先取官方标准message属性，多层兜底，确保不会拿空
    txt = getattr(main_msg, "message", None) or getattr(main_msg, "text", None) or getattr(main_msg, "raw_text", None) or ""
    entities = getattr(main_msg, "entities", None) or []
    
    # 打印排查日志，确认首条消息的文本内容
    log(f"相册首条消息ID:{main_msg.id} | 提取到的文本长度:{len(txt)} | 文本内容:{txt[:100]}")
    
    # 兜底兼容：全量遍历所有消息，找有效文本（极端场景兜底，和你原有逻辑一致）
    if not txt.strip():
        log("相册首条无文本，开始遍历其他消息兜底")
        for m in sorted_msgs[1:]:
            t = getattr(m, "message", None) or getattr(m, "text", None) or getattr(m, "raw_text", None) or ""
            if t.strip():
                txt = t
                entities = getattr(m, "entities", None) or []
                log(f"相册兜底取到文本，消息ID:{m.id} | 文本长度:{len(txt)}")
                break
    
    # 最终兜底：确保实体永远是列表，不会出现None
    return txt, list(entities or [])


# ========= 尾部处理（你原有代码，完全未改动） =========
def trim_tail_keep_entities(text: str, entities=None):
    if not text:
        return text, []
    raw = text.rstrip()
    entities = list(entities or [])
    matches = list(re.finditer(r"\n\s*\n", raw))
    if not matches:
        return raw, entities
    last_sep = matches[-1]
    prefix = raw[:last_sep.start()].rstrip()
    tail = raw[last_sep.end():].strip()
    if not tail or ("@" not in tail and not has_link(tail)):
        return raw, entities
    new_text = prefix + "\n\n" + TAIL_TEXT
    prefix_len = len(prefix)
    new_entities = []
    for ent in entities:
        start = ent.offset
        end = ent.offset + ent.length
        if end <= prefix_len:
            new_entities.append(copy.copy(ent))
            continue
        if start >= prefix_len:
            continue
        ent2 = copy.copy(ent)
        ent2.length = max(0, prefix_len - start)
        if ent2.length > 0:
            new_entities.append(ent2)
    new_entities.append(MessageEntityBold(offset=len(prefix) + 2, length=len(TAIL_TEXT)))
    return new_text, new_entities

def to_html(text: str, entities):
    if not text:
        return ""
    try:
        return tl_html.unparse(text, entities or [])
    except Exception:
        return text

# ========= 发送封装（新增reply_to参数+返回值，原有防限流逻辑完全未改动） =========
async def safe_send_single(*, target, text_html, media, reply_to=None):
    try:
        await client.send_file(
            target,
            file=media,
            caption=text_html,
            parse_mode="html",
            link_preview=False,
            reply_to=reply_to
        )
    except FloodWaitError as e:
        log(f"触发 FloodWait：需要等待 {e.seconds} 秒")
        if e.seconds > 60:
            log("等待时间过长，退出进程，交由 Railway 自动重启")
            await client.disconnect()
            raise SystemExit(1)
        await asyncio.sleep(e.seconds)
        await client.send_file(
            target,
            file=media,
            caption=text_html,
            parse_mode="html",
            link_preview=False,
            reply_to=reply_to
        )
    # 新增：返回发送成功的消息对象，用于记录映射
    return await client.get_messages(target, limit=1)

# ========= 【修复】相册发送（新增reply_to参数+返回值，原有逻辑完全未改动） =========
async def safe_send_album(*, target, files, captions_html, reply_to=None):
    try:
        await client.send_file(
            target,
            file=files,
            caption=captions_html[0],
            parse_mode="html",
            link_preview=False,
            reply_to=reply_to
        )
    except FloodWaitError as e:
        log(f"触发 FloodWait：需要等待 {e.seconds} 秒")
        if e.seconds > 60:
            log("等待时间过长，退出进程，交由 Railway 自动重启")
            await client.disconnect()
            raise SystemExit(1)
        await asyncio.sleep(e.seconds)
        await client.send_file(
            target,
            file=files,
            caption=captions_html[0],
            parse_mode="html",
            link_preview=False,
            reply_to=reply_to
        )
    # 新增：返回发送成功的相册首条消息对象，用于记录映射
    return await client.get_messages(target, limit=1)

# ========= 单条处理（原有拦截逻辑完全未改动，新增回复联动+映射保存） =========
async def message_handler(event):
    try:
        if event.grouped_id:
            return
        msg = event.message
        # 新增：适配多频道，获取当前频道对应的目标频道
        source_channel_id = event.chat_id
        target_entity = CHANNEL_MAP.get(source_channel_id)
        if not target_entity:
            log(f"拦截: 未找到该频道的目标映射 | 频道ID: {source_channel_id}")
            return

        # ========= 以下是你原有的所有拦截代码，完全未改动 =========
        if is_forwarded_msg(msg):
            log("拦截: 其他地方转发的单条消息")
            return
        text, entities = pick_text_from_message(msg)
        btn_count = count_buttons(msg)
        log(f"收到消息 | 文本长度:{len(text)} | 按钮:{btn_count} | 有媒体:{bool(msg.media)}")
        if not msg.media:
            log("拦截: 无媒体")
            return
        if not text.strip():
            log("拦截: 无文字")
            return
        if has_paid_ad(text):
            log("拦截: 含付费广告")
            return
        if 1 <= btn_count <= 3:
            log(f"拦截: 按钮数量 {btn_count}（1~3 禁止）")
            return
        new_text, new_entities = trim_tail_keep_entities(text, entities)
        if has_link(new_text):
            log("拦截: 尾部处理后仍包含链接")
            return
        text_html = to_html(new_text, new_entities)
        # ========= 原有代码结束，新增回复联动+发送+映射保存 =========

        # 新增：获取回复目标消息ID
        reply_to_target_id = get_reply_target_id(source_channel_id, msg)
        # 新增：回复消息找不到原映射时，按配置处理
        if msg.reply_to and not reply_to_target_id and not ALLOW_REPLY_WITHOUT_MAPPING:
            log(f"拦截: 回复消息未找到原消息映射 | 消息ID: {msg.id} | 回复的原消息ID: {msg.reply_to.reply_to_msg_id}")
            return

        # 发送消息，兼容原有逻辑
        sent_msg = await safe_send_single(
            target=target_entity,
            text_html=text_html,
            media=msg.media,
            reply_to=reply_to_target_id
        )

        # 新增：保存消息ID映射，重启不丢失
        if sent_msg:
            sent_msg = sent_msg[0]
            await save_message_mapping(
                source_channel_id=source_channel_id,
                source_msg_id=msg.id,
                target_channel_id=target_entity.id,
                target_msg_id=sent_msg.id
            )
            log(f"转发成功: 单条消息 | 原消息ID: {msg.id} | 目标消息ID: {sent_msg.id}" + (f" | 回复目标ID: {reply_to_target_id}" if reply_to_target_id else ""))

    except Exception as e:
        log(f"消息处理错误: {e}")

# ========= 相册处理（还原原有执行流程，去掉重复排序，保留回复联动功能） =========
async def album_handler(event):
    try:
        msgs = event.messages
        # 新增：适配多频道，获取当前频道对应的目标频道
        source_channel_id = event.chat_id
        target_entity = CHANNEL_MAP.get(source_channel_id)
        if not target_entity:
            log(f"拦截: 未找到该频道的目标映射 | 频道ID: {source_channel_id}")
            return

        # ========= 以下是你原有的所有拦截代码，完全还原，无任何改动 =========
        # 转发相册拦截
        if any(is_forwarded_msg(m) for m in msgs):
            log("拦截: 其他地方转发的相册消息")
            return
        # 按钮计数
        btn_count = sum(count_buttons(m) for m in msgs)
        # 提取相册文本&实体（和你原有逻辑完全一致，不再重复排序）
        text, entities = pick_caption_from_album(event)
        log(f"收到相册 | 媒体数:{len(msgs)} | 最终提取文本长度:{len(text)} | 按钮:{btn_count}")
        # 原有拦截规则
        if not text.strip():
            log("拦截: 相册无文字")
            return
        if has_paid_ad(text):
            log("拦截: 含付费广告")
            return
        if 1 <= btn_count <= 3:
            log(f"拦截: 按钮数量 {btn_count}（1~3 禁止）")
            return
        # 尾部处理&格式转换，和你原有逻辑完全一致
        new_text, new_entities = trim_tail_keep_entities(text, entities)
        if has_link(new_text):
            log("拦截: 尾部处理后仍包含链接")
            return
        first_caption_html = to_html(new_text, new_entities)
        captions_html = [first_caption_html] + [""] * (len(msgs) - 1)
        # ========= 原有逻辑结束，保留新增的回复联动功能 =========

        # 获取回复目标消息ID
        sorted_msgs = sorted(msgs, key=lambda m: m.id)
        first = sorted_msgs[0]
        reply_to_target_id = get_reply_target_id(source_channel_id, first)
        # 回复消息找不到原映射时，按配置处理
        if first.reply_to and not reply_to_target_id and not ALLOW_REPLY_WITHOUT_MAPPING:
            log(f"拦截: 回复相册未找到原消息映射 | 首条消息ID: {first.id} | 回复的原消息ID: {first.reply_to.reply_to_msg_id}")
            return

        # 发送相册，和你原有逻辑完全一致
        sent_msg = await safe_send_album(
            target=target_entity,
            files=[m.media for m in msgs if m.media],
            captions_html=captions_html,
            reply_to=reply_to_target_id
        )

        # 保存消息ID映射
        if sent_msg:
            sent_msg = sent_msg[0]
            await save_message_mapping(
                source_channel_id=source_channel_id,
                source_msg_id=first.id,
                target_channel_id=target_entity.id,
                target_msg_id=sent_msg.id
            )
            log(f"转发成功: 相册 | 原首条消息ID: {first.id} | 目标消息ID: {sent_msg.id}" + (f" | 回复目标ID: {reply_to_target_id}" if reply_to_target_id else ""))

    except Exception as e:
        log(f"相册处理错误: {e}")


# ========= 编辑事件拦截（你原有代码，完全未改动，仅适配多频道） =========
async def edit_handler(event):
    try:
        msg = event.message
        source_channel_id = event.chat_id
        btn_count = count_buttons(msg)
        text, _ = pick_text_from_message(msg)
        if 1 <= btn_count <= 3 or has_paid_ad(text):
            log(f"编辑后触发拦截 | 频道ID:{source_channel_id} | 消息ID:{msg.id} | 按钮数:{btn_count}")
    except Exception as e:
        log(f"编辑事件处理错误: {e}")

# ========= 20小时自动重启（你原有代码，完全未改动） =========
async def auto_restart():
    await asyncio.sleep(RESTART_TIME)
    log("20小时到，执行自动重启")
    await client.disconnect()
    raise SystemExit(1)

# ========= 启动前解析实体并绑定监听（适配txt配置+多频道） =========
async def resolve_and_bind():
    global CHANNEL_MAP, SOURCE_ENTITY_CACHE
    # 加载频道配置
    config_map = load_channel_config()
    # 解析所有频道
    temp_channel_map = {}
    temp_source_cache = {}
    for source_str, target_str in config_map.items():
        try:
            source_entity = await client.get_entity(source_str)
            target_entity = await client.get_entity(target_str)
            # 核心修复：统一用事件触发的完整频道ID（带-100前缀）做key
            full_source_id = int(f"-100{source_entity.id}")
            temp_channel_map[full_source_id] = target_entity
            temp_source_cache[full_source_id] = source_entity
            log(f"频道解析成功 | 监听频道: {source_entity.title} (完整ID:{full_source_id}) | 目标频道: {target_entity.title} (ID:{target_entity.id})")
        except Exception as e:
            log(f"频道解析失败 | 监听标识: {source_str} | 目标标识: {target_str} | 错误: {e}")
            raise SystemExit(1)
    CHANNEL_MAP = temp_channel_map
    SOURCE_ENTITY_CACHE = temp_source_cache

    # 登录信息打印
    me = await client.get_me()
    log(f"启动成功 | 登录账号: {me.username or me.id}")
    log(f"共监听 {len(CHANNEL_MAP)} 个频道，全部绑定完成")

    # 绑定事件，兼容原有逻辑
    source_chats = list(temp_source_cache.values())
    client.add_event_handler(album_handler, events.Album(chats=source_chats))
    client.add_event_handler(message_handler, events.NewMessage(chats=source_chats))
    client.add_event_handler(edit_handler, events.MessageEdited(chats=source_chats))

# ========= 主程序（新增消息映射加载，原有逻辑完全未改动） =========
async def main():
    await client.start()
    # 新增：启动时加载历史消息映射
    await load_message_mapping()
    await resolve_and_bind()
    restart_task = asyncio.create_task(auto_restart())
    await client.run_until_disconnected()
    await restart_task

client.loop.run_until_complete(main())
