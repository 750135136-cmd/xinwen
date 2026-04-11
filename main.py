import re
import copy
import asyncio
from datetime import datetime

from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityBold

# ========= 配置 =========
API_ID = 25559912
API_HASH = "22d3bb9665ad7e6a86e89c1445672e07"

SESSION = "session"   # 根目录下的 session.session

SOURCE = "@djrrw"     # 监听频道用户名 / 可解析频道
TARGET = "@djrrv"     # 目标频道用户名 / 可解析频道

RESTART_TIME = 72000  # 20小时

TAIL_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

client = TelegramClient(SESSION, API_ID, API_HASH)

SOURCE_ID = None
TARGET_ID = None


# ========= 日志 =========
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ========= 基础判断 =========
def has_link(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"(https?://|www\.)", text, re.IGNORECASE))


def has_paid_ad(text: str) -> bool:
    return bool(text and "付费广告" in text)


def count_buttons(msg) -> int:
    if not getattr(msg, "buttons", None):
        return 0
    return sum(len(row) for row in msg.buttons)


def pick_text_from_message(msg):
    txt = getattr(msg, "raw_text", None) or getattr(msg, "message", None) or ""
    return txt, getattr(msg, "entities", None)


def pick_caption_from_album(event):
    """
    相册正文提取：
    1) event.text
    2) event.messages 逐条找
    3) original_update 兜底
    """
    txt = getattr(event, "text", None) or ""
    if txt.strip():
        return txt, None

    for m in event.messages:
        t = getattr(m, "raw_text", None) or getattr(m, "message", None) or ""
        if t.strip():
            return t, getattr(m, "entities", None)

    try:
        orig = getattr(event, "original_update", None)
        if orig and getattr(orig, "message", None):
            msg = orig.message
            t = getattr(msg, "message", None) or ""
            if t.strip():
                return t, getattr(msg, "entities", None)
    except Exception:
        pass

    return "", None


# ========= 尾部处理：只改最后一段 =========
def trim_tail_keep_entities(text: str, entities=None):
    """
    规则：
    - 只检查最后一个空行后面的尾部块
    - 如果尾部块含 @ 或链接，则删除整段尾部块
    - 替换成固定文案
    - 前面的引用框/加粗等实体尽量保留
    """
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

        # 完全在前半段：保留
        if end <= prefix_len:
            new_entities.append(copy.copy(ent))
            continue

        # 完全在尾部：删除
        if start >= prefix_len:
            continue

        # 跨越边界：截断
        ent2 = copy.copy(ent)
        ent2.length = max(0, prefix_len - start)
        if ent2.length > 0:
            new_entities.append(ent2)

    # 给替换后的尾部加粗
    new_entities.append(MessageEntityBold(offset=len(prefix) + 2, length=len(TAIL_TEXT)))

    return new_text, new_entities


# ========= 发送封装 =========
async def safe_send_single(*, target, text, media, entities=None):
    """
    单张图片/视频：caption + formatting_entities 直接发送
    """
    try:
        await client.send_file(
            target,
            file=media,
            caption=text,
            formatting_entities=entities,
            link_preview=False,
            parse_mode=None,
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
            caption=text,
            formatting_entities=entities,
            link_preview=False,
            parse_mode=None,
        )


async def safe_send_album(*, target, files, text, entities=None):
    """
    相册/多媒体组：
    Telethon 要求 caption 可为 list[str]，
    formatting_entities 要为 list[list[MessageEntity...]]，
    每个内层列表和对应文件一一匹配。
    """
    try:
        captions = [text] + [""] * (len(files) - 1)
        entity_groups = [list(entities or [])] + [[] for _ in range(len(files) - 1)]

        await client.send_file(
            target,
            file=files,
            caption=captions,
            formatting_entities=entity_groups,
            link_preview=False,
            parse_mode=None,
        )
    except FloodWaitError as e:
        log(f"触发 FloodWait：需要等待 {e.seconds} 秒")
        if e.seconds > 60:
            log("等待时间过长，退出进程，交由 Railway 自动重启")
            await client.disconnect()
            raise SystemExit(1)
        await asyncio.sleep(e.seconds)
        captions = [text] + [""] * (len(files) - 1)
        entity_groups = [list(entities or [])] + [[] for _ in range(len(files) - 1)]
        await client.send_file(
            target,
            file=files,
            caption=captions,
            formatting_entities=entity_groups,
            link_preview=False,
            parse_mode=None,
        )


# ========= 相册处理 =========
@client.on(events.Album())
async def album_handler(event):
    try:
        if SOURCE_ID is not None and event.chat_id != SOURCE_ID:
            return

        msgs = event.messages
        first = msgs[0]
        btn_count = count_buttons(first)

        text, entities = pick_caption_from_album(event)

        log(f"收到相册 | chat_id:{event.chat_id} | 媒体数:{len(msgs)} | 文本长度:{len(text)} | 按钮:{btn_count}")

        if not text.strip():
            log("拦截: 相册无文字")
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

        await safe_send_album(
            target=TARGET_ID,
            files=[m.media for m in msgs],
            text=new_text,
            entities=new_entities
        )

        log(f"转发成功: 相册 | 实际媒体数:{len(msgs)}")

    except Exception as e:
        log(f"相册处理错误: {e}")


# ========= 单条处理 =========
@client.on(events.NewMessage(incoming=True))
async def handler(event):
    try:
        if event.grouped_id:
            return

        if SOURCE_ID is not None and event.chat_id != SOURCE_ID:
            return

        msg = event.message
        text, entities = pick_text_from_message(msg)
        btn_count = count_buttons(msg)

        log(f"收到消息 | chat_id:{event.chat_id} | 文本长度:{len(text)} | 按钮:{btn_count} | 有媒体:{bool(msg.media)}")

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

        await safe_send_single(
            target=TARGET_ID,
            text=new_text,
            media=msg.media,
            entities=new_entities
        )

        log("转发成功: 单条消息")

    except Exception as e:
        log(f"消息处理错误: {e}")


# ========= 20小时自动重启 =========
async def auto_restart():
    await asyncio.sleep(RESTART_TIME)
    log("20小时到，执行自动重启")
    await client.disconnect()
    raise SystemExit(0)


# ========= 启动前解析实体 =========
async def resolve_entities():
    global SOURCE_ID, TARGET_ID

    source_entity = await client.get_entity(SOURCE)
    target_entity = await client.get_entity(TARGET)

    SOURCE_ID = utils.get_peer_id(source_entity)
    TARGET_ID = utils.get_peer_id(target_entity)

    me = await client.get_me()

    log(f"启动成功 | 登录账号: {me.username or me.id}")
    log(f"监听频道: {getattr(source_entity, 'title', '')} | username: @{getattr(source_entity, 'username', None) or '无公开用户名'} | id: {SOURCE_ID}")
    log(f"目标频道: {getattr(target_entity, 'title', '')} | username: @{getattr(target_entity, 'username', None) or '无公开用户名'} | id: {TARGET_ID}")


# ========= 主程序 =========
async def main():
    await client.start()
    await resolve_entities()
    asyncio.create_task(auto_restart())
    await client.run_until_disconnected()


client.loop.run_until_complete(main())