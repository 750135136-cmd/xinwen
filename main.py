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
SOURCE = "@gegong0000"
TARGET = "@hrxxw"
RESTART_TIME = 72000  # 20小时
TAIL_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

client = TelegramClient(SESSION, API_ID, API_HASH)
SOURCE_ENTITY = None
TARGET_ENTITY = None

# ========= 日志 =========
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ========= 基础判断 =========
def has_link(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"(http|t\.me|@(?!hrxxw\b|LimTGbot\b)\w+)", text, re.IGNORECASE))


def has_paid_ad(text: str) -> bool:
    return bool(text and "付费广告" in text)

def count_buttons(msg) -> int:
    if not getattr(msg, "buttons", None):
        return 0
    return sum(len(row) for row in msg.buttons)

def pick_text_from_message(msg):
    txt = getattr(msg, "message", None) or getattr(msg, "raw_text", None) or ""
    return txt, getattr(msg, "entities", None)


# ========= 【修复】相册文本&实体获取：强制按ID排序，彻底解决3图caption丢失问题 =========
def pick_caption_from_album(event):
    if not event.messages:
        return "", []
    
    # 核心修复：强制按消息ID升序排序，确保第一条是服务端认定的组内首条（唯一保留caption）
    sorted_msgs = sorted(event.messages, key=lambda m: m.id)
    # 取ID最小的首条消息，Telegram仅保留这条的caption
    main_msg = sorted_msgs[0]
    
    # 优化：优先取官方标准的message属性，兜底raw_text，兼容所有Telethon版本
    txt = getattr(main_msg, "message", None) or getattr(main_msg, "raw_text", None) or ""
    entities = getattr(main_msg, "entities", None) or []
    
    # 兜底兼容：全量遍历所有排序后的消息，找有效文本（极端场景兜底）
    if not txt.strip():
        for m in sorted_msgs[1:]:
            t = getattr(m, "message", None) or getattr(m, "raw_text", None) or ""
            if t.strip():
                txt = t
                entities = getattr(m, "entities", None) or []
                break
    
    # 最终兜底：确保实体永远是列表，不会出现None
    return txt, list(entities or [])

# ========= 尾部处理：只改最后一段 =========
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
    """
    把 text + entities 转成 HTML，
    这样发送时不会退回成 * 号，也更稳地保留引用框/加粗。
    """
    if not text:
        return ""
    try:
        return tl_html.unparse(text, entities or [])
    except Exception:
        # 兜底：至少保证不会因为格式转换崩掉
        return text

# ========= 发送封装 =========
async def safe_send_single(*, target, text_html, media):
    try:
        await client.send_file(
            target,
            file=media,
            caption=text_html,
            parse_mode="html",
            link_preview=False,
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
        )

# ========= 【修复】相册发送：删除重复拼接caption的错误逻辑，保证长度和媒体数完全匹配 =========
async def safe_send_album(*, target, files, captions_html):
    """
    相册/多媒体组：
    每个文件都使用 HTML 格式的 caption（确保每个文件单独传递 HTML 格式）。
    """
    try:
        # 修复：直接使用传入的captions_html，已保证长度和files完全一致，无需重复拼接
        await client.send_file(
            target,
            file=files,
            caption=captions_html,
            parse_mode="html",
            link_preview=False,
        )
    except FloodWaitError as e:
        log(f"触发 FloodWait：需要等待 {e.seconds} 秒")
        if e.seconds > 60:
            log("等待时间过长，退出进程，交由 Railway 自动重启")
            await client.disconnect()
            raise SystemExit(1)
        await asyncio.sleep(e.seconds)
        # 重试时也直接使用原captions_html，保证长度匹配
        await client.send_file(
            target,
            file=files,
            caption=captions_html,
            parse_mode="html",
            link_preview=False,
        )

# ========= 单条处理 =========
async def message_handler(event):
    try:
        if event.grouped_id:
            return
        msg = event.message
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
        await safe_send_single(
            target=TARGET_ENTITY,
            text_html=text_html,
            media=msg.media
        )
        log("转发成功: 单条消息")
    except Exception as e:
        log(f"消息处理错误: {e}")

# ========= 相册处理 =========
async def album_handler(event):
    try:
        msgs = event.messages
        sorted_msgs = sorted(msgs, key=lambda m: m.id)
        first = sorted_msgs[0]
        btn_count = count_buttons(first)
        text, entities = pick_caption_from_album(event)
        log(f"收到相册 | 媒体数:{len(msgs)} | 文本长度:{len(text)} | 按钮:{btn_count}")
        if not text.strip():
            log("拦截: 相册无文字")
            return
        if has_paid_ad(text):
            log("拦截: 含付费广告")
            return
        if 1 <= btn_count <= 3:
            log(f"拦截: 按钮数量 {btn_count}（1~3 禁止）")
            return
        # 完全复用单条消息的尾部处理&格式转换逻辑，保证一致性
        new_text, new_entities = trim_tail_keep_entities(text, entities)
        if has_link(new_text):
            log("拦截: 尾部处理后仍包含链接")
            return
        # 转换为 HTML 格式，完整保留加粗、引用框等所有格式
        first_caption_html = to_html(new_text, new_entities)
        captions_html = [first_caption_html] + [""] * (len(msgs) - 1)
        await safe_send_album(
            target=TARGET_ENTITY,
            files=[m.media for m in msgs],
            captions_html=captions_html
        )
        log(f"转发成功: 相册 | 实际媒体数:{len(msgs)}")
    except Exception as e:
        log(f"相册处理错误: {e}")

# ========= 20小时自动重启 =========
async def auto_restart():
    await asyncio.sleep(RESTART_TIME)
    log("20小时到，执行自动重启")
    await client.disconnect()
    raise SystemExit(0)

# ========= 启动前解析实体并绑定监听 =========
async def resolve_and_bind():
    global SOURCE_ENTITY, TARGET_ENTITY
    SOURCE_ENTITY = await client.get_entity(SOURCE)
    TARGET_ENTITY = await client.get_entity(TARGET)
    me = await client.get_me()
    log(f"启动成功 | 登录账号: {me.username or me.id}")
    log(f"监听频道: {getattr(SOURCE_ENTITY, 'title', '')} | username: @{getattr(SOURCE_ENTITY, 'username', None) or '无公开用户名'}")
    log(f"目标频道: {getattr(TARGET_ENTITY, 'title', '')} | username: @{getattr(TARGET_ENTITY, 'username', None) or '无公开用户名'}")
    client.add_event_handler(album_handler, events.Album(chats=SOURCE_ENTITY))
    client.add_event_handler(message_handler, events.NewMessage(chats=SOURCE_ENTITY))

# ========= 主程序 =========
async def main():
    await client.start()
    await resolve_and_bind()
    asyncio.create_task(auto_restart())
    await client.run_until_disconnected()

client.loop.run_until_complete(main())
